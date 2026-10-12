"""eval_dev.py -- one-screen regression check of the semantic validator on the DEVELOPMENT cases (A, B, C, D and, since the
case-awareness fix, E).

    python eval_dev.py            # prints a table
    python eval_dev.py --json x   # also writes the per-claim outcomes (for before/after diffs)

Gold: gold_A_draft.yaml (draft), gold_B_draft.yaml (from the SI), phase13/2e_gold_labels.yaml (C, user), gold_D_labels.yaml.
A hold-out case is never listed here; it is scored once with score_semantic.py after the rules are frozen.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

import edc_semantic_validator as sv
from score_semantic import score

HERE = Path(__file__).resolve().parent
P13 = HERE.parent / "phase13"
DB = HERE.parent.parent / "jinny_DB" / "parsed"

CASES = [
    ("A", [P13 / "run/attempt_chat_20261007_204117/part1/Schema_proto.yaml"], DB / "paper2/paper_h2.md", HERE / "gold_A_draft.yaml"),
    ("B", [HERE / "fixtures_semantic/caseB_Schema_proto.pre_review.yaml"], DB / "paper/auto/paper_main.md", HERE / "gold_B_draft.yaml"),
    ("C", [P13 / "run/attempt_chat_20261007_204345/part1/Schema_proto.yaml"], DB / "paper3/paper3.md", P13 / "2e_gold_labels.yaml"),
    ("D", [HERE / f"run/attempt_chat_20261008_{r}/part1/Schema_validated.yaml" for r in ("182632", "182917", "183140")],
     DB / "paper5/paper5.md", HERE / "gold_D_labels.yaml"),
    # E was a hold-out (HOLDOUT_E_RESULT.md, tag holdout-E-rules-frozen).  The case/section-awareness fix was made after looking
    # at its false clearance, so from that commit on E is a DEVELOPMENT case.
    ("E", [HERE / "run/attempt_chat_20261008_204006/part1/Schema_validated.yaml",
           HERE / "run/attempt_chat_20261008_204415/part1/Schema_proto.yaml",
           HERE / "run/attempt_chat_20261008_204708/part1/Schema_proto.yaml"],
     DB / "paper6/paper6.md", HERE / "gold_E_labels.yaml"),
    # F: same paper as E, other unit (Haber-Bosch loop); semi-independent.  Labels written before its result was seen.
    ("F", [HERE / "run/attempt_chat_20261009_004412/part1/Schema_proto.yaml"], DB / "paper6/paper6.md", HERE / "gold_F_labels.yaml"),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    out, tot = {}, {"hit": 0, "unassessed": 0, "false_alarm": 0, "false_clearance": 0, "wrong_value_clearance": 0, "fixed": 0}
    print(f'{"case":<6}{"run":<5}{"FIXED":>6}{"hit":>5}{"unass":>7}{"alarm":>7}{"FALSE-CLEAR":>13}')
    for name, schemas, paper_path, gold_path in CASES:
        gold = yaml.safe_load(gold_path.read_text(encoding="utf-8"))[name]
        paper = paper_path.read_text(encoding="utf-8", errors="replace")
        for i, sp in enumerate(schemas, 1):
            res = score(sv.validate_schema(yaml.safe_load(sp.read_text(encoding="utf-8")), paper), gold)
            t = res["tally"]
            fc = t["false_clearance"] + t["wrong_value_clearance"]
            print(f'{name:<6}{i:<5}{res["n_fixed"]:>6}{t["hit"]:>5}{t["unassessed"]:>7}{t["false_alarm"]:>7}{fc:>13}')
            out[f"{name}{i}"] = {r["id"]: r["outcome"] for r in res["rows"]}
            for k in ("hit", "unassessed", "false_alarm"):
                tot[k] += t[k]
            tot["false_clearance"] += fc
            tot["fixed"] += res["n_fixed"]
    print(f'{"TOTAL":<11}{tot["fixed"]:>6}{tot["hit"]:>5}{tot["unassessed"]:>7}{tot["false_alarm"]:>7}{tot["false_clearance"]:>13}'
          f'   recall={tot["hit"] / tot["fixed"]:.3f}')
    if a.json:
        a.json.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
