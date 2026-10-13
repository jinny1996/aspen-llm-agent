"""Unit tests for edc_policy (synthetic schemas/papers modelled on cases seen in real runs)."""
import unittest

import edc_policy as ep

PAPER = (
    "The operating conditions of 923 K, 30 bar and S/C of 3.0 have been chosen. "
    "Block\tComponent\tTemperature (K)\tPressure (bar)\nM\tFeed Mixture\t298\t5–40\n"
    "R1\tGibbs Reactor\t773–1473\t5–40\n"
    "In this scenario, the temperature was varied between 100 and 1000 °C as the independent variable. "
    "The syngas temperature is set to 25 °C in the following sections. "
    "Stoichiometric conditions (3.24 kg h−1): the hydrogen mass flow rate was adjusted."
)


def V(value, source="paper", quote=None, conf=0.9):
    n = {"value": value, "source": source, "confidence": conf}
    if quote is not None:
        n["evidence"] = {"doc_id": "paper_text", "locator": "x", "quote": quote}
    return n


def schema(blocks=None, feeds=None, streams=None):
    # dof_ledger only evaluates feeds that are declared as role=FEED streams
    streams = streams if streams is not None else [{"id": f["stream"], "role": "FEED"} for f in (feeds or [])]
    return {"flowsheet_schema": {"blocks": blocks or [], "streams": streams,
                                 "connectivity": {"edges": []}},
            "io": {"feeds": feeds or []}}


def classes(sch, paper=PAPER):
    out = ep.classify_schema(sch, paper)
    return {r["id"]: r for r in out["entries"]}


def rgibbs(t_node, p_node=None):
    return {"id": "B1", "type": "RGIBBS",
            "specs": {"T_C": t_node, "P_bar": p_node or V(30, "paper", "30 bar")}}


class Parsing(unittest.TestCase):
    def test_range_removed_from_numbers(self):
        nums, ranges, cue = ep.parse_quote("30 bar; E2 pressure 5–40 bar")
        self.assertEqual(nums, [30.0])
        self.assertEqual(ranges, [(5.0, 40.0)])

    def test_equipment_tags_do_not_leak_numbers(self):
        nums, ranges, _ = ep.parse_quote("R1 Gibbs Reactor 773–1473 K, P0101, E2, O2/CH4")
        self.assertEqual(nums, [])
        self.assertEqual(ranges, [(773.0, 1473.0)])

    def test_unit_exponent_is_not_a_number(self):
        nums, _, _ = ep.parse_quote("Stoichiometric conditions (3.24 kg h−1)")
        self.assertEqual(nums, [3.24])

    def test_between_and_cue(self):
        nums, ranges, cue = ep.parse_quote("temperature was varied between 100 and 1000 °C")
        self.assertEqual((nums, ranges, cue), ([], [(100.0, 1000.0)], True))

    def test_support_kinds(self):
        self.assertEqual(ep.value_support(30, [30.0], []), "identity")
        self.assertEqual(ep.value_support(649.85, [923.0], []), "conversion")      # K -> degC
        self.assertEqual(ep.value_support(1.01325, [1.0], []), "conversion")       # atm -> bar
        self.assertEqual(ep.value_support(0.45, [45.0], []), "conversion")         # % -> fraction
        self.assertEqual(ep.value_support(100, [], [(100.0, 1000.0)]), "range_endpoint")
        self.assertIsNone(ep.value_support(300, [], [(100.0, 1000.0)]))
        self.assertIsNone(ep.value_support("AUTO", [1.0], []))


class Classes(unittest.TestCase):
    def test_stated_and_derived(self):
        c = classes(schema([rgibbs(
            V(649.85, "paper", "The operating conditions of 923 K, 30 bar and S/C of 3.0 have been chosen"),
            V(30, "paper", "The operating conditions of 923 K, 30 bar and S/C of 3.0 have been chosen"))]))
        self.assertEqual(c["B1.T_C"]["class"], "DERIVED")
        self.assertEqual(c["B1.P_bar"]["class"], "STATED")

    def test_swept_reactor_temperature_with_non_supporting_quote(self):
        q = "the temperature was varied between 100 and 1000 °C as the independent variable"
        c = classes(schema([rgibbs(V(300, "inferred", q, 0.45))]))
        self.assertEqual(c["B1.T_C"]["class"], "SWEPT")          # the case that slipped through

    def test_paper_label_with_range_only_quote_is_swept(self):
        # v0 behaviour (paper-level pass switched off); v1 behaviour is in StatedElsewhere below
        out = ep.classify_schema(schema([rgibbs(V(649.85, "paper", "R1 Gibbs Reactor 773–1473 K"))]),
                                 PAPER, stated_elsewhere=False)
        self.assertEqual({r["id"]: r["class"] for r in out["entries"]}["B1.T_C"], "SWEPT")

    def test_borrowed_suspect_when_inferred_value_sits_in_quote(self):
        feed = {"stream": "S1", "specs": {"T_C": V(25, "inferred",
                "The syngas temperature is set to 25 °C in the following sections", 0.65),
                "P_bar": V(1, "default"), "flow": V(3.24, "paper"), "z": {"H2": 1.0}}}
        c = classes(schema(feeds=[feed]))
        self.assertEqual(c["S1.T_C"]["class"], "BORROWED_SUSPECT")

    def test_commentary_quote_is_assumed_not_supported(self):
        feed = {"stream": "S1", "specs": {
            "T_C": V(25, "default", "Feed temperature not explicitly reported", 0.2),
            "P_bar": V(1, "default"), "flow": V(1, "inferred"), "z": {"H2": 1.0}}}
        c = classes(schema(feeds=[feed]))
        self.assertEqual(c["S1.T_C"]["class"], "ASSUMED")
        self.assertEqual(c["S1.P_bar"]["class"], "ASSUMED")       # no quote at all

    def test_paper_claim_without_real_quote(self):
        c = classes(schema([rgibbs(V(500, "paper", "this sentence was never in the paper at all"))]))
        r = c["B1.T_C"]
        self.assertEqual(r["class"], "UNSUPPORTED_PAPER_CLAIM")
        self.assertIn("no_verifiable_quote", r["flags"])

    def test_paper_claim_with_real_quote_that_lacks_the_value(self):
        c = classes(schema([rgibbs(V(999, "paper", "The syngas temperature is set to 25 °C"))]))
        r = c["B1.T_C"]
        self.assertEqual(r["class"], "UNSUPPORTED_PAPER_CLAIM")
        self.assertIn("value_not_in_quote", r["flags"])

    def test_table_header_row_merge_is_accepted_for_paper_claims(self):
        feed = {"stream": "S1", "specs": {
            "T_C": V(24.85, "paper", "M Feed Mixture Temperature 298 K"),
            "P_bar": V(30, "paper", "30 bar"), "flow": V(1, "paper", "S/C of 3.0"),
            "z": {"CH4": 1.0}}}
        r = classes(schema(feeds=[feed]))["S1.T_C"]
        self.assertEqual(r["class"], "DERIVED")
        self.assertIn("table_merge_quote", r["flags"])

    def test_composition_has_no_provenance(self):
        feed = {"stream": "S1", "specs": {"T_C": V(25, "paper", "set to 25 °C"), "P_bar": V(30, "paper", "30 bar"),
                                          "flow": V(3.24, "paper", "3.24 kg h−1"), "z": {"H2": 1.0}}}
        self.assertEqual(classes(schema(feeds=[feed]))["S1.z"]["class"], "NO_PROVENANCE")

    def test_confidence_does_not_change_the_class(self):
        q = "the temperature was varied between 100 and 1000 °C as the independent variable"
        lo = classes(schema([rgibbs(V(300, "inferred", q, 0.10))]))["B1.T_C"]["class"]
        hi = classes(schema([rgibbs(V(300, "inferred", q, 0.99))]))["B1.T_C"]["class"]
        self.assertEqual(lo, hi)

class StatedElsewhere(unittest.TestCase):
    def test_range_only_quote_but_value_stated_elsewhere_in_paper(self):
        # quote is only the table range; the chosen 30 bar is in the first sentence of PAPER
        sch = schema([rgibbs(V(649.85, "paper", "R1 Gibbs Reactor 773–1473 K"),
                             V(30, "paper", "Pressure (bar) 5–40"))])
        c = classes(sch)
        self.assertEqual(c["B1.P_bar"]["class"], "STATED_ELSEWHERE")
        self.assertIn("quote_does_not_support_value", c["B1.P_bar"]["flags"])
        self.assertEqual(c["B1.T_C"]["class"], "STATED_ELSEWHERE")        # 923 K -> 649.85 degC, stated in PAPER
        off = ep.classify_schema(sch, PAPER, stated_elsewhere=False)
        self.assertEqual({r["id"]: r["class"] for r in off["entries"]}["B1.P_bar"], "SWEPT")

    def test_value_nowhere_in_paper_stays_a_candidate(self):
        c = classes(schema([rgibbs(V(777, "paper", "Pressure (bar) 5–40"))]))
        self.assertEqual(c["B1.T_C"]["class"], "SWEPT")

    def test_inferred_values_never_get_the_pass(self):
        # 30 is stated in PAPER, but an inferred value with a range quote must stay SWEPT
        c = classes(schema([rgibbs(V(30, "inferred", "Pressure (bar) 5–40"))]))
        self.assertEqual(c["B1.T_C"]["class"], "SWEPT")

    def test_not_a_question_candidate(self):
        self.assertNotIn("STATED_ELSEWHERE", ep.CANDIDATE_CLASSES)


class Questions(unittest.TestCase):
    def build(self):
        blocks = [
            {"id": "B1", "type": "RGIBBS", "specs": {"T_C": V(300, "inferred", None, 0.9),   # HIGH + ASSUMED
                                                    "P_bar": V(30, "paper", "30 bar")}},
            {"id": "B2", "type": "PUMP", "specs": {"P_out_bar": V(2, "default", None, 0.2),    # MED + ASSUMED
                                                  "eff": V(0.75, "default", None, 0.2)}},      # LOW + ASSUMED
        ]
        feed = {"stream": "S1", "specs": {
            "T_C": V(25, "inferred", "The syngas temperature is set to 25 °C", 0.9),      # MED + BORROWED
            "P_bar": V(30, "paper", "30 bar"), "flow": V(3.24, "paper", "3.24 kg h−1"),
            "z": {"H2": 1.0}}}
        return schema(blocks, [feed])

    def test_selection_rule(self):
        sel = ep.select_questions(ep.classify_schema(self.build(), PAPER))
        ids = [r["id"] for r in sel["asked"]]
        self.assertIn("B1.T_C", ids)                 # HIGH assumed
        self.assertIn("S1.T_C", ids)                 # MED but BORROWED_SUSPECT
        self.assertNotIn("B2.eff", ids)              # LOW
        self.assertNotIn("S1.flow", ids)             # STATED

    def test_med_assumed_is_asked_only_when_the_cap_has_room(self):
        sch = self.build()
        roomy = [r["id"] for r in ep.select_questions(ep.classify_schema(sch, PAPER), k=5)["asked"]]
        self.assertIn("B2.P_out_bar", roomy)         # MED + ASSUMED fills the unused budget
        full = [r["id"] for r in ep.select_questions(ep.classify_schema(sch, PAPER), k=2)["asked"]]
        self.assertEqual(sorted(full), ["B1.T_C", "S1.T_C"])      # cap used up by the stronger candidates
        self.assertNotIn("B2.P_out_bar", full)
        self.assertNotIn("B2.P_out_bar", [r["id"] for r in ep.select_questions(
            ep.classify_schema(sch, PAPER), k=5)["overflow"]])     # not an overflow item either

    def test_priority_and_cap(self):
        sel = ep.select_questions(ep.classify_schema(self.build(), PAPER), k=1)
        self.assertEqual(len(sel["asked"]), 1)
        self.assertEqual(sel["asked"][0]["id"], "S1.T_C")      # BORROWED outranks ASSUMED
        self.assertEqual([r["id"] for r in sel["overflow"]], ["B1.T_C"])

    def test_message_shows_items_and_reply_syntax(self):
        msg = ep.format_question(ep.select_questions(ep.classify_schema(self.build(), PAPER)))
        self.assertIn("B1.T_C = 300", msg)
        self.assertIn("`ok`", msg)
        self.assertIn("B2.T_C=350", msg)

    def test_nothing_to_ask_gives_empty_message(self):
        sch = schema([{"id": "B1", "type": "RGIBBS", "specs": {
            "T_C": V(649.85, "paper", "The operating conditions of 923 K, 30 bar and S/C of 3.0 have been chosen"),
            "P_bar": V(30, "paper", "30 bar")}}])
        self.assertEqual(ep.format_question(ep.select_questions(ep.classify_schema(sch, PAPER))), "")

    def test_impact_table(self):
        self.assertEqual(ep.impact("RGIBBS", "T_C"), "HIGH")
        self.assertEqual(ep.impact("MHeatX", "T_out_C[S8]"), "HIGH")
        self.assertEqual(ep.impact("PUMP", "eff"), "LOW")
        self.assertEqual(ep.impact("UNKNOWN", "x"), "MED")


if __name__ == "__main__":
    unittest.main()
