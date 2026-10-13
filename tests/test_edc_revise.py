"""EDC-2c: the reply to the schema-review question is applied to the schema (flag PART1_EDC_POLICY).
Policy agreed with the user: out-of-range / impossible values only WARN, the number is used."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import yaml

import edc_policy as ep
import part1_stage_functions as p1
from agent_loop_utils import HumanInputRequired
from test_edc_policy import PAPER, V, rgibbs, schema

HERE = Path(__file__).resolve().parent
REAL_RUN = HERE / "run" / "attempt_chat_20261006_220801" / "part1"
PAPER3 = HERE.parent.parent / "jinny_DB" / "parsed" / "paper3" / "paper3.md"

RANGE_Q = "the temperature was varied between 100 and 1000 °C as the independent variable"
APPLIED_TEXT = "스키마에 그대로 반영"      # "reflected in the schema as is"


def base_schema():
    sch = schema([
        rgibbs(V(300, "inferred", RANGE_Q, 0.45)),
        {"id": "B3", "type": "MHeatX", "specs": {
            "outlet_streams": {"S4": "S5", "S7": "S8"},
            "temp_specs": [{"out_stream": "S8", "T_out_C": V(400, "paper", "set to 25 °C")}]}},
        {"id": "B4", "type": "SPLITTER", "specs": {"split_fractions": {"S2": 0.2, "S3": 0.8}}},
    ], feeds=[{"stream": "S1", "specs": {
        "T_C": V(25, "inferred", "The syngas temperature is set to 25 °C", 0.65),
        "P_bar": V(30, "paper", "30 bar"), "flow": V(3.24, "paper", "3.24 kg h−1"),
        "z": {"H2": 1.0}}}])
    # the ledger only lists splitter fractions when the splitter's outlet edges exist
    sch["flowsheet_schema"]["connectivity"]["edges"] = [
        {"stream": "S2", "from": {"block": "B4"}}, {"stream": "S3", "from": {"block": "B4"}}]
    return sch


class Locate(unittest.TestCase):
    def test_plain_block_feed_mheatx_and_splitter(self):
        s = base_schema()
        self.assertEqual(ep.locate_node(s, "B1", "T_C")["path"], "flowsheet_schema.blocks[0].specs.T_C")
        self.assertEqual(ep.locate_node(s, "S1", "T_C")["path"], "io.feeds[0].specs.T_C")
        self.assertEqual(ep.locate_node(s, "B3", "T_out_C[S8]")["path"],
                         "flowsheet_schema.blocks[1].specs.temp_specs[0].T_out_C")
        self.assertEqual(ep.locate_node(s, "B4", "split_fraction[S2]")["key"], "S2")

    def test_unknown_gives_none(self):
        s = base_schema()
        for owner, var in (("B9", "T_C"), ("B1", "nope"), ("B3", "T_out_C[S99]"), ("B4", "split_fraction[S9]")):
            self.assertIsNone(ep.locate_node(s, owner, var))


class Parse(unittest.TestCase):
    def test_ok_words(self):
        for w in ("ok", "OK", "approve", "네", "그대로"):
            r = ep.parse_review_answer(w)
            self.assertTrue(r["ok"], w)
            self.assertEqual(r["overrides"], [])

    def test_assignments(self):
        r = ep.parse_review_answer("B1.T_C=350; S1.flow = 3.73\nB3.T_out_C[S8]=410.5, B4.split_fraction[S2]=1e-1")
        self.assertEqual([(o["id"], o["value"]) for o in r["overrides"]],
                         [("B1.T_C", 350.0), ("S1.flow", 3.73), ("B3.T_out_C[S8]", 410.5),
                          ("B4.split_fraction[S2]", 0.1)])
        self.assertEqual(r["leftover"], "")
        self.assertFalse(r["ok"])

    def test_negative_numbers(self):
        self.assertEqual(ep.parse_review_answer("B1.T_C=-300")["overrides"][0]["value"], -300.0)

    def test_free_text_and_bare_numbers_are_never_values(self):
        r = ep.parse_review_answer("온도를 더 높여줘")
        self.assertEqual((r["overrides"], r["ok"]), ([], False))
        self.assertNotEqual(r["leftover"], "")
        r2 = ep.parse_review_answer("350")
        self.assertEqual(r2["overrides"], [])
        self.assertEqual(r2["leftover"], "350")

    def test_typing_variants(self):
        ids = ["B2.T_C", "S1.T_C", "B3.T_out_C[S8]"]
        for w in ("B2=T_C=350", "B2. T_C=350", "B2 T_C = 350", "b2:t_c=350", "B2.T_C=350"):
            r = ep.parse_review_answer(w, known_ids=ids)
            self.assertEqual([(o["id"], o["value"]) for o in r["overrides"]], [("B2.T_C", 350.0)], w)
            self.assertEqual(r["leftover"], "", w)
        self.assertIsNone(ep.parse_review_answer("B2.T_C=350", known_ids=ids)["overrides"][0]["interpreted_as"])
        self.assertEqual(ep.parse_review_answer("B2=T_C=350", known_ids=ids)["overrides"][0]["interpreted_as"], "B2.T_C=350")
        r = ep.parse_review_answer("B3 T_out_C[S8]=410", known_ids=ids)
        self.assertEqual(r["overrides"][0]["id"], "B3.T_out_C[S8]")

    def test_variants_do_not_invent_ids(self):
        ids = ["B2.T_C", "S1.T_C"]
        r = ep.parse_review_answer("B9=T_C=350", known_ids=ids)
        self.assertEqual(r["overrides"], [])                # unknown owner is not guessed

    def test_bare_variable_only_when_unique(self):
        self.assertEqual(ep.parse_review_answer("T_C=350", known_ids=["B2.T_C", "S1.T_C"])["overrides"], [])
        r = ep.parse_review_answer("T_C=350", known_ids=["B2.T_C", "S1.flow"])
        self.assertEqual(r["overrides"][0]["id"], "B2.T_C")

    def test_unknown_ids_are_reported(self):
        r = ep.parse_review_answer("B3.T_C=425; B0103.P_bar=2.95", known_ids=["B0103.P_bar", "B0103.T_C"])
        self.assertEqual([o["id"] for o in r["overrides"]], ["B0103.P_bar"])
        self.assertEqual(r["unknown_ids"], ["B3.T_C"])
        self.assertEqual(ep.parse_review_answer("ok", known_ids=["B1.T_C"])["unknown_ids"], [])

    def test_mixed_reply_keeps_the_assignment(self):
        self.assertEqual(len(ep.parse_review_answer("ok B1.T_C=350")["overrides"]), 1)


class Apply(unittest.TestCase):
    def setUp(self):
        self.sch = base_schema()
        self.cl = ep.classify_schema(self.sch, PAPER)

    def apply(self, text):
        return ep.apply_overrides(self.sch, ep.parse_review_answer(text)["overrides"], self.cl)

    def test_value_node_is_updated_with_user_provenance(self):
        out = self.apply("B1.T_C=350")
        node = self.sch["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]
        self.assertEqual((node["value"], node["source"], node["confidence"]), (350.0, "user", 1.0))
        self.assertEqual(node["evidence"]["quote"], "B1.T_C=350")
        self.assertIn("300", node["rationale"])
        self.assertEqual((out["applied"][0]["old"], out["applied"][0]["new"]), (300, 350.0))
        self.assertEqual(out["warnings"], [])

    def test_raw_number_container_and_feed_and_mheatx(self):
        self.apply("B4.split_fraction[S2]=0.3; S1.flow=3.73; B3.T_out_C[S8]=420")
        s = self.sch
        self.assertEqual(s["flowsheet_schema"]["blocks"][2]["specs"]["split_fractions"]["S2"], 0.3)
        self.assertEqual(s["io"]["feeds"][0]["specs"]["flow"]["value"], 3.73)
        self.assertEqual(s["flowsheet_schema"]["blocks"][1]["specs"]["temp_specs"][0]["T_out_C"]["value"], 420.0)

    def test_warn_and_accept_for_impossible_values(self):
        out = self.apply("B1.T_C=-300; S1.P_bar=0; S1.flow=-1; B4.split_fraction[S2]=1.5")
        self.assertEqual(len(out["applied"]), 4)                       # every number was used
        self.assertEqual(self.sch["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]["value"], -300.0)
        text = " ".join(out["warnings"])
        for needle in ("절대영도", "압력", "유량", "0~1"):
            self.assertIn(needle, text)

    def test_warn_and_accept_outside_the_paper_range(self):
        out = self.apply("B1.T_C=1500")
        self.assertEqual(self.sch["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]["value"], 1500.0)
        self.assertTrue(any("100–1000" in w for w in out["warnings"]))

    def test_inside_the_range_has_no_warning(self):
        self.assertEqual(self.apply("B1.T_C=1000")["warnings"], [])

    def test_unknown_ids_are_ignored_not_guessed(self):
        out = self.apply("B9.T_C=1; B1.nope=2; B1.T_C=350")
        self.assertEqual([i["id"] for i in out["ignored"]], ["B9.T_C", "B1.nope"])
        self.assertEqual(len(out["applied"]), 1)


class Messages(unittest.TestCase):
    def test_question_says_the_reply_is_applied_only_when_it_is(self):
        sel = ep.select_questions(ep.classify_schema(base_schema(), PAPER))
        self.assertNotIn(APPLIED_TEXT, ep.format_question(sel))
        self.assertIn(APPLIED_TEXT, ep.format_question(sel, will_apply=True))

    def test_policy_items_carry_path_and_id(self):
        res = ep.policy_review_items(base_schema(), PAPER)
        ids = {i["edc_id"]: i for i in res["items"]}
        self.assertIn("B1.T_C", ids)
        self.assertEqual(ids["B1.T_C"]["field_path"], "flowsheet_schema.blocks[0].specs.T_C")
        self.assertEqual(ids["B1.T_C"]["class"], "SWEPT")

    def test_assumption_ledger_lists_only_non_stated_values(self):
        res = ep.policy_review_items(base_schema(), PAPER)
        led = ep.assumption_ledger(res["classified"], res["selected"])
        classes = {r["class"] for r in led["not_stated"]}
        self.assertFalse(classes & {"STATED", "DERIVED", "STATED_ELSEWHERE", "NO_PROVENANCE"})
        self.assertTrue(any(r["id"] == "B1.T_C" and r["asked"] for r in led["not_stated"]))


class Part1Integration(unittest.TestCase):
    def setUp(self):
        self._orig = p1._session_write_status
        self.written = []
        p1._session_write_status = lambda session, upd: self.written.append(upd)
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "paper.md").write_text(PAPER, encoding="utf-8")
        self.schema_path = d / "Schema_proto.yaml"
        self.original = base_schema()
        self.schema_path.write_text(yaml.safe_dump(self.original, sort_keys=False, allow_unicode=True),
                                    encoding="utf-8")

    def tearDown(self):
        p1._session_write_status = self._orig
        self.tmp.cleanup()

    def session(self, edc=True):
        d = Path(self.tmp.name)
        return {"state": {"schema_obj": copy.deepcopy(self.original)},
                "config": {"part1_hitl_mode": "chat", "out_schema_path": str(self.schema_path),
                           "part1_run_dir": str(d), "paper_text_path": str(d / "paper.md"),
                           "edc_policy": edc}}

    def start(self, edc=True):
        s = self.session(edc)
        items = p1._review_items_for_session(s, s["state"]["schema_obj"])
        s["state"]["schema_assessment"] = {"human_review_items": items}
        return s, items

    def saved_schema(self):
        return yaml.safe_load(self.schema_path.read_text(encoding="utf-8"))

    def test_flag_off_keeps_the_old_selector(self):
        s, items = self.start(edc=False)
        self.assertEqual(items, p1.collect_schema_human_review_items(s["state"]["schema_obj"]))
        self.assertNotIn("edc", s["state"])
        self.assertFalse((Path(self.tmp.name) / "edc_assumption_ledger.json").exists())

    def test_flag_on_selects_by_policy_and_writes_the_ledger(self):
        s, items = self.start()
        self.assertIn("B1.T_C", [i["edc_id"] for i in items])
        led = json.loads((Path(self.tmp.name) / "edc_assumption_ledger.json").read_text(encoding="utf-8"))
        self.assertTrue(led["not_stated"])

    def test_question_uses_the_policy_text(self):
        s, _ = self.start()
        with self.assertRaises(HumanInputRequired) as cm:
            p1.review_schema_with_human(s, human_answer=None)
        q = cm.exception.payload["question"]
        self.assertIn("B1.T_C = 300", q)
        self.assertIn(APPLIED_TEXT, q)

    def test_reply_changes_the_schema_file_and_keeps_a_backup(self):
        s, _ = self.start()
        out = p1.review_schema_with_human(s, human_answer="B1.T_C=350")
        self.assertEqual(out["decision"], "approve")
        node = self.saved_schema()["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]
        self.assertEqual((node["value"], node["source"]), (350.0, "user"))
        backup = yaml.safe_load((Path(self.tmp.name) / "Schema_proto.pre_review.yaml").read_text(encoding="utf-8"))
        self.assertEqual(backup["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]["value"], 300)
        self.assertEqual(out["edc_review"]["applied"][0]["new"], 350.0)
        self.assertEqual(s["state"]["schema_obj"]["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]["value"], 350.0)
        self.assertEqual(self.written[-1]["final_output_kind"], "proto_human_approved")
        led = json.loads((Path(self.tmp.name) / "edc_assumption_ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(led["review"]["applied"][0]["id"], "B1.T_C")

    def test_impossible_value_is_used_with_a_warning(self):
        s, _ = self.start()
        out = p1.review_schema_with_human(s, human_answer="B1.T_C=-300")
        self.assertEqual(self.saved_schema()["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]["value"], -300.0)
        self.assertTrue(out["edc_review"]["warnings"])

    def test_ok_leaves_the_schema_untouched(self):
        s, _ = self.start()
        before = self.schema_path.read_text(encoding="utf-8")
        out = p1.review_schema_with_human(s, human_answer="ok")
        self.assertEqual(self.schema_path.read_text(encoding="utf-8"), before)
        self.assertEqual(out["edc_review"]["applied"], [])
        self.assertTrue(out["edc_review"]["reply_was_ok"])
        self.assertFalse((Path(self.tmp.name) / "Schema_proto.pre_review.yaml").exists())

    def test_free_text_is_recorded_not_applied(self):
        s, _ = self.start()
        before = self.schema_path.read_text(encoding="utf-8")
        with self.assertRaises(HumanInputRequired):        # first unrecognised reply: one chance to retype
            p1.review_schema_with_human(s, human_answer="온도를 높여줘")
        out = p1.review_schema_with_human(s, human_answer="온도를 높여줘")   # second time: note only
        self.assertEqual(self.schema_path.read_text(encoding="utf-8"), before)
        self.assertNotEqual(out["edc_review"]["unparsed_text"], "")

    def test_unrecognised_reply_asks_once_then_the_retyped_value_is_applied(self):
        s, _ = self.start()
        with self.assertRaises(HumanInputRequired) as cm:
            p1.review_schema_with_human(s, human_answer="B1 is 350 please")
        self.assertIn("인식하지 못했습니다", cm.exception.payload["question"])
        self.assertIn("ID=숫자", cm.exception.payload["question"])
        self.assertIn("`ok`", cm.exception.payload["question"])
        out = p1.review_schema_with_human(s, human_answer="B1.T_C=350")
        self.assertEqual(self.saved_schema()["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]["value"], 350.0)
        self.assertEqual(out["edc_review"]["applied"][0]["new"], 350.0)

    def test_ids_from_another_run_are_named_and_the_valid_ids_listed(self):
        s, _ = self.start()
        with self.assertRaises(HumanInputRequired) as cm:
            p1.review_schema_with_human(s, human_answer="B9.T_C=350")      # no such id in this run
        q = cm.exception.payload["question"]
        self.assertIn("이번 질문에 없는 ID입니다: B9.T_C", q)
        id_line = [l for l in q.splitlines() if l.startswith("이번 질문의 ID:")][0]
        self.assertIn("B1.T_C", id_line)
        out = p1.review_schema_with_human(s, human_answer="B9.T_C=350")     # second time: recorded, schema untouched
        self.assertEqual(self.saved_schema()["flowsheet_schema"]["blocks"][0]["specs"]["T_C"]["value"], 300)
        self.assertEqual(out["edc_review"]["ignored"][0]["id"], "B9.T_C")

    def test_example_in_the_retry_message_is_not_a_composition(self):
        parsed = {"leftover": "x", "unknown_ids": []}
        self.assertIn("`B1.T_C=350`", p1._edc_unrecognised_message(parsed, ["S1.z", "B1.T_C"]))

    def test_typing_variants_from_real_runs_are_applied(self):
        for variant in ("B1=T_C=350", "B1. T_C=350", "b1.t_c = 350"):
            s, _ = self.start()
            out = p1.review_schema_with_human(s, human_answer=variant)
            self.assertEqual(out["edc_review"]["applied"][0]["new"], 350.0, variant)
            self.assertEqual(out["edc_review"]["applied"][0]["interpreted_as"], "B1.T_C=350", variant)

    def test_flag_off_reply_is_notes_only_as_before(self):
        s, _ = self.start(edc=False)
        before = self.schema_path.read_text(encoding="utf-8")
        out = p1.review_schema_with_human(s, human_answer="B1.T_C=350")
        self.assertNotIn("edc_review", out)
        self.assertEqual(self.schema_path.read_text(encoding="utf-8"), before)


@unittest.skipUnless((REAL_RUN / "Schema_proto.yaml").exists() and PAPER3.exists(), "real run not present")
class RealRun(unittest.TestCase):
    def test_the_case_the_user_ran(self):
        sch = yaml.safe_load((REAL_RUN / "Schema_proto.yaml").read_text(encoding="utf-8"))
        text = PAPER3.read_text(encoding="utf-8", errors="replace")
        res = ep.policy_review_items(sch, text)
        ids = [i["edc_id"] for i in res["items"]]
        self.assertIn("B2.T_C", ids)                       # swept reactor temperature
        self.assertIn("S1.T_C", ids)                       # H2 temperature borrowed from the syngas statement
        out = ep.apply_overrides(sch, ep.parse_review_answer("B2.T_C=350; S1.T_C=25")["overrides"], res["classified"])
        self.assertEqual(len(out["applied"]), 2)
        self.assertEqual(out["warnings"], [])              # 350 is inside the paper's 100-1000 range
        self.assertEqual(sch["flowsheet_schema"]["blocks"][1]["specs"]["T_C"]["value"], 350.0)


if __name__ == "__main__":
    unittest.main()
