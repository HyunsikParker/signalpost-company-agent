# 100-company smoke test

Run `smoke-2026-10-16-v2`, 16 October 2026, 19:15–19:19 UTC, on the version 2 code.

- Input: [`companies.jsonl`](companies.jsonl), 100 companies drawn from the public universe by `select_entry_batch.py` with its default seed.
- Output: [`envelopes.jsonl.gz`](envelopes.jsonl.gz), one terminal envelope per company, and the run report [`report.json`](report.json).
- Result: 100 of 100 envelopes, every module state terminal, no company with an error, validation passed.
- Cost: 753 company-level requests plus 126 Nav job-feed requests; no paid API, so $0.
- Time: 261 s wall clock with 16 workers. That includes the Nav job-feed index (60 days, 125 pages), which is built in parallel with the company work and takes about the same time whatever the batch size. Per company: p50 10.4 s, p95 36.8 s.

The registry snapshot for this run was built with `scripts/build_local_bulk.py`, which reads the same open Enhetsregisteret API row by row, because the full bulk download kept dropping on this connection. Official runs use the snapshot Builderr supplies.

| Family | Companies with a claim | Claims |
|---|---:|---:|
| legal_identity | 100 | 709 |
| annual_accounts | 100 | 276 |
| leadership | 100 | 384 |
| workplaces | 73 | 76 |
| group | 8 | 8 |
| official_website | 7 | 7 |
| company_description | 5 | 5 |
| public_brand | 4 | 5 |
| contact | 3 | 4 |
| company_profiles | 2 | 5 |
| public_activity | 3 | 19 |
| hiring | 0 | 0 |

Only 7 of these 100 companies list a website in the registry; three of those pass the identity gate (the other four redirect to a different business, a franchise chain or an expired domain, or do not resolve). Four more sites were found from the legal name and proven by the organisation number or by the legal name plus the registry phone number on the site. The starter kit published two websites and no profiles for the same batch.

A 300-company development batch (different seed, run `dev-2026-10-16-v2`) gave 36 verified websites (15 registry-listed, 3 from the registry e-mail domain, 18 from the legal name), owned profiles for 25 companies, dated activity for 10, careers pages for 5 and current Nav job ads, verified by employer number, for 2. No envelope had an error. Every published website, profile and job ad in both batches was checked by hand against the page that supports it, and the dated items were reviewed by title and source. Version 1 published 32 websites and 23 profiles on the same batch.
