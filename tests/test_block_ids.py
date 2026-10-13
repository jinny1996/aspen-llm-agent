"""Block IDs are kept short for Aspen (MHeatX helper blocks are named "$<id>HTR")."""
from paths import SRC_DIR
import copy
import tempfile
import unittest
from pathlib import Path

import yaml

import block_ids as bi
import part1_stage_functions as p1

HERE = Path(__file__).resolve().parent
REAL_DRAFT = SRC_DIR / "run" / "attempt_chat_20261007_232130" / "part1" / "Schema_proto_draft.yaml"


def sch(ids):
    blocks = [{"id": i, "type": "MHeatX" if k == 1 else "PUMP", "specs": {"notes": f"see {i} in the paper"}}
              for k, i in enumerate(ids)]
    edges = [{"stream": f"S{k}", "from": {"block": a}, "to": {"block": b}}
             for k, (a, b) in enumerate(zip(ids, ids[1:]), start=1)]
    return {"flowsheet_schema": {"blocks": blocks, "streams": [], "connectivity": {"edges": edges}},
            "validation": {"topology_consistency": {"block_ids_declared": list(ids),
                                                    "block_ids_referenced_in_edges": list(ids)}}}


class Shorten(unittest.TestCase):
    def test_long_ids_are_renamed_everywhere(self):
        s = sch(["B0101", "B0102", "B0103"])
        m = bi.shorten_block_ids(s)
        self.assertEqual(m, {"B0101": "B1", "B0102": "B2", "B0103": "B3"})
        self.assertEqual([b["id"] for b in s["flowsheet_schema"]["blocks"]], ["B1", "B2", "B3"])
        edges = s["flowsheet_schema"]["connectivity"]["edges"]
        self.assertEqual([(e["from"]["block"], e["to"]["block"]) for e in edges], [("B1", "B2"), ("B2", "B3")])
        topo = s["validation"]["topology_consistency"]
        self.assertEqual(topo["block_ids_declared"], ["B1", "B2", "B3"])
        self.assertEqual(topo["block_ids_referenced_in_edges"], ["B1", "B2", "B3"])

    def test_old_id_is_kept_and_notes_are_not_rewritten(self):
        s = sch(["B0101", "B0102"])
        bi.shorten_block_ids(s)
        b = s["flowsheet_schema"]["blocks"][0]
        self.assertEqual(b["specs"]["original_id"], "B0101")
        self.assertEqual(b["specs"]["notes"], "see B0101 in the paper")        # free text untouched
        s2 = sch(["B0101", "B0102"])
        s2["flowsheet_schema"]["blocks"][0]["specs"]["original_id"] = "P0101"
        bi.shorten_block_ids(s2)
        self.assertEqual(s2["flowsheet_schema"]["blocks"][0]["specs"]["original_id"], "P0101")   # a paper tag wins

    def test_short_ids_are_never_touched(self):
        for ids in (["B1", "B2", "B3"], ["E101", "R101"], ["PUMP", "HX"]):
            s = sch(ids)
            ref = copy.deepcopy(s)
            self.assertEqual(bi.shorten_block_ids(s), {})
            self.assertEqual(s, ref)

    def test_no_collision_with_existing_short_ids(self):
        s = sch(["B1", "B0102", "B3", "B0104"])
        m = bi.shorten_block_ids(s)
        self.assertEqual(m, {"B0102": "B2", "B0104": "B4"})
        ids = [b["id"] for b in s["flowsheet_schema"]["blocks"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_odd_input_does_not_raise(self):
        for s in ({}, {"flowsheet_schema": None}, {"flowsheet_schema": {"blocks": "x"}},
                  {"flowsheet_schema": {"blocks": [{"type": "PUMP"}, None]}}):
            self.assertEqual(bi.shorten_block_ids(s), {})

    @unittest.skipUnless(REAL_DRAFT.exists(), "real draft not present")
    def test_the_draft_that_failed_in_aspen(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "Schema_proto.yaml"
            sch_obj = p1.finalize_schema_from_draft(str(REAL_DRAFT), str(out), meta=None)
            ids = [b["id"] for b in sch_obj["flowsheet_schema"]["blocks"]]
            self.assertEqual(ids, ["B1", "B2", "B3", "B4"])
            self.assertEqual(sch_obj["meta"]["block_ids_shortened"]["B0102"], "B2")
            written = yaml.safe_load(out.read_text(encoding="utf-8"))
            used = {e["from"]["block"] for e in written["flowsheet_schema"]["connectivity"]["edges"] if e.get("from")} | \
                   {e["to"]["block"] for e in written["flowsheet_schema"]["connectivity"]["edges"] if e.get("to")}
            self.assertTrue(used - {None} <= set(ids))                      # every edge endpoint still exists


if __name__ == "__main__":
    unittest.main()
