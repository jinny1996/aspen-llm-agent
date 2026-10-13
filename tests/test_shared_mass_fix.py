"""The shared aspen_code_v1.stream_set_total_flow (fixed 2026-10-08): MASS really lands in kg/hr."""
import importlib.util
import unittest
from pathlib import Path

from paths import shared_file

SHARED = shared_file("aspen_code_v1.py")


def load_shared():
    spec = importlib.util.spec_from_file_location("aspen_code_v1_shared_under_test", SHARED)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeNode:
    """kg/hr only for unit code `kg_code`; any other code leaves a mole unit (like a different unit set)."""
    def __init__(self, kg_code=3, honest=True):
        self.Value, self.UnitString, self.kg_code, self.honest, self.calls = 0.0, "kmol/hr", kg_code, honest, []

    def SetValueUnitAndBasis(self, value, code, basis):
        self.calls.append((code, basis))
        if code == self.kg_code:
            self.UnitString = "kg/hr"
            self.Value = value if self.honest else value * 2
        else:
            self.UnitString = "kmol/hr"


class FakeDoc:
    def __init__(self, node):
        self.node, self.paths = node, []
        self.Tree = self

    def FindNode(self, path):
        self.paths.append(path)
        return self.node


@unittest.skipUnless(SHARED.exists(), "shared aspen_code_v1.py not found")
class SharedFix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.mod = load_shared()
        except Exception as exc:                       # e.g. win32com missing on this machine
            raise unittest.SkipTest(f"cannot import shared aspen_code_v1: {exc!r}")

    def test_mass_sets_kg_per_hr(self):
        node = FakeNode()
        self.mod.stream_set_total_flow(FakeDoc(node), "S1", 303823.76, base="MASS")
        self.assertEqual(node.UnitString, "kg/hr")
        self.assertEqual(node.Value, 303823.76)
        self.assertEqual(node.calls, [(3, "MASS")])

    def test_mole_is_unchanged(self):
        node = FakeNode()
        self.mod.stream_set_total_flow(FakeDoc(node), "S1", 10.0)             # default base
        self.mod.stream_set_total_flow(FakeDoc(node), "S1", 12.5, base="mole")
        self.assertEqual((node.Value, node.UnitString, node.calls), (12.5, "kmol/hr", []))

    def test_other_unit_code_is_searched(self):
        node = FakeNode(kg_code=7)
        self.mod.stream_set_total_flow(FakeDoc(node), "S1", 5.0, base="MASS")
        self.assertEqual(node.UnitString, "kg/hr")
        self.assertIn((7, "MASS"), node.calls)

    def test_no_kg_unit_available_raises_instead_of_falling_back(self):
        with self.assertRaises(RuntimeError):
            self.mod.stream_set_total_flow(FakeDoc(FakeNode(kg_code=99)), "S1", 5.0, base="MASS")

    def test_a_value_that_does_not_read_back_raises(self):
        with self.assertRaises(RuntimeError):
            self.mod.stream_set_total_flow(FakeDoc(FakeNode(honest=False)), "S1", 5.0, base="MASS")

    def test_missing_node_raises(self):
        with self.assertRaises(RuntimeError):
            self.mod.stream_set_total_flow(FakeDoc(None), "S1", 5.0, base="MASS")

    def test_stream_define_still_uses_the_mole_path(self):
        src = SHARED.read_text(encoding="utf-8")
        self.assertIn('stream_set_total_flow(doc, stream_name, total_flow, base="MOLE")', src)


if __name__ == "__main__":
    unittest.main()
