"""Unit tests for edc_claims / edc_semantic_validator (P0-1, report-only).

Synthetic papers use different equipment words and numbers than case B on purpose: the rules must not
be case-B specific.  The case-B fixture tests are in class CaseB and prove only that the original
motivating example is caught -- not that the rules generalise."""
import copy
import hashlib
import unittest
from pathlib import Path

import edc_claims
import edc_semantic_validator as sv

HERE = Path(__file__).parent


def V(value, source="paper", quote=None, rationale=None):
    n = {"value": value, "source": source, "confidence": 0.8}
    if quote is not None:
        n["evidence"] = {"doc_id": "paper_text", "locator": "x", "quote": quote}
    if rationale:
        n["rationale"] = rationale
    return n


def schema(blocks, feeds=None):
    feeds = feeds or []
    return {"flowsheet_schema": {"blocks": blocks,
                                 "streams": [{"id": f["stream"], "role": "FEED"} for f in feeds],
                                 "connectivity": {"edges": []}},
            "io": {"feeds": feeds}}


def reactor(t_node, p_node=None, name="R-101"):
    return {"id": "B1", "type": "RGIBBS", "name": name,
            "specs": {"T_C": t_node, "P_bar": p_node or V(20, "paper", "pressure of 20 bar in the reactor")}}


def by_id(rep):
    return {c["id"]: c for c in rep["claims"]}


class Borrowing(unittest.TestCase):
    PAPER = ("Natural gas is preheated to 650 °C in the fired heater before it enters the reformer. "
             "The reformer pressure is 20 bar. The product gas is cooled to 40 °C in the condenser.")

    def test_upstream_condition_given_to_reactor_is_flagged(self):
        s = schema([reactor(V(650, "inferred", rationale="Assumed equal to the inlet temperature."))])
        c = by_id(sv.validate_schema(s, self.PAPER))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "POTENTIAL_CROSS_EQUIPMENT_BORROWING")
        self.assertEqual(c["category"], "EVIDENCE_GAP")

    def test_same_number_with_owner_context_is_supported(self):
        paper = "The reformer operates at 650 °C. The feed is preheated to 500 °C in the fired heater."
        s = schema([reactor(V(650, "paper", "The reformer operates at 650 °C."))])
        c = by_id(sv.validate_schema(s, paper))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_heater_outlet_is_supported_for_a_heater_block(self):
        heater = {"id": "B2", "type": "HEATER", "name": "H-1",
                  "specs": {"spec_mode": "TP", "T_out_C": V(650, "paper", "preheated to 650 °C in the fired heater"),
                            "P_bar": V(20, "paper", "20 bar")}}
        c = by_id(sv.validate_schema(schema([heater]), self.PAPER))["B2.T_out_C"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_location_word_does_not_make_the_reactor_the_subject(self):
        # "reformer" appears in the sentence, but only as a destination
        c = by_id(sv.validate_schema(schema([reactor(V(650, "inferred"))]), self.PAPER))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_equipment_named_before_the_number_only_as_a_location(self):
        paper = "Heat from the gas leaving the reformer is used to warm the feedstock to 650 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(650, "inferred"))]), paper))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "POTENTIAL_CROSS_EQUIPMENT_BORROWING")

    def test_location_noun_closest_to_the_number_is_not_the_subject(self):
        paper = "Steam entering the reformer at 650 °C is mixed with the gas."
        c = by_id(sv.validate_schema(schema([reactor(V(650, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_boundary_condition_after_the_equipment_name_is_not_the_equipment(self):
        paper = "It was decided to cool the reformer exit stream to 330 °C before recycling."
        c = by_id(sv.validate_schema(schema([reactor(V(330, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_entrance_of_the_equipment_is_not_the_equipment(self):
        paper = "The temperature is reduced to 330 °C at the entrance of all four reformers."
        c = by_id(sv.validate_schema(schema([reactor(V(330, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_owner_context_is_not_enough_when_policy_sees_a_sweep(self):
        paper = ("The reformer temperature was varied between 600 and 900 °C. "
                 "In the first run the reformer was operated at 600 °C.")
        q = "The reformer temperature was varied between 600 and 900 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(600, "inferred", q))]), paper))["B1.T_C"]
        self.assertEqual(c["edc_policy_class"], "SWEPT")
        self.assertEqual(c["evidence_relation"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("owner_context_but_multiple_values", c["flags"])

    def test_product_stream_of_the_equipment_is_not_the_equipment(self):
        paper = "The reformer product stream is at 330 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(330, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_exit_temperature_is_not_the_operating_temperature(self):
        paper = "The exit temperature of the reformer is 700 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(700, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_inlet_temperature_is_not_the_reactor_temperature(self):
        paper = "The inlet temperatures of all reformers were set at 330 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(330, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_boundary_word_after_the_number_is_also_seen(self):
        paper = "Inside the reformers the gas is cooled and the temperature is reduced to 330 °C at the entrance of all four reformers."
        c = by_id(sv.validate_schema(schema([reactor(V(330, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_outlet_temperature_supports_an_outlet_variable_but_not_the_operating_one(self):
        paper = "The reformer outlet temperature is 700 °C."
        heater = {"id": "B2", "type": "HEATER", "name": "H-1",
                  "specs": {"spec_mode": "TP", "T_out_C": V(700, "paper", "The heater outlet temperature is 700 °C."),
                            "P_bar": V(20, "paper", "20 bar")}}
        paper_h = "The heater outlet temperature is 700 °C."
        self.assertEqual(by_id(sv.validate_schema(schema([heater]), paper_h))["B2.T_out_C"]["evidence_relation"],
                         "DIRECT_SUPPORT")
        c = by_id(sv.validate_schema(schema([reactor(V(700, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_equipment_named_before_the_number_as_the_subject(self):
        paper = "Inside the reformer, the temperature is held at 650 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(650, "inferred"))]), paper))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_rationale_alone_can_reveal_the_borrowing(self):
        paper = "The plant converts feedstock to product."          # number not in the paper at all
        r = "The feed is preheated to 650 °C before the reactor; that value is reused as the reactor temperature."
        c = by_id(sv.validate_schema(schema([reactor(V(650, "inferred", rationale=r))]), paper))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "POTENTIAL_CROSS_EQUIPMENT_BORROWING")
        self.assertIn("rationale_only", c["flags"])


class ClearErrors(unittest.TestCase):
    def test_pressure_value_that_the_quote_gives_as_temperature(self):
        paper = "The reactor temperature is 30 °C and the pressure is 5 bar."
        b = {"id": "B1", "type": "RGIBBS", "name": "R-1",
             "specs": {"T_C": V(30, "paper", "The reactor temperature is 30 °C"),
                       "P_bar": V(30, "paper", "The reactor temperature is 30 °C")}}
        c = by_id(sv.validate_schema(schema([b]), paper))
        self.assertEqual(c["B1.P_bar"]["evidence_relation"], "WRONG_VARIABLE")
        self.assertEqual(c["B1.P_bar"]["category"], "CLEAR_ERROR")
        self.assertEqual(c["B1.T_C"]["evidence_relation"], "DIRECT_SUPPORT")

    def test_kelvin_number_used_as_celsius(self):
        paper = "The reactor operates at 923 K."
        c = by_id(sv.validate_schema(schema([reactor(V(923, "paper", "The reactor operates at 923 K."))]), paper))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "NUMERIC_OR_UNIT_CONFLICT")

    def test_kelvin_converted_correctly_is_not_a_conflict(self):
        paper = "The reactor operates at 923 K."
        c = by_id(sv.validate_schema(schema([reactor(V(649.85, "paper", "The reactor operates at 923 K."))]), paper))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_mpa_number_used_as_bar(self):
        paper = "The reactor pressure is 3 MPa."
        b = reactor(V(500, "inferred"), V(3, "paper", "The reactor pressure is 3 MPa."))
        c = by_id(sv.validate_schema(schema([b]), paper))["B1.P_bar"]
        self.assertEqual(c["evidence_relation"], "NUMERIC_OR_UNIT_CONFLICT")

    def test_paper_label_with_value_absent_from_its_quote(self):
        paper = "The reactor operates at 700 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(710, "paper", "The reactor operates at 700 °C."))]), paper))["B1.T_C"]
        self.assertEqual(c["category"], "CLEAR_ERROR")

    def test_negative_temperature_is_not_matched_to_positive(self):
        paper = "The reactor is kept at - 25 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(25, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")
        c = by_id(sv.validate_schema(schema([reactor(V(-25, "inferred"))]), paper))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")


class AssumptionsAndUnverifiable(unittest.TestCase):
    def test_declared_default_without_borrowing_signal(self):
        p = V(1.01325, "default", rationale="Not specified in the paper; atmospheric pressure used as placeholder.")
        c = by_id(sv.validate_schema(schema([reactor(V(700, "paper", "reactor at 700 °C"), p)]),
                                     "The reactor is at 700 °C."))["B1.P_bar"]
        self.assertEqual(c["evidence_relation"], "ASSUMPTION_DECLARED")
        self.assertEqual(c["category"], "DECLARED_ASSUMPTION")

    def test_undeclared_inference_is_not_accepted(self):
        c = by_id(sv.validate_schema(schema([reactor(V(777, "inferred"))]), "The reactor is at 700 °C."))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("undeclared", " ".join(c["reasons"]))

    def test_value_without_provenance_is_not_assessed(self):
        b = {"id": "B1", "type": "RGIBBS", "name": "R-1", "specs": {"T_C": 700, "P_bar": 20}}
        rep = sv.validate_schema(schema([b]), "The reactor is at 700 °C and 20 bar.")
        for c in rep["claims"]:
            self.assertEqual(c["evidence_relation"], "NOT_ASSESSED")
            self.assertEqual(c["category"], "UNVERIFIABLE")
        self.assertEqual(rep["summary"]["coverage"], 0.0)

    def test_user_value_is_recorded_not_judged(self):
        c = by_id(sv.validate_schema(schema([reactor(V(425, "user"))]), "x"))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "NOT_ASSESSED")
        self.assertEqual(c["resolution_state"], "HUMAN_REVISED")

    def test_range_only_is_insufficient(self):
        paper = "The reactor temperature was varied between 600 and 900 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(600, "inferred"))]), paper))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "INSUFFICIENT_EVIDENCE")

    def test_common_small_number_is_not_used_as_evidence(self):
        paper = " ".join(f"Stage {i} uses 3 units." for i in range(12)) + " The reactor is at 700 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(3, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")


class SummaryAndSafety(unittest.TestCase):
    def test_coverage_counts_only_decided_claims(self):
        v = [{"evidence_relation": "DIRECT_SUPPORT", "category": "SUPPORTED"},
             {"evidence_relation": "INSUFFICIENT_EVIDENCE", "category": "EVIDENCE_GAP"},
             {"evidence_relation": "NOT_ASSESSED", "category": "UNVERIFIABLE"},
             {"evidence_relation": "ASSUMPTION_DECLARED", "category": "DECLARED_ASSUMPTION"}]
        s = sv.summarize(v)
        self.assertEqual((s["assessed"], s["not_assessed"], s["coverage"]), (2, 2, 0.5))
        self.assertIn("WRONG_CASE", s["reserved_unimplemented"])

    def test_validation_does_not_modify_the_schema(self):
        s = schema([reactor(V(650, "inferred", rationale="reuse inlet temperature"))])
        before = copy.deepcopy(s)
        sv.validate_schema(s, "Gas is preheated to 650 °C before the reformer.")
        self.assertEqual(s, before)

    def test_claims_carry_owner_context(self):
        cl = edc_claims.extract_claims(schema([reactor(V(650, "inferred"))]))
        c = {x["id"]: x for x in cl}["B1.T_C"]
        self.assertEqual((c["owner_type"], c["kind"]), ("RGIBBS", "temperature"))
        self.assertEqual(c["owner_context"]["name"], "R-101")


def sch_with(blocks, feeds, streams, scope=None):
    s = {"flowsheet_schema": {"blocks": blocks, "streams": streams, "connectivity": {"edges": []}},
         "io": {"feeds": feeds}}
    if scope:
        s["scope"] = scope
    return s


class TableUnits(unittest.TestCase):
    TABLE = ("Block\tComponent\tTemperature (K)\tPressure (bar)\n"
             "R9\tReformer\t1000\t10\n"
             "H1\tCooler\t500\t10\n")

    def test_number_without_unit_takes_its_column_unit(self):
        c = by_id(sv.validate_schema(schema([reactor(V(726.85, "inferred"))]), self.TABLE))["B1.T_C"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")     # 1000 K = 726.85 degC, reformer row

    def test_pressure_column(self):
        b = reactor(V(726.85, "inferred"), V(10, "inferred"))
        c = by_id(sv.validate_schema(schema([b]), self.TABLE))["B1.P_bar"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_a_kelvin_column_number_is_not_a_celsius_value(self):
        c = by_id(sv.validate_schema(schema([reactor(V(1000, "inferred"))]), self.TABLE))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")  # identity in a K column = unit conflict


class NamesAndScope(unittest.TestCase):
    def _feeds(self):
        mk = lambda sid, name: {"id": sid, "type": "MATERIAL", "role": "FEED", "name": name}
        streams = [mk("S1", "HYDRO feed"), mk("S2", "SYNGAS")]
        feeds = [{"stream": "S1", "specs": {"T_C": V(30, "inferred"), "P_bar": V(5, "inferred"),
                                           "flow": V(1, "inferred"), "z": {"H2": 1.0}}},
                 {"stream": "S2", "specs": {"T_C": V(30, "paper", "temperature is set to 30 °C"),
                                           "P_bar": V(5, "inferred"), "flow": V(2, "inferred"), "z": {"CO": 1.0}}}]
        return streams, feeds

    def test_stream_name_in_the_sentence_decides_the_owner(self):
        streams, feeds = self._feeds()
        paper = "The syngas temperature is set to 30 °C in the following sections."
        rep = by_id(sv.validate_schema(sch_with([], feeds, streams), paper))
        self.assertEqual(rep["S2.T_C"]["evidence_relation"], "DIRECT_SUPPORT")
        self.assertEqual(rep["S1.T_C"]["evidence_relation"], "POTENTIAL_CROSS_EQUIPMENT_BORROWING")

    def test_sentence_about_an_excluded_process_is_not_evidence(self):
        scope = {"target_unit": "ATR hydrogen process", "included_units": ["ATR reformer"],
                 "excluded_units": ["SMR process", "SE-ATR process"]}
        b = reactor(V(800, "inferred"))
        paper = "In the SMR case the reformer operates at 800 °C. In the SE-ATR case the reformer is at 800 °C."
        c = by_id(sv.validate_schema(sch_with([b], [], [], scope), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")
        paper2 = paper + " The ATR reformer operates at 800 °C."
        c2 = by_id(sv.validate_schema(sch_with([b], [], [], scope), paper2))["B1.T_C"]
        self.assertEqual(c2["evidence_relation"], "DIRECT_SUPPORT")

    def test_neighbouring_table_row_does_not_lend_its_id(self):
        paper = ("Block\tComponent\tTemperature (K)\n"
                 "H-7\tCooler\t-\n"
                 "X-1\tTank\t1000\n")
        b = {"id": "B2", "type": "HEATER", "name": "H-7 - cooler",
             "specs": {"spec_mode": "TP", "T_out_C": V(726.85, "inferred"), "P_bar": V(1, "default")}}
        c = by_id(sv.validate_schema(schema([b]), paper))["B2.T_out_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")


class ProcessLevelAndReference(unittest.TestCase):
    def _heater(self, t=None, p=V(30, "paper", "30 bar")):
        return {"id": "B2", "type": "HEATER", "name": "H-1 - cooler",
                "specs": {"spec_mode": "TP", "T_out_C": t or V(400, "paper", "x"), "P_bar": p}}

    def test_process_wide_pressure_is_process_level_support(self):
        paper = "Operating conditions of 30 bar were chosen for all simulations."
        c = by_id(sv.validate_schema(schema([self._heater()]), paper))["B2.P_bar"]
        self.assertEqual(c["evidence_relation"], "PROCESS_LEVEL_SUPPORT")
        self.assertEqual(c["category"], "SUPPORTED_PROCESS_LEVEL")

    def test_another_equipment_stating_the_same_number_does_not_contradict_it(self):
        paper = "The reformer pressure is 30 bar. Operating conditions of 30 bar were chosen for all simulations."
        c = by_id(sv.validate_schema(schema([self._heater()]), paper))["B2.P_bar"]
        self.assertEqual(c["evidence_relation"], "PROCESS_LEVEL_SUPPORT")

    def test_other_equipment_alone_is_still_borrowing_without_a_process_wide_statement(self):
        paper = "The reformer pressure is 30 bar."
        # no quote of its own: the heater pressure is an inference, so the only text is the reformer sentence
        c = by_id(sv.validate_schema(schema([self._heater(p=V(30, "inferred"))]), paper))["B2.P_bar"]
        self.assertEqual(c["evidence_relation"], "POTENTIAL_CROSS_EQUIPMENT_BORROWING")

    def test_a_plain_mention_without_a_process_wide_cue_is_not_process_level(self):
        paper = "A pressure of 30 bar is reported in Table 2."
        c = by_id(sv.validate_schema(schema([self._heater(p=V(30, "inferred"))]), paper))["B2.P_bar"]
        self.assertEqual(c["evidence_relation"], "INSUFFICIENT_EVIDENCE")

    def test_process_wide_cue_without_a_unit_or_quantity_word_is_not_process_level(self):
        paper = "Operating conditions of 30 were chosen for all simulations."
        c = by_id(sv.validate_schema(schema([self._heater(p=V(30, "inferred"))]), paper))["B2.P_bar"]
        self.assertNotEqual(c["evidence_relation"], "PROCESS_LEVEL_SUPPORT")

    def test_process_level_is_withheld_when_the_policy_sees_a_sweep(self):
        q = "The pressure was varied between 30 and 40 bar."
        paper = q + " Operating conditions of 30 bar were chosen for all simulations."
        c = by_id(sv.validate_schema(schema([self._heater(p=V(30, "inferred", q))]), paper))["B2.P_bar"]
        self.assertEqual(c["edc_policy_class"], "SWEPT")
        self.assertEqual(c["evidence_relation"], "INSUFFICIENT_EVIDENCE")
        self.assertIn("process_level_but_multiple_values", c["flags"])

    def test_an_inlet_condition_is_not_a_process_level_value(self):
        paper = "The inlet temperature was chosen as 330 °C."
        c = by_id(sv.validate_schema(schema([reactor(V(330, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "PROCESS_LEVEL_SUPPORT")

    def test_process_level_is_for_temperature_and_pressure_only(self):
        paper = "Operating conditions of 500 kg/h were chosen."
        feeds = [{"stream": "S1", "specs": {"T_C": V(25, "default"), "P_bar": V(1, "default"),
                                           "flow": V(500, "inferred"), "z": {"H2": 1.0}}}]
        c = by_id(sv.validate_schema(sch_with([], feeds, [{"id": "S1", "role": "FEED"}]), paper))["S1.flow"]
        self.assertNotEqual(c["evidence_relation"], "PROCESS_LEVEL_SUPPORT")

    def test_small_integer_needs_a_unit(self):
        feeds = [{"stream": "S1", "specs": {"T_C": V(25, "default"), "P_bar": V(1, "default"),
                                           "flow": V(3, "inferred"), "z": {"H2": 1.0}}}]
        paper = "A steam-to-carbon ratio of 3.0 was used. Operating conditions of 1 bar were chosen."
        rep = by_id(sv.validate_schema(sch_with([], feeds, [{"id": "S1", "role": "FEED"}]), paper))
        self.assertNotIn(rep["S1.flow"]["evidence_relation"], ("DIRECT_SUPPORT", "PROCESS_LEVEL_SUPPORT"))
        # the bare "3.0" must not even count as a located number: the claim is judged as an undeclared assumption
        self.assertIn("undeclared", " ".join(rep["S1.flow"]["reasons"]))
        self.assertEqual(rep["S1.P_bar"]["evidence_relation"], "PROCESS_LEVEL_SUPPORT")

    def test_small_integer_in_the_claims_own_quote_needs_a_unit_too(self):
        feeds = [{"stream": "S1", "specs": {"T_C": V(25, "default"), "P_bar": V(1, "default"),
                                           "flow": V(3, "paper", "A steam-to-carbon ratio of 3.0 was used."),
                                           "z": {"H2": 1.0}}}]
        paper = "A steam-to-carbon ratio of 3.0 was used."
        c = by_id(sv.validate_schema(sch_with([], feeds, [{"id": "S1", "role": "FEED"}]), paper))["S1.flow"]
        self.assertIn("paper-labelled value, but no supporting text", " ".join(c["reasons"]))

    REF_TABLE = ("Block\tComponent\tTemperature (K)\tPressure (bar)\n"
                 "R1\tReformer\t900\t10\n"
                 "H-1\tCooler\tTo the reformer temperature\t10\n")

    def test_owner_row_pointing_at_another_unit_is_supported_by_reference(self):
        c = by_id(sv.validate_schema(schema([self._heater(V(626.85, "inferred"), V(10, "paper", "10 bar"))]),
                                     self.REF_TABLE))["B2.T_out_C"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")
        self.assertIn("by_reference_in_owner_row", c["flags"])

    def test_without_the_pointer_the_same_number_is_borrowing(self):
        paper = self.REF_TABLE.replace("To the reformer temperature", "-")
        c = by_id(sv.validate_schema(schema([self._heater(V(626.85, "inferred"), V(10, "paper", "10 bar"))]),
                                     paper))["B2.T_out_C"]
        self.assertEqual(c["evidence_relation"], "POTENTIAL_CROSS_EQUIPMENT_BORROWING")


class SubjectAfterTheNumber(unittest.TestCase):
    def _heater(self, duty=2.5):
        return {"id": "B2", "type": "HEATER", "name": "EVAP-1 - evaporator",
                "specs": {"spec_mode": "DUTY", "duty": V(duty, "paper", "x"), "P_bar": V(2, "default")}}

    def test_subject_named_after_the_number_is_used_when_nothing_precedes_it(self):
        paper = "2.5 kW is the heat power supplied from waste heat in an evaporator."
        c = by_id(sv.validate_schema(schema([self._heater()]), paper))["B2.duty"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_subject_of_the_next_clause_is_not_used(self):
        paper = "400 °C is the preheat condition, and the reformer is operated at 500 °C."
        b = {"id": "B1", "type": "RGIBBS", "name": "R-1",
             "specs": {"T_C": V(400, "inferred"), "P_bar": V(20, "paper", "20 bar in the reformer")}}
        c = by_id(sv.validate_schema(schema([b]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_location_after_the_number_is_not_the_subject(self):
        paper = "650 °C is reached before it enters the reformer."
        c = by_id(sv.validate_schema(schema([reactor(V(650, "inferred"))]), paper))["B1.T_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")


class AbbreviationsAndRepeats(unittest.TestCase):
    def test_three_letter_abbreviation_in_a_stream_name_is_a_subject_cue(self):
        streams = [{"id": "S1", "type": "MATERIAL", "role": "FEED", "name": "Cold MCH"},
                   {"id": "S2", "type": "MATERIAL", "role": "FEED", "name": "Cold WAT"}]
        feeds = [{"stream": sid, "specs": {"T_C": V(20, "inferred"), "P_bar": V(1, "default"), "flow": V(5, "default"),
                                          "z": {"H2": 1.0}}} for sid in ("S1", "S2")]
        paper = "The inlet temperature of MCH is 20 °C."
        rep = by_id(sv.validate_schema(sch_with([], feeds, streams), paper))
        self.assertEqual(rep["S1.T_C"]["evidence_relation"], "DIRECT_SUPPORT")
        self.assertEqual(rep["S2.T_C"]["evidence_relation"], "POTENTIAL_CROSS_EQUIPMENT_BORROWING")

    def test_abbreviation_that_is_only_the_letters_of_an_id_is_not_a_second_cue(self):
        import edc_claims as ec
        self.assertEqual(ec.name_tokens("HPC-1")["words"], [])
        self.assertEqual(ec.name_tokens("Cold MCH")["words"], ["mch"])

    def test_a_value_repeated_many_times_with_its_unit_is_still_judged(self):
        paper = " ".join("Section %d is run at 2 bar." % i for i in range(12)) + " The reformer operates at 2 bar."
        b = reactor(V(500, "inferred"), V(2, "inferred"))
        c = by_id(sv.validate_schema(schema([b]), paper))["B1.P_bar"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")


class BareNumbers(unittest.TestCase):
    """A bare number with no unit and no temperature / pressure word is not evidence for a T or P claim (case F: "Feed stream stage 25")."""
    STREAMS = [{"id": "S1", "type": "MATERIAL", "role": "FEED", "name": "H2 feed"}]

    def feed_t(self, paper):
        feeds = [{"stream": "S1", "specs": {"T_C": V(25, "default"), "P_bar": V(1, "default"), "flow": V(5, "default"), "z": {"H2": 1.0}}}]
        return by_id(sv.validate_schema(sch_with([], feeds, self.STREAMS), paper))["S1.T_C"]["evidence_relation"]

    def test_stage_number_is_not_a_feed_temperature(self):
        self.assertNotEqual(self.feed_t("The column has 31 stages. Feed stream stage 25. Reflux ratio 2.46."), "DIRECT_SUPPORT")

    def test_the_same_number_with_a_unit_still_counts(self):
        self.assertEqual(self.feed_t("The feed temperature of the H2 feed is 25 °C."), "DIRECT_SUPPORT")


class MultiCasePapers(unittest.TestCase):
    PAPER = ("2.3.1. Case 1: Conventional route\n"
             "The syngas is heated to 850 °C and fed into the reformer.\n"
             "2.3.2. Case 2: Looping route\n"
             "The product gas is cooled to 30 °C in the cooler.\n"
             "4. Results\n"
             "Nothing else is stated about temperatures here.\n")
    SCOPE = {"target_unit": "Case 1 base configuration C1B reforming section", "included_units": ["REFORM"],
             "excluded_units": []}

    def _cooler(self, t=30):
        return {"id": "B2", "type": "HEATER", "name": "COOL-1 - cooler",
                "specs": {"spec_mode": "TP", "T_out_C": V(t, "inferred"), "P_bar": V(1, "default")}}

    def test_case_ids(self):
        import edc_claims as ec
        self.assertEqual(ec.case_ids("Case 4-1: TDM; C2B and C4-2H; Cases 3"), {"4-1", "2", "4-2", "3"})
        self.assertEqual(ec.case_ids("C2 is ethylene and C3H6 propylene"), set())

    def test_target_cases_come_from_the_schema_scope(self):
        import edc_claims as ec
        self.assertEqual(ec.target_cases(sch_with([], [], [], self.SCOPE)), {"1"})
        self.assertEqual(ec.target_cases(sch_with([], [], [], {"target_unit": "ATR process"})), set())

    def test_section_about_another_case_is_not_evidence(self):
        s = sch_with([self._cooler()], [], [], self.SCOPE)
        c = by_id(sv.validate_schema(s, self.PAPER))["B2.T_out_C"]
        self.assertNotEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_same_text_without_cases_in_the_scope_is_not_filtered(self):
        s = sch_with([self._cooler()], [], [], {"target_unit": "reforming section"})
        c = by_id(sv.validate_schema(s, self.PAPER))["B2.T_out_C"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_section_of_the_requested_case_is_evidence(self):
        paper = self.PAPER.replace("heated to 850 °C and fed into the reformer", "cooled to 30 °C in the cooler")
        s = sch_with([self._cooler()], [], [], self.SCOPE)
        c = by_id(sv.validate_schema(s, paper))["B2.T_out_C"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")

    def test_inline_mention_of_another_case(self):
        s = sch_with([self._cooler()], [], [], self.SCOPE)
        other = by_id(sv.validate_schema(s, "In C2B the gas is cooled to 30 °C."))["B2.T_out_C"]
        mine = by_id(sv.validate_schema(s, "In C1B the gas is cooled to 30 °C."))["B2.T_out_C"]
        self.assertNotEqual(other["evidence_relation"], "DIRECT_SUPPORT")
        self.assertEqual(mine["evidence_relation"], "DIRECT_SUPPORT")

    def test_a_heading_without_a_case_closes_the_case_section(self):
        paper = "2.3.2. Case 2: Looping route\nNothing here.\n4. Results\nThe gas is cooled to 30 °C in the cooler.\n"
        s = sch_with([self._cooler()], [], [], self.SCOPE)
        c = by_id(sv.validate_schema(s, paper))["B2.T_out_C"]
        self.assertEqual(c["evidence_relation"], "DIRECT_SUPPORT")
