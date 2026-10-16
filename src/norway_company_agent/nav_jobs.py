"""Current job ads from Nav's public job feed (pam-stilling-feed.nav.no), tied to the company by organisation number.

Once per run the feed is paged from a look-back date to its end, keeping the latest state of every ad. An ad
becomes a candidate when its business name is the company's legal name or one of its registered sub-units
(legal-form suffixes ignored), and it is published only when the ad's own record names the company's
organisation number or one of those sub-units. Contact persons in the ads are never read or stored.

Nav's terms let anyone use the feed free of charge and republish the ads. The run uses the public token Nav
publishes for this API; it is fetched at run time and never stored.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

FEED_BASE = "https://pam-stilling-feed.nav.no"
TOKEN_URL = FEED_BASE + "/api/publicToken"
AD_PAGE = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
LEGAL_FORMS = {"as", "asa", "ans", "da", "sa", "nuf", "enk", "ba", "sti", "iks", "ks", "bbl", "brl", "fli", "esek", "sam", "hf", "kf"}
MAX_CANDIDATES = 8


def name_key(name: Any) -> str:
    """Lower-case words without legal-form tokens, so "X AS AVD OSLO" and "X avd Oslo" compare equal."""
    return " ".join(word for word in re.findall(r"[^\W_]+", str(name or "").casefold()) if word not in LEGAL_FORMS)


def _utc(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")


def _day(value: Any) -> str | None:
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(value or ""))
    return match.group(1) if match else None


class NavJobIndex:
    """Latest state of every ad changed since the look-back date, indexed by business name."""

    def __init__(self, days: int = 60, page_timeout: float = 60.0) -> None:
        self.days = days
        self.page_timeout = page_timeout
        self.status = "not_started"
        self.token: str | None = None
        self.pages = 0
        self.items = 0
        self.requests = 0
        self.errors: list[str] = []
        self.window_start: str | None = None
        self.finished_at: str | None = None
        self.by_name: dict[str, list[dict[str, Any]]] = {}
        self.ready = threading.Event()
        self._lock = threading.Lock()

    def _get(self, path: str, headers: dict[str, str] | None = None, timeout: float | None = None) -> bytes:
        request = urllib.request.Request(FEED_BASE + path if path.startswith("/") else path, headers={"User-Agent": USER_AGENT, **(headers or {})})
        with self._lock:
            self.requests += 1
        with urllib.request.urlopen(request, timeout=timeout or self.page_timeout) as response:
            return response.read(20_000_000)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}

    def build(self, deadline_seconds: float) -> None:
        """Page the feed; stop at its end or the deadline. Never raises."""
        started = time.monotonic()
        latest: dict[str, dict[str, Any]] = {}
        try:
            self.status = "building"
            body = self._get(TOKEN_URL, timeout=20).decode("utf-8", errors="replace")
            match = re.search(r"eyJ[A-Za-z0-9_.-]+", body)
            if not match:
                raise ValueError("public token not found")
            self.token = match.group(0)
            since = datetime.now(timezone.utc) - timedelta(days=self.days)
            self.window_start = _utc(since)
            path: str | None = "/api/v1/feed"
            first = True
            while path:
                if time.monotonic() - started > deadline_seconds:
                    self.errors.append("feed paging stopped at the time limit; the newest ads may be missing")
                    break
                headers = self._auth()
                if first:
                    headers["If-Modified-Since"] = format_datetime(since, usegmt=True)
                page = json.loads(self._get(path, headers))
                first = False
                self.pages += 1
                for item in page.get("items") or []:
                    entry = item.get("_feed_entry") or {}
                    uuid = entry.get("uuid")
                    if uuid:
                        latest[uuid] = {"uuid": uuid, "status": entry.get("status"), "business_name": entry.get("businessName"),
                                        "title": entry.get("title"), "municipal": entry.get("municipal"), "modified": item.get("date_modified")}
                        self.items += 1
                path = page.get("next_url")
            for ad in latest.values():
                if ad["status"] == "ACTIVE":
                    self.by_name.setdefault(name_key(ad["business_name"]), []).append(ad)
            self.status = "ready" if not self.errors else "partial"
        except Exception as exc:  # the jobs module reports the failure; the batch carries on
            self.errors.append(f"{type(exc).__name__}: {str(exc)[:160]}")
            self.status = "partial" if latest else "failed"
            for ad in latest.values():
                if ad["status"] == "ACTIVE":
                    self.by_name.setdefault(name_key(ad["business_name"]), []).append(ad)
        finally:
            self.finished_at = _utc()
            self.ready.set()

    def candidates(self, profile: dict[str, Any]) -> list[dict[str, Any]]:
        legal = name_key(profile.get("name"))
        if not legal:
            return []
        locations = ((profile.get("evidence", {}).get("locations") or {}).get("value") or {}).get("locations") or []
        names = {legal} | {name_key(item.get("name")) for item in locations if item.get("name")}
        found: dict[str, dict[str, Any]] = {}
        for name in names:
            for ad in self.by_name.get(name, []):
                found[ad["uuid"]] = ad
        prefix = legal + " avd"
        for name, ads in self.by_name.items():
            if name.startswith(prefix):
                for ad in ads:
                    found[ad["uuid"]] = ad
        return sorted(found.values(), key=lambda ad: str(ad.get("modified") or ""), reverse=True)[:MAX_CANDIDATES]

    def verify(self, ad: dict[str, Any], orgs: set[str]) -> dict[str, Any] | None:
        """Fetch the ad's own record; keep it only if its employer number is the company or a sub-unit."""
        path = f"/api/v1/feedentry/{ad['uuid']}"
        raw = self._get(path, self._auth(), timeout=30)
        body = json.loads(raw)
        content = body.get("json") if isinstance(body.get("json"), dict) else body.get("ad_content") or {}
        employer = content.get("employer") or {}
        orgnr = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
        status = body.get("status") or ad.get("status")
        if orgnr not in orgs or status != "ACTIVE":
            return None
        expires = _day(content.get("expires"))
        if expires and expires < datetime.now(timezone.utc).date().isoformat():
            return None
        location = (content.get("workLocations") or [{}])[0] or {}
        title = re.sub(r"\s+", " ", str(content.get("title") or ad.get("title") or "")).strip()[:300]
        return {
            "title": title,
            "url": AD_PAGE.format(uuid=ad["uuid"]),
            "date_posted": _day(content.get("published")),
            "valid_through": expires,
            "location": location.get("city") or location.get("municipal") or None,
            "hiring_organisation": employer.get("name"),
            "employer_organisation_number": orgnr,
            "method": "nav_public_job_feed",
            "source_url": AD_PAGE.format(uuid=ad["uuid"]),
            "api_url": FEED_BASE + path,
            "content_sha256": hashlib.sha256(raw).hexdigest(),
            "claim_span": f"employer {employer.get('name')} (orgnr {orgnr}); status {status}; title {title}",
            "retrieved_at": _utc(),
        }


def attach_jobs(profile: dict[str, Any], index: NavJobIndex) -> int:
    """Merge verified Nav ads into the profile's jobs module. Returns the number of requests made."""
    record = profile.get("evidence", {}).get("jobs")
    if not record:
        return 0
    value = record.setdefault("value", {}) or {}
    org = profile["organisation_number"]
    locations = ((profile.get("evidence", {}).get("locations") or {}).get("value") or {}).get("locations") or []
    orgs = {org} | {item.get("organisation_number") for item in locations if item.get("organisation_number")}
    summary: dict[str, Any] = {"status": index.status, "window_start": index.window_start, "index_finished_at": index.finished_at,
                               "ads_indexed": index.items, "candidates": 0, "verified": 0, "rejected": 0, "errors": []}
    requests = 0
    postings = list(value.get("postings") or [])
    if index.status in {"ready", "partial"}:
        seen = {item.get("url") for item in postings}
        for ad in index.candidates(profile):
            summary["candidates"] += 1
            try:
                requests += 1
                posting = index.verify(ad, orgs)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                summary["errors"].append(f"{ad['uuid']}: {type(exc).__name__}")
                continue
            if posting and posting["url"] not in seen:
                postings.append(posting)
                seen.add(posting["url"])
                summary["verified"] += 1
            elif not posting:
                summary["rejected"] += 1
    value["postings"] = postings
    value["nav_feed"] = summary
    if postings:
        record["status"], record["note"] = "available", None
    elif index.status in {"ready", "partial"}:
        checked = "Nav's public job feed" + (" and the verified company site" if value.get("site_checked") else "")
        record["status"], record["note"] = "not_found", f"No active job ad tied to this organisation number or its sub-units in {checked}"
    elif not value.get("site_checked"):
        record["status"], record["note"] = "source_error", "Nav's job feed could not be read and no verified company site was available"
    record["retrieved_at"] = _utc()
    return requests
