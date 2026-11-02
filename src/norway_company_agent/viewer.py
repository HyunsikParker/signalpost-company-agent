"""Self-contained, read-only viewer for batch envelopes. No server or remote assets."""
from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
from typing import Any


def read_envelopes(path: Path) -> list[dict[str, Any]]:
    raw = gzip.decompress(path.read_bytes()) if path.suffix == ".gz" else path.read_bytes()
    text = raw.decode("utf-8-sig")
    data = json.loads(text) if text.lstrip().startswith("[") else [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(data, list) or any(not isinstance(item, dict) or not item.get("organisation_number") for item in data):
        raise ValueError("Expected envelopes with an organisation_number")
    return data


def write_viewer(envelopes: list[dict[str, Any]], output: Path, *, label: str = "Batch output") -> None:
    # Base64 keeps imported content out of the HTML parser, including </script> strings.
    payload = base64.b64encode(json.dumps({"label": label, "envelopes": envelopes}, ensure_ascii=False).encode()).decode("ascii")
    template = (Path(__file__).parent / "viewer" / "index.html").read_text(encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(template.replace("__ENVELOPES_BASE64__", payload), encoding="utf-8")
