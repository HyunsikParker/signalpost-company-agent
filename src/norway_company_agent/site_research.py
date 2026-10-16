"""Company-site research: candidate sites, exact-entity proof, and dated signals from company-owned pages.

Every value returned here comes from a page the company controls (or a page it links to), fetched with the
same public-URL and robots checks as the registry-website crawl. Nothing is inferred from a search result.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"

# Mailbox providers and shared administrators: their domain says nothing about the company's own site.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.no",
    "live.com", "msn.com", "yahoo.no", "yahoo.com", "icloud.com", "me.com", "mac.com", "online.no",
    "c2i.net", "lyse.net", "broadpark.no", "getmail.no", "frisurf.no", "start.no", "chello.no",
    "enivest.net", "haugnett.no", "altibox.no", "tele2.no", "netcom.no", "telia.no", "ebnett.no",
    "bluezone.no", "loqal.no", "gmx.com", "gmx.net", "proton.me", "protonmail.com", "aol.com",
    "styrerommet.no", "styrerommet.net", "obos.no", "usbl.no", "nobl.no", "bate.no", "pbbl.no",
}
LEGAL_SUFFIXES = {
    "as", "asa", "ans", "da", "sa", "nuf", "enk", "ba", "sti", "iks", "ks", "bbl", "brl", "fli", "esek",
    "sam", "stiftelsen", "ab", "ltd", "limited",
}
NEWS_PATH = re.compile(r"/(nyheter|nyhet|aktuelt|news|blogg|blog|artikler|artikkel|presse|press|innlegg|arkiv)/", re.I)
CAREER_SEGMENTS = {
    "karriere", "career", "careers", "jobs", "jobb", "jobber", "stillinger", "ledige-stillinger", "ledigestillinger", "ledige-jobber",
    "jobbe-hos-oss", "jobb-hos-oss", "jobbmuligheter", "vacancies", "join-us", "work-with-us", "arbeid-hos-oss",
}
# Placeholder posts that site builders and themes install; they say nothing about the company.
PLACEHOLDER_TITLES = {"hello world", "hei verden", "hallo verden", "hallo welt", "sample page", "eksempelside", "uncategorized"}
CAREER_TEXTS = {
    "karriere", "jobb hos oss", "jobbe hos oss", "ledige stillinger", "stillinger", "careers", "career", "jobs", "ledige jobber",
    "jobbmuligheter", "join us", "work with us", "jobb", "vacancies", "jobb i", "bli med på laget",
}
IDENTITY_TERMS = ("kontakt", "contact", "om-oss", "om_oss", "om oss", "about", "personvern", "privacy", "vilkar", "vilkår", "impressum", "kjopsbetingelser", "salgsbetingelser")
ATS_HOSTS = {
    "teamtailor.com": "teamtailor", "recman.page": "recman", "recman.no": "recman", "webcruiter.no": "webcruiter",
    "webcruiter.com": "webcruiter", "jobbnorge.no": "jobbnorge", "reachmee.com": "reachmee", "easycruit.com": "easycruit",
    "hrmanager.no": "hrmanager", "jobylon.com": "jobylon", "varbi.com": "varbi", "emply.com": "emply", "career.emply.com": "emply",
}
ATS_JOB_PATH = {
    "teamtailor": re.compile(r"/jobs/\d+"),
    "recman": re.compile(r"/job/|/jobb/"),
    "webcruiter": re.compile(r"advertid=\d+|/job/\d+", re.I),
    "jobbnorge": re.compile(r"/ledige-stillinger/stilling/\d+|/job/\d+", re.I),
    "reachmee": re.compile(r"job_id=\d+|/job/\d+", re.I),
    "easycruit": re.compile(r"/vacancy/\d+", re.I),
    "hrmanager": re.compile(r"/stilling|/job/|ShowAd|stillingsannonse", re.I),
    "jobylon": re.compile(r"/jobs/\d+", re.I),
    "varbi": re.compile(r"/what:job/jobID:\d+|jobID", re.I),
    "emply": re.compile(r"/ad/|/job/", re.I),
}
ORG_PATTERN = re.compile(r"(?<![\d])(\d{3})[\s.   -]?(\d{3})[\s.   -]?(\d{3})(?![\d])")
MAX_FEED_ITEMS = 10
# Only shared role mailboxes are published; personal addresses on a company site stay out of the profile.
ROLE_MAILBOXES = {
    "post", "postmottak", "info", "kontakt", "contact", "firmapost", "salg", "sales", "office", "kontor", "hei", "hello",
    "booking", "bestilling", "ordre", "order", "service", "kundeservice", "support", "admin", "faktura", "regnskap", "mail",
    "epost", "resepsjon", "jobb", "jobs", "karriere", "careers", "presse", "press", "marked", "marketing", "butikk", "shop",
    "verksted", "drift", "prosjekt", "tilbud", "kunde", "kundesenter", "felles", "firma", "mottak",
}
MAX_REQUEST_SECONDS = 25.0
MAX_JOBS = 15


@dataclass
class RequestLog:
    """Thread-local tally of outbound requests for one company."""

    requests: int = 0
    bytes: int = 0
    latencies_ms: list[int] = field(default_factory=list)

    def add(self, size: int, elapsed_ms: int) -> None:
        self.requests += 1
        self.bytes += size
        self.latencies_ms.append(elapsed_ms)


def valid_organisation_number(digits: str) -> bool:
    """Norwegian organisation numbers carry a MOD11 check digit."""
    if not re.fullmatch(r"\d{9}", digits):
        return False
    weights = (3, 2, 7, 6, 5, 4, 3, 2)
    remainder = sum(int(d) * w for d, w in zip(digits[:8], weights)) % 11
    check = 0 if remainder == 0 else 11 - remainder
    return check != 10 and check == int(digits[8])


def organisation_numbers_in(text: str) -> set[str]:
    found = {"".join(match.groups()) for match in ORG_PATTERN.finditer(text)}
    return {digits for digits in found if valid_organisation_number(digits)}


def org_number_span(text: str, org: str, width: int = 110) -> str | None:
    for match in ORG_PATTERN.finditer(text):
        if "".join(match.groups()) == org:
            start, end = max(0, match.start() - width), min(len(text), match.end() + 40)
            return re.sub(r"\s+", " ", text[start:end]).strip()
    return None


def visible_text(soup: BeautifulSoup) -> str:
    clone = BeautifulSoup(str(soup), "lxml")
    for node in clone(["script", "style", "noscript", "template", "svg"]):
        node.decompose()
    return re.sub(r"\s+", " ", clone.get_text(" ", strip=True))


def legal_name_pattern(name: str) -> re.Pattern[str] | None:
    """Case-insensitive pattern for the legal name's words in order, without the legal-form suffix."""
    words = [word for word in re.findall(r"[^\W_]+", str(name or "").casefold()) if word not in LEGAL_SUFFIXES]
    if not words:
        return None
    return re.compile(r"(?<![^\W_])" + r"[\W_]{1,3}".join(re.escape(word) for word in words) + r"(?![^\W_])", re.I)


def full_legal_name_pattern(name: str) -> re.Pattern[str] | None:
    """The complete registered name, legal form included ("Finnmark Utleie AS"), as written on a page."""
    words = re.findall(r"[^\W_]+", str(name or "").casefold())
    if len(words) < 2 or not any(word in LEGAL_SUFFIXES for word in words):
        return None
    return re.compile(r"(?<![^\W_])" + r"[\W_]{1,3}".join(re.escape(word) for word in words) + r"(?![^\W_])", re.I)


def place_corroborations(text: str, registry_row: dict[str, Any], legal_name: str) -> list[str]:
    """Registered municipality, postal place or postcode on the page, ignoring places that are part of the name."""
    found = []
    name = str(legal_name or "").casefold()
    for key in ("forretningsadresse.poststed", "forretningsadresse.kommune", "forretningsadresse.postnummer", "postadresse.poststed"):
        value = str(registry_row.get(key) or "").strip()
        if len(value) < 3 or value.casefold() in name:
            continue
        if re.search(r"(?<![^\W_])" + re.escape(value) + r"(?![^\W_])", text, re.I) and value not in found:
            found.append(value)
    return found


def name_span(text: str, pattern: re.Pattern[str] | None, width: int = 80) -> str | None:
    match = pattern.search(text) if pattern else None
    if not match:
        return None
    start, end = max(0, match.start() - width), min(len(text), match.end() + width)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _ascii_tokens(name: str, table: dict[str, str]) -> list[str]:
    text = name.casefold().replace("&", " og ").translate(str.maketrans(table))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return [token for token in re.findall(r"[a-z0-9]+", text) if token not in LEGAL_SUFFIXES]


def name_domain_guesses(name: str, limit: int = 8) -> list[str]:
    """Registered-domain guesses from the legal name, most likely first. Guesses are only candidates."""
    variants = []
    for table in ({"æ": "ae", "ø": "o", "å": "a"}, {"æ": "ae", "ø": "oe", "å": "aa"}):
        tokens = _ascii_tokens(name, table)
        if tokens and tokens not in variants:
            variants.append(tokens)
    if not variants:
        return []
    plain = [[token for token in tokens if token != "og"] or tokens for tokens in variants]
    first = plain[0]
    ordered = [
        *("".join(parts) + ".no" for parts in plain),
        "-".join(first) + ".no",
        "".join(first) + ".com",
        "".join(variants[0]) + ".no",
        "-".join(variants[0]) + ".no",
        "".join(first) + "as.no",
    ]
    if len(first) >= 2 and first[-1] in {"holding", "invest", "eiendom", "consulting", "drift", "service", "gruppen", "group"}:
        ordered.append("".join(first[:-1]) + ".no")
    guesses: list[str] = []
    for domain in ordered:
        label = domain.rsplit(".", 1)[0]
        if 3 <= len(label) <= 63 and domain not in guesses:
            guesses.append(domain)
    return guesses[:limit]


def email_domain(address: str | None) -> str | None:
    address = str(address or "").strip().casefold()
    if "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip(". ")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", domain) or domain in FREE_MAIL_DOMAINS:
        return None
    parts = domain.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 and parts[-1] == "no" else domain


def candidate_sites(profile: dict[str, Any], registry_row: dict[str, Any]) -> list[dict[str, str]]:
    """Candidate homepages in the order they are tried: registry e-mail domain, then legal-name guesses."""
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    registered = str(profile.get("website") or "").casefold()
    registered_host = urllib.parse.urlparse(registered if "://" in registered else "https://" + registered).hostname or ""
    registered_host = registered_host.removeprefix("www.")
    if registered_host:
        seen.add(registered_host)
    mail = email_domain(registry_row.get("epostadresse"))
    if mail and mail not in seen:
        seen.add(mail)
        candidates.append({"domain": mail, "origin": "registry_email_domain"})
    for domain in name_domain_guesses(str(profile.get("name") or "")):
        if domain not in seen:
            seen.add(domain)
            candidates.append({"domain": domain, "origin": "legal_name_domain_guess"})
    return candidates


def resolving_host(domain: str) -> str | None:
    for host in ("www." + domain, domain):
        try:
            socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            return host
        except OSError:
            continue
    return None


def phone_digits(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0047"):
        digits = digits[4:]
    elif digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    return digits if len(digits) == 8 else None


def corroborations(text: str, registry_row: dict[str, Any]) -> list[str]:
    """Registry facts (phone, street address, e-mail) that also appear on the page."""
    found = []
    digits = re.sub(r"\D", "", text)
    for key in ("telefon", "mobil"):
        number = phone_digits(registry_row.get(key))
        if number and number in digits:
            found.append(f"registry {key} {registry_row.get(key)} appears on the page")
    street = str(registry_row.get("forretningsadresse.adresse") or "").strip()
    postcode = str(registry_row.get("forretningsadresse.postnummer") or "").strip()
    folded = text.casefold()
    if street and len(street) >= 6 and street.casefold() in folded and (not postcode or postcode in text):
        found.append(f"registry business address '{street}' appears on the page")
    mail = str(registry_row.get("epostadresse") or "").strip().casefold()
    if mail and mail in folded:
        found.append(f"registry e-mail {mail} appears on the page")
    return found


class RobotsCache:
    """One robots.txt fetch per host per run; an unreachable robots file allows a bounded crawl."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.sitemaps: dict[str, list[str]] = {}

    def allowed(self, url: str, opener: Any, timeout: float, log: RequestLog | None = None) -> bool:
        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        with self._lock:
            cached = origin in self._parsers
            parser = self._parsers.get(origin)
        if not cached:
            parser = None
            started = time.monotonic()
            try:
                request = urllib.request.Request(origin + "/robots.txt", headers={"User-Agent": USER_AGENT})
                with opener.open(request, timeout=timeout) as response:
                    raw = response.read(500_000)
                lines = raw.decode("utf-8", errors="replace").splitlines()
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(lines)
                sitemaps = [line.split(":", 1)[1].strip() for line in lines if line.casefold().startswith("sitemap:")]
                with self._lock:
                    self.sitemaps[origin] = sitemaps[:5]
                if log:
                    log.add(len(raw), int((time.monotonic() - started) * 1000))
            except Exception:
                if log:
                    log.add(0, int((time.monotonic() - started) * 1000))
            with self._lock:
                self._parsers[origin] = parser
        return True if parser is None else parser.can_fetch(USER_AGENT, url)


ROBOTS = RobotsCache()


def fetch_text(url: str, opener: Any, *, timeout: float, max_bytes: int, log: RequestLog, accept: str = "text/html,application/xhtml+xml") -> tuple[bytes | None, str | None, str, str]:
    """GET one public URL after the robots check. Returns (raw, final_url, content_type, error)."""
    if not ROBOTS.allowed(url, opener, timeout, log):
        return None, None, "", "robots.txt disallows this URL"
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with opener.open(request, timeout=timeout) as response:
            chunks: list[bytes] = []
            size = 0
            while size <= max_bytes:
                chunk = response.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if time.monotonic() - started > MAX_REQUEST_SECONDS:
                    log.add(size, int((time.monotonic() - started) * 1000))
                    return None, None, "", "response exceeded the per-request time cap"
            raw = b"".join(chunks)
            log.add(len(raw), int((time.monotonic() - started) * 1000))
            if len(raw) > max_bytes:
                return None, response.geturl(), response.headers.get("content-type", ""), "oversized response"
            return raw, response.geturl(), response.headers.get("content-type", ""), ""
    except urllib.error.HTTPError as exc:
        log.add(0, int((time.monotonic() - started) * 1000))
        return None, None, "", f"HTTP {exc.code}"
    except Exception as exc:  # network errors are recorded, never raised
        log.add(0, int((time.monotonic() - started) * 1000))
        return None, None, "", f"{type(exc).__name__}: {str(exc)[:120]}"


def _iso(value: str | None) -> str | None:
    """Normalise RFC 822, ISO 8601 and Norwegian dd.mm.yyyy dates to YYYY-MM-DD; None when unparseable."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
        if parsed:
            return parsed.astimezone(timezone.utc).date().isoformat() if parsed.tzinfo else parsed.date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "-".join(match.groups())
    match = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if match:
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    return None


def _plausible_date(day: str | None, now: datetime) -> bool:
    if not day:
        return False
    try:
        parsed = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime(1995, 1, 1, tzinfo=timezone.utc) <= parsed <= now


def jsonld_blocks(soup: BeautifulSoup) -> list[Any]:
    blocks = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            blocks.append(json.loads(node.string or node.get_text() or ""))
        except (json.JSONDecodeError, TypeError):
            continue
    return blocks


def _walk_types(value: Any, wanted: set[str]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        kind = value.get("@type")
        kinds = set(kind if isinstance(kind, list) else [kind])
        if kinds & wanted:
            found.append(value)
        for child in value.values():
            found.extend(_walk_types(child, wanted))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_types(child, wanted))
    return found


def _text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value") or ""
    if isinstance(value, list):
        value = value[0] if value else ""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def same_site(url: str, registered_domain: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").casefold()
    return host == registered_domain or host.endswith("." + registered_domain)


def structured_activity(page_url: str, blocks: list[Any], registered_domain: str, now: datetime) -> list[dict[str, Any]]:
    items = []
    for node in _walk_types(blocks, {"Article", "NewsArticle", "BlogPosting", "PressRelease", "Event"}):
        day = _iso(node.get("datePublished") or node.get("startDate") or node.get("dateCreated"))
        title = _text(node.get("headline") or node.get("name"))
        url = urllib.parse.urljoin(page_url, _text(node.get("url")) or page_url)
        if title and _plausible_date(day, now) and same_site(url, registered_domain):
            items.append({"title": title[:300], "url": url, "date": day, "date_type": "published",
                          "kind": "event" if "Event" in str(node.get("@type")) else "article",
                          "method": "json_ld", "source_url": page_url})
    return items


def structured_jobs(page_url: str, blocks: list[Any], now: datetime) -> list[dict[str, Any]]:
    jobs = []
    for node in _walk_types(blocks, {"JobPosting"}):
        title = _text(node.get("title") or node.get("name"))
        if not title:
            continue
        valid_through = _iso(node.get("validThrough"))
        if valid_through and valid_through < now.date().isoformat():
            continue
        location = node.get("jobLocation")
        address = location[0] if isinstance(location, list) and location else location
        locality = _text((address or {}).get("address", {}).get("addressLocality")) if isinstance(address, dict) and isinstance(address.get("address"), dict) else ""
        jobs.append({
            "title": title[:300],
            "url": urllib.parse.urljoin(page_url, _text(node.get("url")) or page_url),
            "date_posted": _iso(node.get("datePosted")),
            "valid_through": valid_through,
            "location": locality or None,
            "hiring_organisation": _text(node.get("hiringOrganization")) or None,
            "method": "json_ld_jobposting",
            "source_url": page_url,
        })
    return jobs


def parse_feed(raw: bytes, feed_url: str, registered_domain: str, now: datetime) -> list[dict[str, Any]]:
    """RSS 2.0 and Atom items with a parseable date and a link on the company's own domain."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in {"item", "entry"}:
            continue
        values: dict[str, str] = {}
        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "link" and child.get("href"):
                values.setdefault("link", child.get("href") or "")
            elif child.text and name not in values:
                values[name] = child.text.strip()
        title = re.sub(r"\s+", " ", values.get("title", "")).strip()
        link = urllib.parse.urljoin(feed_url, values.get("link", ""))
        day = _iso(values.get("pubDate") or values.get("published") or values.get("updated") or values.get("date"))
        if title and link and _plausible_date(day, now) and same_site(link, registered_domain):
            items.append({"title": title[:300], "url": link, "date": day, "date_type": "published", "kind": "post",
                          "method": "rss_atom_feed", "source_url": feed_url})
    return items


def parse_sitemap(raw: bytes) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Return (url, lastmod) pairs and child sitemap URLs."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return [], []
    urls, children = [], []
    for node in root:
        tag = node.tag.rsplit("}", 1)[-1]
        loc = lastmod = None
        for child in node:
            name = child.tag.rsplit("}", 1)[-1]
            if name == "loc" and child.text:
                loc = child.text.strip()
            elif name == "lastmod" and child.text:
                lastmod = child.text.strip()
        if not loc:
            continue
        if tag == "sitemap":
            children.append(loc)
        elif tag == "url":
            urls.append((loc, lastmod))
    return urls, children


def slug_title(url: str) -> str | None:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    slug = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    slug = re.sub(r"\.(html?|php|aspx)$", "", slug, flags=re.I)
    words = [word for word in re.split(r"[-_]+", slug) if word]
    if len(words) < 2 or all(word.isdigit() for word in words):
        return None
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def career_links(page_url: str, soup: BeautifulSoup, registered_domain: str) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for anchor in soup.select("a[href]"):
        url = urllib.parse.urljoin(page_url, str(anchor.get("href") or "").strip())
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        host = (parsed.hostname or "").casefold()
        ats = next((label for domain, label in ATS_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
        segments = {segment.casefold() for segment in urllib.parse.unquote(parsed.path).split("/") if segment}
        label = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).casefold().strip(" .:!")
        careers = bool(segments & CAREER_SEGMENTS) or label in CAREER_TEXTS
        if parsed.path.strip("/") == "" and not ats:
            continue
        if ats or (same_site(url, registered_domain) and careers):
            clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))
            found.setdefault(clean, {"url": clean, "ats": ats or ""})
    return sorted(found.values(), key=lambda item: (item["ats"] == "", len(item["url"])))[:3]


def ats_jobs(list_url: str, soup: BeautifulSoup, ats: str) -> list[dict[str, Any]]:
    pattern = ATS_JOB_PATH.get(ats)
    if not pattern:
        return []
    jobs: dict[str, dict[str, Any]] = {}
    for anchor in soup.select("a[href]"):
        url = urllib.parse.urljoin(list_url, str(anchor.get("href") or "").strip())
        title = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True)).strip()
        if pattern.search(url) and 4 <= len(title) <= 200 and url not in jobs:
            jobs[url] = {"title": title, "url": url, "date_posted": None, "valid_through": None, "location": None,
                         "hiring_organisation": None, "method": f"ats_listing_{ats}", "source_url": list_url}
    return list(jobs.values())[:MAX_JOBS]


def contacts(page_url: str, soup: BeautifulSoup, registered_domain: str) -> dict[str, list[str]]:
    emails, phones = set(), set()
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if href.casefold().startswith("mailto:"):
            address = urllib.parse.unquote(href[7:].split("?", 1)[0]).strip().casefold()
            local, _, host = address.partition("@")
            if re.fullmatch(r"[^@\s]+@[a-z0-9.-]+\.[a-z]{2,}", address) and host.endswith(registered_domain) and local in ROLE_MAILBOXES:
                emails.add(address)
        elif href.casefold().startswith("tel:"):
            number = phone_digits(urllib.parse.unquote(href[4:]))
            if number:
                phones.add("+47" + number)
    return {"emails": sorted(emails)[:3], "phones": sorted(phones)[:3]}


def brand_names(soup: BeautifulSoup, blocks: list[Any]) -> list[str]:
    names = []
    site_name = soup.select_one('meta[property="og:site_name"]')
    if site_name and site_name.get("content"):
        names.append(str(site_name.get("content")).strip())
    for node in _walk_types(blocks, {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant", "ProfessionalService"}):
        for key in ("name", "alternateName", "legalName"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
    unique: list[str] = []
    for name in names:
        if 2 <= len(name) <= 80 and name not in unique:
            unique.append(name)
    return unique[:3]


def content_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


MONTHS = {
    "januar": 1, "jan": 1, "january": 1, "februar": 2, "feb": 2, "february": 2, "mars": 3, "mar": 3, "march": 3,
    "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "jun": 6, "june": 6, "juli": 7, "jul": 7, "july": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10, "oct": 10, "october": 10,
    "november": 11, "nov": 11, "desember": 12, "des": 12, "dec": 12, "december": 12,
}
_MONTH_WORDS = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_PATTERNS = (
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)"), "ymd"),
    (re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)"), "dmy"),
    (re.compile(rf"(?<!\d)(\d{{1,2}})\.?\s+({_MONTH_WORDS})\.?,?\s+(\d{{4}})(?!\d)", re.I), "d_month_y"),
    (re.compile(rf"\b({_MONTH_WORDS})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})(?!\d)", re.I), "month_d_y"),
)


def find_date(text: str) -> str | None:
    """The first full date written in a common Norwegian or English form, as YYYY-MM-DD."""
    for pattern, kind in DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        a, b, c = match.groups()
        try:
            if kind == "ymd":
                year, month, day = int(a), int(b), int(c)
            elif kind == "dmy":
                day, month, year = int(a), int(b), int(c)
            elif kind == "d_month_y":
                day, month, year = int(a), MONTHS[b.casefold()], int(c)
            else:
                month, day, year = MONTHS[a.casefold()], int(b), int(c)
            return datetime(year, month, day).date().isoformat()
        except (ValueError, KeyError):
            continue
    return None


def _item_date(node: Any) -> str | None:
    stamp = node.find("time") if hasattr(node, "find") else None
    if stamp is not None:
        day = _iso(stamp.get("datetime")) or find_date(stamp.get_text(" ", strip=True))
        if day:
            return day
    return find_date(node.get_text(" ", strip=True)[:400])


def listing_items(page_url: str, soup: BeautifulSoup, registered_domain: str, now: datetime, limit: int = MAX_FEED_ITEMS) -> list[dict[str, Any]]:
    """Dated entries on a company's news listing: links below the listing's own path whose item block shows a date.

    The block climbs from the link to the nearest ancestor that still holds only this article, so a date printed
    beside a neighbouring article is never borrowed."""
    base = urllib.parse.urlparse(page_url).path.rstrip("/")
    items: dict[str, dict[str, Any]] = {}
    for anchor in soup.select("a[href]"):
        url = urllib.parse.urljoin(page_url, str(anchor.get("href") or "").strip()).split("#", 1)[0]
        path = urllib.parse.urlparse(url).path.rstrip("/")
        if not same_site(url, registered_domain) or not base or not path.startswith(base + "/") or url in items:
            continue
        if re.search(r"/(page|side|tag|tags|kategori|category|author|forfatter)/", path + "/", re.I):
            continue
        block, day = anchor, None
        for _ in range(5):
            parent = block.parent
            if parent is None or parent.name in {"body", "html", "main"}:
                break
            links = {urllib.parse.urljoin(page_url, str(link.get("href") or "")).split("#", 1)[0].rstrip("/")
                     for link in parent.select("a[href]")}
            links = {link for link in links if urllib.parse.urlparse(link).path.rstrip("/").startswith(base + "/")}
            if len(links) > 1:
                break
            block = parent
            day = _item_date(block)
            if day:
                break
        heading = block.find(["h1", "h2", "h3", "h4"]) if block is not anchor else None
        title = re.sub(r"\s+", " ", (heading.get_text(" ", strip=True) if heading else anchor.get_text(" ", strip=True))).strip()
        if day and _plausible_date(day, now) and 8 <= len(title) <= 300:
            items[url] = {"title": title, "url": url, "date": day, "date_type": "listed", "kind": "news",
                          "method": "news_listing_page", "source_url": page_url}
    return sorted(items.values(), key=lambda item: item["date"], reverse=True)[:limit]


def listing_links(page_url: str, soup: BeautifulSoup, registered_domain: str, limit: int = 3) -> list[str]:
    """Article links below a listing's own path, in page order (listings usually start with the newest)."""
    base = urllib.parse.urlparse(page_url).path.rstrip("/")
    found: list[str] = []
    for anchor in soup.select("a[href]"):
        url = urllib.parse.urljoin(page_url, str(anchor.get("href") or "").strip()).split("#", 1)[0]
        path = urllib.parse.urlparse(url).path.rstrip("/")
        if (same_site(url, registered_domain) and base and path.startswith(base + "/") and url not in found
                and not re.search(r"/(page|side|tag|tags|kategori|category|author|forfatter)/", path + "/", re.I)):
            found.append(url)
    return found[:limit]


def article_entry(page_url: str, soup: BeautifulSoup, now: datetime) -> dict[str, Any] | None:
    """Title and publication date of one company article, from metadata first and the visible header last."""
    day, date_type = None, "published"
    for selector in ('meta[property="article:published_time"]', 'meta[name="date"]', 'meta[itemprop="datePublished"]', 'meta[name="DC.date.issued"]'):
        node = soup.select_one(selector)
        if node and node.get("content"):
            day = _iso(str(node.get("content")))
            if day:
                break
    if not day:
        for item in structured_activity(page_url, jsonld_blocks(soup), urllib.parse.urlparse(page_url).hostname or "", now):
            day = item["date"]
            break
    if not day:
        stamp = soup.find("time")
        if stamp is not None:
            day = _iso(stamp.get("datetime")) or find_date(stamp.get_text(" ", strip=True))
    if not day:
        body = soup.find("article") or soup.find("main") or soup.body
        day = find_date(re.sub(r"\s+", " ", body.get_text(" ", strip=True))[:600]) if body else None
        date_type = "shown_on_page"
    heading = soup.find("h1")
    og_title = soup.select_one('meta[property="og:title"]')
    title = re.sub(r"\s+", " ", heading.get_text(" ", strip=True) if heading else str(og_title.get("content") or "") if og_title else "").strip()
    if not (day and _plausible_date(day, now) and 8 <= len(title) <= 300):
        return None
    return {"title": title, "url": page_url, "date": day, "date_type": date_type, "kind": "news", "method": "company_article_page", "source_url": page_url}


def credible_activity(items: list[dict[str, Any]], founded: str | None) -> tuple[list[dict[str, Any]], int]:
    """Drop placeholder posts and anything dated before the company existed (theme demo content)."""
    kept = []
    for item in items:
        title = re.sub(r"[^\w ]+", "", str(item.get("title") or "").casefold()).strip()
        if title in PLACEHOLDER_TITLES:
            continue
        if founded and str(item.get("date") or "") < founded[:10]:
            continue
        kept.append(item)
    return kept, len(items) - len(kept)
