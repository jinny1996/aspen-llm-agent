"""phase14: an intermediate FLASH2 whose V / L outlets feed other blocks needs no placeholder product streams."""
import unittest

import part1_stage_functions as p1


def schema(edges, blocks):
    return {"flowsheet_schema": {"blocks": blocks, "streams": [{"id": e["stream"]} for e in edges], "connectivity": {"edges": edges}}}


def edge(stream, fb, fr, tb, tr="IN"):
    return {"stream": stream, "from": {"block": fb, "port_role": fr}, "to": {"block": tb, "port_role": tr}}


class FlashOutlets(unittest.TestCase):
    FLASH = [{"id": "F1", "type": "FLASH2"}, {"id": "B2", "type": "HEATER"}, {"id": "B3", "type": "HEATER"}]

    def test_outlets_that_feed_downstream_blocks_are_present(self):
        s = schema([edge("S1", "F1", "V", "B2"), edge("S2", "F1", "L", "B3")], self.FLASH)
        self.assertEqual(p1.validate_multi_outlet_product_coverage(s), [])

    def test_terminal_outlets_are_present(self):
        s = schema([edge("S1", "F1", "V", None, "V"), edge("S2", "F1", "L", None, "L")], self.FLASH)
        self.assertEqual(p1.validate_multi_outlet_product_coverage(s), [])

    def test_a_really_missing_outlet_is_still_reported(self):
        s = schema([edge("S1", "F1", "V", "B2")], self.FLASH)
        ids = [t["id"] for t in p1.validate_multi_outlet_product_coverage(s)]
        self.assertTrue(any("F1_L_product" in i for i in ids))
        self.assertFalse(any("F1_V_product" in i for i in ids))


if __name__ == "__main__":
    unittest.main()
