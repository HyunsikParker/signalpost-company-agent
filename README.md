# Signalpost company research agent

Give the agent a batch of Norwegian organisation numbers. For each company it returns one terminal envelope: the official registry record, filed accounts, leadership, registered workplaces and group links, plus what the company publishes about itself. The external half covers a verified official website, company-owned social profiles, role contact addresses, current job ads and dated activity. Every claim carries its source URL, retrieval time, content hash and the text span that supports it. A source that was checked and had nothing is reported as `not_available`, never as zero.

The agent is built on the Builderr Signalpost starter kit. The starter's registry, accounts, roles, group and sub-unit modules are unchanged; the website identity gate, site discovery, signal extraction, job-ad connector, claim contract and batch runner are new.

## Run it

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked
uv run python scripts/run_competition_batch.py \
  --organisations batch.jsonl \
  --bulk brreg-enheter.csv.gz \
  --output out/envelopes.jsonl \
  --profiles-output out/profiles.jsonl \
  --report out/report.json \
  --run-id daily-001
```

- `--organisations` accepts JSONL (`{"organisation_number": "…"}` per line), a JSON list, or plain text with one number per line.
- `--bulk` is the Brønnøysund entity snapshot (`enheter/lastned/csv`). A company missing from the snapshot is read from the live registry instead of stopping the run.
- `--expected-count` is optional. Every input row gets exactly one envelope; invalid or repeated rows are reported in `report.json`.
- `--previous out/profiles.jsonl` compares this run with an earlier one and writes typed changes into each envelope. Without the flag, a profiles file that an earlier run left at `--profiles-output` is used as the previous run (never with `--resume`); `--previous none` turns the comparison off.
- `--time-budget-minutes` (default 50, or `SIGNALPOST_TIME_BUDGET_MINUTES`) bounds the whole run. After 70% of it, new companies skip candidate discovery; at the limit, unfinished companies are closed as `budget_exhausted` and the file is still complete.
- `--workers` (default 16) sets company concurrency; `--registry-workers` (default 8) caps concurrent requests to data.brreg.no.

Tests: `uv run --with pytest pytest -q`.

## What each envelope contains

The envelope keeps the starter's fields (`run_id`, `organisation_number`, `state`, `modules`, `profile`) and adds the claim contract:

| Field | Content |
|---|---|
| `run` | run id, start and end time, terminal status |
| `claims` | `family`, `field`, `value`, `availability`, `confidence`, `evidence_ids`, and `reporting_period` or `effective_at` where one exists |
| `evidence` | `id`, `source_url`, `source_class`, `retrieved_at`, `content_sha256`, `claim_span` |
| `summary` | a short description built only from published claims: what the company is (form, place, industry, founding year, registry status, registered purpose or the site's own description, accounts, leaders, group), what changed since the previous run, and what could not be found or checked; with the evidence ids it rests on |
| `changes` | material differences from the previous run, with old and new evidence hashes |
| `errors` | per-stage failures; a failing source never removes the envelope |
| `operations` | requests, runtime and third-party cost (always 0) |

Availability is one of `available`, `not_available`, `blocked`, `not_applicable`, `ambiguous` or `failed`. A site that was reached but could not be tied to the exact entity is `ambiguous`.

Claim families: `legal_identity`, `annual_accounts`, `leadership`, `workplaces`, `group`, `official_website`, `public_brand`, `company_description`, `company_profiles`, `contact`, `hiring`, `public_activity`.

## How a website becomes "official"

Candidates are tried in this order: the website in the registry, the domain of the registry e-mail address (free-mail and shared-administrator domains are skipped), then domains built from the legal name (`sandneselektriske.no`, `sandnes-elektriske.no`, transliterated variants). Search engines are not used. A candidate is published only when one of these holds:

1. The company's organisation number (MOD11-valid) appears on the homepage or on a contact, about or privacy page that shows no other organisation number.
2. The legal name appears on the site together with a registry fact for the same company: its phone number, street address or registry e-mail.
3. The complete registered name, legal form included ("Finnmark Utleie AS"), appears together with the registered municipality, postal place or postcode. A place that is part of the company name does not count, and a domain guessed from the name must be a `.no` domain.
4. The site is the registry website or the registry e-mail domain, and the legal name matches the homepage title, description, host name or structured organisation name (the starter's rule).

A legal-name domain guess never publishes on name similarity alone. Parked domains, for-sale pages and franchise or group pages listing several companies' numbers do not pass. Social profiles are published only from a site that passed, and only when the handle names the company: its legal name, the verified domain name or the brand the site declares.

## Sources and rights

| Source | Use | Basis |
|---|---|---|
| Brønnøysundregistrene: Enhetsregisteret, roles, sub-units, group structure, Regnskapsregisteret | identity, leadership, workplaces, group, filed accounts | open data under NLOD 2.0 |
| Company websites (registry-listed or proven by the rules above) | official site, brand, description, profile links, role mailboxes, careers pages, feeds and sitemaps | the company's own public pages; robots.txt is honoured per host, and every URL must resolve to a public address |
| arbeidsplassen.nav.no (Nav's public job database) | current job ads tied to the company's organisation number or one of its sub-units | public pages and the site's JSON search; robots.txt allows all paths; Nav's job-ad terms let anyone republish ads. Only title, dates, location, ad URL and employer number are kept; contact persons are not read |

No LinkedIn, Meta, Glassdoor, Indeed or search-engine data is collected. No API key or paid service is used, so the expected third-party cost of an official batch is $0. Outbound requests identify themselves as `builderr-signalpost-poc/0.1 (+https://builderr.ai)`.

## Limitations

- A company with no registry website, no company-domain e-mail and a site that does not show its organisation number, phone or address stays without a verified website.
- Job ads are found when Nav lists the employer under the legal name or a registered sub-unit name. Ads that only exist on other boards, or are posted under a brand name, are missed.
- Activity comes from the company's own RSS/Atom feeds, schema.org article data, news listing pages and article pages, and news sitemaps. Sitemap items are dated by `lastmod` and labelled `last_modified`, and their titles come from the URL slug. Placeholder posts ("Hello world!") and items dated before the company was founded (theme demo content) are dropped.
- JavaScript-only sites are fetched as static HTML and may yield little.
- Personal e-mail addresses on company sites are not published, only shared role mailboxes such as `post@` or `info@`.

## Smoke test

The 100-company smoke test is in [`smoke/`](smoke/): the run report, the envelopes and a short summary. It used the starter's `select_entry_batch.py` with its default seed.
