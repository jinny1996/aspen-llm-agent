"""The EDC question is also asked when the draft needed completion (assess -> needs_completion): the reply is
written into Schema_validated.yaml, the file Part2 reads."""
import json
import tempfile
import unittest
from pathlib import Path

import yaml

import part1_stage_functions as p1
from agent_loop_utils import HumanInputRequired
from test_edc_policy import PAPER
from test_edc_revise import base_schema


class PostCompletion(unittest.TestCase):
    def setUp(self):
        self._orig = p1._session_write_status
        self.written = []
        p1._session_write_status = lambda session, upd: self.written.append(upd)
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "paper.md").write_text(PAPER, encoding="utf-8")
        self.proto = d / "Schema_proto.yaml"
        self.validated = d / "Schema_validated.yaml"
        for p in (self.proto, self.validated):
            p.write_text(yaml.safe_dump(base_schema(), sort_keys=False, allow_unicode=True), encoding="utf-8")

    def tearDown(self):
        p1._session_write_status = self._orig
        self.tmp.cleanup()

    def session(self, edc=True, mode="chat", reviewed=False):
        d = Path(self.tmp.name)
        state = {}
        if reviewed:
            state["human_review_decision"] = "approve"
        return {"state": state, "config": {
            "part1_hitl_mode": mode, "out_schema_path": str(self.proto), "final_schema_path": str(self.validated),
            "part1_run_dir": str(d), "paper_text_path": str(d / "paper.md"), "edc_policy": edc}}

    def load(self, p):
        return yaml.safe_load(p.read_text(encoding="utf-8"))

    def test_nothing_happens_when_off_bypassed_or_already_reviewed(self):
        for kw in ({"edc": False}, {"mode": "bypass"}, {"reviewed": True}):
            s = self.session(**kw)
            self.assertIsNone(p1.edc_review_after_completion(s), kw)
            self.assertFalse((Path(self.tmp.name) / "edc_assumption_ledger.json").exists(), kw)

    def test_no_validated_file_means_no_question(self):
        self.validated.unlink()
        self.assertIsNone(p1.edc_review_after_completion(self.session()))

    def test_the_question_is_asked_on_the_completed_schema(self):
        s = self.session()
        with self.assertRaises(HumanInputRequired) as cm:
            p1.edc_review_after_completion(s)
        pay = cm.exception.payload
        self.assertTrue(pay["context"]["after_completion"])
        self.assertIn("B1.T_C", pay["question"])
        self.assertTrue((Path(self.tmp.name) / "edc_assumption_ledger.json").exists())

    def test_the_reply_goes_into_the_validated_file_not_the_proto(self):
        s = self.session()
        proto_before = self.proto.read_text(encoding="utf-8")
        with self.assertRaises(HumanInputRequired):
            p1.edc_review_after_completion(s)
        review = p1.edc_review_after_completion(s, human_answer="B1.T_C=350")
        self.assertEqual(review["applied"][0]["new"], 350.0)
        self.assertEqual(self.load(self.validated)["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]["value"], 350.0)
        self.assertEqual(self.load(self.validated)["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]["source"], "user")
        self.assertEqual(self.proto.read_text(encoding="utf-8"), proto_before)
        backup = Path(self.tmp.name) / "Schema_validated.pre_review.yaml"
        self.assertEqual(self.load(backup)["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]["value"], 300)
        self.assertEqual(s["state"]["human_review_decision"], "approve")
        upd = self.written[-1]
        self.assertEqual(upd["final_output_schema_path"], str(self.validated))
        self.assertTrue(upd["edc_review_after_completion"])
        led = json.loads((Path(self.tmp.name) / "edc_assumption_ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(led["review"]["applied"][0]["id"], "B1.T_C")

    def test_ok_leaves_the_file_alone(self):
        s = self.session()
        before = self.validated.read_text(encoding="utf-8")
        review = p1.edc_review_after_completion(s, human_answer="ok")
        self.assertTrue(review["reply_was_ok"])
        self.assertEqual(self.validated.read_text(encoding="utf-8"), before)

    def test_unrecognised_reply_asks_once_more(self):
        s = self.session()
        with self.assertRaises(HumanInputRequired) as cm:
            p1.edc_review_after_completion(s, human_answer="make it hotter")
        self.assertTrue(cm.exception.payload["context"]["retry"])
        review = p1.edc_review_after_completion(s, human_answer="make it hotter")
        self.assertFalse(review["applied"])

    def test_a_second_call_after_the_review_is_a_no_op(self):
        s = self.session()
        p1.edc_review_after_completion(s, human_answer="ok")
        self.assertIsNone(p1.edc_review_after_completion(s))


class Tool(unittest.TestCase):
    """the paid completion pass must not run again when the tool is resumed with the human's answer"""

    def test_completion_runs_once_across_pause_and_resume(self):
        import chat_orchestrator as co
        calls = {"complete": 0}
        orig_complete, orig_review = p1.complete_and_validate, p1.edc_review_after_completion

        def fake_complete(session):
            calls["complete"] += 1
            return {"ok": True, "final_schema_path": "x"}

        def fake_review(session, human_answer=None):
            if human_answer is None:
                raise HumanInputRequired({"type": "schema_review", "question": "q"})
            return {"reply_was_ok": True, "applied": []}

        p1.complete_and_validate, p1.edc_review_after_completion = fake_complete, fake_review
        try:
            session = {"state": {}, "config": {}}
            tool = [t for t in co._build_part1_tools(session) if t.name == "complete_and_validate"][0]
            with self.assertRaises(HumanInputRequired):
                tool.invoke({})
            out = json.loads(tool.invoke({"human_answer": "ok"}))
            self.assertEqual(calls["complete"], 1)
            self.assertEqual(out["edc_review"]["reply_was_ok"], True)
            self.assertTrue(out["ok"])
        finally:
            p1.complete_and_validate, p1.edc_review_after_completion = orig_complete, orig_review

    def test_without_a_review_the_result_is_unchanged(self):
        import chat_orchestrator as co
        orig_complete, orig_review = p1.complete_and_validate, p1.edc_review_after_completion
        p1.complete_and_validate = lambda session: {"ok": True, "final_schema_path": "x"}
        p1.edc_review_after_completion = lambda session, human_answer=None: None
        try:
            tool = [t for t in co._build_part1_tools({"state": {}, "config": {}}) if t.name == "complete_and_validate"][0]
            self.assertEqual(json.loads(tool.invoke({})), {"ok": True, "final_schema_path": "x"})
        finally:
            p1.complete_and_validate, p1.edc_review_after_completion = orig_complete, orig_review


if __name__ == "__main__":
    unittest.main()
