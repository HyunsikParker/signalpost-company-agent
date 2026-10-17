"""Claim-level output contract: claims, evidence, availability states and an evidence-bounded summary."""

from __future__ import annotations

import hashlib
from typing import Any

AVAILABILITY = {
    "available": "available",
    "not_found": "not_available",
    "blocked": "blocked",
    "not_applicable": "not_applicable",
    "source_error": "failed",
    "not_fetched": "failed",
}
ROLE_ORDER = {"DAGL": 0, "LEDE": 1, "NEST": 2, "MEDL": 3, "VARA": 4, "OBS": 5, "REVI": 6, "REGN": 7, "KONT": 8}


def _fmt_nok(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    amount = float(value)
    for limit, unit in ((1e9, "bn"), (1e6, "m"), (1e3, "k")):
        if abs(amount) >= limit:
            return f"NOK {amount / limit:.1f}{unit}"
    return f"NOK {amount:.0f}"


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


class ContractBuilder:
    def __init__(self, profile: dict[str, Any], change_note: str | None = None) -> None:
        self.profile = profile
        self.change_note = change_note
        self.org = profile["organisation_number"]
        self.records = profile.get("evidence", {})
        self.claims: list[dict[str, Any]] = []
        self.evidence: dict[str, dict[str, Any]] = {}

    def _evidence_id(self, record: dict[str, Any], span: str, url: str | None = None) -> str:
        source_url = url or record.get("source_url") or ""
        key = "|".join([self.org, source_url, str(record.get("content_sha256") or ""), span])
        evidence_id = "ev-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        if evidence_id not in self.evidence:
            item = {
                "id": evidence_id,
                "source_url": source_url,
                "source_class": record.get("source_class") or record.get("source_type"),
                "retrieved_at": record.get("retrieved_at"),
                "content_sha256": record.get("content_sha256"),
                "claim_span": span[:500],
            }
            if record.get("effective_at"):
                item["effective_at"] = record.get("effective_at")
            self.evidence[evidence_id] = item
        return evidence_id

    def claim(self, family: str, field: str, value: Any, record: dict[str, Any], span: str, *, confidence: float = 1.0,
              url: str | None = None, extra: dict[str, Any] | None = None) -> str:
        evidence_id = self._evidence_id(record, span, url)
        claim = {"family": family, "field": field, "value": value, "availability": "available", "confidence": confidence, "evidence_ids": [evidence_id]}
        if extra:
            claim.update(extra)
        self.claims.append(claim)
        return evidence_id

    def absent(self, family: str, field: str, record: dict[str, Any] | None, *, availability: str | None = None, note: str | None = None) -> None:
        state = availability or AVAILABILITY.get((record or {}).get("status") or "", "failed")
        claim = {"family": family, "field": field, "value": None, "availability": state, "confidence": None, "evidence_ids": []}
        reason = note or (record or {}).get("note")
        if reason:
            claim["note"] = reason
        if record and record.get("source_url"):
            claim["evidence_ids"] = [self._evidence_id(record, f"checked {record.get('source_url')}: {reason or state}")]
        self.claims.append(claim)

    # Official foundation -------------------------------------------------------------------------------
    def identity(self) -> list[str]:
        registry = self.records.get("registry") or {}
        row = registry.get("value") or {}
        ids = []
        fields = [
            ("legal_name", "navn"), ("legal_form", "organisasjonsform.beskrivelse"), ("industry", "naeringskode1.beskrivelse"),
            ("founded_date", "stiftelsesdato"), ("registered_employees", "antallAnsatte"), ("business_purpose", "vedtektsfestetFormaal"),
        ]
        for field, column in fields:
            value = row.get(column)
            if value in (None, ""):
                if field == "registered_employees":
                    self.absent("legal_identity", field, registry, availability="not_available", note="The registry snapshot reports no employee count; it is not treated as zero")
                continue
            if field == "registered_employees":
                value = int(value) if str(value).isdigit() else value
            if field == "industry" and row.get("naeringskode1.kode"):
                value = f"{row['naeringskode1.kode']} {value}"
            ids.append(self.claim("legal_identity", field, value, registry, f"{column}: {value}"))
        address = ", ".join(part for part in [row.get("forretningsadresse.adresse"), " ".join(filter(None, [row.get("forretningsadresse.postnummer"), row.get("forretningsadresse.poststed")]))] if part)
        if address:
            ids.append(self.claim("legal_identity", "business_address", address, registry, f"forretningsadresse: {address}"))
        status = "bankrupt" if row.get("konkurs") == "true" else "being wound up" if row.get("underAvvikling") == "true" else "active"
        ids.append(self.claim("legal_identity", "registry_status", status, registry, f"konkurs: {row.get('konkurs')}; underAvvikling: {row.get('underAvvikling')}"))
        return ids

    def accounts(self) -> list[dict[str, Any]]:
        record = self.records.get("financials") or {}
        rows = ((record.get("value") or {}).get("records") or []) if record.get("status") == "available" else []
        if not rows:
            self.absent("annual_accounts", "latest_annual_accounts", record or None,
                        availability="not_available" if record.get("status") == "available" else None if record else "failed")
            return []
        ordered = sorted(rows, key=lambda item: str((item.get("period") or {}).get("tilDato") or ""), reverse=True)
        published = []
        for index, item in enumerate(ordered):
            period = item.get("period") or {}
            figures = {key: item.get(key) for key in ("revenue", "operating_result", "profit_before_tax", "annual_result", "assets", "equity", "debt") if item.get(key) is not None}
            value = {"year": str(period.get("tilDato") or "")[:4] or None, "period_start": period.get("fraDato"), "period_end": period.get("tilDato"),
                     "currency": item.get("currency"), "account_type": item.get("account_type"), **figures}
            span = f"regnskapsperiode {period.get('fraDato')}..{period.get('tilDato')}: " + ", ".join(f"{key} {val}" for key, val in figures.items())
            self.claim("annual_accounts", "latest_annual_accounts" if index == 0 else "earlier_annual_accounts", value, record, span,
                       extra={"reporting_period": {"start": period.get("fraDato"), "end": period.get("tilDato")}})
            published.append(value)
        return published

    def leadership(self) -> list[dict[str, Any]]:
        record = self.records.get("roles") or {}
        roles = []
        for item in (record.get("value") or {}).get("roles") or []:
            name = item.get("name")
            name = " ".join(str(part) for part in name) if isinstance(name, list) else str(name or "").strip()
            if name and not item.get("inactive"):
                roles.append({**item, "name": name})
        if record.get("status") != "available" or not roles:
            self.absent("leadership", "role", record or None, availability=None if record and record.get("status") != "available" else "not_available")
            return []
        roles.sort(key=lambda item: (ROLE_ORDER.get(item.get("role_code") or "", 9), item.get("name") or ""))
        for item in roles[:15]:
            value = {"role": item.get("role"), "role_code": item.get("role_code"), "name": item.get("name")}
            if item.get("organisation_number"):
                value["organisation_number"] = item["organisation_number"]
            self.claim("leadership", "role", value, record, f"{item.get('role')}: {item.get('name')} (sist endret {item.get('last_changed')})",
                       extra={"effective_at": item.get("last_changed")})
        return roles

    def workplaces(self) -> list[dict[str, Any]]:
        record = self.records.get("locations") or {}
        rows = (record.get("value") or {}).get("locations") or []
        if record.get("status") != "available" or not rows:
            self.absent("workplaces", "registered_workplace", record or None, availability=None if record and record.get("status") != "available" else "not_available",
                        note=None if record.get("status") != "available" else "No registered sub-units")
            return []
        for item in rows[:25]:
            address = item.get("address") or {}
            line = ", ".join(filter(None, [" ".join(address.get("adresse") or []), " ".join(filter(None, [address.get("postnummer"), address.get("poststed")]))]))
            value = {"name": item.get("name"), "organisation_number": item.get("organisation_number"), "address": line or None,
                     "municipality": address.get("kommune"), "employees": item.get("employees")}
            self.claim("workplaces", "registered_workplace", value, record, f"underenhet {item.get('organisation_number')}: {item.get('name')}, {line}")
        return rows

    def group(self) -> dict[str, Any] | None:
        record = self.records.get("group") or {}
        body = record.get("value") if record.get("status") == "available" else None
        if not isinstance(body, dict):
            return None

        def find(node: dict[str, Any]) -> dict[str, Any] | None:
            if node.get("organisasjonsnummer") == self.org:
                return node
            for child in node.get("children") or []:
                hit = find(child)
                if hit:
                    return hit
            return None

        node = find(body)
        if not node:
            return None
        relation = {"group_top": body.get("navn"), "group_top_organisation_number": body.get("organisasjonsnummer"),
                    "parent": node.get("parentNavn"), "parent_organisation_number": node.get("parentOrganisasjonsnummer"),
                    "direct_subsidiaries": len(node.get("children") or [])}
        span = f"konsernstruktur: {body.get('navn')} ({body.get('organisasjonsnummer')}); parent {node.get('parentNavn')}; children {relation['direct_subsidiaries']}"
        self.claim("group", "group_relation", relation, record, span)
        return relation

    # External, company-owned --------------------------------------------------------------------------
    def website(self) -> dict[str, Any] | None:
        record = self.records.get("website") or {}
        value = record.get("value") or {}
        assessment = value.get("identity_assessment") or {}
        if record.get("status") != "available":
            self.absent("official_website", "official_website", record or None)
            return None
        if not assessment.get("publishable"):
            self.absent("official_website", "official_website", record, availability="ambiguous",
                        note="A candidate site was reached but did not prove the exact legal entity: " + "; ".join(assessment.get("reasons") or [])[:300])
            return None
        proof = value.get("identity_proof") or {}
        span = proof.get("claim_span") or value.get("legal_name_span") or value.get("title") or value.get("final_url")
        url = proof.get("page_url") if proof else record.get("source_url")
        proof_record = {**record, "content_sha256": proof.get("content_sha256") or record.get("content_sha256")}
        self.claim("official_website", "official_website", value.get("final_url") or record.get("source_url"), proof_record, span,
                   confidence=float(assessment.get("score") or 0.9), url=url,
                   extra={"identity_method": assessment.get("method"), "identity_reasons": assessment.get("reasons"), "discovery_origin": value.get("discovery_origin")})
        for brand in value.get("brand_names") or []:
            self.claim("public_brand", "public_brand", brand, record, f"site declares the name '{brand}'")
        if value.get("description"):
            self.claim("company_description", "company_description", value["description"][:600], record, value["description"][:300])
        return value

    def owned(self, site: dict[str, Any] | None) -> None:
        profiles = self.records.get("company_profiles") or {}
        items = (profiles.get("value") or {}).get("profiles") or []
        if items:
            for item in items:
                self.claim("company_profiles", "social_profile", {"platform": item["platform"], "url": item["url"]}, profiles,
                           f"verified company site links to {item['url']}", url=(site or {}).get("final_url"))
        else:
            self.absent("company_profiles", "social_profile", profiles or None)
        contacts = self.records.get("site_contacts") or {}
        value = contacts.get("value") or {}
        for address in value.get("emails") or []:
            self.claim("contact", "site_email", address, contacts, f"mailto:{address}")
        for number in value.get("phones") or []:
            self.claim("contact", "site_phone", number, contacts, f"tel:{number}")
        jobs = self.records.get("jobs") or {}
        jobs_value = jobs.get("value") or {}
        if jobs_value.get("careers_page"):
            self.claim("hiring", "careers_page", jobs_value["careers_page"], jobs, f"careers link {jobs_value['careers_page']}", url=jobs_value["careers_page"])
        postings = jobs_value.get("postings") or []
        for job in postings:
            nav = job.get("method") == "nav_arbeidsplassen_public_search"
            job_record = {**jobs, "source_class": "public_job_database" if nav else "company_owned_careers_pages",
                          "retrieved_at": job.get("retrieved_at") or jobs.get("retrieved_at"),
                          "content_sha256": job.get("content_sha256") or jobs.get("content_sha256")}
            value = {key: job.get(key) for key in ("title", "url", "date_posted", "valid_through", "location", "hiring_organisation", "employer_organisation_number") if job.get(key) is not None}
            span = f"{job['title']} — {job.get('claim_span') or job['url']}"
            self.claim("hiring", "job_posting", value, job_record, span, url=job.get("source_url"),
                       extra={"effective_at": job.get("date_posted"), "extraction_method": job.get("method")})
        if not postings:
            self.absent("hiring", "job_posting", jobs or None)
        activity = self.records.get("activity") or {}
        items = (activity.get("value") or {}).get("items") or []
        for item in items:
            self.claim("public_activity", "dated_activity", {key: item.get(key) for key in ("title", "url", "date", "date_type", "kind")}, activity,
                       f"{item['date']} {item['title']}", url=item.get("source_url"),
                       extra={"effective_at": item.get("date"), "extraction_method": item.get("method")})
        if not items:
            self.absent("public_activity", "dated_activity", activity or None)

    def summary(self, accounts: list[dict[str, Any]], roles: list[dict[str, Any]], site: dict[str, Any] | None, group: dict[str, Any] | None) -> dict[str, Any]:
        row = (self.records.get("registry") or {}).get("value") or {}
        name = row.get("navn") or self.profile.get("name")
        sentences = []
        kind = row.get("organisasjonsform.beskrivelse") or row.get("organisasjonsform.kode") or "entity"
        place = row.get("forretningsadresse.poststed") or row.get("forretningsadresse.kommune")
        industry = row.get("naeringskode1.beskrivelse")
        founded = str(row.get("stiftelsesdato") or "")[:4]
        intro = (f"{name} is a Norwegian {kind.lower()}" + (f" in {place.title()}" if place else "")
                 + (f", registered for {industry.lower()}" if industry else "") + (f", founded in {founded}" if founded.isdigit() else "") + ".")
        sentences.append(intro)
        if row.get("konkurs") == "true":
            sentences.append("The registry records it as bankrupt.")
        elif row.get("underAvvikling") == "true":
            sentences.append("The registry records it as being wound up.")
        elif row.get("underTvangsavviklingEllerTvangsopplosning") == "true":
            sentences.append("The registry records forced winding-up or dissolution proceedings.")
        elif row.get("konkurs") == "false" and row.get("underAvvikling") == "false":
            sentences.append("The registry records no bankruptcy or winding-up.")
        employees = row.get("antallAnsatte")
        if employees not in (None, ""):
            sentences.append(f"The registry lists {employees} employee{'' if str(employees) == '1' else 's'}.")
        if site and site.get("description"):
            sentences.append(f"Its website describes the business as: “{site['description'][:220].strip()}”")
        elif row.get("vedtektsfestetFormaal"):
            sentences.append(f"Its registered purpose is “{_clip(row['vedtektsfestetFormaal'], 200).rstrip('.')}”.")
        if accounts:
            latest = accounts[0]
            parts = [f"revenue {_fmt_nok(latest['revenue'])}" if _fmt_nok(latest.get("revenue")) else None,
                     f"annual result {_fmt_nok(latest['annual_result'])}" if _fmt_nok(latest.get("annual_result")) else None,
                     f"equity {_fmt_nok(latest['equity'])}" if _fmt_nok(latest.get("equity")) else None]
            parts = [part for part in parts if part]
            if parts:
                sentences.append(f"The latest filed accounts ({latest.get('year')}) show " + ", ".join(parts) + ".")
            if len(accounts) > 1 and isinstance(latest.get("revenue"), (int, float)) and isinstance(accounts[1].get("revenue"), (int, float)) and accounts[1]["revenue"]:
                change = (latest["revenue"] - accounts[1]["revenue"]) / abs(accounts[1]["revenue"]) * 100
                sentences.append(f"Revenue changed {change:+.0f}% from {accounts[1].get('year')}.")
        leaders = [f"{item.get('role')} {item.get('name')}" for item in roles if item.get("role_code") in {"DAGL", "LEDE"}]
        if leaders:
            sentences.append("Leadership: " + "; ".join(leaders[:3]) + ".")
        if group and group.get("parent"):
            sentences.append(f"It belongs to the {group.get('group_top')} group; its parent is {group.get('parent')}.")
        elif group and group.get("direct_subsidiaries"):
            count = group["direct_subsidiaries"]
            sentences.append(f"It is the parent of {count} registered subsidiar{'y' if str(count) == '1' else 'ies'}.")
        profiles = ((self.records.get("company_profiles") or {}).get("value") or {}).get("profiles") or []
        if site:
            sentences.append(f"Verified website: {site.get('final_url')}" + (f", linking to {', '.join(sorted({item['platform'] for item in profiles}))} profiles." if profiles else "."))
        jobs = ((self.records.get("jobs") or {}).get("value") or {}).get("postings") or []
        if jobs:
            from_nav = sum(1 for job in jobs if job.get("method") == "nav_public_job_feed")
            where = "in Nav's public job database" if from_nav == len(jobs) else "on its own careers pages" if not from_nav else "in Nav's job database and on its own careers pages"
            sentences.append(f"Hiring: {len(jobs)} current job ad{'s' if len(jobs) != 1 else ''} {where}, including “{jobs[0]['title']}”.")
        activity = ((self.records.get("activity") or {}).get("value") or {}).get("items") or []
        if activity:
            sentences.append(f"Most recent dated activity on its site: “{activity[0]['title']}” ({activity[0]['date']}).")
        if self.change_note:
            sentences.append(self.change_note)
        jobs_value = (self.records.get("jobs") or {}).get("value") or {}
        nav_status = (jobs_value.get("nav_feed") or {}).get("status")
        unknowns = []
        if not site:
            unknowns.append("no website could be verified as this exact company, so no site-based profiles, contacts or posts were collected")
        if not accounts:
            unknowns.append("no filed accounts were available from Regnskapsregisteret")
        if not roles:
            unknowns.append("no registered roles were returned by the registry")
        if not jobs:
            if nav_status in {"ready", "partial"}:
                where = "Nav's public job feed" + (" (read in part this run)" if nav_status == "partial" else "")
                where += " or on its own site" if jobs_value.get("site_checked") else ""
                unknowns.append(f"no current job ads were found in {where}")
            elif nav_status:
                unknowns.append("Nav's public job feed could not be read this run, so current hiring is unknown")
            elif site:
                unknowns.append("no current job postings were found on its own site")
        if site and not activity:
            unknowns.append("no dated news or posts were found on its own site")
        if unknowns:
            sentences.append("Unknown: " + "; ".join(unknowns) + ".")
        cited = sorted({evidence_id for claim in self.claims if claim["availability"] == "available" for evidence_id in claim["evidence_ids"]})
        return {"text": " ".join(sentences), "evidence_ids": cited, "unknowns": unknowns, "method": "deterministic_template_over_published_claims_v2"}

    def build(self) -> dict[str, Any]:
        self.identity()
        accounts = self.accounts()
        roles = self.leadership()
        self.workplaces()
        group = self.group()
        site = self.website()
        self.owned(site)
        summary = self.summary(accounts, roles, site, group)
        return {"claims": self.claims, "evidence": list(self.evidence.values()), "summary": summary}


def build_contract(profile: dict[str, Any], change_note: str | None = None) -> dict[str, Any]:
    """`change_note` is the refresh sentence for the summary; the batch runner builds it from `--previous`."""
    return ContractBuilder(profile, change_note).build()
