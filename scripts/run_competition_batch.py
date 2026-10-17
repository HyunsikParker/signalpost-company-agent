#!/usr/bin/env python3
"""Signalpost batch: one terminal envelope per input organisation number, whatever happens to a source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import profile_complete_for_modules, terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.contract import build_contract  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.http import fetch_json  # noqa: E402
from norway_company_agent.nav_jobs import NavJobIndex, attach_jobs  # noqa: E402
from norway_company_agent.official import accounting_obligation_assessment, fetch_official_modules  # noqa: E402
from norway_company_agent.pipeline import EXTERNAL_MODULES, research_company  # noqa: E402
from norway_company_agent.refresh import change_note, diff_profile  # noqa: E402
from norway_company_agent.sampling import iter_bulk  # noqa: E402

AGENT_VERSION = "signalpost-agent-3.0.0"
DEFAULT_MODULES = "registry,accounting_obligation,registry_live,financials,roles,group,locations,website," + ",".join(EXTERNAL_MODULES)
BULK_SOURCE = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv"
LIVE_SOURCE = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(path)


def read_inputs(path: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Organisation numbers in input order. Invalid or repeated rows are reported, never fatal."""
    source = Path(path)
    text = source.read_text(encoding="utf-8")
    if source.suffix == ".json":
        body = json.loads(text)
        values = body if isinstance(body, list) else body.get("organisation_numbers") or body.get("organisations") or []
    elif source.suffix == ".jsonl":
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        values = [line.strip() for line in text.splitlines() if line.strip()]
    records: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for value in values:
        raw = value.get("organisation_number") if isinstance(value, dict) else value
        org = "".join(character for character in str(raw or "") if character.isdigit())
        if len(org) != 9:
            warnings.append(f"invalid organisation number in input: {str(raw)[:40]!r}")
            org = org or str(raw or "")[:40]
        if org in seen:
            warnings.append(f"duplicate input row for {org}; one envelope is emitted")
            continue
        seen.add(org)
        record = {"organisation_number": org, "valid": len(org) == 9 and org.isdigit()}
        if isinstance(value, dict):
            for key in ("evaluation_split", "sample_slice"):
                if value.get(key) is not None:
                    record[key] = value[key]
        records.append(record)
    return records, warnings


def flatten(value: Any, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            if key != "_links":
                flat.update(flatten(child, f"{prefix}{key}."))
    elif isinstance(value, list):
        flat[prefix[:-1]] = ", ".join(str(item) for item in value)
    elif isinstance(value, bool):
        flat[prefix[:-1]] = "true" if value else "false"
    elif value is not None:
        flat[prefix[:-1]] = str(value)
    return flat


def base_profile(org: str, row: dict[str, Any], registry_record: dict[str, Any]) -> dict[str, Any]:
    employees = row.get("antallAnsatte")
    profile = {
        "organisation_number": org,
        "name": row.get("navn") or None,
        "legal_form": row.get("organisasjonsform.kode") or None,
        "employees": int(employees) if str(employees or "").isdigit() else None,
        "bankrupt": str(row.get("konkurs") or "").lower() == "true",
        "liquidating": str(row.get("underAvvikling") or "").lower() == "true",
        "municipality": row.get("forretningsadresse.kommune") or None,
        "municipality_number": row.get("forretningsadresse.kommunenummer") or None,
        "industry_code": row.get("naeringskode1.kode") or None,
        "industry_label": row.get("naeringskode1.beskrivelse") or None,
        "website": row.get("hjemmeside") or "",
        "latest_submitted_accounts": row.get("sisteInnsendteAarsregnskap") or None,
    }
    profile["evidence"] = {"registry": registry_record, "accounting_obligation": accounting_obligation_assessment(profile)}
    return profile


def load_registry(bulk: str, orgs: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Profiles from the frozen snapshot; anything the snapshot lacks is read from the live registry."""
    wanted = set(orgs)
    found: dict[str, dict[str, Any]] = {}
    metadata: dict[str, Any] = {"requested": len(orgs)}
    try:
        snapshot_sha256 = hashlib.sha256(Path(bulk).read_bytes()).hexdigest()
        retrieved_at = utc_now()
        scanned = 0
        for record in iter_bulk(bulk):
            scanned += 1
            org = record["organisation_number"]
            if org not in wanted or org in found:
                continue
            raw = record.get("raw", {})
            registry = evidence("registry", "available", "official_registry_bulk", BULK_SOURCE, value=raw, retrieved_at=retrieved_at,
                                content_sha256=snapshot_sha256, source_row_key=org)
            found[org] = base_profile(org, raw, registry)
            if len(found) == len(wanted):
                break
        metadata.update({"registry_snapshot_sha256": snapshot_sha256, "registry_rows_scanned": scanned})
    except Exception as exc:  # a damaged snapshot falls back to the live registry for every company
        metadata["snapshot_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    missing = [org for org in orgs if org not in found]
    metadata["selected_from_snapshot"] = len(found)
    metadata["live_registry_fallback"] = missing
    for org in missing:
        result = fetch_json(LIVE_SOURCE.format(org=org)) if len(org) == 9 and org.isdigit() else None
        if result is not None and result.status == 200 and isinstance(result.body, dict):
            raw = flatten(result.body)
            registry = evidence("registry", "available", "official_registry_live", result.url, value=raw, retrieved_at=result.retrieved_at,
                                content_sha256=result.content_sha256, source_row_key=org, note="Absent from the supplied snapshot; read from the live registry")
        else:
            raw = {}
            status = "not_found" if result is not None and result.status in {404, 410} else "source_error"
            registry = evidence("registry", status, "official_registry_live", LIVE_SOURCE.format(org=org),
                                note="Organisation number absent from the snapshot and the live registry lookup failed" if status == "source_error" else "Organisation number not found in the registry")
        found[org] = base_profile(org, raw, registry)
    return found, metadata


def previous_profiles_path(previous: str | None, profiles_output: Path, resume: bool) -> Path | None:
    """The earlier run to compare with: --previous, else a profiles file an earlier run left at --profiles-output."""
    if previous:
        return None if previous.casefold() == "none" else Path(previous)
    return profiles_output if profiles_output.exists() and not resume else None


def failed_record(module: str, message: str) -> dict[str, Any]:
    return evidence(module, "source_error", "pipeline", "about:blank", note=message[:300])


def main() -> None:
    parser = argparse.ArgumentParser(description="Signalpost company research batch (evaluator contract)")
    parser.add_argument("--organisations", required=True, help="JSON, JSONL, or text organisation-number list")
    parser.add_argument("--bulk", required=True, help="Frozen Brreg entity snapshot (.csv.gz)")
    parser.add_argument("--output", required=True, help="Terminal envelope JSONL")
    parser.add_argument("--profiles-output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-count", type=int, default=None, help="Defaults to the number of input rows")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--registry-workers", type=int, default=8, help="Concurrent requests to data.brreg.no")
    parser.add_argument("--checkpoint-every", type=int, default=25)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--previous", help="Profiles JSONL from an earlier run; material changes are reported per envelope. "
                        "Defaults to an existing --profiles-output left by an earlier run (not with --resume); 'none' turns refresh off")
    parser.add_argument("--time-budget-minutes", type=float, default=float(os.environ.get("SIGNALPOST_TIME_BUDGET_MINUTES", "50")))
    parser.add_argument("--company-budget-seconds", type=float, default=60.0)
    parser.add_argument("--no-discovery", action="store_true", help="Only crawl registry-listed websites")
    parser.add_argument("--nav-days", type=int, default=60, help="Look-back window for Nav's job feed; 0 disables the connector")
    parser.add_argument("--modules", default=DEFAULT_MODULES)
    args = parser.parse_args()

    clock_start = time.monotonic()
    budget = max(60.0, args.time_budget_minutes * 60)
    started_at = utc_now()
    inputs, input_warnings = read_inputs(args.organisations)
    orgs = [item["organisation_number"] for item in inputs]
    expected = args.expected_count if args.expected_count is not None else len(orgs)
    if expected != len(orgs):
        input_warnings.append(f"expected {expected} organisations, received {len(orgs)} unique rows; every received row gets an envelope")
    profiles, registry_metadata = load_registry(args.bulk, orgs)
    for item in inputs:
        for key in ("evaluation_split", "sample_slice"):
            if key in item:
                profiles[item["organisation_number"]][key] = item[key]
    requested_modules = [item.strip() for item in args.modules.split(",") if item.strip()]
    fetch_modules = set(requested_modules) - {"registry", "accounting_obligation", "website", *EXTERNAL_MODULES}
    external_requested = "website" in requested_modules
    registry_gate = threading.Semaphore(max(1, args.registry_workers))
    totals = {"requests": 0, "bytes": 0, "latencies_ms": []}
    totals_lock = threading.Lock()
    errors: dict[str, list[dict[str, Any]]] = {org: [] for org in orgs}

    def enrich(profile: dict[str, Any]) -> dict[str, Any]:
        org = profile["organisation_number"]
        company_started = time.monotonic()
        requests, size, latencies = 0, 0, []
        valid = len(org) == 9 and org.isdigit()
        try:
            if valid and fetch_modules:
                with registry_gate:
                    records, metrics = fetch_official_modules(org, fetch_modules)
                profile["evidence"].update(records)
                requests += len(metrics)
                size += sum(item.bytes_received for item in metrics)
                latencies += [item.elapsed_ms for item in metrics]
        except Exception as exc:
            errors[org].append({"stage": "official_modules", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        try:
            if valid and external_requested:
                elapsed = time.monotonic() - clock_start
                late = elapsed > budget * 0.7
                company_budget = max(5.0, min(args.company_budget_seconds, budget * 0.9 - elapsed))
                metrics = research_company(profile, budget_seconds=company_budget, discovery=not (late or args.no_discovery), signals=not late)
                requests += metrics["requests"]
                size += metrics["bytes"]
                latencies += metrics["latencies_ms"]
                if late:
                    errors[org].append({"stage": "external_research", "error": "run time budget 70% used: candidate discovery and signal collection skipped"})
        except Exception as exc:
            errors[org].append({"stage": "external_research", "error": f"{type(exc).__name__}: {str(exc)[:200]}", "trace": traceback.format_exc(limit=3)[-600:]})
        for module in requested_modules:
            if module not in profile["evidence"]:
                profile["evidence"][module] = failed_record(module, "; ".join(item["error"] for item in errors[org]) or "module not produced")
        profile["run_metrics"] = {"requests": requests, "bytes": size, "latencies_ms": latencies, "runtime_ms": int((time.monotonic() - company_started) * 1000)}
        with totals_lock:
            totals["requests"] += requests
            totals["bytes"] += size
            totals["latencies_ms"].extend(latencies)
        return profile

    state: dict[str, dict[str, Any]] = {}
    resumed = 0
    profiles_output = Path(args.profiles_output)
    if args.resume and profiles_output.exists():
        prior = [json.loads(line) for line in profiles_output.read_text(encoding="utf-8").splitlines() if line.strip()]
        state = {item["organisation_number"]: item for item in prior
                 if item.get("organisation_number") in profiles and profile_complete_for_modules(item, requested_modules)}
        resumed = len(state)
    # Read the earlier run before the first checkpoint overwrites the profiles file it may share.
    previous_path = previous_profiles_path(args.previous, profiles_output, args.resume)
    previous: dict[str, dict[str, Any]] = {}
    if previous_path and previous_path.exists():
        for line in previous_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("organisation_number") in profiles:
                    previous[row["organisation_number"]] = row
    nav_index = NavJobIndex(days=args.nav_days)
    nav_thread = None
    if args.nav_days > 0 and external_requested:
        nav_thread = threading.Thread(target=nav_index.build, args=(budget * 0.6,), daemon=True)
        nav_thread.start()
    pending = [profiles[org] for org in orgs if org not in state]
    pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
    futures = {pool.submit(enrich, profile): profile["organisation_number"] for profile in pending}
    remaining = set(futures)
    completed = 0
    timed_out: list[str] = []
    while remaining:
        left = budget - (time.monotonic() - clock_start)
        if left <= 0:
            break
        done, remaining = wait(remaining, timeout=min(left, 30), return_when=FIRST_COMPLETED)
        for future in done:
            org = futures[future]
            try:
                state[org] = future.result()
            except Exception as exc:
                errors[org].append({"stage": "worker", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                state[org] = profiles[org]
            completed += 1
            if completed % args.checkpoint_every == 0:
                write_jsonl(profiles_output, [state[item] for item in orgs if item in state])
    for future in remaining:
        org = futures[future]
        timed_out.append(org)
        errors[org].append({"stage": "run", "error": "run time budget exhausted before this company finished"})
        profile = profiles[org]
        for module in requested_modules:
            if module not in profile["evidence"] or profile["evidence"][module].get("status") == "not_fetched":
                profile["evidence"][module] = evidence(module, "source_error", "pipeline", "about:blank", note="budget_exhausted: run time budget ended first")
        state[org] = profile
    pool.shutdown(wait=False, cancel_futures=True)

    nav_requests = 0
    if nav_thread is not None:
        nav_index.ready.wait(timeout=max(0.0, min(budget * 0.75, budget - 60 - (time.monotonic() - clock_start))))
        if nav_index.ready.is_set() and nav_index.status in {"ready", "partial"}:
            nav_pool = ThreadPoolExecutor(max_workers=4)
            nav_futures = {nav_pool.submit(attach_jobs, state[org], nav_index): org for org in orgs if org in state and len(org) == 9 and org not in timed_out}
            nav_left = max(5.0, budget - 30 - (time.monotonic() - clock_start))
            done, not_done = wait(nav_futures, timeout=nav_left)
            for future in done:
                try:
                    nav_requests += future.result()
                except Exception as exc:
                    errors[nav_futures[future]].append({"stage": "nav_jobs", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            for future in not_done:
                errors[nav_futures[future]].append({"stage": "nav_jobs", "error": "run time budget ended before Nav ads were verified"})
            nav_pool.shutdown(wait=False, cancel_futures=True)
        else:
            for org in orgs:
                record = state.get(org, {}).get("evidence", {}).get("jobs")
                if record is not None:
                    record.setdefault("value", {})["nav_feed"] = {"status": nav_index.status if nav_index.ready.is_set() else "not_ready", "errors": nav_index.errors[:3]}

    completed_at = utc_now()
    envelopes = []
    def assemble(org: str) -> dict[str, Any]:
        profile = state[org]
        envelope = terminal_envelope(profile, run_id=args.run_id, modules=requested_modules, started_at=started_at, completed_at=completed_at)
        if org in timed_out:
            for module, item in envelope["modules"].items():
                if item["state"] == "source_error" and "budget_exhausted" in str(profile["evidence"].get(module, {}).get("note")):
                    item["state"] = "budget_exhausted"
        changes = []
        compared = org in previous
        if compared:
            try:
                changes = diff_profile(previous[org], profile)
            except Exception as exc:
                compared = False
                errors[org].append({"stage": "refresh_diff", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
        try:
            contract = build_contract(profile, change_note=change_note(changes, compared))
        except Exception as exc:  # a malformed source record must not cost the batch its other envelopes
            errors[org].append({"stage": "contract", "error": f"{type(exc).__name__}: {str(exc)[:200]}", "trace": traceback.format_exc(limit=3)[-600:]})
            contract = {"claims": [], "evidence": [], "summary": None}
        metrics = profile.get("run_metrics") or {}
        failed = any(item["state"] in {"source_error", "submission_error", "budget_exhausted"} for item in envelope["modules"].values())
        envelope.update({
            "agent_version": AGENT_VERSION,
            "run": {"run_id": args.run_id, "started_at": started_at, "completed_at": completed_at,
                    "terminal_status": "completed_with_errors" if failed or errors[org] else "completed"},
            "claims": contract["claims"],
            "evidence": contract["evidence"],
            "summary": contract["summary"],
            "changes": changes,
            "errors": errors[org],
            "operations": {"requests": metrics.get("requests", 0), "runtime_ms": metrics.get("runtime_ms"), "third_party_cost_usd": 0},
        })
        return envelope

    def minimal_envelope(org: str) -> dict[str, Any]:
        return {
            "run_id": args.run_id, "organisation_number": org, "state": "submission_error", "started_at": started_at, "completed_at": completed_at,
            "modules": {module: {"state": "submission_error", "retry_count": 0, "final_timestamp": completed_at} for module in requested_modules},
            "profile": {"organisation_number": org}, "agent_version": AGENT_VERSION,
            "run": {"run_id": args.run_id, "started_at": started_at, "completed_at": completed_at, "terminal_status": "failed"},
            "claims": [], "evidence": [], "summary": None, "changes": [], "errors": errors[org],
            "operations": {"requests": 0, "runtime_ms": None, "third_party_cost_usd": 0},
        }

    for org in orgs:
        try:
            envelopes.append(assemble(org))
        except Exception as exc:
            errors[org].append({"stage": "envelope", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            envelopes.append(minimal_envelope(org))
    validation = validate_envelopes(envelopes, len(orgs))
    write_jsonl(profiles_output, [state[org] for org in orgs])
    write_jsonl(Path(args.output), envelopes)

    latencies = sorted(totals.pop("latencies_ms"))
    totals["p50_ms"] = latencies[len(latencies) // 2] if latencies else None
    totals["p95_ms"] = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else None
    families: dict[str, dict[str, int]] = {}
    for envelope in envelopes:
        seen: set[str] = set()
        for claim in envelope["claims"]:
            bucket = families.setdefault(claim["family"], {"companies_available": 0, "claims_available": 0})
            if claim["availability"] == "available":
                bucket["claims_available"] += 1
                if claim["family"] not in seen:
                    seen.add(claim["family"])
                    bucket["companies_available"] += 1
    runtimes = sorted((state[org].get("run_metrics") or {}).get("runtime_ms") or 0 for org in orgs)
    report = {
        "run_id": args.run_id,
        "agent_version": AGENT_VERSION,
        "started_at": started_at,
        "completed_at": completed_at,
        "wall_seconds": round(time.monotonic() - clock_start, 1),
        "expected_count": expected,
        "input_rows": len(orgs),
        "emitted_envelopes": len(envelopes),
        "input_warnings": input_warnings,
        "resumed_profiles": resumed,
        "profiles_fetched_this_run": len(pending),
        "timed_out_companies": timed_out,
        "modules": requested_modules,
        "registry": registry_metadata,
        "operations": {**totals, "third_party_cost_usd": 0,
                       "company_runtime_p50_ms": runtimes[len(runtimes) // 2] if runtimes else None,
                       "company_runtime_p95_ms": runtimes[min(len(runtimes) - 1, int(len(runtimes) * 0.95))] if runtimes else None},
        "nav_job_feed": {"status": nav_index.status, "window_days": args.nav_days, "window_start": nav_index.window_start, "pages": nav_index.pages,
                         "ads_indexed": nav_index.items, "feed_requests": nav_index.requests, "verification_requests": nav_requests,
                         "finished_at": nav_index.finished_at, "errors": nav_index.errors[:5]},
        "refresh": {"previous_profiles": str(previous_path) if previous else None, "companies_compared": len(previous),
                    "companies_with_changes": sum(1 for envelope in envelopes if envelope.get("changes"))},
        "coverage_by_family": dict(sorted(families.items())),
        "companies_with_errors": sum(1 for org in orgs if errors[org]),
        "validation": validation,
    }
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("run_id", "wall_seconds", "emitted_envelopes", "coverage_by_family", "companies_with_errors", "validation")}, ensure_ascii=False, indent=2))
    sys.stdout.flush()
    os._exit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
