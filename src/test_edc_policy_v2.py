"""Tests for edc_policy_v2 (flag PART1_EDC_POLICY_V2, default off)."""
import copy
import os
import unittest
from unittest import mock

import edc_policy as ep
import edc_policy_v2 as v2
import edc_semantic_validator as sv

ON = {"PART1_EDC_POLICY_V2": "1"}


def V(value, source="inferred", quote=None):
    n = {"value": value, "source": source, "confidence": 0.5}
    n["evidence"] = {"doc_id": "paper_text", "locator": "x", "quote": quote or ""}
    return n


def feed(sid, name, z=None, flow=None, t=None, p=None):
    return ({"id": sid, "type": "MATERIAL", "role": "FEED", "name": name},
            {"stream": sid, "specs": {
                "T_C": t or V(25, "paper", "feeds are at 25 °C"), "P_bar": p or V(1, "paper", "feeds are at 1 bar"),
                "flow": flow or V(1, "paper", "1 kg/h of feed"),
                "z": z or {"METHANE": 1.0},
                "z_provenance": {"source": "inferred", "confidence": 0.5, "evidence": {"quote": ""}}}})


def schema(*feeds, blocks=None):
    return {"flowsheet_schema": {"blocks": blocks or [], "streams": [f[0] for f in feeds],
                                 "connectivity": {"edges": []}},
            "io": {"feeds": [f[1] for f in feeds]}}


PAPER = "Feeds are at 25 °C. Feeds are at 1 bar. 1 kg/h of feed."


def klass(sch, paper=PAPER):
    return {r["id"]: r for r in ep.classify_schema(sch, paper)["entries"]}


class DefaultOff(unittest.TestCase):
    def test_flag_unset_changes_nothing(self):
        s = schema(feed("S1", "CH4 feed"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PART1_EDC_POLICY_V2", None)
            a = ep.classify_schema(s, PAPER)
            b = ep.classify_schema(s, PAPER, apply_v2=False)
            self.assertEqual(a, b)
            self.assertEqual(ep.select_questions(a, 5), ep.select_questions(b, 5))
            self.assertEqual(a["entries"][0].get("class") if a["entries"] else None,
                             b["entries"][0].get("class") if b["entries"] else None)

    def test_no_new_class_appears_without_the_flag(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PART1_EDC_POLICY_V2", None)
            self.assertNotIn("NAME_IMPLIED", klass(schema(feed("S1", "CH4 feed")))["S1.z"]["class"])

    def test_flag_values(self):
        for val, expect in (("1", True), ("true", True), ("on", True), ("0", False), ("", False), ("no", False)):
            with mock.patch.dict(os.environ, {"PART1_EDC_POLICY_V2": val}):
                self.assertEqual(v2.enabled(), expect, val)


class NameImplied(unittest.TestCase):
    def test_pure_composition_of_a_stream_named_after_the_species(self):
        with mock.patch.dict(os.environ, ON):
            c = klass(schema(feed("S1", "CH4 feed")))["S1.z"]
        self.assertEqual(c["class"], "NAME_IMPLIED")

    def test_alias_in_another_spelling(self):
        with mock.patch.dict(os.environ, ON):
            c = klass(schema(feed("S1", "H2O feed", z={"WATER": 1.0})))["S1.z"]
        self.assertEqual(c["class"], "NAME_IMPLIED")

    def test_generic_stream_name_is_not_implied(self):
        with mock.patch.dict(os.environ, ON):
            c = klass(schema(feed("S1", "FEED", z={"METHANE": 1.0})))["S1.z"]
        self.assertEqual(c["class"], "ASSUMED")

    def test_mixture_is_not_implied(self):
        with mock.patch.dict(os.environ, ON):
            c = klass(schema(feed("S1", "CH4 feed", z={"METHANE": 0.9, "WATER": 0.1})))["S1.z"]
        self.assertEqual(c["class"], "ASSUMED")

    def test_it_is_asked_only_when_the_budget_is_not_used_up_by_others(self):
        s = schema(feed("S1", "CH4 feed"), feed("S2", "AIR feed", z={"OXYGEN": 0.21, "NITROGEN": 0.79}))
        with mock.patch.dict(os.environ, ON):
            cl = ep.classify_schema(s, PAPER)
            one = [r["id"] for r in ep.select_questions(cl, k=1)["asked"]]
            two = [r["id"] for r in ep.select_questions(cl, k=2)["asked"]]
        self.assertEqual(one, ["S2.z"])                     # the unexplained composition goes first
        self.assertEqual(sorted(two), ["S1.z", "S2.z"])     # budget left -> the name-implied one is asked too

    def test_without_the_flag_the_order_is_the_old_one(self):
        s = schema(feed("S1", "CH4 feed"), feed("S2", "AIR feed", z={"OXYGEN": 0.21, "NITROGEN": 0.79}))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PART1_EDC_POLICY_V2", None)
            one = [r["id"] for r in ep.select_questions(ep.classify_schema(s, PAPER), k=1)["asked"]]
        self.assertEqual(one, ["S1.z"])                     # by id, as before


class ComponentNames(unittest.TestCase):
    def _s(self, stream_name, comp_notes):
        s = schema(feed("S1", stream_name, z={"METHY-01": 1.0}))
        s["aspen_schema"] = {"components": [{"aspen_id": "METHY-01", "formula": "C7H14", "name": "methylcyclohexane",
                                              "notes": comp_notes}]}
        return s

    def test_abbreviation_from_the_component_notes_links_stream_and_component(self):
        with mock.patch.dict(os.environ, ON):
            c = klass(self._s("Cold MCH", "LOHC feed component, abbreviated MCH in the paper."))["S1.z"]
        self.assertEqual(c["class"], "NAME_IMPLIED")

    def test_component_name_in_the_stream_name(self):
        with mock.patch.dict(os.environ, ON):
            c = klass(self._s("methylcyclohexane tank", ""))["S1.z"]
        self.assertEqual(c["class"], "NAME_IMPLIED")

    def test_unrelated_stream_name_is_not_implied(self):
        with mock.patch.dict(os.environ, ON):
            c = klass(self._s("Cold liquid", "LOHC feed component, abbreviated MCH in the paper."))["S1.z"]
        self.assertEqual(c["class"], "ASSUMED")


class RatioDerived(unittest.TestCase):
    QUOTE = "A steam-to-carbon ratio of 3.0 was used."

    def test_flow_taken_from_a_stated_ratio_is_not_asked(self):
        s = schema(feed("S1", "H2O feed", z={"WATER": 1.0}, flow=V(3, "inferred", self.QUOTE)))
        with mock.patch.dict(os.environ, ON):
            cl = ep.classify_schema(s, self.QUOTE)
            row = {r["id"]: r for r in cl["entries"]}["S1.flow"]
            asked = [r["id"] for r in ep.select_questions(cl, 5)["asked"]]
        self.assertEqual(row["class"], "RATIO_DERIVED")
        self.assertNotIn("S1.flow", asked)

    def test_a_plain_flow_quote_is_not_a_ratio(self):
        q = "The water feed is 3 kg/h."
        s = schema(feed("S1", "H2O feed", z={"WATER": 1.0}, flow=V(3, "inferred", q)))
        with mock.patch.dict(os.environ, ON):
            row = {r["id"]: r for r in ep.classify_schema(s, q)["entries"]}["S1.flow"]
        self.assertNotEqual(row["class"], "RATIO_DERIVED")

    def test_it_stays_visible_in_the_assumption_ledger(self):
        s = schema(feed("S1", "H2O feed", z={"WATER": 1.0}, flow=V(3, "inferred", self.QUOTE)))
        with mock.patch.dict(os.environ, ON):
            cl = ep.classify_schema(s, self.QUOTE)
            led = ep.assumption_ledger(cl, ep.select_questions(cl, 5))
        self.assertIn("S1.flow", [r["id"] for r in led["not_stated"]])
        self.assertIn("스트림", ep.format_assumption_summary(led) + "스트림")      # formatting does not raise


class ValidatorIntegration(unittest.TestCase):
    def _reactor_schema(self, t):
        b = {"id": "B1", "type": "RGIBBS", "name": "R-101",
             "specs": {"T_C": t, "P_bar": V(20, "paper", "pressure of 20 bar in the reformer")}}
        return {"flowsheet_schema": {"blocks": [b], "streams": [], "connectivity": {"edges": []}}, "io": {"feeds": []}}

    def test_borrowed_value_is_promoted_to_borrowed_suspect(self):
        paper = "Natural gas is preheated to 650 °C in the fired heater before it enters the reformer. The reformer pressure is 20 bar."
        s = self._reactor_schema(V(650, "inferred"))
        with mock.patch.dict(os.environ, ON):
            row = {r["id"]: r for r in ep.classify_schema(s, paper)["entries"]}["B1.T_C"]
        self.assertEqual(row["class"], "BORROWED_SUSPECT")
        self.assertIn("v2_borrowed_from_other_equipment", row["flags"])

    def test_text_supported_candidate_is_not_asked(self):
        paper = "The reformer operates at 650 °C. The reformer pressure is 20 bar."
        s = self._reactor_schema(V(650, "default"))                 # a `default` label is a candidate class (ASSUMED)
        base = {r["id"]: r for r in ep.classify_schema(s, paper, apply_v2=False)["entries"]}["B1.T_C"]
        with mock.patch.dict(os.environ, ON):
            row = {r["id"]: r for r in ep.classify_schema(s, paper)["entries"]}["B1.T_C"]
        self.assertIn(base["class"], ep.CANDIDATE_CLASSES)
        self.assertEqual(row["class"], "STATED")
        self.assertTrue(any(f.startswith("v2_supported_by_text") for f in row["flags"]))

    def test_validator_does_not_recurse_when_the_flag_is_on(self):
        s = self._reactor_schema(V(650, "inferred"))
        with mock.patch.dict(os.environ, ON):
            rep = sv.validate_schema(s, "The reformer operates at 650 °C.")      # would raise RecursionError otherwise
        self.assertEqual(rep["summary"]["total_claims"], 2)

    def test_relabel_does_not_modify_its_input(self):
        s = self._reactor_schema(V(650, "inferred"))
        paper = "Natural gas is preheated to 650 °C in the fired heater before it enters the reformer."
        base = ep.classify_schema(s, paper, apply_v2=False)
        before = copy.deepcopy(base)
        v2.relabel(s, base, paper)
        self.assertEqual(base, before)


if __name__ == "__main__":
    unittest.main()
