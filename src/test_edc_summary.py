"""The plain-language assumption summary shown at the end of a chat run."""
import json
import tempfile
import unittest
from pathlib import Path

import edc_policy as ep

LEDGER = {
    "class_counts": {},
    "not_stated": [
        {"id": "B2.T_C", "value": 330.0, "class": "BORROWED_SUSPECT", "impact": "HIGH", "asked": True},
        {"id": "S1.flow", "value": 3.24, "class": "ALTERNATIVE", "impact": "HIGH", "asked": True},
        {"id": "S1.T_C", "value": 25.0, "class": "ASSUMED", "impact": "MED", "asked": False},
        {"id": "S1.z", "value": {"H2": 1.0, "CO": 0.0}, "class": "ASSUMED", "impact": "HIGH", "asked": False},
    ],
    "review": {"reply_was_ok": False, "applied": [{"id": "B2.T_C", "old": 330.0, "new": 350.0, "warnings": []}]},
    "property_method_substitution": {"paper_method": "PR", "used_method": "PR-BM", "reason": "스키마에 PR 없음"},
}


class Summary(unittest.TestCase):
    def test_sections(self):
        t = ep.format_assumption_summary(LEDGER)
        self.assertIn("직접 바꾸신 값", t)
        self.assertIn("B2.T_C: 330 → 350", t)
        self.assertIn("질문에서 확인하고 그대로 쓴 값", t)
        self.assertIn("S1.flow = 3.24", t)
        self.assertIn("묻지 않고 가정한 값", t)
        self.assertIn("S1.T_C = 25", t)
        self.assertIn("조성 {H2 1}", t)                       # zero fractions are not listed
        self.assertIn("논문 PR → 실제 PR-BM", t)

    def test_without_a_review_nothing_counts_as_confirmed(self):
        led = json.loads(json.dumps(LEDGER))
        led["review"] = {}                                  # the question was never put to the user
        t = ep.format_assumption_summary(led)
        self.assertNotIn("질문에서 확인하고", t)
        self.assertNotIn("직접 바꾸신 값", t)
        self.assertIn("묻지 않고 가정한 값", t)
        for rid in ("B2.T_C", "S1.flow", "S1.T_C"):
            self.assertIn(rid, t)

    def test_a_changed_value_is_not_listed_twice(self):
        t = ep.format_assumption_summary(LEDGER)
        self.assertEqual(t.count("B2.T_C"), 1)

    def test_warnings_are_shown(self):
        led = json.loads(json.dumps(LEDGER))
        led["review"]["applied"][0]["warnings"] = ["논문이 다룬 범위(100–1000) 밖"]
        self.assertIn("경고: 논문이 다룬 범위", ep.format_assumption_summary(led))

    def test_long_lists_are_cut(self):
        led = {"not_stated": [{"id": f"B{i}.T_C", "value": 1.0, "class": "ASSUMED", "asked": False} for i in range(20)]}
        t = ep.format_assumption_summary(led, max_items=12)
        self.assertIn("외 8개", t)
        self.assertEqual(t.count("  - B"), 12)

    def test_nothing_to_report_is_empty(self):
        self.assertEqual(ep.format_assumption_summary({"not_stated": [], "review": {}}), "")
        self.assertEqual(ep.format_assumption_summary({}), "")

    def test_from_file_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ep.summary_from_file(Path(d) / "missing.json"), "")
            bad = Path(d) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertEqual(ep.summary_from_file(bad), "")
            ok = Path(d) / "ok.json"
            ok.write_text(json.dumps(LEDGER, ensure_ascii=False), encoding="utf-8")
            self.assertIn("[가정 요약]", ep.summary_from_file(ok))


class Orchestrator(unittest.TestCase):
    def test_block_is_empty_without_ledger_and_present_with_it(self):
        from chat_orchestrator import PipelineSession
        s = PipelineSession.__new__(PipelineSession)          # no LLM/paper needed for this helper
        with tempfile.TemporaryDirectory() as d:
            s.part1_dir = Path(d)
            self.assertEqual(s._assumption_summary_block(), "")
            (Path(d) / "edc_assumption_ledger.json").write_text(json.dumps(LEDGER, ensure_ascii=False), encoding="utf-8")
            self.assertTrue(s._assumption_summary_block().startswith("\n\n[가정 요약]"))


if __name__ == "__main__":
    unittest.main()
