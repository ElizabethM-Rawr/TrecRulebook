from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trec_explorer.engine import (  # noqa: E402
    Library,
    content_tokens,
    detect_concepts,
    extract_deadlines,
    extract_penalties,
    highlight_spans,
)

DATA = ROOT / "data" / "trec_library.json"

A = (
    "A sales agent wants to advertise a team name, \"The Smith Group,\" on "
    "social media and on a yard sign. The sponsoring broker has not registered "
    "the team name. The agent also wants to pay a $250 referral fee to an "
    "unlicensed friend who introduced a buyer."
)
B = (
    "A licensed inspector is asked by a real estate broker to inspect a property "
    "that the broker has listed. The inspector wants to advertise a free repair "
    "estimate and then perform repairs on the defects found during the inspection."
)


class LibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.lib = Library(DATA)

    def test_counts_and_chapters(self) -> None:
        self.assertEqual(len(self.lib.sections), 515)
        counts = self.lib.meta["counts"]
        self.assertEqual(counts["trec-rules"], 205)
        self.assertEqual(counts["oc1101"], 164)
        self.assertEqual(counts["oc1102"], 50)
        self.assertEqual(counts["oc1105"], 12)
        self.assertEqual(counts["pr221"], 62)
        self.assertEqual(counts["oc53"], 22)
        self.assertEqual(len(self.lib.chapters()), 12)
        self.assertFalse([s for s in self.lib.sections if not s.body.strip()])
        self.assertIn("source_hashes", self.lib.meta)
        self.assertEqual(len(self.lib.meta["source_hashes"]), 6)

    def test_search_trust_money(self) -> None:
        hits = self.lib.search("trust money", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].section.uid, "rule-535.146")
        self.assertIn("trust", hits[0].section.title.lower())

    def test_search_exact_citation(self) -> None:
        hits = self.lib.search("535.155", limit=5)
        self.assertTrue(hits)
        self.assertEqual(hits[0].section.uid, "rule-535.155")

    def test_search_advertising(self) -> None:
        hits = self.lib.search("advertising team name", limit=5)
        uids = [hit.section.uid for hit in hits[:3]]
        self.assertIn("rule-535.154", uids)
        self.assertIn("rule-535.155", uids)

    def test_source_and_chapter_filters(self) -> None:
        hits = self.lib.search("advertising", source="statutes", limit=20)
        self.assertTrue(hits)
        self.assertTrue(all(hit.section.kind == "Statute" for hit in hits))
        chapter = next(c for c in self.lib.chapters() if c.key == "rule:535")
        hits = self.lib.search("", chapter_key=chapter.key, limit=300)
        # Empty queries are handled by the UI, not the engine; use a broad term.
        hits = self.lib.search("license", chapter_key=chapter.key, limit=300)
        self.assertTrue(hits)
        self.assertTrue(all(hit.section.chapter_key == "rule:535" for hit in hits))

    def test_scenario_a(self) -> None:
        report = self.lib.analyze_scenario(A)
        citations = [match.section.citation_short for match in report.matches]
        self.assertIn("§535.154", citations)
        self.assertIn("§535.155", citations)
        self.assertIn("§535.147", citations)
        keys = {key for key, _, _ in report.concepts}
        self.assertIn("advertising", keys)
        self.assertIn("unlicensed", keys)
        self.assertNotIn("§221.031", citations)

    def test_scenario_b(self) -> None:
        report = self.lib.analyze_scenario(B)
        citations = [match.section.citation_short for match in report.matches]
        self.assertIn("§1102.303", citations)
        self.assertIn("§1102.304", citations)
        self.assertIn("§535.220", citations)
        keys = {key for key, _, _ in report.concepts}
        self.assertIn("inspections", keys)
        self.assertIn("conflict", keys)

    def test_comparison_shared_and_unique(self) -> None:
        report = self.lib.compare(A, B)
        self.assertEqual(len(report.a.matches), 12)
        self.assertEqual(len(report.b.matches), 12)
        self.assertTrue(report.overview)
        self.assertTrue(report.checklist)
        shared = {row.section.citation_short for row in report.shared}
        only_a = {row.section.citation_short for row in report.only_a}
        only_b = {row.section.citation_short for row in report.only_b}
        self.assertFalse(shared & only_a)
        self.assertFalse(shared & only_b)
        self.assertFalse(only_a & only_b)

    def test_comparison_shared_when_facts_overlap(self) -> None:
        a2 = "A sales agent advertises a team name without the broker registering it."
        b2 = "A broker reviews a team name advertisement for a sponsored sales agent."
        report = self.lib.compare(a2, b2)
        cited = {row.section.citation_short for row in report.shared}
        self.assertIn("§535.154", cited)

    def test_related_authorities_and_added_statutes(self) -> None:
        related = {
            section.citation_short
            for section in self.lib.related_sections("rule-535.154")
        }
        self.assertIn("§535.155", related)
        sources = {source["id"]: source for source in self.lib.meta["sources"]}
        self.assertEqual(sources["oc1105"]["scope"], "TREC")
        self.assertEqual(sources["oc53"]["scope"], "Related licensing law")
        self.assertEqual(self.lib.section_by_number("1105.002").kind, "Statute")

    def test_deadline_and_penalty_extraction(self) -> None:
        body = (
            "A license holder must notify the commission within 30 days after "
            "the event. The commission may suspend or revoke a license and may "
            "impose an administrative penalty."
        )
        deadlines = extract_deadlines(body)
        self.assertTrue(deadlines)
        self.assertIn("30 days", deadlines[0])
        penalties = dict(extract_penalties(body))
        self.assertIn("License status", penalties)
        self.assertIn("administrative", penalties["License status"].lower())

    def test_comparison_includes_review_aids(self) -> None:
        report = self.lib.compare(A, B)
        self.assertTrue(report.deadlines)
        self.assertTrue(report.penalties)
        self.assertTrue(report.facts_to_confirm)
        self.assertTrue(any("clock" in fact for fact in report.facts_to_confirm))

    def test_text_helpers(self) -> None:
        self.assertIn("trust", content_tokens("Trust money and earnest money"))
        concepts = {key for key, _, _ in detect_concepts("The inspector has a repair conflict")}
        self.assertIn("inspections", concepts)
        spans = highlight_spans("A team name appears in the advertisement.", "team name")
        self.assertGreaterEqual(len(spans), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
