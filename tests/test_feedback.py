"""Offline host-feedback regressions. These are software tests, not a prize score."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from norway_company_agent.contract import build_contract
from norway_company_agent.site_crawl import Deadline, _page, collect_signals
from norway_company_agent.site_research import RequestLog, credible_activity, meaningful_description
from norway_company_agent.viewer import read_envelopes, write_viewer


def fixture_profile(org="810034882"):
    source = {"status": "available", "source_url": "https://fixture.example/", "retrieved_at": "2026-10-01T08:30:00Z", "content_sha256": "a" * 64}
    return {"organisation_number": org, "name": "Fixture AS", "evidence": {
        "registry": {**source, "value": {"navn": "Fixture AS", "stiftelsesdato": "2000-01-01"}},
        "website": {**source, "value": {"final_url": "https://fixture.example/", "description": "—", "identity_assessment": {"publishable": True}}},
        "activity": {**source, "value": {"items": [{"title": "customer.no", "url": "https://fixture.example/portfolio/customer", "date": "2026-10-01"}]}},
    }}


class FeedbackTest(unittest.TestCase):
    def test_placeholder_omitted_from_both_claims_and_summary_without_mutating_sources(self):
        profile = fixture_profile()
        original = json.dumps(profile)
        contract = build_contract(profile)
        for field in ("company_description", "dated_activity"):
            claim = next(c for c in contract["claims"] if c["field"] == field)
            self.assertIsNone(claim["value"])
            self.assertEqual(claim["availability"], "not_available")
            self.assertTrue(claim["note"])
        self.assertNotIn("customer.no", contract["summary"]["text"])
        self.assertNotIn("“—”", contract["summary"]["text"])
        self.assertEqual(json.dumps(profile), original)

    def test_real_description_and_news_are_preserved(self):
        self.assertEqual(meaningful_description("  Vi bygger nettsider.  "), "Vi bygger nettsider.")
        for text in ["-", "—", "...", "N/A", "Lorem ipsum dolor sit amet"]:
            self.assertIsNone(meaningful_description(text))
        good = {"title": "Vi lanserer kunde.no", "date": "2026-09-12", "url": "https://fixture.example/news/launch"}
        self.assertEqual(credible_activity([good, {**good, "title": "https://customer.no/"}], "2000-01-01"), ([good], 1))

    def test_unchecked_sources_stay_unknown_in_the_summary(self):
        profile = fixture_profile()
        profile["evidence"]["activity"] = {"status": "source_error", "note": "time budget ended"}
        profile["evidence"]["jobs"] = {"status": "source_error", "note": "time budget ended"}
        text = build_contract(profile)["summary"]["text"]
        self.assertIn("hiring could not be assessed", text)
        self.assertIn("activity could not be assessed", text)
        self.assertNotIn("no dated news", text)

    def test_activity_and_social_claims_use_their_actual_page_hash_and_time(self):
        profile = fixture_profile()
        item = {"title": "New service", "url": "https://fixture.example/news/service", "date": "2026-10-01",
                "source_url": "https://fixture.example/feed", "retrieved_at": "2026-10-02T00:00:00Z", "content_sha256": "f" * 64}
        profile["evidence"]["activity"]["value"]["items"] = [item]
        profile["evidence"]["company_profiles"] = {"status": "available", "value": {"profiles": [{"platform": "facebook", "url": "https://facebook.com/fixture", "source_url": "https://fixture.example/contact", "content_sha256": "c" * 64, "retrieved_at": item["retrieved_at"]}]}}
        contract = build_contract(profile)
        sources = {e["id"]: e for e in contract["evidence"]}
        for field, digest, url in [("dated_activity", "f" * 64, item["source_url"]), ("social_profile", "c" * 64, "https://fixture.example/contact")]:
            claim = next(c for c in contract["claims"] if c["field"] == field)
            ev = sources[claim["evidence_ids"][0]]
            self.assertEqual(ev["source_url"], url)
            self.assertEqual(ev["content_sha256"], digest)
            self.assertEqual(ev["retrieved_at"], item["retrieved_at"])

    def test_feed_collection_binds_content_hash(self):
        raw = b'<rss><channel><item><title>New service</title><link>https://fixture.example/news/service</link><pubDate>Thu, 01 Oct 2026 08:00:00 GMT</pubDate></item></channel></rss>'
        home = _page("https://fixture.example/", b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed"></head><body>Fixture</body></html>', org="810034882")
        record = {"value": {"registered_domain": "fixture.example", "final_url": home["url"]}}
        with patch("norway_company_agent.site_crawl.fetch_text", return_value=(raw, "https://fixture.example/feed", "application/rss+xml", "")):
            found = collect_signals(record, [home], deadline=Deadline(10), log=RequestLog())
        self.assertEqual(found["activity"][0]["content_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(found["activity"][0]["retrieved_at"])

    def test_viewer_does_not_interpolate_untrusted_text_into_html(self):
        attack = '</script><img src=x onerror="alert(1)">'
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "viewer.html"
            write_viewer([{"organisation_number": "810034882", "claims": [], "summary": {"text": attack}}], target, label=attack)
            text = target.read_text()
            self.assertNotIn(attack, text)
            self.assertIn("connect-src 'none'", text)
            self.assertNotIn("__ENVELOPES_BASE64__", text)
            self.assertNotIn("innerHTML", text)

    def test_300_company_batch_completes_with_failed_sources_offline(self):
        spec = importlib.util.spec_from_file_location("feedback_batch", ROOT / "scripts/run_competition_batch.py")
        batch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(batch)
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder)
            orgs = [str(810000000 + n) for n in range(300)]
            (p / "input.txt").write_text("\n".join(orgs))
            def fake_registry(*args):
                return {org: fixture_profile(org) for org in orgs}, {"fixture": True}
            def fake_official(*args):
                raise RuntimeError("offline fixture: unavailable official source")
            def fake_research(profile, **kwargs):
                return {"requests": 0, "bytes": 0, "latencies_ms": []}
            argv = ["batch", "--organisations", str(p / "input.txt"), "--bulk", str(p / "unused.gz"), "--output", str(p / "envelopes.jsonl"), "--profiles-output", str(p / "profiles.jsonl"), "--report", str(p / "report.json"), "--viewer-output", str(p / "viewer.html"), "--run-id", "offline-fixture-300", "--nav-days", "0", "--modules", "registry,roles,website"]
            with patch.object(sys, "argv", argv), patch.object(batch, "load_registry", fake_registry), patch.object(batch, "fetch_official_modules", fake_official), patch.object(batch, "research_company", fake_research), patch.object(batch.os, "_exit", side_effect=SystemExit), patch.object(batch, "fetch_json", side_effect=AssertionError("network prohibited")):
                with self.assertRaises(SystemExit):
                    batch.main()
            output = read_envelopes(p / "envelopes.jsonl")
            self.assertEqual([row["organisation_number"] for row in output], orgs)
            self.assertTrue(all(row["errors"] and row["run"]["terminal_status"] == "completed_with_errors" for row in output))
            self.assertTrue(json.loads((p / "report.json").read_text())["validation"]["passed"])
            self.assertTrue((p / "viewer.html").exists())

    def test_viewer_is_written_by_default_and_can_be_turned_off(self):
        spec = importlib.util.spec_from_file_location("feedback_batch_viewer", ROOT / "scripts/run_competition_batch.py")
        batch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(batch)
        orgs = [str(820000000 + n) for n in range(5)]
        for flag, expected in ((None, True), ("none", False)):
            with tempfile.TemporaryDirectory() as folder:
                p = Path(folder)
                (p / "input.txt").write_text("\n".join(orgs))
                argv = ["batch", "--organisations", str(p / "input.txt"), "--bulk", str(p / "unused.gz"), "--output", str(p / "envelopes.jsonl"),
                        "--profiles-output", str(p / "profiles.jsonl"), "--report", str(p / "report.json"), "--run-id", "viewer-default",
                        "--nav-days", "0", "--modules", "registry,roles,website"] + (["--viewer-output", flag] if flag else [])
                with patch.object(sys, "argv", argv), \
                        patch.object(batch, "load_registry", lambda *a: ({org: fixture_profile(org) for org in orgs}, {"fixture": True})), \
                        patch.object(batch, "fetch_official_modules", side_effect=RuntimeError("offline fixture")), \
                        patch.object(batch, "research_company", lambda profile, **kw: {"requests": 0, "bytes": 0, "latencies_ms": []}), \
                        patch.object(batch.os, "_exit", side_effect=SystemExit), \
                        patch.object(batch, "fetch_json", side_effect=AssertionError("network prohibited")):
                    with self.assertRaises(SystemExit):
                        batch.main()
                self.assertEqual((p / "viewer.html").exists(), expected)
                self.assertEqual(bool((json.loads((p / "report.json").read_text()).get("viewer") or {}).get("written")), expected)


if __name__ == "__main__":
    unittest.main()
