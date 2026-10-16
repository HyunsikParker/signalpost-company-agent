#!/usr/bin/env python3
"""Build a small Brreg-format bulk snapshot for a local test batch.

The official run receives Builderr's frozen registry snapshot. Locally, the full
`enheter/lastned/csv` download can be slow, so this helper fetches only the test
batch from the same open Enhetsregisteret API and writes it with the bulk CSV's
column names. It is a development aid; the batch runner never calls it.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import read_organisation_numbers  # noqa: E402
from norway_company_agent.http import fetch_json  # noqa: E402

COLUMNS = [
    "organisasjonsnummer", "navn", "organisasjonsform.kode", "organisasjonsform.beskrivelse",
    "naeringskode1.kode", "naeringskode1.beskrivelse", "naeringskode2.kode", "naeringskode2.beskrivelse",
    "naeringskode3.kode", "naeringskode3.beskrivelse", "hjelpeenhetskode.kode", "hjelpeenhetskode.beskrivelse",
    "harRegistrertAntallAnsatte", "antallAnsatte", "registreringsdatoAntallAnsatteEnhetsregisteret",
    "registreringsdatoantallansatteNAVAaregisteret", "hjemmeside", "epostadresse", "telefon", "mobil",
    "postadresse.adresse", "postadresse.poststed", "postadresse.postnummer", "postadresse.kommune",
    "postadresse.kommunenummer", "postadresse.land", "postadresse.landkode", "forretningsadresse.adresse",
    "forretningsadresse.poststed", "forretningsadresse.postnummer", "forretningsadresse.kommune",
    "forretningsadresse.kommunenummer", "forretningsadresse.land", "forretningsadresse.landkode",
    "institusjonellSektorkode.kode", "institusjonellSektorkode.beskrivelse", "sisteInnsendteAarsregnskap",
    "registreringsdatoenhetsregisteret", "stiftelsesdato", "registrertIMvaRegisteret", "konkurs",
    "underAvvikling", "overordnetEnhet", "maalform", "vedtektsfestetFormaal", "aktivitet", "erIKonsern",
]
RENAMED = {
    "registreringsdatoantallansatteNAVAaregisteret": "registreringsdatoAntallAnsatteNAVAaregisteret",
    "registreringsdatoenhetsregisteret": "registreringsdatoEnhetsregisteret",
    "registrertIMvaRegisteret": "registrertIMvaregisteret",
}


def flatten(value: Any, prefix: str = "") -> dict[str, str]:
    """Flatten the API JSON into dotted keys, joining lists the way the bulk export does."""
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "_links":
                continue
            flat.update(flatten(child, f"{prefix}{key}."))
    elif isinstance(value, list):
        flat[prefix[:-1]] = ", ".join(str(item) for item in value)
    elif isinstance(value, bool):
        flat[prefix[:-1]] = "true" if value else "false"
    elif value is not None:
        flat[prefix[:-1]] = str(value)
    return flat


def row_for(org: str) -> dict[str, str]:
    result = fetch_json(f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}")
    if result.status != 200 or not isinstance(result.body, dict):
        raise SystemExit(f"Registry lookup failed for {org}: {result.error or result.status}")
    flat = flatten(result.body)
    return {column: flat.get(RENAMED.get(column, column), "") for column in COLUMNS}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--organisations", required=True)
    parser.add_argument("--output", required=True, help="Output .csv.gz path")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    orgs = read_organisation_numbers(args.organisations)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(row_for, orgs))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} registry rows to {output}")


if __name__ == "__main__":
    main()
