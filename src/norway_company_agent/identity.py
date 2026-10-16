from __future__ import annotations

import re
import unicodedata
import urllib.parse
from typing import Any


LEGAL_AND_GENERIC = {
    "as", "asa", "ans", "da", "enk", "iks", "sa", "sam", "sti", "stiftelsen",
    "nuf", "ab", "b", "v", "limited", "ltd", "inc", "plc", "the", "og", "and",
}


def _tokens(value: Any) -> list[str]:
    text = str(value or "").translate(str.maketrans({"ø": "o", "Ø": "O", "å": "a", "Å": "A", "æ": "ae", "Æ": "AE"}))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_AND_GENERIC and len(token) > 1]


def _structured_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"name", "legalName", "alternateName"} and isinstance(child, str):
                names.append(child)
            else:
                names.extend(_structured_names(child))
    elif isinstance(value, list):
        for child in value:
            names.extend(_structured_names(child))
    return names


def assess_website_identity(profile: dict[str, Any]) -> dict[str, Any]:
    website = profile.get("evidence", {}).get("website", {})
    value = website.get("value") or {}
    core = _tokens(profile.get("name"))
    hostname = urllib.parse.urlparse(value.get("final_url") or website.get("source_url") or "").hostname or ""
    structured_names = _structured_names(value.get("structured_organisations") or [])
    rendered = value.get("js_fallback") or {}
    homepage_identity_parts = [
        value.get("title"), value.get("description"), value.get("identity_text_excerpt"), hostname, *structured_names,
        rendered.get("title"),
    ]
    candidate_parts = [
        *homepage_identity_parts, value.get("main_text_excerpt"),
        *[page.get("title") for page in value.get("pages", [])],
        *[page.get("main_text_excerpt") for page in value.get("pages", [])],
        *[page.get("identity_text_excerpt") for page in value.get("pages", [])],
    ]
    candidate_parts.append(rendered.get("main_text_excerpt"))
    candidate_text = " ".join(str(part or "") for part in candidate_parts)
    homepage_candidate_text = " ".join(str(part or "") for part in [*homepage_identity_parts, value.get("main_text_excerpt"), rendered.get("main_text_excerpt")])
    normalized_candidate_text = " ".join(_tokens(candidate_text))
    candidate_tokens = set(_tokens(candidate_text))
    org_digits = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    compact_candidate = re.sub(r"\D", "", candidate_text)
    compact_homepage_candidate = re.sub(r"\D", "", homepage_candidate_text)
    overlap = sorted(set(core) & candidate_tokens)
    ratio = len(overlap) / len(set(core)) if core else 0.0
    reasons = []
    parked_markers = (
        "domain is for sale", "domain for sale", "hugedomains", "parked at", "miss hosting",
        "her flytter snart en ny gjest", "has been informing visitors",
        "find the best information and most relevant links on all topics related to",
    )
    normalized_raw = unicodedata.normalize("NFKD", candidate_text).encode("ascii", "ignore").decode().casefold()
    homepage_token_sets = [set(_tokens(part)) for part in homepage_identity_parts if part]
    exact_homepage_name = bool(core and any(set(core).issubset(tokens) for tokens in homepage_token_sets))
    substantive_homepage = len(str(value.get("main_text_excerpt") or "").strip()) >= 100
    is_business_sports_club = bool(re.search(r"(?:^|\s)B\.?\s*I\.?\s*L\.?(?:\s|$)", str(profile.get("name") or ""), re.I))
    if any(marker in normalized_raw for marker in parked_markers):
        score = 0.1
        reasons.append("captured page is a parked, for-sale, or generic hosting placeholder")
    elif is_business_sports_club and "bedriftsidrett" not in normalized_candidate_text and "b i l" not in normalized_candidate_text:
        score = 0.3
        reasons.append("business sports-club entity points to the operating company's site without club evidence")
    elif org_digits and org_digits in compact_homepage_candidate:
        score = 1.0
        reasons.append("exact organisation number appears in homepage identity evidence")
    elif len(core) >= 2 and exact_homepage_name:
        score = 0.95
        reasons.append("all normalized legal-name tokens appear together in homepage identity evidence")
    elif len(core) == 1 and exact_homepage_name and substantive_homepage:
        score = 0.95
        reasons.append("single distinctive legal-name token appears in homepage identity evidence with substantive content")
    elif ratio >= 0.75 and len(overlap) >= 2:
        score = 0.85
        reasons.append("most legal-name tokens appear, but exact identity is incomplete")
    elif ratio >= 0.5 and len(overlap) >= 2:
        score = 0.65
        reasons.append("partial legal-name overlap only")
    else:
        score = 0.3
        reasons.append("registry-linked URL lacks strong exact-entity identity evidence")
    status = "exact" if score >= 0.9 else "review" if score >= 0.8 else "related_or_uncertain"
    return {
        "status": status,
        "score": score,
        "publishable": status == "exact",
        "legal_name_tokens": core,
        "matched_tokens": overlap,
        "reasons": reasons,
        "method": "deterministic_name_org_evidence_v2",
    }


def assess_social_identity(profile: dict[str, Any], link: dict[str, str]) -> dict[str, Any]:
    core = _tokens(profile.get("name"))
    parsed = urllib.parse.urlparse(link.get("url") or "")
    handle_text = urllib.parse.unquote(parsed.path)
    handle_compact = "".join(_tokens(handle_text))
    matched = [token for token in core if token in handle_compact]
    core_compact = "".join(core)
    ratio = len(set(matched)) / len(set(core)) if core else 0.0
    if core_compact and core_compact in handle_compact:
        score = 0.98
        reason = "normalized legal-name sequence appears in the social handle"
    elif len(core) == 1 and matched:
        score = 0.95
        reason = "single distinctive legal-name token appears in the social handle"
    elif ratio >= 0.75 and len(set(matched)) >= 2:
        score = 0.9
        reason = "most legal-name tokens appear in the social handle"
    else:
        score = 0.3
        reason = "social handle lacks strong exact-entity name evidence"
    return {
        **link,
        "identity_score": score,
        "publishable": score >= 0.9,
        "matched_tokens": matched,
        "reason": reason,
        "method": "deterministic_social_handle_identity_v1",
    }


def apply_website_identity_gate(profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    if website.get("status") != "available":
        return {"website": website, "assessment": None, "quarantined_social_links": 0}
    temporary_profile = {**profile, "evidence": {**profile.get("evidence", {}), "website": website}}
    value = website.get("value") or {}
    assessment = assess_website_identity(temporary_profile)
    value["identity_assessment"] = assessment
    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    value["discovered_social_links"] = original
    social_assessments = [assess_social_identity(profile, link) for link in original]
    value["social_link_assessments"] = social_assessments
    value["social_links"] = [
        {"platform": item["platform"], "url": item["url"]}
        for item in social_assessments
        if assessment["publishable"] and item["publishable"]
    ]
    website["value"] = value
    return {
        "website": website,
        "assessment": assessment,
        "quarantined_social_links": len(original) - len(value["social_links"]),
    }


GROUP_WORDS = ("group", "gruppen", "konsern", "holding", "asa")


def _compact(value: Any) -> str:
    return "".join(_tokens(value))


def assess_site_identity(profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    """Publication gate for a crawled company site, whichever way the site was found.

    An organisation number shown on a single-company page is exact proof. Otherwise the legal name must match
    the homepage identity (the reference rule), and a site found without an official link to the entity also
    needs a registry fact (phone, street address or e-mail) repeated on its pages.
    """
    value = website.get("value") or {}
    proof = value.get("identity_proof") or {}
    origin = value.get("discovery_origin") or "registry_website"
    others = int(proof.get("other_organisation_numbers_on_page") or 0) if proof else 0
    blanked = {**value, "identity_text_excerpt": "", "pages": [{**page, "identity_text_excerpt": ""} for page in value.get("pages", [])]}
    reference = assess_website_identity({**profile, "evidence": {**profile.get("evidence", {}), "website": {**website, "value": blanked}}})
    name_exact = reference["score"] >= 0.95
    parked_or_club = reference["score"] <= 0.3 and any("parked" in reason or "sports-club" in reason for reason in reference["reasons"])
    corroborating = list(value.get("registry_corroborations") or [])
    reasons: list[str] = []
    if parked_or_club:
        score, reasons = reference["score"], list(reference["reasons"])
    elif proof and others == 0:
        score = 1.0
        reasons.append(f"exact organisation number appears on company page {proof.get('page_url')}")
    elif proof and others <= 2 and name_exact:
        score = 0.97
        reasons.append("organisation number appears beside other entities' numbers, and the legal name matches the homepage")
    elif value.get("legal_name_span") and corroborating:
        score = 0.96
        reasons.append("the legal name and a registry fact appear on the site: " + corroborating[0])
    elif value.get("full_legal_name_span") and value.get("registry_place_matches") and (
            origin != "legal_name_domain_guess" or str(value.get("registered_domain") or "").endswith(".no")):
        score = 0.93
        reasons.append("the full registered name, legal form included, and the registered place ("
                       + ", ".join(value["registry_place_matches"][:2]) + ") appear on the site")
    elif origin == "registry_website" and name_exact:
        score = reference["score"]
        reasons.extend(reference["reasons"])
    elif origin == "registry_email_domain" and name_exact:
        score = 0.95
        reasons.append("site is the domain of the registry e-mail address and the legal name matches the homepage")
    elif origin == "legal_name_domain_guess" and name_exact and corroborating:
        score = 0.95
        reasons.append("legal name matches the homepage and a registry fact repeats on the site: " + corroborating[0])
    else:
        score = min(reference["score"], 0.85)
        reasons.extend(reference["reasons"])
        if proof:
            reasons.append(f"organisation number shares the page with {others} other organisation numbers")
        if origin != "registry_website":
            reasons.append("site was found by candidate generation and lacks an organisation number or registry corroboration")
    status = "exact" if score >= 0.9 else "review" if score >= 0.8 else "related_or_uncertain"
    return {
        "status": status,
        "score": score,
        "publishable": status == "exact",
        "legal_name_tokens": reference["legal_name_tokens"],
        "matched_tokens": reference["matched_tokens"],
        "reasons": reasons,
        "registry_corroborations": corroborating,
        "discovery_origin": origin,
        "method": "deterministic_org_number_name_registry_evidence_v3",
    }


def assess_owned_profile(profile: dict[str, Any], link: dict[str, str], website_value: dict[str, Any], site_exact: bool) -> dict[str, Any]:
    """A social profile linked from a verified company site. The handle must still name the company: its legal
    name (reference rule), the verified site's domain label, or the site's declared brand name."""
    reference = assess_social_identity(profile, link)
    parsed = urllib.parse.urlparse(link.get("url") or "")
    handle = _compact(urllib.parse.unquote(parsed.path))
    legal = _compact(profile.get("name"))
    group_word = next((word for word in GROUP_WORDS if word in handle and word not in legal and word not in _compact(website_value.get("registered_domain"))), None)
    if group_word:
        return {**reference, "identity_score": 0.3, "publishable": False,
                "reason": f"handle names a '{group_word}' entity that the legal name does not; likely a parent or group profile",
                "method": "deterministic_owned_profile_identity_v2"}
    if not site_exact or reference["publishable"]:
        return {**reference, "publishable": site_exact and reference["publishable"]}
    label = _compact(str(website_value.get("registered_domain") or "").rsplit(".", 1)[0])
    brands = [_compact(name) for name in website_value.get("brand_names") or []]
    if len(label) >= 4 and label in handle:
        return {**reference, "identity_score": 0.95, "publishable": True, "reason": "handle contains the verified company site's domain name", "method": "deterministic_owned_profile_identity_v2"}
    brand = next((name for name in brands if len(name) >= 4 and name in handle), None)
    if brand:
        return {**reference, "identity_score": 0.92, "publishable": True, "reason": "handle contains the brand name the verified site declares", "method": "deterministic_owned_profile_identity_v2"}
    return {**reference, "publishable": False}


def gate_company_site(profile: dict[str, Any], website: dict[str, Any]) -> dict[str, Any]:
    """Attach the site and profile assessments; only exact sites publish their profile links."""
    if website.get("status") != "available":
        return website
    value = website.get("value") or {}
    assessment = assess_site_identity(profile, website)
    value["identity_assessment"] = assessment
    original = list(value.get("discovered_social_links") or value.get("social_links") or [])
    value["discovered_social_links"] = original
    assessments = [assess_owned_profile(profile, link, value, assessment["publishable"]) for link in original]
    value["social_link_assessments"] = assessments
    value["social_links"] = [{"platform": item["platform"], "url": item["url"]} for item in assessments if item["publishable"]]
    website["value"] = value
    return website
