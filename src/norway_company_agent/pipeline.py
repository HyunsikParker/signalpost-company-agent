"""Per-company research after the official modules: verified site, owned profiles, jobs, dated activity."""

from __future__ import annotations

from typing import Any

from .evidence import evidence
from .identity import gate_company_site
from .site_crawl import Deadline, collect_signals, crawl_site
from .site_research import RequestLog, candidate_sites, resolving_host

EXTERNAL_MODULES = ("website_discovery", "company_profiles", "jobs", "activity", "site_contacts")
REGISTRY_SOURCE = "https://data.brreg.no/enhetsregisteret/api/enheter"


def registry_row(profile: dict[str, Any]) -> dict[str, Any]:
    """The bulk registry row, with e-mail and phone from the live registry when the snapshot lacks them."""
    row = dict((profile.get("evidence", {}).get("registry") or {}).get("value") or {})
    live = (profile.get("evidence", {}).get("registry_live") or {}).get("value") or {}
    for key in ("epostadresse", "telefon", "mobil"):
        if not row.get(key) and live.get(key):
            row[key] = live[key]
    if not row.get("hjemmeside") and live.get("website"):
        row["hjemmeside"] = live["website"]
    return row


def _published(record: dict[str, Any] | None) -> bool:
    return bool(record and record.get("status") == "available" and ((record.get("value") or {}).get("identity_assessment") or {}).get("publishable"))


def _attempt(record: dict[str, Any], domain: str, origin: str) -> dict[str, Any]:
    value = record.get("value") or {}
    assessment = value.get("identity_assessment") or {}
    return {
        "domain": domain,
        "origin": origin,
        "fetch_status": record.get("status"),
        "final_url": value.get("final_url") or record.get("source_url"),
        "identity_status": assessment.get("status"),
        "identity_score": assessment.get("score"),
        "reasons": assessment.get("reasons") or ([record.get("note")] if record.get("note") else []),
        "content_sha256": record.get("content_sha256"),
        "retrieved_at": record.get("retrieved_at"),
    }


def research_company(
    profile: dict[str, Any],
    *,
    budget_seconds: float = 45.0,
    discovery: bool = True,
    signals: bool = True,
    max_candidates: int = 5,
) -> dict[str, Any]:
    """Fill profile['evidence'] with the website and external modules. Returns request metrics."""
    log = RequestLog()
    deadline = Deadline(budget_seconds)
    org = profile["organisation_number"]
    row = registry_row(profile)
    records = profile.setdefault("evidence", {})
    attempts: list[dict[str, Any]] = []
    published: dict[str, Any] | None = None
    published_pages: list[dict[str, Any]] = []
    registry_record: dict[str, Any] | None = None

    registry_website = profile.get("website") or row.get("hjemmeside")
    if registry_website:
        registry_record, pages = crawl_site(
            registry_website, org=org, registry_row=row, legal_name=str(profile.get("name") or ""), source_class="registry_linked_company_website",
            origin="registry_website", deadline=deadline, log=log,
        )
        registry_record = gate_company_site(profile, registry_record)
        attempts.append(_attempt(registry_record, str(registry_website), "registry_website"))
        if _published(registry_record):
            published, published_pages = registry_record, pages

    tried = 0
    if published is None and discovery:
        for candidate in candidate_sites(profile, row):
            if tried >= max_candidates or deadline.left() < 6:
                break
            host = resolving_host(candidate["domain"])
            if not host:
                attempts.append({"domain": candidate["domain"], "origin": candidate["origin"], "fetch_status": "not_found", "reasons": ["domain does not resolve"]})
                continue
            tried += 1
            record, pages = crawl_site(
                f"https://{host}/", org=org, registry_row=row, legal_name=str(profile.get("name") or ""), source_class="discovered_company_website",
                origin=candidate["origin"], deadline=deadline, log=log, secondary_limit=2, identity_limit=2,
            )
            record = gate_company_site(profile, record)
            attempts.append(_attempt(record, candidate["domain"], candidate["origin"]))
            if _published(record):
                published, published_pages = record, pages
                break

    if published is not None:
        records["website"] = published
    elif registry_record is not None:
        records["website"] = registry_record
    else:
        records["website"] = evidence("website", "not_found", "registry_linked_company_website", REGISTRY_SOURCE, note="No registry website, and no candidate site passed the identity gate")

    site_url = published.get("source_url") if published else REGISTRY_SOURCE
    records["website_discovery"] = evidence(
        "website_discovery",
        "available" if published is not None else "not_found",
        "candidate_generation_with_identity_gate",
        site_url,
        value={"attempts": attempts, "published_origin": (published.get("value") or {}).get("discovery_origin") if published else None},
        note=None if published is not None else "Candidates tried: registry website, registry e-mail domain and legal-name domains; none proved the exact entity",
    )

    found: dict[str, Any] = {}
    if published is not None and signals and deadline.left() > 3:
        found = collect_signals(published, published_pages, deadline=deadline, log=log)
    elif published is not None:
        found = {"activity": [], "jobs": [], "careers_page": None, "contacts": {"emails": [], "phones": []}, "errors": [{"url": site_url, "error": "company time budget exhausted before signal collection"}]}

    value = (published or {}).get("value") or {}
    site_note = "No verified company website, so no company-owned source was available to check"
    social = value.get("social_links") or []
    records["company_profiles"] = evidence(
        "company_profiles",
        "available" if social else "not_found",
        "company_owned_outbound_links",
        site_url,
        value={"profiles": social, "quarantined": [item for item in value.get("social_link_assessments", []) if not item.get("publishable")]},
        note=None if social else ("Verified company site links no profile that names the company" if published else site_note),
        content_sha256=(published or {}).get("content_sha256"),
    )
    jobs = list(found.get("jobs") or [])
    records["jobs"] = evidence(
        "jobs",
        "available" if jobs else "not_found",
        "public_job_database_and_company_careers_pages",
        found.get("careers_page") or site_url,
        value={"postings": jobs, "careers_page": found.get("careers_page"), "pages_checked": found.get("careers_checked", []),
               "site_checked": published is not None},
        note=None if jobs else ("No current job posting on the verified site or the careers page it links" if published else site_note),
    )
    activity = found.get("activity") or []
    records["activity"] = evidence(
        "activity",
        "available" if activity else "not_found",
        "company_owned_news_and_feeds",
        site_url,
        value={"items": activity, "feeds_checked": found.get("feeds_checked", []), "sitemaps_checked": found.get("sitemaps_checked", [])},
        note=None if activity else ("No dated item found in the verified site's pages, feeds or sitemap" if published else site_note),
    )
    site_contacts = found.get("contacts") or {"emails": [], "phones": []}
    has_contacts = bool(site_contacts.get("emails") or site_contacts.get("phones"))
    records["site_contacts"] = evidence(
        "site_contacts",
        "available" if has_contacts else "not_found",
        "company_owned_contact_links",
        site_url,
        value=site_contacts,
        note=None if has_contacts else ("No e-mail or phone link on the company's own domain" if published else site_note),
    )
    if found.get("errors"):
        records["activity"]["value"]["errors"] = found["errors"][:10]
    return {"requests": log.requests, "bytes": log.bytes, "latencies_ms": log.latencies_ms}
