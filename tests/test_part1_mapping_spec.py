"""phase14: Part1's required-field check must accept mapping specs such as SPLITTER split_fractions = {out_stream: fraction}."""
import unittest

import part1_stage_functions as p1
from paths import shared_file


class MappingSpecs(unittest.TestCase):
    def test_spec_has_value_accepts_a_filled_mapping(self):
        self.assertTrue(p1.spec_has_value({"S15": 0.99, "S16": 0.01}))
        self.assertTrue(p1.spec_has_value({"S15": {"value": 0.99, "source": "user"}}))

    def test_empty_or_null_mappings_are_still_missing(self):
        self.assertFalse(p1.spec_has_value({}))
        self.assertFalse(p1.spec_has_value({"S15": None, "S16": None}))
        self.assertFalse(p1.spec_has_value(None))

    def test_existing_value_wrappers_are_unchanged(self):
        self.assertTrue(p1.spec_has_value({"value": 0.5, "source": "paper"}))
        self.assertFalse(p1.spec_has_value({"value": None, "source": "paper"}))
        self.assertFalse(p1.spec_has_value({"value": ""}))
        self.assertTrue(p1.spec_has_value(3))
        self.assertTrue(p1.spec_has_value("MOLE"))
        self.assertFalse(p1.spec_has_value(""))

    def test_a_splitter_with_filled_fractions_is_not_reported_missing(self):
        import yaml
        from pathlib import Path
        run = Path(__file__).parent / "run" / "attempt_chat_20261008_235612" / "part1" / "Schema_validated.yaml"
        if not run.exists():
            self.skipTest("real F run not present")
        schema = yaml.safe_load(run.read_text(encoding="utf-8"))
        layer = yaml.safe_load(shared_file("Schema_v5.yaml").read_text(encoding="utf-8"))
        missing = [t["id"] for t in p1.validate_schema_for_execution(schema, layer)]
        self.assertNotIn("block_B13_split_fractions", missing)


if __name__ == "__main__":
    unittest.main()
