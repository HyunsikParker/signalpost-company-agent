"""Turn a Signalpost run's profiles.jsonl into a compact, public-safe dataset for the JBOX site.

Each company keeps the facts a reader can check (registry, accounts, website, group, locations, news, jobs) and, for
every fact, where it came from and when it was fetched. Person names (roles, c/o addresses), e-mail addresses, phone
numbers and street addresses are left out.
usage: python3 scripts/build_site_dataset.py <profiles.jsonl> <out.json> [<number of companies>]
"""
import json
import sys

LABELS = {"registry": "Business register entry", "registry_live": "Live register record",
          "accounting_obligation": "Accounts filing obligation", "financials": "Annual accounts",
          "group": "Group structure", "locations": "Business locations", "website": "Company website",
          "website_discovery": "Website search", "company_profiles": "Social and directory profiles",
          "jobs": "Job postings", "activity": "News and updates", "site_contacts": "Contact channels on the website"}


STOPWORDS = {"for", "og", "med", "til", "av", "i", "på", "om", "the", "and", "of", "to", "in", "a"}


def looks_like_a_person(title):
    """Two or three plain words and no function word, e.g. a staff or customer page named after a person."""
    words = (title or "").split()
    return 2 <= len(words) <= 3 and all(w.isalpha() for w in words) and not STOPWORDS & {w.lower() for w in words}


def source(ev):
    return {"url": ev.get("source_url"), "type": ev.get("source_type"), "fetched_at": ev.get("retrieved_at")}


def accounts(ev):
    recs = sorted((ev.get("value") or {}).get("records") or [], key=lambda r: r["period"]["tilDato"])
    if not recs:
        return None
    last, prev = recs[-1], (recs[-2] if len(recs) > 1 else None)
    out = {"year": last["period"]["tilDato"][:4], "currency": last.get("currency"), "revenue": last.get("revenue"),
           "operating_result": last.get("operating_result"), "annual_result": last.get("annual_result"),
           "equity": last.get("equity"), "assets": last.get("assets"),
           "history": [{"year": r["period"]["tilDato"][:4], "revenue": r.get("revenue"),
                        "annual_result": r.get("annual_result")} for r in recs]}
    if prev and prev.get("revenue") and last.get("revenue") is not None:
        out["revenue_change_pct"] = round(100 * (last["revenue"] - prev["revenue"]) / abs(prev["revenue"]), 1)
        out["compared_with_year"] = prev["period"]["tilDato"][:4]
    return out


def fact_text(field, ev, company):
    v = ev.get("value")
    if ev.get("status") != "available":
        return None
    if field == "financials":
        a = company.get("accounts")
        return f"{a['year']}: revenue {a['revenue']:,.0f} {a['currency']}" if a and a.get("revenue") is not None else "filed"
    if field == "website":
        return v.get("final_url") if isinstance(v, dict) else None
    if field == "group" and isinstance(v, dict):
        n = len(v.get("children") or [])
        return f"{n} subsidiar{'y' if n == 1 else 'ies'}" + (", has a parent company" if v.get("parent") else "")
    if field == "locations" and isinstance(v, dict):
        n = len(v.get("locations") or [])
        return f"{n} registered business location{'' if n == 1 else 's'}"
    if field == "activity" and isinstance(v, dict):
        n = len(v.get("items") or [])
        return f"{n} dated page{'' if n == 1 else 's'} or post{'' if n == 1 else 's'}"
    if field == "company_profiles" and isinstance(v, dict):
        return ", ".join(p.get("platform") or p.get("url", "") for p in v.get("profiles") or [])
    if field == "jobs" and isinstance(v, dict):
        n = len(v.get("postings") or [])
        return f"{n} open posting{'' if n == 1 else 's'}"
    if field == "site_contacts":
        return "contact channels listed on the company website"
    if field == "accounting_obligation" and isinstance(v, dict):
        return v.get("classification", "").replace("_", " ")
    if field in ("registry", "registry_live"):
        return f"{company['legal_form']} in {company['municipality'].title()}" if company.get("municipality") else company["legal_form"]
    return None


def company(row):
    ev = row.get("evidence") or {}
    c = {"id": row["organisation_number"], "name": row["name"], "legal_form": row.get("legal_form"),
         "municipality": row.get("municipality"), "industry": {"code": row.get("industry_code"), "label": row.get("industry_label")},
         "employees": row.get("employees"), "bankrupt": row.get("bankrupt"), "liquidating": row.get("liquidating"),
         "latest_filed_accounts": row.get("latest_submitted_accounts")}
    if ev.get("financials", {}).get("status") == "available":
        c["accounts"] = accounts(ev["financials"])
    w = ev.get("website") or {}
    if w.get("status") == "available" and isinstance(w.get("value"), dict):
        c["website"] = {"url": w["value"].get("final_url"), "title": w["value"].get("title"),
                        "description": (w["value"].get("description") or "")[:240] or None}
    act = ev.get("activity") or {}
    if act.get("status") == "available":
        items = [i for i in (act.get("value") or {}).get("items", []) if not looks_like_a_person(i.get("title"))]
        if items:
            c["news"] = [{"title": i.get("title"), "date": i.get("date"), "url": i.get("url")} for i in items[:3]]
    prof = ev.get("company_profiles") or {}
    if prof.get("status") == "available":
        c["profiles"] = [{"platform": p.get("platform"), "url": p.get("url")} for p in (prof.get("value") or {}).get("profiles", [])]
    c["facts"] = []
    for field, label in LABELS.items():
        e = ev.get(field)
        if not e:
            continue
        c["facts"].append({"field": field, "label": label, "status": e.get("status"), "value": fact_text(field, e, c),
                           "note": (e.get("note") or None) if e.get("status") != "available" else None,
                           "source": source(e)})
    return c


def main(src, dst, limit=None):
    rows = [json.loads(line) for line in open(src, encoding="utf-8")]
    if limit:
        # keep the companies with the most to show first: a confirmed website, then news, then filed accounts
        rows.sort(key=lambda r: ((r.get("evidence") or {}).get("website", {}).get("status") != "available",
                                 (r.get("evidence") or {}).get("activity", {}).get("status") != "available",
                                 r["name"]))
        rows = rows[:int(limit)]
    out = {"dataset": "Signalpost company profiles (sample run)", "generated_from": "Kildespor agent v4 run, 2026-11-02",
           "companies": [company(r) for r in rows]}
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(len(out["companies"]), "companies")


if __name__ == "__main__":
    main(*sys.argv[1:4])
