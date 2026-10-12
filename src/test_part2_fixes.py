"""Tests for the two phase13 Part2 fixes (no Aspen, no LLM, no network).

1. component-id mapping: z keyed by a schema aspen_id that differs from the resolved_name
   (e.g. 'CARBON' -> 'C') must be mapped.  The nested function is extracted from the source file
   with `ast` (it only lives inside a big function), and run against BOTH the phase12 file
   (must still show the bug) and the phase13 file (must be fixed).
2. stream_set_total_flow: MOLE keeps old behaviour; MASS sets kg/hr explicitly and raises if the
   unit never takes.
"""
import ast
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
P13 = HERE / "part2_stage_functions.py"
P12 = HERE.parent / "phase12" / "part2_stage_functions.py"

_NEEDED = ("_to_float_or_none", "_norm", "_get_component_list", "_iter_feeds",
           "normalize_feed_z_for_codegen", "apply_resolved_aliases_to_schema")


def load_alias_functions(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    host = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and any(
                isinstance(c, ast.FunctionDef) and c.name == "apply_resolved_aliases_to_schema"
                for c in ast.walk(node) if c is not node):
            host = node          # keep the innermost enclosing function found last is fine
    assert host is not None, "enclosing function not found"
    nested = {c.name: c for c in host.body if isinstance(c, ast.FunctionDef)}
    ns = {"re": re, "os": os, "json": json, "Path": Path}
    for name in _NEEDED:
        mod = ast.Module(body=[nested[name]], type_ignores=[])
        exec(compile(ast.fix_missing_locations(mod), str(path), "exec"), ns)
    return ns


def make_case(tmp: Path):
    resolved = {"resolved_components": [
        {"schema_name": "methane", "formula": "CH4", "resolved_alias": "CH4", "resolved_name": "METHANE"},
        {"schema_name": "hydrogen", "formula": "H2", "resolved_alias": "H2", "resolved_name": "HYDROGEN"},
        {"schema_name": "solid carbon", "formula": "C", "resolved_alias": "C",
         "resolved_name": "CARBON-GRAPHITE"},
    ]}
    inp = tmp / "x.inp"
    (tmp / "x_resolved_components.json").write_text(json.dumps(resolved), encoding="utf-8")
    schema = {
        "aspen_schema": {"components": [
            {"aspen_id": "METHANE", "formula": "CH4", "name": "methane"},
            {"aspen_id": "HYDROGEN", "formula": "H2", "name": "hydrogen"},
            {"aspen_id": "CARBON", "formula": "C", "name": "solid carbon"},
        ]},
        "io": {"feeds": [{"stream": "S1", "specs": {"z": {"METHANE": 0.5, "HYDROGEN": 0.5, "CARBON": 0.0}}}]},
    }
    return schema, str(inp)


def z_keys(ns, schema, inp):
    s = ns["normalize_feed_z_for_codegen"](schema)
    s = ns["apply_resolved_aliases_to_schema"](s, inp)
    return set(s["io"]["feeds"][0]["specs"]["z"])


class AliasFix(unittest.TestCase):
    def test_phase13_maps_original_aspen_id(self):
        with tempfile.TemporaryDirectory() as d:
            schema, inp = make_case(Path(d))
            keys = z_keys(load_alias_functions(P13), schema, inp)
        self.assertEqual(keys, {"CH4", "H2", "C"})

    @unittest.skipUnless(P12.exists(), "phase12 not present")
    def test_phase12_still_has_the_bug(self):
        with tempfile.TemporaryDirectory() as d:
            schema, inp = make_case(Path(d))
            keys = z_keys(load_alias_functions(P12), schema, inp)
        self.assertIn("CARBON", keys)        # documents the original defect
        self.assertNotIn("C", keys)


class FakeNode:
    def __init__(self, working_codes, start_unit="kmol/hr"):
        self.Value = 0.0
        self.UnitString = start_unit
        self.working_codes = set(working_codes)
        self.calls = []

    def SetValueUnitAndBasis(self, value, code, basis):
        self.calls.append((value, code, basis))
        self.Value = value
        if basis == "MASS" and code in self.working_codes:
            self.UnitString = "kg/hr"


class FakeDoc:
    def __init__(self, node):
        self.node = node
        self.Tree = self

    def FindNode(self, path):
        return self.node


class TotalFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import part2_stage_functions as p2
        cls.p2 = p2

    def test_mole_keeps_old_behaviour(self):
        node = FakeNode(working_codes={3})
        self.p2.stream_set_total_flow(FakeDoc(node), "S1", 1.5, base="MOLE")
        self.assertEqual((node.Value, node.UnitString, node.calls), (1.5, "kmol/hr", []))

    def test_mass_uses_explicit_kg_per_hr(self):
        node = FakeNode(working_codes={3})
        self.p2.stream_set_total_flow(FakeDoc(node), "S1", 58.32, base="MASS")
        self.assertEqual(node.UnitString, "kg/hr")
        self.assertEqual(node.Value, 58.32)
        self.assertEqual(node.calls, [(58.32, 3, "MASS")])      # first code worked, no scan

    def test_mass_scans_other_codes_when_default_code_fails(self):
        node = FakeNode(working_codes={5})
        self.p2.stream_set_total_flow(FakeDoc(node), "S1", 3.24, base="MASS")
        self.assertEqual(node.UnitString, "kg/hr")

    def test_mass_raises_instead_of_silent_fallback(self):
        node = FakeNode(working_codes=set())
        with self.assertRaises(RuntimeError):
            self.p2.stream_set_total_flow(FakeDoc(node), "S1", 3.24, base="MASS")

    def test_missing_node_raises(self):
        with self.assertRaises(RuntimeError):
            self.p2.stream_set_total_flow(FakeDoc(None), "S9", 1.0, base="MASS")

    def test_helper_module_uses_the_fixed_function(self):
        import aspen_code_v1
        self.assertIs(aspen_code_v1.stream_set_total_flow, self.p2.stream_set_total_flow)


class FakeUnitNode:
    """TOTFLOW node whose unit follows SetValueUnitAndBasis (or ignores it if stuck=True)."""
    def __init__(self, unit, stuck=False, value=0.0):
        self.UnitString = unit
        self.Value = value
        self.stuck = stuck

    def SetValueUnitAndBasis(self, value, code, basis):
        self.Value = value
        if not self.stuck and basis == "MASS" and code == 3:
            self.UnitString = "kg/hr"


class FakeStreamsDoc:
    def __init__(self, nodes):
        self.nodes = nodes
        self.Tree = self

    def FindNode(self, path):
        for sname, node in self.nodes.items():
            if rf"\Data\Streams\{sname}\Input\TOTFLOW\MIXED" == path:
                return node
        return None


def feed_schema(**flows):
    feeds = [{"stream": s, "specs": {"flow": {"value": v, "basis": b}}} for s, (v, b) in flows.items()]
    return {"io": {"feeds": feeds}}


class FeedUnitCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import part2_stage_functions as p2
        cls.p2 = p2

    def test_mass_feed_in_mole_unit_is_detected_and_corrected(self):
        n = FakeUnitNode("kmol/hr", value=3.24)
        out = self.p2.check_feed_flow_units(FakeStreamsDoc({"S1": n}), feed_schema(S1=(3.24, "MASS")))
        self.assertEqual(len(out["fixed"]), 1)
        self.assertEqual(out["mismatches"], [])
        self.assertEqual(n.UnitString, "kg/hr")

    def test_detect_only_when_fix_is_off(self):
        n = FakeUnitNode("kmol/hr", value=3.24)
        out = self.p2.check_feed_flow_units(FakeStreamsDoc({"S1": n}), feed_schema(S1=(3.24, "MASS")), fix=False)
        self.assertEqual((len(out["fixed"]), len(out["mismatches"])), (0, 1))
        self.assertEqual(n.UnitString, "kmol/hr")                      # untouched

    def test_unfixable_mismatch_is_reported(self):
        n = FakeUnitNode("kmol/hr", stuck=True, value=3.24)
        out = self.p2.check_feed_flow_units(FakeStreamsDoc({"S1": n}), feed_schema(S1=(3.24, "MASS")))
        self.assertEqual(len(out["mismatches"]), 1)
        self.assertIn("fix_error", out["mismatches"][0])

    def test_matching_units_pass_for_both_bases(self):
        doc = FakeStreamsDoc({"S1": FakeUnitNode("kg/hr", value=3.24), "S2": FakeUnitNode("kmol/hr", value=1.0)})
        out = self.p2.check_feed_flow_units(doc, feed_schema(S1=(3.24, "MASS"), S2=(1.0, "MOLE")))
        self.assertEqual((len(out["checked"]), len(out["mismatches"]), len(out["fixed"])), (2, 0, 0))

    def test_other_mole_and_mass_units_are_recognised_by_family(self):
        doc = FakeStreamsDoc({"S1": FakeUnitNode("lb/hr"), "S2": FakeUnitNode("lbmol/hr")})
        out = self.p2.check_feed_flow_units(doc, feed_schema(S1=(1.0, "MASS"), S2=(1.0, "MOLE")))
        self.assertEqual(out["mismatches"], [])

    def test_missing_node_and_missing_schema_values_are_handled(self):
        out = self.p2.check_feed_flow_units(FakeStreamsDoc({}), feed_schema(S1=(3.24, "MASS")))
        self.assertEqual(out["mismatches"][0]["reason"], "TOTFLOW node not found")
        none_basis = {"io": {"feeds": [{"stream": "S1", "specs": {"flow": {"value": 1.0}}}]}}
        self.assertEqual(self.p2.check_feed_flow_units(FakeStreamsDoc({}), none_basis)["checked"], [])

    def test_validator_phase_turns_an_unfixable_mismatch_into_an_issue(self):
        p2 = self.p2
        orig_run, orig_schema = p2.run_feeds, p2.schema
        p2.run_feeds = lambda doc, print_report=True: {"ok": True, "issues": [], "summary": {"n_issues": 0}}
        p2.schema = feed_schema(S1=(3.24, "MASS"))
        try:
            res = p2.run_validator_phase(FakeStreamsDoc({"S1": FakeUnitNode("kmol/hr", stuck=True)}),
                                         "feeds", base_dir=".", print_report=False)
        finally:
            p2.run_feeds, p2.schema = orig_run, orig_schema
        self.assertFalse(res["ok"])
        issue = res["bundle"]["feeds"]["issues"][0]
        self.assertEqual(issue["rule_id"], "feed_flow_unit_mismatch")
        self.assertEqual(res["summary"]["issues_by_validator"]["feeds"], 1)

    def test_validator_phase_unchanged_when_units_match(self):
        p2 = self.p2
        orig_run, orig_schema = p2.run_feeds, p2.schema
        p2.run_feeds = lambda doc, print_report=True: {"ok": True, "issues": [], "summary": {"n_issues": 0}}
        p2.schema = feed_schema(S1=(3.24, "MASS"))
        try:
            res = p2.run_validator_phase(FakeStreamsDoc({"S1": FakeUnitNode("kg/hr")}), "feeds",
                                         base_dir=".", print_report=False)
        finally:
            p2.run_feeds, p2.schema = orig_run, orig_schema
        self.assertTrue(res["ok"])
        self.assertEqual(res["n_issues"], 0)

    def test_feed_repair_module_uses_the_fixed_setter(self):
        import valid_feed_schema_repair
        self.assertIs(valid_feed_schema_repair.stream_set_total_flow, self.p2.stream_set_total_flow)


if __name__ == "__main__":
    unittest.main()
