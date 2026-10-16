"""Bounded crawl of one company site: identity pages first, then company-owned signals for a verified site."""

from __future__ import annotations

import time
import urllib.parse

from typing import Any

import extruct
import trafilatura
from bs4 import BeautifulSoup

from .evidence import evidence
from .site_research import (
    IDENTITY_TERMS,
    MAX_FEED_ITEMS,
    MAX_JOBS,
    NEWS_PATH,
    RequestLog,
    ats_jobs,
    brand_names,
    career_links,
    content_hash,
    contacts,
    corroborations,
    fetch_text,
    jsonld_blocks,
    article_entry,
    full_legal_name_pattern,
    legal_name_pattern,
    listing_items,
    listing_links,
    name_span,
    org_number_span,
    organisation_numbers_in,
    parse_feed,
    place_corroborations,
    parse_sitemap,
    same_site,
    slug_title,
    structured_activity,
    structured_jobs,
    utc_now,
    visible_text,
    ROBOTS,
    _iso,
    _plausible_date,
)
from .website import (
    SAFE_OPENER,
    _extraction_state,
    _jsonld_organisations,
    _priority_links,
    _registered_domain,
    _social_links,
    assert_public_url,
    normalize_homepage,
    structured_social_links,
)

PAGE_LIMIT = 1_000_000
HOME_LIMIT = 2_000_000


class Deadline:
    def __init__(self, seconds: float) -> None:
        self.at = time.monotonic() + seconds

    def left(self) -> float:
        return self.at - time.monotonic()

    def expired(self) -> bool:
        return self.left() <= 0


def _page(url: str, raw: bytes, *, org: str, name_pattern: Any = None) -> dict[str, Any]:
    html = raw.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "lxml")
    text = visible_text(soup)
    numbers = organisation_numbers_in(text)
    return {
        "url": url,
        "html": html,
        "soup": soup,
        "text": text,
        "title": soup.title.get_text(" ", strip=True)[:500] if soup.title else "",
        "content_sha256": content_hash(raw),
        "org_numbers": numbers,
        "org_span": org_number_span(text, org) if org in numbers else None,
        "name_span": name_span(text, name_pattern),
        "jsonld": jsonld_blocks(soup),
    }


def _identity_links(base_url: str, soup: BeautifulSoup, limit: int) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    found: list[str] = []
    for anchor in soup.select("a[href]"):
        url = urllib.parse.urljoin(base_url, str(anchor.get("href") or "").strip())
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.casefold() != base.netloc.casefold():
            continue
        haystack = (urllib.parse.unquote(parsed.path) + " " + anchor.get_text(" ", strip=True)).casefold()
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        if any(term in haystack for term in IDENTITY_TERMS) and clean.rstrip("/") != base_url.rstrip("/") and clean not in found:
            found.append(clean)
    return found[:limit]


def crawl_site(
    url: str | None,
    *,
    org: str,
    registry_row: dict[str, Any],
    legal_name: str = "",
    source_class: str = "registry_linked_company_website",
    origin: str,
    deadline: Deadline,
    log: RequestLog,
    timeout: float = 12.0,
    secondary_limit: int = 4,
    identity_limit: int = 2,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch the homepage and a few same-site pages. Returns the evidence record and the parsed pages
    (kept in memory for signal extraction, never serialised)."""
    supplied = str(url or "").strip()
    normalized = normalize_homepage(supplied)
    if not normalized:
        return evidence("website", "not_found", source_class, "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid website URL"), []
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        status = "not_found" if "did not resolve" in str(exc) else "blocked"
        return evidence("website", status, source_class, normalized, note=str(exc)), []
    raw, final_url, content_type, error = fetch_text(normalized, SAFE_OPENER, timeout=min(timeout, max(2.0, deadline.left())), max_bytes=HOME_LIMIT, log=log)
    if raw is None and not error.startswith(("HTTP", "robots")) and normalized.startswith("https://") and "://" not in supplied:
        normalized = "http://" + normalized[len("https://"):]
        raw, final_url, content_type, error = fetch_text(normalized, SAFE_OPENER, timeout=min(timeout, max(2.0, deadline.left())), max_bytes=HOME_LIMIT, log=log)
    if raw is None:
        if error.startswith("robots"):
            return evidence("website", "blocked", source_class, normalized, note="robots.txt disallows this user agent"), []
        status = "not_found" if error in {"HTTP 404", "HTTP 410"} else "source_error"
        return evidence("website", status, source_class, normalized, note=error or "empty response"), []
    if "html" not in content_type.casefold():
        return evidence("website", "source_error", source_class, normalized, note=f"Unsupported content type: {content_type[:80]}"), []
    try:
        assert_public_url(final_url or normalized)
    except ValueError as exc:
        return evidence("website", "blocked", source_class, normalized, note=str(exc)), []
    final_url = final_url or normalized
    name_pattern = legal_name_pattern(legal_name)
    home = _page(final_url, raw, org=org, name_pattern=name_pattern)
    soup = home["soup"]
    try:
        structured = extruct.extract(home["html"], base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
    except Exception:
        structured = {}
    main_text = trafilatura.extract(home["html"], url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
    description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
    description = str(description_tag.get("content") or "").strip() if description_tag else ""
    domain = _registered_domain(final_url)
    pages = [home]
    crawl_errors: list[dict[str, str]] = []

    def fetch_page(page_url: str) -> None:
        if deadline.expired():
            crawl_errors.append({"url": page_url, "error": "company time budget exhausted"})
            return
        page_raw, page_final, page_type, page_error = fetch_text(page_url, SAFE_OPENER, timeout=min(timeout, max(2.0, deadline.left())), max_bytes=PAGE_LIMIT, log=log)
        if page_raw is None or "html" not in page_type.casefold():
            crawl_errors.append({"url": page_url, "error": page_error or "unsupported content"})
            return
        if _registered_domain(page_final or page_url) != domain:
            crawl_errors.append({"url": page_url, "error": "redirected outside registered domain"})
            return
        pages.append(_page(page_final or page_url, page_raw, org=org, name_pattern=name_pattern))

    for page_url in _priority_links(final_url, soup, limit=secondary_limit):
        fetch_page(page_url)
    if not any(page["org_span"] for page in pages):
        fetched = {page["url"].rstrip("/") for page in pages}
        for page_url in _identity_links(final_url, soup, identity_limit + len(fetched)):
            if page_url.rstrip("/") in fetched:
                continue
            fetch_page(page_url)
            fetched.add(page_url.rstrip("/"))
            if any(page["org_span"] for page in pages) or len(fetched) > secondary_limit + identity_limit:
                break

    proof = None
    for page in pages:
        if not page["org_span"]:
            continue
        others = page["org_numbers"] - {org}
        if page is home or len(others) <= 1:
            proof = {
                "method": "organisation_number_on_company_page",
                "page_url": page["url"],
                "content_sha256": page["content_sha256"],
                "claim_span": page["org_span"],
                "other_organisation_numbers_on_page": len(others),
            }
            break
    corroborating = []
    places: list[str] = []
    full_pattern = full_legal_name_pattern(legal_name)
    full_span = None
    for page in pages:
        for item in corroborations(page["text"], registry_row):
            if item not in corroborating:
                corroborating.append(item)
        for place in place_corroborations(page["text"], registry_row, legal_name):
            if place not in places:
                places.append(place)
        full_span = full_span or name_span(page["text"], full_pattern)

    social = list(_social_links(final_url, soup))
    for page in pages[1:]:
        social.extend(_social_links(page["url"], page["soup"]))
    social.extend(structured_social_links(structured.get("json-ld", [])))
    for page in pages:
        social.extend(structured_social_links(page["jsonld"]))

    value = {
        "requested_url": normalized,
        "final_url": final_url,
        "registered_domain": domain,
        "title": home["title"],
        "description": description[:2000],
        "main_text_excerpt": main_text[:5000],
        "identity_text_excerpt": home["org_span"] or "",
        "social_links": list({(item["platform"], item["url"]): item for item in social}.values()),
        "structured_organisations": _jsonld_organisations(structured),
        "content_sha256": home["content_sha256"],
        "extraction_state": _extraction_state(main_text, soup),
        "pages": [
            {
                "url": page["url"],
                "title": page["title"],
                "main_text_excerpt": (main_text if page is home else page["text"])[:2000],
                "identity_text_excerpt": page["org_span"] or "",
                "content_sha256": page["content_sha256"],
            }
            for page in pages
        ],
        "crawl_errors": crawl_errors,
        "discovery_origin": origin,
        "identity_proof": proof,
        "registry_corroborations": corroborating,
        "legal_name_span": next((page["name_span"] for page in pages if page["name_span"]), None),
        "full_legal_name_span": full_span,
        "registry_place_matches": places,
        "brand_names": brand_names(soup, home["jsonld"]),
    }
    record = evidence(
        "website",
        "available",
        source_class,
        final_url,
        value=value,
        note="Company-controlled claim layer; not an official registry fact",
        content_sha256=home["content_sha256"],
    )
    return record, pages


def collect_signals(record: dict[str, Any], pages: list[dict[str, Any]], *, deadline: Deadline, log: RequestLog, timeout: float = 12.0) -> dict[str, Any]:
    """Dated activity, job postings, contacts and a careers page from a site that already passed the identity gate."""
    value = record.get("value") or {}
    domain = value.get("registered_domain") or ""
    final_url = value.get("final_url") or record.get("source_url")
    now = utc_now()
    activity: dict[str, dict[str, Any]] = {}
    jobs: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    feeds_checked: list[str] = []
    sitemaps_checked: list[str] = []
    careers_checked: list[str] = []
    found_contacts: dict[str, list[str]] = {"emails": [], "phones": []}

    def add_activity(items: list[dict[str, Any]]) -> None:
        for item in items:
            activity.setdefault(item["url"], item)

    for page in pages:
        add_activity(structured_activity(page["url"], page["jsonld"], domain, now))
        for job in structured_jobs(page["url"], page["jsonld"], now):
            jobs.setdefault(job["url"] + "#" + job["title"], job)
        page_contacts = contacts(page["url"], page["soup"], domain)
        for key in found_contacts:
            found_contacts[key] = sorted(set(found_contacts[key]) | set(page_contacts[key]))[:3]

    home = pages[0] if pages else None
    feed_urls: list[str] = []
    if home:
        for link in home["soup"].select('link[rel~="alternate"][href]'):
            kind = str(link.get("type") or "").casefold()
            if "rss" in kind or "atom" in kind:
                url = urllib.parse.urljoin(home["url"], str(link.get("href")))
                if same_site(url, domain) and "comments" not in url.casefold() and url not in feed_urls:
                    feed_urls.append(url)
        if not feed_urls and "wp-content" in home["html"]:
            feed_urls.append(urllib.parse.urljoin(home["url"], "/feed/"))
    for feed_url in feed_urls[:2]:
        if deadline.expired():
            break
        raw, final, _, error = fetch_text(feed_url, SAFE_OPENER, timeout=min(timeout, max(2.0, deadline.left())), max_bytes=PAGE_LIMIT, log=log, accept="application/rss+xml, application/atom+xml, application/xml, text/xml")
        feeds_checked.append(feed_url)
        if raw is None:
            errors.append({"url": feed_url, "error": error})
            continue
        add_activity(parse_feed(raw, final or feed_url, domain, now))
        if activity:
            break

    if not activity and home:
        listings = [page for page in pages if NEWS_PATH.search(urllib.parse.urlparse(page["url"]).path.rstrip("/") + "/")]
        if not listings and not deadline.expired():
            linked = [urllib.parse.urljoin(home["url"], str(anchor.get("href") or "")) for anchor in home["soup"].select("a[href]")]
            linked = [url for url in linked if same_site(url, domain) and NEWS_PATH.search(urllib.parse.urlparse(url).path.rstrip("/") + "/")]
            listing_url = min(linked, key=len) if linked else None
            if listing_url:
                raw, final, kind, error = fetch_text(listing_url, SAFE_OPENER, timeout=min(timeout, max(2.0, deadline.left())), max_bytes=PAGE_LIMIT, log=log)
                if raw is not None and "html" in kind.casefold():
                    soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml")
                    listings = [{"url": final or listing_url, "soup": soup}]
                elif error:
                    errors.append({"url": listing_url, "error": error})
        for page in listings[:2]:
            add_activity(listing_items(page["url"], page["soup"], domain, now))
        if not activity and listings:
            for article_url in listing_links(listings[0]["url"], listings[0]["soup"], domain):
                if deadline.expired():
                    break
                raw, final, kind, error = fetch_text(article_url, SAFE_OPENER, timeout=min(timeout, max(2.0, deadline.left())), max_bytes=PAGE_LIMIT, log=log)
                if raw is None or "html" not in kind.casefold():
                    errors.append({"url": article_url, "error": error or "unsupported content"})
                    continue
                entry = article_entry(final or article_url, BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml"), now)
                if entry and same_site(entry["url"], domain):
                    add_activity([entry])

    if not activity and home and not deadline.expired():
        origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(home["url"]))
        sitemap_urls = list(ROBOTS.sitemaps.get(origin, [])) or [origin + "/sitemap.xml"]
        candidates: list[tuple[str, str | None]] = []
        queue = sitemap_urls[:2]
        while queue and len(sitemaps_checked) < 3 and not deadline.expired():
            sitemap_url = queue.pop(0)
            if not same_site(sitemap_url, domain):
                continue
            raw, _, _, error = fetch_text(sitemap_url, SAFE_OPENER, timeout=min(timeout, max(2.0, deadline.left())), max_bytes=3_000_000, log=log, accept="application/xml, text/xml")
            sitemaps_checked.append(sitemap_url)
            if raw is None:
                errors.append({"url": sitemap_url, "error": error})
                continue
            urls, children = parse_sitemap(raw)
            candidates.extend(urls)
            newsy = [child for child in children if any(term in child.casefold() for term in ("post", "news", "nyhet", "artikl", "blog", "aktuelt"))]
            queue.extend(newsy[:1])
        dated = []
        for loc, lastmod in candidates:
            day = _iso(lastmod)
            title = slug_title(loc)
            if NEWS_PATH.search(urllib.parse.urlparse(loc).path + "/") and title and _plausible_date(day, now) and same_site(loc, domain):
                dated.append({"title": title[:300], "url": loc, "date": day, "date_type": "last_modified", "kind": "page",
                              "method": "sitemap_lastmod", "source_url": sitemaps_checked[-1] if sitemaps_checked else loc})
        dated.sort(key=lambda item: item["date"], reverse=True)
        add_activity(dated[:5])

    careers = []
    if home:
        seen_links: set[str] = set()
        for page in pages:
            for link in career_links(page["url"], page["soup"], domain):
                if link["url"] not in seen_links:
                    seen_links.add(link["url"])
                    careers.append(link)
    careers.sort(key=lambda item: (item["ats"] == "", len(item["url"])))
    careers_page = careers[0]["url"] if careers else None
    for link in careers[:2]:
        if deadline.expired() or len(jobs) >= MAX_JOBS:
            break
        try:
            assert_public_url(link["url"])
        except ValueError as exc:
            errors.append({"url": link["url"], "error": str(exc)})
            continue
        raw, final, kind, error = fetch_text(link["url"], SAFE_OPENER, timeout=min(timeout, max(2.0, deadline.left())), max_bytes=PAGE_LIMIT, log=log)
        careers_checked.append(link["url"])
        if raw is None or "html" not in kind.casefold():
            errors.append({"url": link["url"], "error": error or "unsupported content"})
            continue
        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml")
        page_url = final or link["url"]
        for job in structured_jobs(page_url, jsonld_blocks(soup), now):
            jobs.setdefault(job["url"] + "#" + job["title"], job)
        ats = link["ats"]
        if not ats:
            for nested in career_links(page_url, soup, domain):
                if nested["ats"]:
                    ats, page_url = nested["ats"], nested["url"]
                    raw, final, kind, error = fetch_text(page_url, SAFE_OPENER, timeout=min(timeout, max(2.0, deadline.left())), max_bytes=PAGE_LIMIT, log=log)
                    careers_checked.append(page_url)
                    soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml") if raw and "html" in kind.casefold() else None
                    page_url = final or page_url
                    break
        if ats and soup is not None:
            for job in ats_jobs(page_url, soup, ats):
                jobs.setdefault(job["url"] + "#" + job["title"], job)

    ordered_activity = sorted(activity.values(), key=lambda item: item["date"], reverse=True)[:MAX_FEED_ITEMS]
    return {
        "activity": ordered_activity,
        "jobs": list(jobs.values())[:MAX_JOBS],
        "careers_page": careers_page,
        "careers_checked": careers_checked,
        "feeds_checked": feeds_checked,
        "sitemaps_checked": sitemaps_checked,
        "contacts": found_contacts,
        "errors": errors,
        "checked_at": now.isoformat().replace("+00:00", "Z"),
    }
