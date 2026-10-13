"""EDC-2b: the schema-review question must show WHAT is being approved, and nothing else changes."""
from paths import SRC_DIR
import json
import unittest
from pathlib import Path

import yaml

import part1_stage_functions as p1
from agent_loop_utils import HumanInputRequired

HERE = Path(__file__).resolve().parent
REAL_RUN = SRC_DIR / "run" / "attempt_chat_20261006_220801" / "part1"

SCHEMA = {
    "flowsheet_schema": {"blocks": [
        {"id": "B1", "type": "MIXER", "specs": {}},
        {"id": "B2", "type": "RGIBBS", "name": "R-GIBBS reactor", "specs": {"T_C": {
            "value": 300.0, "source": "inferred", "confidence": 0.45,
            "evidence": {"quote": "varied between 100 and 1000 C"},
            "rationale": "Paper treats temperature as a sensitivity variable."}}},
    ]},
    "io": {"feeds": [{"stream": "S1", "name": "EXTRA-H2", "specs": {"T_C": {
        "value": 25.0, "source": "inferred", "confidence": 0.65}}}]},
}
ITEMS = [
    {"kind": "low_confidence_assumption", "field_path": "flowsheet_schema.blocks[1].specs.T_C",
     "source": "inferred", "confidence": 0.45, "value": 300.0},
    {"kind": "low_confidence_assumption", "field_path": "io.feeds[0].specs.T_C",
     "source": "inferred", "confidence": 0.65, "value": 25.0},
]


class Message(unittest.TestCase):
    def test_names_block_variable_value_and_source(self):
        msg = p1.format_schema_review_question(SCHEMA, ITEMS[:1])
        self.assertIn("B2 (RGIBBS, R-GIBBS reactor) · T_C = 300.0", msg)
        self.assertIn("inferred", msg)
        self.assertIn("0.45", msg)

    def test_shows_models_own_rationale_and_quote(self):
        msg = p1.format_schema_review_question(SCHEMA, ITEMS[:1])
        self.assertIn("sensitivity variable", msg)
        self.assertIn("varied between 100 and 1000 C", msg)

    def test_feed_items_are_labelled_by_stream(self):
        msg = p1.format_schema_review_question(SCHEMA, ITEMS[1:])
        self.assertIn("피드 S1, EXTRA-H2 · T_C = 25.0", msg)

    def test_states_that_the_answer_does_not_change_values(self):
        msg = p1.format_schema_review_question(SCHEMA, ITEMS)
        self.assertIn("값을 바꾸지 않고", msg)
        self.assertIn("approve", msg)

    def test_unknown_path_falls_back_to_the_raw_path(self):
        msg = p1.format_schema_review_question(SCHEMA, [{"field_path": "weird.path", "value": 1,
                                                        "source": "llm", "confidence": None}])
        self.assertIn("weird.path = 1", msg)
        self.assertIn("신뢰도 없음", msg)

    def test_any_formatting_error_falls_back_to_the_legacy_question(self):
        msg = p1.format_schema_review_question(SCHEMA, [None])        # items must be dicts -> error
        self.assertEqual(msg, p1._LEGACY_REVIEW_QUESTION.format(n=1))

    @unittest.skipUnless((REAL_RUN / "part1_status.json").exists(), "real run not present")
    def test_on_the_real_run_the_user_saw(self):
        status = json.loads((REAL_RUN / "part1_status.json").read_text(encoding="utf-8"))
        schema = yaml.safe_load((REAL_RUN / "Schema_proto.yaml").read_text(encoding="utf-8"))
        msg = p1.format_schema_review_question(schema, status["human_review_items"])
        self.assertIn("T_C = 300.0", msg)
        self.assertIn("RGIBBS", msg)


class FlowUnchanged(unittest.TestCase):
    def setUp(self):
        self._orig = p1._session_write_status
        self.written = []
        p1._session_write_status = lambda session, upd: self.written.append(upd)

    def tearDown(self):
        p1._session_write_status = self._orig

    def session(self):
        return {"state": {"schema_obj": SCHEMA, "schema_assessment": {"human_review_items": ITEMS}},
                "config": {"part1_hitl_mode": "chat", "out_schema_path": "unused.yaml"}}

    def test_pause_payload_keeps_items_and_only_the_text_changes(self):
        with self.assertRaises(HumanInputRequired) as cm:
            p1.review_schema_with_human(self.session(), human_answer=None)
        payload = cm.exception.payload
        self.assertEqual(payload["type"], "schema_review")
        self.assertEqual(payload["context"]["human_review_items"], ITEMS)      # selection untouched
        self.assertIn("B2 (RGIBBS", payload["question"])

    def test_answer_path_is_unchanged(self):
        out = p1.review_schema_with_human(self.session(), human_answer="looks fine")
        self.assertEqual(out["decision"], "approve")
        self.assertEqual(out["human_notes"], "looks fine")
        self.assertEqual(self.written[-1]["final_output_kind"], "proto_human_approved")

    def test_bypass_mode_is_unchanged(self):
        s = self.session()
        s["config"]["part1_hitl_mode"] = "bypass"
        out = p1.review_schema_with_human(s)
        self.assertTrue(out["bypassed"])


if __name__ == "__main__":
    unittest.main()
