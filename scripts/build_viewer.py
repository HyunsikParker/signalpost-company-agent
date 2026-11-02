#!/usr/bin/env python3
"""Build a portable company-evidence viewer from JSONL or JSONL.gz."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from norway_company_agent.viewer import read_envelopes, write_viewer


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--label", default="Batch output")
    args = parser.parse_args()
    rows = read_envelopes(args.input)
    write_viewer(rows, args.output, label=args.label)
    print(f"Wrote {len(rows)} companies to {args.output}")
