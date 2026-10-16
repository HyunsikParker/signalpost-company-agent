"""Regression tests for site discovery, identity gates, signal parsers, the Nav jobs matcher and the contract."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.contract import build_contract  # noqa: E402
from norway_company_agent.identity import assess_owned_profile, assess_site_identity, gate_company_site  # noqa: E402
from norway_company_agent.nav_jobs import NavJobIndex, name_key  # noqa: E402
from norway_company_agent.site_research import (  # noqa: E402
    candidate_sites,
    corroborations,
    email_domain,
    legal_name_pattern,
    name_domain_guesses,
    name_span,
    organisation_numbers_in,
    parse_feed,
    parse_sitemap,
    slug_title,
    valid_organisation_number,
)

NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def site(value: dict, status: str = "available") -> dict:
    base = {"final_url": "https://example.no/", "registered_domain": "example.no", "title": "", "description": "", "main_text_excerpt": "",
            "pages": [], "structured_organisations": [], "social_links": [], "registry_corroborations": [], "identity_proof": None}
    base.update(value)
    return {"field": "website", "status": status, "source_url": base["final_url"], "retrieved_at": "2026-10-16T00:00:00Z",
            "content_sha256": "a" * 64, "source_class": "discovered_company_website", "value": base}


def profile(name: str = "FJORD ELEKTRO AS", org: str = "810034882") -> dict:
    return {"organisation_number": org, "name": name, "evidence": {}}


class OrganisationNumberTest(unittest.TestCase):
    def test_mod11_check_digit(self):
        self.assertTrue(valid_organisation_number("938702675"))
        self.assertTrue(valid_organisation_number("810034882"))
        self.assertFalse(valid_organisation_number("938702676"))
        self.assertFalse(valid_organisation_number("12345678"))

    def test_numbers_found_with_common_separators(self):
        text = "Fjord Elektro AS · Org.nr. 810 034 882 MVA · tlf 51 68 57 00 · NO938702675MVA"
        self.assertEqual(organisation_numbers_in(text), {"810034882", "938702675"})

    def test_phone_numbers_are_not_organisation_numbers(self):
        self.assertEqual(organisation_numbers_in("Ring 51 68 57 00 eller 900 49 299"), set())


class CandidateTest(unittest.TestCase):
    def test_free_mail_and_shared_administrators_are_not_candidates(self):
        self.assertIsNone(email_domain("ola@gmail.com"))
        self.assertIsNone(email_domain("styret@styrerommet.no"))
        self.assertEqual(email_domain("Post@Mail.SandnesElektriske.no"), "sandneselektriske.no")

    def test_name_guesses_drop_legal_form_and_transliterate(self):
        guesses = name_domain_guesses("BÆRUM BÅT & MOTOR AS")
        self.assertEqual(guesses[0], "baerumbatmotor.no")
        self.assertIn("baerum-bat-og-motor.no", guesses)
        self.assertIn("baerumbaatmotor.no", guesses)

    def test_registry_e_mail_domain_comes_first_and_registry_site_is_skipped(self):
        row = {"epostadresse": "post@fjordelektro.no"}
        found = candidate_sites({"name": "FJORD ELEKTRO AS", "website": "www.fjord-el.no"}, row)
        self.assertEqual(found[0], {"domain": "fjordelektro.no", "origin": "registry_email_domain"})
        self.assertNotIn("fjord-el.no", [item["domain"] for item in found])

    def test_corroborations_use_registry_phone_address_and_e_mail(self):
        row = {"telefon": "51 68 57 00", "forretningsadresse.adresse": "Strandgata 21", "forretningsadresse.postnummer": "4307", "epostadresse": "post@x.no"}
        found = corroborations("Strandgata 21, 4307 Sandnes · tlf +47 51685700", row)
        self.assertEqual(len(found), 2)

    def test_legal_name_pattern_needs_words_in_order(self):
        pattern = legal_name_pattern("SANDNES ELEKTRISKE AS")
        self.assertTrue(name_span("Velkommen til Sandnes Elektriske AS i Strandgata", pattern))
        self.assertIsNone(name_span("Elektriske tjenester i Sandnes", pattern))


class IdentityGateTest(unittest.TestCase):
    def test_organisation_number_alone_on_a_page_is_exact(self):
        record = site({"identity_proof": {"page_url": "https://example.no/kontakt", "other_organisation_numbers_on_page": 0, "claim_span": "Org.nr 810 034 882"},
                       "discovery_origin": "legal_name_domain_guess"})
        self.assertTrue(assess_site_identity(profile(), record)["publishable"])

    def test_number_among_many_entities_is_not_exact_without_the_name(self):
        record = site({"identity_proof": {"page_url": "https://group.no/selskaper", "other_organisation_numbers_on_page": 2, "claim_span": "..."},
                       "discovery_origin": "registry_email_domain", "title": "Konsern AS"})
        self.assertFalse(assess_site_identity(profile(), record)["publishable"])

    def test_guessed_domain_with_matching_name_needs_a_registry_fact(self):
        record = site({"title": "Fjord Elektro", "discovery_origin": "legal_name_domain_guess", "main_text_excerpt": "x" * 200})
        self.assertFalse(assess_site_identity(profile(), record)["publishable"])
        record["value"]["registry_corroborations"] = ["registry telefon 51 68 57 00 appears on the page"]
        self.assertTrue(assess_site_identity(profile(), record)["publishable"])

    def test_full_registered_name_and_place_prove_a_no_domain(self):
        record = site({"discovery_origin": "legal_name_domain_guess", "registered_domain": "fjordelektro.no",
                       "full_legal_name_span": "Fjord Elektro AS, Lakselv", "registry_place_matches": ["LAKSELV"]})
        self.assertTrue(assess_site_identity(profile(), record)["publishable"])
        record["value"]["registered_domain"] = "fjordelektro.com"
        self.assertFalse(assess_site_identity(profile(), record)["publishable"])
        record["value"].update(registered_domain="fjordelektro.no", registry_place_matches=[])
        self.assertFalse(assess_site_identity(profile(), record)["publishable"])

    def test_place_inside_the_name_does_not_count(self):
        from norway_company_agent.site_research import place_corroborations
        row = {"forretningsadresse.kommune": "EIDSVOLL", "forretningsadresse.poststed": "RÅHOLT"}
        self.assertEqual(place_corroborations("Milano Eidsvoll, Eidsvoll sentrum", row, "MILANO EIDSVOLL AS"), [])
        self.assertEqual(place_corroborations("Milano Eidsvoll, Råholt", row, "MILANO EIDSVOLL AS"), ["RÅHOLT"])

    def test_registry_e_mail_domain_with_matching_name_is_exact(self):
        record = site({"title": "Fjord Elektro AS", "discovery_origin": "registry_email_domain"})
        self.assertTrue(assess_site_identity(profile(), record)["publishable"])

    def test_parked_domain_never_publishes(self):
        record = site({"title": "fjordelektro.no is for sale", "description": "This domain is for sale", "discovery_origin": "registry_email_domain",
                       "identity_proof": None})
        self.assertFalse(assess_site_identity(profile(), record)["publishable"])

    def test_owned_profile_by_domain_label_only_for_exact_site(self):
        value = {"registered_domain": "fjordel.no", "brand_names": []}
        link = {"platform": "facebook", "url": "https://facebook.com/fjordel"}
        self.assertTrue(assess_owned_profile(profile(), link, value, True)["publishable"])
        self.assertFalse(assess_owned_profile(profile(), link, value, False)["publishable"])
        other = {"platform": "facebook", "url": "https://facebook.com/webbyraaet"}
        self.assertFalse(assess_owned_profile(profile(), other, value, True)["publishable"])

    def test_parent_group_profile_is_not_the_subsidiary(self):
        value = {"registered_domain": "leroyseafood.com", "brand_names": []}
        link = {"platform": "linkedin", "url": "https://linkedin.com/company/leroyseafoodgroupasa"}
        self.assertFalse(assess_owned_profile(profile("LERØY SEAFOOD AS"), link, value, True)["publishable"])

    def test_gate_quarantines_profiles_of_an_unverified_site(self):
        record = site({"title": "Something else", "discovery_origin": "legal_name_domain_guess",
                       "social_links": [{"platform": "facebook", "url": "https://facebook.com/fjordelektro"}]})
        gated = gate_company_site(profile(), record)
        self.assertEqual(gated["value"]["social_links"], [])


class SignalParserTest(unittest.TestCase):
    def test_feed_items_need_own_domain_and_plausible_date(self):
        rss = b"""<?xml version="1.0"?><rss><channel>
        <item><title>Ny avtale</title><link>https://www.example.no/nyheter/ny-avtale</link><pubDate>Tue, 06 Oct 2026 08:00:00 +0200</pubDate></item>
        <item><title>Fremmed</title><link>https://other.no/x</link><pubDate>Tue, 06 Oct 2026 08:00:00 +0200</pubDate></item>
        <item><title>Fremtid</title><link>https://example.no/f</link><pubDate>Tue, 06 Oct 2099 08:00:00 +0200</pubDate></item>
        </channel></rss>"""
        items = parse_feed(rss, "https://example.no/feed/", "example.no", NOW)
        self.assertEqual([(item["title"], item["date"]) for item in items], [("Ny avtale", "2026-10-06")])

    def test_atom_entries(self):
        atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Lansering</title><link href="https://example.no/blogg/lansering"/>
        <updated>2026-09-01T10:00:00Z</updated></entry></feed>"""
        self.assertEqual(parse_feed(atom, "https://example.no/atom", "example.no", NOW)[0]["date"], "2026-09-01")

    def test_sitemap_urls_and_children(self):
        xml = b"""<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.no/nyheter/apner-ny-butikk</loc>
        <lastmod>2026-08-02</lastmod></url></urlset>"""
        urls, children = parse_sitemap(xml)
        self.assertEqual(urls, [("https://example.no/nyheter/apner-ny-butikk", "2026-08-02")])
        self.assertEqual(children, [])
        self.assertEqual(slug_title(urls[0][0]), "Apner ny butikk")


class CareerLinkTest(unittest.TestCase):
    def test_whole_segments_or_exact_labels_only(self):
        from bs4 import BeautifulSoup
        from norway_company_agent.site_research import career_links
        html = """<a href="/blogs/news/tagged/utstillinger">Utstillinger</a><a href="/vis/annonseinnstillinger">Annonser</a>
        <a href="/jobber-du-i-restaurant">Jobber du i restaurant?</a><a href="/">Hjem</a>
        <a href="/om-oss/karriere/">Karriere</a><a href="/hjelp">Ledige stillinger</a><a href="https://firma.teamtailor.com/jobs">Se jobber</a>"""
        found = [item["url"] for item in career_links("https://example.no/", BeautifulSoup(html, "lxml"), "example.no")]
        self.assertEqual(found, ["https://firma.teamtailor.com/jobs", "https://example.no/hjelp", "https://example.no/om-oss/karriere/"])


class NewsListingTest(unittest.TestCase):
    def test_dates_in_norwegian_and_english_forms(self):
        from norway_company_agent.site_research import find_date
        self.assertEqual(find_date("Publisert 12. okt. 2026"), "2026-10-12")
        self.assertEqual(find_date("3. mars 2025 – nyhet"), "2025-03-03")
        self.assertEqual(find_date("Oppdatert 07.09.2026"), "2026-09-07")
        self.assertEqual(find_date("October 5, 2026"), "2026-10-05")
        self.assertIsNone(find_date("Tlf 51 68 57 00"))

    def test_listing_uses_each_items_own_date(self):
        from bs4 import BeautifulSoup
        from norway_company_agent.site_research import listing_items
        html = """<main><ul>
          <li><a href="/nyheter/ny-avtale-med-kommunen"><h3>Ny avtale med kommunen</h3></a><span>12.10.2026</span></li>
          <li><a href="/nyheter/apner-avdeling-i-bergen">Vi åpner avdeling i Bergen</a></li>
          <li><time datetime="2026-08-01">1. august</time><a href="/nyheter/sommerstengt">Sommerstengt i juli</a></li>
          <li><a href="/om-oss">Om oss 01.01.2026</a></li>
        </ul></main>"""
        items = listing_items("https://example.no/nyheter", BeautifulSoup(html, "lxml"), "example.no", NOW)
        self.assertEqual([(item["title"], item["date"]) for item in items],
                         [("Ny avtale med kommunen", "2026-10-12"), ("Sommerstengt i juli", "2026-08-01")])


class NavJobsTest(unittest.TestCase):
    def test_name_key_ignores_legal_form(self):
        self.assertEqual(name_key("TAXI TR 1807 AS"), name_key("Taxi TR 1807"))
        self.assertEqual(name_key("AS SNERTINGDAL AUTO"), "snertingdal auto")

    def test_candidates_by_legal_name_sub_unit_and_department(self):
        index = NavJobIndex(days=1)
        ads = [{"uuid": "a", "status": "ACTIVE", "business_name": "Nettpartner AS", "modified": "2026-10-01"},
               {"uuid": "b", "status": "ACTIVE", "business_name": "NETTPARTNER AS AVD OSLO", "modified": "2026-10-02"},
               {"uuid": "c", "status": "ACTIVE", "business_name": "Nettpartner Norge AS", "modified": "2026-10-03"}]
        for ad in ads:
            index.by_name.setdefault(name_key(ad["business_name"]), []).append(ad)
        found = index.candidates({"name": "NETTPARTNER AS", "evidence": {}})
        self.assertEqual([ad["uuid"] for ad in found], ["b", "a"])

    def test_verify_requires_the_employer_number(self):
        index = NavJobIndex(days=1)
        record = {"uuid": "x", "status": "ACTIVE", "json": {"title": "Montør", "published": "2026-10-01T08:00:00Z", "expires": "2099-01-01",
                  "employer": {"name": "NETTPARTNER AS AVD OSLO", "orgnr": "926059602"}, "workLocations": [{"city": "OSLO"}]}}
        index._get = lambda *args, **kwargs: json.dumps(record).encode()
        index.token = "t"
        self.assertIsNotNone(index.verify({"uuid": "x", "status": "ACTIVE"}, {"998407567", "926059602"}))
        self.assertIsNone(index.verify({"uuid": "x", "status": "ACTIVE"}, {"998407567"}))


class ContractTest(unittest.TestCase):
    def test_claims_evidence_and_states(self):
        registry = {"field": "registry", "status": "available", "source_url": "https://data.brreg.no/x", "retrieved_at": "2026-10-16T00:00:00Z",
                    "content_sha256": "b" * 64, "source_class": "official_registry_bulk",
                    "value": {"navn": "FJORD ELEKTRO AS", "organisasjonsform.beskrivelse": "Aksjeselskap", "naeringskode1.kode": "43.210",
                              "naeringskode1.beskrivelse": "Elektrisk installasjonsarbeid", "antallAnsatte": "", "konkurs": "false", "underAvvikling": "false",
                              "forretningsadresse.poststed": "SANDNES"}}
        roles = {"field": "roles", "status": "available", "source_url": "https://data.brreg.no/r", "retrieved_at": "2026-10-16T00:00:00Z",
                 "value": {"roles": [{"name": ["REVISJON", "AS"], "role": "Revisor", "role_code": "REVI", "organisation_number": "123456785"},
                                     {"name": "Kari Nordmann", "role": "Daglig leder", "role_code": "DAGL"}]}}
        website = site({"title": "Fjord Elektro AS", "discovery_origin": "registry_email_domain", "description": "Elektriker i Sandnes"})
        record = {"organisation_number": "810034882", "name": "FJORD ELEKTRO AS", "evidence": {"registry": registry, "roles": roles}}
        record["evidence"]["website"] = gate_company_site(record, website)
        contract = build_contract(record)
        fields = {(claim["field"], claim["availability"]) for claim in contract["claims"]}
        self.assertIn(("official_website", "available"), fields)
        self.assertIn(("registered_employees", "not_available"), fields)
        self.assertIn(("latest_annual_accounts", "failed"), fields)
        evidence_ids = {item["id"] for item in contract["evidence"]}
        for claim in contract["claims"]:
            self.assertTrue(set(claim["evidence_ids"]) <= evidence_ids)
        self.assertIn("Daglig leder Kari Nordmann", contract["summary"]["text"])
        self.assertEqual(build_contract(json.loads(json.dumps(record)))["evidence"], contract["evidence"])


class BatchInputTest(unittest.TestCase):
    def test_invalid_and_duplicate_rows_never_abort(self):
        spec = importlib.util.spec_from_file_location("run_competition_batch", ROOT / "scripts" / "run_competition_batch.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "orgs.txt"
            path.write_text("810034882\n810 034 882\n12345\n", encoding="utf-8")
            records, warnings = module.read_inputs(str(path))
        self.assertEqual([item["organisation_number"] for item in records], ["810034882", "12345"])
        self.assertEqual(len(warnings), 2)


if __name__ == "__main__":
    unittest.main()
