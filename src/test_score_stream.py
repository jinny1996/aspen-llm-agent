"""score_stream: duplicated / missing / dangling material streams lower Stream F1, which Block/Edge F1 cannot see."""
import unittest
from collections import Counter

import score_stream as ss


class StreamF1(unittest.TestCase):
    def test_gold_adds_the_boundary_outlets_every_unit_needs(self):
        g = ss.gold_streams("F")                      # SPL-1 feeds MIX-4 -> one purge; SEP-3 is terminal -> two outlets; NH3 liquid of SEP-2 goes on
        self.assertEqual(g[("SPL-1", "PRODUCT")], 1)
        self.assertEqual(g[("SEP-3", "PRODUCT")], 2)
        self.assertEqual(g[("SEP-2", "PRODUCT")], 0)

    def test_perfect_match_is_one(self):
        g = ss.gold_streams("F")
        self.assertEqual(ss.prf_multi(g, g)[0], 1.0)

    def test_a_duplicate_stream_on_one_port_is_a_false_positive(self):
        g = ss.gold_streams("F")
        p = Counter(g)
        p[("SEP-2", "PRODUCT")] += 2                 # the failed F run: two placeholder products on SEP-2
        f1, prec, rec = ss.prf_multi(p, g)
        self.assertLess(f1, 1.0)
        self.assertEqual(rec, 1.0)

    def test_a_missing_product_is_a_false_negative(self):
        g = ss.gold_streams("F")
        p = Counter(g)
        p[("SEP-3", "PRODUCT")] -= 1
        f1, prec, rec = ss.prf_multi(p, g)
        self.assertLess(rec, 1.0)
        self.assertEqual(prec, 1.0)

    def test_streams_from_code_counts_consumers_and_products(self):
        code = ('connect_stream(doc, "B1", "S1", "P(OUT)")\nconnect_stream(doc, "B2", "S1", "F(IN)")\n'
                'connect_stream(doc, "B2", "S2", "P(OUT)")\nconnect_stream(doc, "B2", "S3", "P(OUT)")\n')
        c = ss.streams_from_code(code, {"B1": "X", "B2": "Y"})
        self.assertEqual(c, Counter({("X", "Y"): 1, ("Y", "PRODUCT"): 2}))


if __name__ == "__main__":
    unittest.main()


class SpecItems(unittest.TestCase):
    CODE = ('connect_stream(doc, "B1", "S1", "F(IN)")\nconnect_stream(doc, "B1", "S2", "P(OUT)")\n'
            'stream_set_temperature(doc, "S1", 25)\nstream_set_pressure(doc, "S1", 20)\n'
            'comp_set_model_type(doc, "B1", "COMPRESSOR")\ncomp_set_spec(doc, "B1", "PRES", 200)\n'
            'splt_set_frac(doc, "B2", "S5", 0.01)\nsplt_set_frac(doc, "B2", "S6", 0.99)\n')

    def test_code_items(self):
        items, feeds = ss.spec_items_code(self.CODE, {"B1": "CP", "B2": "SPL"})
        self.assertEqual(feeds, ["S1"])
        self.assertEqual(items, {("FEED:S1", "T"), ("FEED:S1", "P"), ("CP", "model_type"), ("CP", "P"), ("SPL", "frac0"), ("SPL", "frac1")})

    def test_over_specified_splitter_and_missing_valve_pressure_are_both_errors(self):
        items, feeds = ss.spec_items_code(self.CODE, {"B1": "CP-4", "B2": "SPL-1"})
        gold = ss.gold_specs("F", ["S1"], aspen=True)
        self.assertIn(("SPL-1", "frac1"), items - gold)           # second fraction = false positive
        self.assertIn(("V-1", "P"), gold - items)                 # valve pressure never set = false negative
        self.assertIn(("FEED:S1", "flow"), gold - items)

    def test_schema_feed_composition_is_a_mapping(self):
        schema = {"flowsheet_schema": {"blocks": []}, "io": {"feeds": [{"stream": "S1", "specs": {"z": {"H2": 1.0}, "basis": "MOLE-FRAC",
                                                                                                      "T_C": {"value": 25}}}]}}
        items, feeds = ss.spec_items_schema(schema, {})
        self.assertEqual(items, {("FEED:S1", "z"), ("FEED:S1", "T")})

    def test_efficiency_and_mixers_are_not_scored(self):
        items = {("MIX-3", "P"), ("CP-4", "eff"), ("CP-4", "P")}
        self.assertEqual(ss._ignore_mheatx(items, "F"), {("CP-4", "P")})


class Orphans(unittest.TestCase):
    def test_declared_but_unconnected_stream_is_an_extra_stream(self):
        code = ('stream_add(doc, "S1", "MATERIAL")\nstream_add(doc, "S2", "MATERIAL")\nstream_add(doc, "S13", "MATERIAL")\n'
                'connect_stream(doc, "B1", "S1", "P(OUT)")\nconnect_stream(doc, "B2", "S1", "F(IN)")\nconnect_stream(doc, "B2", "S2", "P(OUT)")\n')
        c = ss.streams_from_code(code, {"B1": "X", "B2": "Y"})
        self.assertEqual(c[("orphan:S13", "PRODUCT")], 1)
        self.assertEqual(c[("X", "Y")], 1)
