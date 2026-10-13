"""phase14: the Part 2 validators accept VALVE and FSPLIT blocks (the shared 2608 validators had no rules for them)."""
import unittest
from unittest import mock

import part2_stage_functions as p2
import valid_block_specification as vbs
import valid_connectivity as vc


def run_one(btype, ports):
    with mock.patch.object(vc, "get_block_type", return_value=btype), \
            mock.patch.object(vc, "get_port_connections", return_value=ports):
        return vc.validate_connectivity_one(None, "B1", ["S1", "S2", "S3"])


class ConnectivityRules(unittest.TestCase):
    def test_valve_in_out_is_clean(self):
        self.assertEqual(run_one("VALVE", {"F(IN)": ["S1"], "P(OUT)": ["S2"]})[1], [])

    def test_valve_without_outlet_is_reported(self):
        miss = run_one("VALVE", {"F(IN)": ["S1"], "P(OUT)": []})[1]
        self.assertEqual(len(miss), 1)

    def test_fsplit_one_in_two_out_is_clean(self):
        self.assertEqual(run_one("FSPLIT", {"F(IN)": ["S1"], "P(OUT)": ["S2", "S3"]})[1], [])

    def test_fsplit_with_a_single_outlet_is_reported(self):
        self.assertEqual(len(run_one("FSPLIT", {"F(IN)": ["S1"], "P(OUT)": ["S2"]})[1]), 1)

    def test_unknown_types_still_go_to_the_shared_validator(self):
        self.assertEqual(run_one("MYSTERY", {})[1][0]["rule_id"], "block_type_not_supported")

    def test_heater_unchanged(self):
        self.assertEqual(run_one("HEATER", {"F(IN)": ["S1"], "P(OUT)": ["S2"]})[1], [])
        self.assertEqual(len(run_one("HEATER", {"F(IN)": [], "P(OUT)": ["S2"]})[1]), 1)


class BlockRules(unittest.TestCase):
    def setUp(self):
        self.rules = p2._load_block_rules(str(p2._PROJECT_ROOT))

    def test_valve_and_fsplit_have_rules(self):
        for t in ("VALVE", "FSPLIT"):
            self.assertIsNotNone(vbs._resolve_rule(self.rules, t), t)

    def test_existing_rules_untouched(self):
        self.assertIn("spec_opt_required_and_allowed", str(self.rules["blocks"]["FLASH2"]))


if __name__ == "__main__":
    unittest.main()
