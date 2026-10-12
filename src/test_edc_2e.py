"""EDC-2e step 1: the classifier consumes the structured evidence the Part1 prompt asks for
(alternatives, quote_scope, derivation, z provenance, property-method note).  Synthetic data only."""
import unittest

import edc_policy as ep
from test_edc_policy import V, rgibbs, schema

PAPER = (
    "Case I uses 3.24 kg h-1 of hydrogen. Case II uses 3.73 kg h-1 of hydrogen. "
    "In the base configuration the reactor operates at 300 °C and 30 bar. "
    "Later methanation configurations heat the gas mixture to 330 °C before the reactor. "
    "The syngas contains CO 0.159, CO2 0.0916 and H2 0.122 by mole fraction (nitrogen is the balance). "
    "The feed flow is 10 kmol/h and the split ratio is 0.25."
)


def one(sch, rid, paper=PAPER):
    out = ep.classify_schema(sch, paper)
    return {r["id"]: r for r in out["entries"]}[rid], out


def feed(flow=None, z=None, zp=None, t=None):
    specs = {"T_C": t or V(25, "paper", "reactor operates at 300 °C"),
             "P_bar": V(30, "paper", "30 bar"),
             "flow": flow or V(10, "paper", "The feed flow is 10 kmol/h")}
    specs["z"] = z or {"CO": 0.159, "CO2": 0.0916, "H2": 0.122}
    if zp:
        specs["z_provenance"] = zp
    return {"stream": "S1", "specs": specs}


class Alternatives(unittest.TestCase):
    def node(self, alts):
        n = V(3.24, "paper", "Case I uses 3.24 kg h-1 of hydrogen")
        n["alternatives"] = alts
        return n

    def test_two_verified_values_become_alternative(self):
        n = self.node([{"value": 3.24, "quote": "Case I uses 3.24 kg h-1 of hydrogen"},
                       {"value": 3.73, "quote": "Case II uses 3.73 kg h-1 of hydrogen"}])
        row, _ = one(schema([], feeds=[feed(flow=n)]), "S1.flow")
        self.assertEqual(row["class"], "ALTERNATIVE")
        self.assertEqual(sorted(row["alternatives_verified"]), [3.24, 3.73])

    def test_invented_alternative_is_not_counted(self):
        n = self.node([{"value": 3.24, "quote": "Case I uses 3.24 kg h-1 of hydrogen"},
                       {"value": 9.99, "quote": "Case III uses 9.99 kg h-1 of hydrogen"}])
        row, _ = one(schema([], feeds=[feed(flow=n)]), "S1.flow")
        self.assertEqual(row["class"], "STATED")
        self.assertEqual(row["alternatives_verified"], [3.24])

    def test_quote_that_does_not_contain_its_value_is_not_counted(self):
        n = self.node([{"value": 3.24, "quote": "Case I uses 3.24 kg h-1 of hydrogen"},
                       {"value": 5.0, "quote": "Case II uses 3.73 kg h-1 of hydrogen"}])
        row, _ = one(schema([], feeds=[feed(flow=n)]), "S1.flow")
        self.assertEqual(row["class"], "STATED")

    def test_duplicate_values_count_once(self):
        n = self.node([{"value": 3.24, "quote": "Case I uses 3.24 kg h-1 of hydrogen"},
                       {"value": 3.24, "quote": "Case I uses 3.24 kg h-1 of hydrogen"}])
        row, _ = one(schema([], feeds=[feed(flow=n)]), "S1.flow")
        self.assertEqual(row["class"], "STATED")

    def test_alternative_is_asked_even_at_med_impact(self):
        n = V(25, "paper", "reactor operates at 300 °C")      # MED impact variable (feed T_C)
        n["value"] = 300
        n["alternatives"] = [{"value": 300, "quote": "the reactor operates at 300 °C"},
                             {"value": 330, "quote": "heat the gas mixture to 330 °C"}]
        sch = schema([], feeds=[feed(t=n)])
        res = ep.policy_review_items(sch, PAPER)
        self.assertIn("S1.T_C", [i["edc_id"] for i in res["items"]])
        q = ep.format_question(res["selected"])
        self.assertIn("ALTERNATIVE", q)
        self.assertIn("후보: 300, 330", q)

    def test_user_value_is_never_overridden(self):
        n = V(3.24, "user", "x")
        n["alternatives"] = [{"value": 3.24, "quote": "Case I uses 3.24 kg h-1 of hydrogen"},
                             {"value": 3.73, "quote": "Case II uses 3.73 kg h-1 of hydrogen"}]
        row, _ = one(schema([], feeds=[feed(flow=n)]), "S1.flow")
        self.assertEqual(row["class"], "USER")

    def test_without_the_new_fields_nothing_changes(self):
        row, _ = one(schema([], feeds=[feed(flow=V(3.24, "paper", "Case I uses 3.24 kg h-1 of hydrogen"))]),
                     "S1.flow")
        self.assertEqual(row["class"], "STATED")
        self.assertNotIn("alternatives_verified", row)


class QuoteScope(unittest.TestCase):
    def reactor(self, scope):
        n = V(330, "paper", "Later methanation configurations heat the gas mixture to 330 °C before the reactor")
        if scope:
            n["quote_scope"] = scope
        return rgibbs(n)

    def test_other_config_turns_a_stated_value_into_a_question(self):
        sch = schema([self.reactor("other_config")])
        row, _ = one(sch, "B1.T_C")
        self.assertEqual(row["class"], "BORROWED_SUSPECT")
        self.assertIn("declared_scope_other_config", row["flags"])
        self.assertIn("B1.T_C", [i["edc_id"] for i in ep.policy_review_items(sch, PAPER)["items"]])

    def test_sweep_range_scope(self):
        row, _ = one(schema([self.reactor("sweep_range")]), "B1.T_C")
        self.assertEqual(row["class"], "SWEPT")

    def test_this_unit_and_missing_scope_do_not_change_anything(self):
        for scope in ("this_unit", "general", None):
            row, _ = one(schema([self.reactor(scope)]), "B1.T_C")
            self.assertEqual(row["class"], "STATED", scope)

    def test_declared_scope_never_removes_a_question(self):
        n = V(300, "inferred", "reactor operates at 300 °C")      # BORROWED_SUSPECT already
        n["quote_scope"] = "this_unit"
        row, _ = one(schema([rgibbs(n)]), "B1.T_C")
        self.assertEqual(row["class"], "BORROWED_SUSPECT")


class Derivation(unittest.TestCase):
    def test_recomputed_value_becomes_derived(self):
        sch = schema([rgibbs(V(300, "paper", "reactor operates at 300 °C"))],
                     feeds=[{"stream": "S1", "specs": {
                         "T_C": V(25, "paper", "reactor operates at 300 °C"), "P_bar": V(30, "paper", "30 bar"),
                         "flow": V(10, "paper", "The feed flow is 10 kmol/h"), "z": {"CO": 1.0}}},
                         {"stream": "S2", "specs": {
                             "T_C": V(25, "paper", "reactor operates at 300 °C"), "P_bar": V(30, "paper", "30 bar"),
                             "flow": {**V(2.5, "llm"), "derivation": {"formula": "f*r", "inputs": {"f": "S1.flow", "r": "S1.P_bar"}}},
                             "z": {"CO": 1.0}}}])
        # 10 * 30 = 300 != 2.5 -> mismatch (llm source: stays ASSUMED, but the check is recorded)
        row, _ = one(sch, "S2.flow")
        self.assertEqual(row["derivation_check"]["status"], "mismatch")
        self.assertEqual(row["class"], "ASSUMED")
        sch["io"]["feeds"][1]["specs"]["flow"] = {**V(300.0, "llm"), "derivation": {
            "formula": "f*r", "inputs": {"f": "S1.flow", "r": "S1.P_bar"}}}
        row, _ = one(sch, "S2.flow")
        self.assertEqual(row["derivation_check"]["status"], "ok")
        self.assertEqual(row["class"], "DERIVED")
        self.assertIn("derived_by_calculation", row["flags"])

    def test_constants_must_be_free_or_in_the_paper(self):
        mk = lambda formula: {"stream": "S2", "specs": {
            "T_C": V(25, "paper", "reactor operates at 300 °C"), "P_bar": V(30, "paper", "30 bar"),
            "flow": {**V(4.2, "llm"), "derivation": {"formula": formula, "inputs": {"f": "S1.flow"}}},
            "z": {"CO": 1.0}}}
        base = [{"stream": "S1", "specs": {"T_C": V(25, "paper", "reactor operates at 300 °C"),
                                           "P_bar": V(30, "paper", "30 bar"),
                                           "flow": V(10, "paper", "The feed flow is 10 kmol/h"), "z": {"CO": 1.0}}}]
        row, _ = one(schema([], feeds=base + [mk("f*0.42")]), "S2.flow")
        self.assertEqual(row["derivation_check"]["status"], "constant_not_in_paper")
        self.assertNotEqual(row["class"], "DERIVED")
        row, _ = one(schema([], feeds=base + [mk("f*0.25+1.7")]), "S2.flow")       # 1.7 invented
        self.assertNotEqual(row["class"], "DERIVED")

    def test_unresolved_and_malformed(self):
        ctx = ep.PaperContext(PAPER)
        ctx.values = {"S1.flow": 10.0}
        f = lambda formula, inputs, v=5.0: ep.check_derivation(
            {"value": v, "derivation": {"formula": formula, "inputs": inputs}}, ctx)["status"]
        self.assertEqual(f("a/2", {"a": "S9.flow"}), "unresolved")
        self.assertEqual(f("a/2", {"a": "S1.flow"}), "ok")
        self.assertEqual(f("__import__('os')", {}), "malformed")
        self.assertEqual(f("a/0", {"a": "S1.flow"}), "malformed")
        self.assertEqual(f("a**99", {"a": "S1.flow"}), "malformed")
        self.assertEqual(f("a +", {"a": "S1.flow"}), "malformed")
        self.assertEqual(ep.check_derivation({"value": 5, "derivation": "S1.flow/2"}, ctx)["status"], "malformed")


class Composition(unittest.TestCase):
    ZP = {"source": "paper", "confidence": 0.9,
          "evidence": {"doc_id": "paper_text", "locator": "x",
                       "quote": "The syngas contains CO 0.159, CO2 0.0916 and H2 0.122 by mole fraction"}}

    def test_z_without_provenance_is_unchanged(self):
        row, _ = one(schema([], feeds=[feed()]), "S1.z")
        self.assertEqual(row["class"], "NO_PROVENANCE")

    def test_z_with_supporting_quote_is_stated(self):
        row, _ = one(schema([], feeds=[feed(zp=self.ZP)]), "S1.z")
        self.assertEqual(row["class"], "STATED")

    def test_z_given_as_percentages_in_the_quote_is_stated(self):
        zp = {"source": "paper", "confidence": 0.9, "evidence": {"doc_id": "paper_text", "locator": "Table 1", "quote":
              "Syngas composition adopted in the simulation. Component [v/v %] d.b. CO 15.9 CO2 9.16 H2 12.2"}}
        paper = PAPER + " Syngas composition adopted in the simulation. Component [v/v %] d.b. CO 15.9 CO2 9.16 H2 12.2"
        row, _ = one(schema([], feeds=[feed(zp=zp)]), "S1.z", paper)
        self.assertEqual(row["class"], "STATED")

    def test_z_with_a_fraction_the_quote_does_not_contain(self):
        row, _ = one(schema([], feeds=[feed(z={"CO": 0.159, "CO2": 0.0916, "H2": 0.2}, zp=self.ZP)]), "S1.z")
        self.assertEqual(row["class"], "UNSUPPORTED_PAPER_CLAIM")

    def test_zero_fractions_need_no_support(self):
        z = {"CO": 0.159, "CO2": 0.0916, "H2": 0.122, "CARBON": 0.0}
        row, _ = one(schema([], feeds=[feed(z=z, zp=self.ZP)]), "S1.z")
        self.assertEqual(row["class"], "STATED")

    def test_assumed_z_is_asked_but_cannot_be_overwritten_by_a_number(self):
        zp = {"source": "llm", "confidence": 0.4, "rationale": "balance assumed"}
        sch = schema([], feeds=[feed(zp=zp)])
        res = ep.policy_review_items(sch, PAPER)
        self.assertIn("S1.z", [i["edc_id"] for i in res["items"]])
        self.assertIn("조성은 `ok`만 가능", ep.format_question(res["selected"]))
        out = ep.apply_overrides(sch, [{"id": "S1.z", "value": 1.0, "text": "S1.z=1"}], res["classified"])
        self.assertEqual(out["applied"], [])
        self.assertIsInstance(sch["io"]["feeds"][0]["specs"]["z"], dict)


class PropertyMethod(unittest.TestCase):
    def test_substitution_is_recorded_in_the_ledger(self):
        sch = schema([rgibbs(V(300, "paper", "reactor operates at 300 °C"))], feeds=[feed()])
        sch["aspen_schema"] = {"property_method": "PR-BM",
                               "property_method_note": {"paper_method": "PR", "used_method": "PR-BM",
                                                        "reason": "PR-BM is the Aspen default for hydrocarbons"}}
        res = ep.classify_schema(sch, PAPER)
        self.assertEqual(res["property_method_substitution"]["used_method"], "PR-BM")
        led = ep.assumption_ledger(res, {"asked": [], "overflow": []})
        self.assertEqual(led["property_method_substitution"]["paper_method"], "PR")

    def test_same_method_or_no_note_records_nothing(self):
        sch = schema([rgibbs(V(300, "paper", "reactor operates at 300 °C"))], feeds=[feed()])
        self.assertNotIn("property_method_substitution", ep.classify_schema(sch, PAPER))
        sch["aspen_schema"] = {"property_method_note": {"paper_method": "pr", "used_method": "PR"}}
        self.assertNotIn("property_method_substitution", ep.classify_schema(sch, PAPER))


class Resolutions(unittest.TestCase):
    """rules added after the first scoring: the quote itself can settle an alternative; z stated in a table."""
    P = ("The syngas temperature is 47.6 °C as measured. Nevertheless, the syngas temperature is set to 25 °C in the following sections. "
         "In Case I the temperature is set to 300 °C. In Case II the temperature is set to 330 °C. "
         "Case I uses 3.24 kg h-1 of hydrogen. Case II uses 3.73 kg h-1 of hydrogen. "
         "Syngas composition adopted in the simulation. Component [v/v %] CO 15.9 CO2 9.16 H2 12.2")

    def t(self, quote, value=25):
        n = V(value, "paper", quote)
        n["alternatives"] = [{"value": 47.6, "quote": "The syngas temperature is 47.6 °C as measured"},
                             {"value": value, "quote": quote}]
        return one(schema([], feeds=[feed(t=n)]), "S1.T_C", self.P)[0]

    def test_a_quote_that_fixes_the_value_resolves_the_alternatives(self):
        row = self.t("Nevertheless, the syngas temperature is set to 25 °C in the following sections")
        self.assertEqual(row["class"], "STATED")
        self.assertIn("alternatives_resolved_by_quote", row["flags"])
        self.assertEqual(sorted(row["alternatives_verified"]), [25.0, 47.6])      # still on record

    def test_a_quote_tied_to_one_case_does_not(self):
        n = V(300, "paper", "In Case I the temperature is set to 300 °C")
        n["alternatives"] = [{"value": 300, "quote": "In Case I the temperature is set to 300 °C"},
                             {"value": 330, "quote": "In Case II the temperature is set to 330 °C"}]
        row = one(schema([], feeds=[feed(t=n)]), "S1.T_C", self.P)[0]
        self.assertEqual(row["class"], "ALTERNATIVE")

    def test_a_quote_without_commitment_wording_does_not(self):
        n = V(3.24, "paper", "Case I uses 3.24 kg h-1 of hydrogen")
        n["alternatives"] = [{"value": 3.24, "quote": "Case I uses 3.24 kg h-1 of hydrogen"},
                             {"value": 3.73, "quote": "Case II uses 3.73 kg h-1 of hydrogen"}]
        self.assertEqual(one(schema([], feeds=[feed(flow=n)]), "S1.flow", self.P)[0]["class"], "ALTERNATIVE")

    def test_commitment_detector(self):
        for q in ("is set to 25 °C", "was kept constant at 1 bar", "have been chosen", "the value adopted here"):
            self.assertTrue(ep._commits_to_value(q), q)
        for q in ("In Case II it is set to 3 bar", "varied between 100 and 1000", "heated to 330 °C", None, ""):
            self.assertFalse(ep._commits_to_value(q), q)

    def zp(self, quote):
        return {"source": "paper", "confidence": 0.9, "evidence": {"doc_id": "paper_text", "locator": "Table 1", "quote": quote}}

    def test_z_given_in_a_table_but_cited_by_its_caption(self):
        z = {"CO": 0.159, "CO2": 0.0916, "H2": 0.122}
        row = one(schema([], feeds=[feed(z=z, zp=self.zp("Syngas composition adopted in the simulation"))]),
                  "S1.z", self.P)[0]
        self.assertEqual(row["class"], "STATED_ELSEWHERE")

    def test_z_not_in_the_paper_stays_unsupported(self):
        z = {"CO": 0.159, "CO2": 0.0916, "H2": 0.5}
        row = one(schema([], feeds=[feed(z=z, zp=self.zp("Syngas composition adopted in the simulation"))]),
                  "S1.z", self.P)[0]
        self.assertEqual(row["class"], "UNSUPPORTED_PAPER_CLAIM")

    def test_pure_stream_never_gets_the_pass(self):
        paper = self.P + " The recycle ratio was 1.0."
        row = one(schema([], feeds=[feed(z={"H2": 1.0}, zp=self.zp("Syngas composition adopted in the simulation"))]),
                  "S1.z", paper)[0]
        self.assertNotEqual(row["class"], "STATED_ELSEWHERE")


class FoundOnCaseB(unittest.TestCase):
    """two classifier errors seen on the methanol-aromatization run (units, MinerU LaTeX digits)."""
    def test_same_quantity_in_another_unit_is_not_an_alternative(self):
        paper = "Input (kg/h) Methanol 303823.76. The processing capacity of raw methanol is 303.8 ton/h."
        n = V(303823.76, "paper", "Input (kg/h) Methanol 303823.76")
        n["alternatives"] = [{"value": 303823.76, "quote": "Input (kg/h) Methanol 303823.76"},
                             {"value": 303.8, "quote": "The processing capacity of raw methanol is 303.8 ton/h"}]
        row = one(schema([], feeds=[feed(flow=n)]), "S1.flow", paper)[0]
        self.assertEqual(row["class"], "STATED")
        self.assertEqual(row["alternatives_verified"], [303823.76])

    def test_real_alternatives_are_still_alternatives(self):
        self.assertTrue(ep._same_quantity(303823.76, 303.8))
        self.assertTrue(ep._same_quantity(60.0, 1.0))                  # per hour vs per minute
        for a, b in ((3.24, 3.73), (0.0, 3.24), (1.0, 15.0), (25.0, 47.6), (300.0, 330.0)):
            self.assertFalse(ep._same_quantity(a, b), (a, b))

    def test_spaced_latex_digits(self):
        q = r"preheat the feed to $4 0 0 ~ ^ { \circ } \mathrm { C }$ ."
        self.assertEqual(ep.parse_quote(q)[0], [400.0])
        self.assertEqual(ep.parse_quote(r"cooled to $4 0 ~ ^ { \circ } \mathrm { C }$ (E0102)")[0], [40.0])
        self.assertEqual(ep.parse_quote("to $2 . 4$ bar")[0], [2.4])
        self.assertEqual(ep.parse_quote("Table 1 2 3 4")[0], [1.0, 2.0, 3.0, 4.0])      # outside math: untouched

    def test_value_in_a_latex_quote_is_stated(self):
        paper = r"the remaining heat is used to preheat the feed to $4 0 0 ~ ^ { \circ } \mathrm { C }$ ."
        n = V(400, "paper", r"the remaining heat is used to preheat the feed to $4 0 0 ~ ^ { \circ } \mathrm { C }$ .")
        row = one(schema([rgibbs(n)]), "B1.T_C", paper)[0]
        self.assertEqual(row["class"], "STATED")


if __name__ == "__main__":
    unittest.main()
