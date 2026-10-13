"""Unit tests for dof_ledger (synthetic schemas, no Aspen/LLM/network)."""
import unittest

import dof_ledger as dl


def V(v, source="paper", quote=None, conf=0.9):
    node = {"value": v, "source": source, "confidence": conf}
    if quote is not None:
        node["evidence"] = {"doc_id": "paper_text", "locator": "T1", "quote": quote}
    return node


def schema(blocks, edges=None, streams=None, feeds=None):
    return {
        "flowsheet_schema": {
            "blocks": blocks,
            "streams": streams or [],
            "connectivity": {"edges": edges or []},
        },
        "io": {"feeds": feeds or []},
    }


def ids(ledger, status):
    return {e["id"] for e in ledger["entries"] if e["status"] == status}


class BlockRules(unittest.TestCase):
    def test_flash_tp_complete(self):
        s = schema([{"id": "B1", "type": "FLASH2",
                     "specs": {"spec_mode": "TP", "T_out_C": V(25), "P_out_bar": V(30)}}])
        L = dl.build_ledger(s)
        self.assertEqual(ids(L, dl.MISSING), set())
        self.assertEqual(ids(L, dl.PROVIDED), {"B1.T_out_C", "B1.P_out_bar"})

    def test_flash_tp_missing_pressure(self):
        s = schema([{"id": "B1", "type": "FLASH2",
                     "specs": {"spec_mode": "TP", "T_out_C": V(25), "P_out_bar": None}}])
        self.assertEqual(ids(dl.build_ledger(s), dl.MISSING), {"B1.P_out_bar"})

    def test_flash_mode_inferred_when_spec_mode_null(self):
        s = schema([{"id": "B1", "type": "FLASH2",
                     "specs": {"spec_mode": None, "P_out_bar": V(5), "duty": V(0)}}])
        L = dl.build_ledger(s)
        self.assertEqual(ids(L, dl.MISSING), set())      # PD satisfied
        self.assertIn("B1.duty", ids(L, dl.PROVIDED))

    def test_flash_nothing_given_is_one_group_missing(self):
        s = schema([{"id": "B1", "type": "FLASH2", "specs": {}}])
        self.assertEqual(ids(dl.build_ledger(s), dl.MISSING), {"B1.spec_mode"})

    def test_heater_outlet_t_alias(self):
        s = schema([{"id": "B3", "type": "HEATER",
                     "specs": {"spec_mode": "OUTLET_T", "T_out_C": V(650), "P_bar": V(30),
                               "duty": None}}])
        self.assertEqual(ids(dl.build_ledger(s), dl.MISSING), set())

    def test_heater_missing_pressure_in_outlet_t(self):
        s = schema([{"id": "B3", "type": "HEATER",
                     "specs": {"spec_mode": "OUTLET_T", "T_out_C": V(650)}}])
        self.assertEqual(ids(dl.build_ledger(s), dl.MISSING), {"B3.P_bar"})

    def test_rgibbs_requires_t_and_p(self):
        s = schema([{"id": "B4", "type": "RGIBBS", "specs": {"T_C": V(650)}}])
        self.assertEqual(ids(dl.build_ledger(s), dl.MISSING), {"B4.P_bar"})

    def test_pump_one_of(self):
        ok = schema([{"id": "P1", "type": "PUMP", "specs": {"dP_bar": V(2)}}])
        bad = schema([{"id": "P1", "type": "PUMP", "specs": {}}])
        self.assertEqual(ids(dl.build_ledger(ok), dl.MISSING), set())
        self.assertEqual(ids(dl.build_ledger(bad), dl.MISSING),
                         {"P1.one_of(P_out_bar|dP_bar)"})

    def test_pump_efficiency_absent_is_default_candidate_not_missing(self):
        s = schema([{"id": "P1", "type": "PUMP", "specs": {"dP_bar": V(2)}}])
        L = dl.build_ledger(s)
        self.assertIn("P1.eff", ids(L, dl.DEFAULT_CANDIDATE))
        self.assertNotIn("P1.eff", ids(L, dl.MISSING))

    def test_pump_efficiency_given_is_provided(self):
        s = schema([{"id": "P1", "type": "PUMP",
                     "specs": {"dP_bar": V(2), "eff": V(0.75)}}])
        L = dl.build_ledger(s)
        self.assertIn("P1.eff", ids(L, dl.PROVIDED))
        self.assertNotIn("P1.eff", ids(L, dl.DEFAULT_CANDIDATE))

    def test_splitter_needs_n_minus_1(self):
        edges = [{"stream": "S2", "from": {"block": "SP1"}},
                 {"stream": "S3", "from": {"block": "SP1"}},
                 {"stream": "S4", "from": {"block": "SP1"}}]
        one = schema([{"id": "SP1", "type": "SPLITTER",
                       "specs": {"split_fractions": {"S2": 0.2}}}], edges)
        two = schema([{"id": "SP1", "type": "SPLITTER",
                       "specs": {"split_fractions": {"S2": 0.2, "S3": 0.3}}}], edges)
        self.assertEqual(len(ids(dl.build_ledger(one), dl.MISSING)), 1)
        self.assertEqual(ids(dl.build_ledger(two), dl.MISSING), set())

    def test_splitter_with_single_outlet_flagged(self):
        s = schema([{"id": "SP1", "type": "SPLITTER", "specs": {}}],
                   [{"stream": "S2", "from": {"block": "SP1"}}])
        self.assertEqual(ids(dl.build_ledger(s), dl.MISSING), {"SP1.outlets"})

    def test_mheatx_needs_pairs_minus_1(self):
        base = {"outlet_streams": {"S4": "S5", "S7": "S8"}}
        ok = schema([{"id": "E1", "type": "MHeatX",
                      "specs": {**base, "temp_specs": [{"out_stream": "S8", "T_out_C": V(400)}]}}])
        bad = schema([{"id": "E1", "type": "MHeatX", "specs": {**base, "temp_specs": []}}])
        self.assertEqual(ids(dl.build_ledger(ok), dl.MISSING), set())
        self.assertEqual(len(ids(dl.build_ledger(bad), dl.MISSING)), 1)

    def test_unknown_block_type_flagged(self):
        s = schema([{"id": "X1", "type": "FOO", "specs": {}}])
        self.assertEqual(ids(dl.build_ledger(s), dl.MISSING), {"X1.block_type"})


class Radfrac(unittest.TestCase):
    def test_radfrac_is_analysis_only_and_excluded_from_totals(self):
        s = schema([{"id": "C1", "type": "RADFRAC",
                     "specs": {"nstage": V(40), "condenser": "TOTAL", "reboiler": "KETTLE"}}])
        L = dl.build_ledger(s)
        missing = {e["variable"] for e in L["entries"]
                   if e["status"] == dl.MISSING and e["owner"] == "C1"}
        self.assertEqual(missing, {"feed_stage", "P_top_bar", "design_specs"})
        self.assertTrue(all(e.get("analysis_only") for e in L["entries"] if e["owner"] == "C1"
                            and e["status"] == dl.MISSING))
        self.assertEqual(L["summary"]["required"], 0)      # excluded from end-to-end totals
        self.assertEqual(L["summary"]["missing"], 0)

    def test_radfrac_two_design_specs(self):
        full = {"nstage": V(40), "feed_stage": V(20), "P_top_bar": V(1.01325),
                "condenser": "TOTAL", "reboiler": "KETTLE",
                "design_specs": [{"type": "RR", "value": 4}, {"type": "D:F", "value": 0.12}]}
        L = dl.build_ledger(schema([{"id": "C1", "type": "RADFRAC", "specs": full}]))
        self.assertEqual(ids(L, dl.MISSING), set())
        one = {**full, "design_specs": [{"type": "RR", "value": 4}]}
        L2 = dl.build_ledger(schema([{"id": "C1", "type": "RADFRAC", "specs": one}]))
        self.assertEqual(ids(L2, dl.MISSING), {"C1.design_specs"})


class Feeds(unittest.TestCase):
    streams = [{"id": "S1", "role": "FEED"}, {"id": "S2", "role": "FEED"}]

    def feed(self, sid, z):
        return {"stream": sid, "specs": {"T_C": V(25), "P_bar": V(30),
                                         "flow": V(1), "z": z}}

    def test_missing_feed_entry(self):
        s = schema([], streams=self.streams, feeds=[self.feed("S1", {"A": 1.0})])
        self.assertEqual(ids(dl.build_ledger(s), dl.MISSING), {"S2.feed_specs"})

    def test_composition_sum_checked(self):
        s = schema([], streams=self.streams[:1], feeds=[self.feed("S1", {"A": 0.5, "B": 0.4})])
        L = dl.build_ledger(s)
        z = [e for e in L["entries"] if e["id"] == "S1.z"][0]
        self.assertIn("sum", z.get("note", ""))

    def test_composition_has_no_provenance(self):
        s = schema([], streams=self.streams[:1], feeds=[self.feed("S1", {"A": 1.0})])
        L = dl.build_ledger(s)
        self.assertEqual(L["summary"]["provided_without_provenance"], 1)   # only z


class Provenance(unittest.TestCase):
    def test_source_counts(self):
        s = schema([{"id": "B1", "type": "RGIBBS",
                     "specs": {"T_C": V(650, "paper"), "P_bar": V(30, "inferred")}}])
        L = dl.build_ledger(s)
        self.assertEqual(L["summary"]["provided_by_source"], {"paper": 1, "inferred": 1})


class Quotes(unittest.TestCase):
    paper = ("Table 1. R1 Gibbs Reactor 773–1473 K, 5–40 bar. "
             "The operating conditions of 923 K, 30 bar and S/C of 3.0, O2/CH4 of 0.45 "
             "have been chosen.")

    def test_exact(self):
        q = "The operating conditions of 923 K, 30 bar and S/C of 3.0"
        self.assertEqual(dl.check_quote(q, self.paper), "EXACT")

    def test_whitespace_and_dash_tolerant(self):
        self.assertEqual(dl.check_quote("R1  Gibbs Reactor 773-1473 K", self.paper), "EXACT")

    def test_fragments_with_ellipsis(self):
        q = "The operating conditions of 923 K, 30 bar ... have been chosen"
        self.assertEqual(dl.check_quote(q, self.paper), "FRAGMENTS_ALL")

    def test_stitched_quote_from_distant_places_counts_as_fragments(self):
        q = "R1 Gibbs Reactor 773-1473 K; have been chosen"
        self.assertEqual(dl.check_quote(q, self.paper), "FRAGMENTS_ALL")

    def test_partial(self):
        q = "R1 Gibbs Reactor 773-1473 K; E9 Imaginary Unit Operation Temperature"
        self.assertEqual(dl.check_quote(q, self.paper), "PARTIAL")

    table = ("Block\tComponent\tTemperature (K)\tPressure (bar)\n"
             "R2\tHT Shift Convertor\t673\t5–40\n"
             "R3\tLT Shift Convertor\t523\t5–40\n")

    def test_table_row_reconstruction_is_not_exact_but_grounded(self):
        q = "R2 HT Shift Convertor 673 K, 5–40 bar"
        self.assertEqual(dl.check_quote(q, self.table), "RECONSTRUCTED")

    def test_reconstruction_rejects_wrong_number(self):
        q = "R2 HT Shift Convertor 999 K, 5–40 bar"
        self.assertEqual(dl.check_quote(q, self.table), "NOT_FOUND")

    def test_reconstruction_requires_order(self):
        q = "673 Convertor HT Shift R2"
        self.assertEqual(dl.check_quote(q, self.table), "NOT_FOUND")

    def test_latex_degree_sign_in_parsed_paper(self):
        paper = "the products are cooled to $4 0 ~ ^ { \\circ } \\mathrm { C }$ (E0102). The red line"
        q = "the products are cooled to 40 °C (E0102)."
        self.assertEqual(dl.check_quote(q, paper), "EXACT")

    def test_header_row_merge_fails_strict_but_numbers_are_grounded(self):
        q = "M Feed Mixture Temperature 298 K"
        paper = "Block\tComponent\tTemperature (K)\tPressure (bar)\nM\tFeed Mixture\t298\t5–40\n"
        self.assertEqual(dl.check_quote(q, paper), "NOT_FOUND")
        self.assertTrue(dl.numbers_grounded(q, set(dl._numbers(paper))))
        self.assertFalse(dl.numbers_grounded("M Feed Mixture Temperature 777 K",
                                             set(dl._numbers(paper))))
        self.assertIsNone(dl.numbers_grounded("no digits here", set()))

    def test_not_found_and_no_quote(self):
        self.assertEqual(dl.check_quote("a completely invented sentence here", self.paper),
                         "NOT_FOUND")
        self.assertEqual(dl.check_quote(None, self.paper), "NO_QUOTE")

    def test_verify_evidence_counts_by_source(self):
        s = schema([{"id": "B1", "type": "RGIBBS", "specs": {
            "T_C": V(650, "paper", "The operating conditions of 923 K, 30 bar and S/C of 3.0"),
            "P_bar": V(30, "paper", "this sentence was never in the paper at all")}}])
        L = dl.build_ledger(s)
        counts = dl.verify_evidence(L, self.paper)
        self.assertEqual(counts["paper"], {"EXACT": 1, "NOT_FOUND": 1})


if __name__ == "__main__":
    unittest.main()
