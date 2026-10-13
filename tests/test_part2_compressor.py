"""phase14 Part2 compat patch: schema block type COMPRESSOR is created as the Aspen block type Compr."""
import unittest

import part2_stage_functions  # noqa: F401  (applies the patch to the shared aspen_code_v1 module)
import aspen_code_v1


class FakeElements:
    def __init__(self):
        self.added = []

    def Add(self, spec):
        self.added.append(spec)


class FakeDoc:
    def __init__(self):
        self.elements = FakeElements()
        self.Tree = self

    def FindNode(self, path):
        node = type("N", (), {})()
        node.Elements = self.elements
        return node


class CompressorBlockType(unittest.TestCase):
    def _added(self, btype):
        doc = FakeDoc()
        aspen_code_v1.block_add(doc, "B7", btype)
        return doc.elements.added[-1]

    def test_compressor_becomes_compr(self):
        for t in ("COMPRESSOR", "Compressor", "compr", "COMPR"):
            self.assertEqual(self._added(t), "B7!Compr", t)

    def test_other_block_types_are_unchanged(self):
        self.assertEqual(self._added("HEATER"), "B7!HEATER")
        self.assertEqual(self._added("Splitter"), "B7!FSplit")
        self.assertEqual(self._added("HeatX"), "B7!MHeatX")
        self.assertEqual(self._added("PUMP"), "B7!PUMP")


if __name__ == "__main__":
    unittest.main()


class CompressorDefaults(unittest.TestCase):
    """merge_compressor_defaults_code: the settings Aspen needs, added when the LLM-written COM code leaves them out."""

    SCHEMA = {"flowsheet_schema": {"blocks": [
        {"id": "B1", "type": "COMPRESSOR", "specs": {"P_out_bar": {"value": 20}, "eff": {"value": 0.72}}},
        {"id": "B2", "type": "HEATER", "specs": {}}]}}
    # what run E generated: block_add + connections + comp_set_spec only
    E_CODE = ('block_add(doc, "B1", "COMPRESSOR")\nblock_add(doc, "B2", "HEATER")\n'
              'connect_stream(doc, "B1", "S1", "F(IN)")\ncomp_set_spec(doc, "B1", "PRES", 20)\n')

    def merge(self, code, schema=None):
        return part2_stage_functions.merge_compressor_defaults_code(code, schema or self.SCHEMA)

    def test_missing_settings_are_added_right_after_block_add(self):
        out = self.merge(self.E_CODE).splitlines()
        i = out.index('block_add(doc, "B1", "COMPRESSOR")')
        self.assertEqual(out[i + 1:i + 4], ['comp_set_model_type(doc, "B1", "COMPRESSOR")',
                                           'comp_set_type(doc, "B1", "ISENTROPIC")', 'comp_set_eff(doc, "B1", seff=0.72)'])
        self.assertIn('comp_set_spec(doc, "B1", "PRES", 20)', out)

    def test_nothing_is_duplicated_when_the_code_already_has_them(self):
        code = self.E_CODE + 'comp_set_model_type(doc, "B1", "COMPRESSOR")\ncomp_set_type(doc, \'B1\', "ISENTROPIC")\n'
        out = self.merge(code)
        self.assertEqual(out.count("comp_set_model_type"), 1)
        self.assertEqual(out.count("comp_set_type"), 1)
        self.assertIn("comp_set_eff", out)                      # still missing -> added

    def test_comp_configure_counts_as_everything(self):
        code = self.E_CODE + 'comp_configure(doc, "B1", spec="PRES", value=20)\n'
        self.assertEqual(self.merge(code).strip(), code.strip())

    def test_no_efficiency_in_the_schema_no_eff_call(self):
        schema = {"flowsheet_schema": {"blocks": [{"id": "B1", "type": "COMPRESSOR", "specs": {"P_out_bar": {"value": 20}}}]}}
        self.assertNotIn("comp_set_eff", self.merge(self.E_CODE, schema))

    def test_other_blocks_and_other_ids_are_untouched(self):
        out = self.merge(self.E_CODE)
        self.assertNotIn('"B2", "COMPRESSOR"', out)
        self.assertNotIn("comp_set_model_type(doc, \"B2\"", out)

    def test_a_compressor_without_block_add_gets_its_lines_at_the_end(self):
        out = self.merge('comp_set_spec(doc, "B1", "PRES", 20)\n')
        self.assertTrue(out.strip().splitlines()[-1].startswith("comp_set_eff"))


class SplitterFractions(unittest.TestCase):
    """merge_splitter_fractions_code: Aspen takes fractions for all FSplit outlets but one."""

    SCHEMA = {"flowsheet_schema": {"connectivity": {"edges": [
        {"stream": "S5", "from": {"block": "B13", "port_role": "OUT"}, "to": {"block": "B3", "port_role": "IN"}},
        {"stream": "S16", "from": {"block": "B13", "port_role": "OUT"}, "to": {"block": None, "port_role": "V"}},
        {"stream": "S9", "from": {"block": "B2", "port_role": "OUT"}, "to": {"block": None, "port_role": "V"}}]}}}

    def merge(self, code):
        return part2_stage_functions.merge_splitter_fractions_code(code, self.SCHEMA)

    def test_the_largest_fraction_is_left_to_aspen(self):
        code = 'splt_set_frac(doc, "B13", "S16", 0.01)\nsplt_set_frac(doc, "B13", "S5", 0.99)\n'
        self.assertEqual(self.merge(code), 'splt_set_frac(doc, "B13", "S16", 0.01)\n')

    def test_n_minus_1_calls_are_left_alone(self):
        code = 'splt_set_frac(doc, "B13", "S16", 0.01)\nheater_set_spec(doc, "B2", "TP")\n'
        self.assertEqual(self.merge(code), code)

    def test_other_code_and_other_blocks_are_kept(self):
        code = ('block_add(doc, "B13", "FSplit")\nsplt_set_frac(doc, \'B13\', \'S5\', 0.99)\nsplt_set_frac(doc, "B13", "S16", 0.01)\n'
                'splt_set_frac(doc, "B99", "S1", 0.5)\n')
        out = self.merge(code)
        self.assertIn('block_add(doc, "B13", "FSplit")', out)
        self.assertIn('splt_set_frac(doc, "B99", "S1", 0.5)', out)          # unknown block: no outlet count, untouched
        self.assertNotIn("0.99", out)
        self.assertIn("0.01", out)
