"""score_semantic.py -- compare the semantic validator with gold labels (FIXED / OPEN / ABSENT).

  python score_semantic.py <Schema.yaml> <paper.md> <gold.yaml> <CASE> [--json out.json]

How a verdict is scored against a gold label
  gold FIXED   positive (DIRECT_SUPPORT / PROCESS_LEVEL_SUPPORT) -> hit; the value must also equal gold
               correct_value / acceptable_values, otherwise it is a WRONG-VALUE CLEARANCE.
               POTENTIAL_CROSS_EQUIPMENT_BORROWING / CLEAR_ERROR -> false alarm (the paper does fix it).
               anything else -> unassessed (validator stayed silent).
  gold OPEN / ABSENT
               positive -> FALSE CLEARANCE (the worst outcome: it would tell the user not to worry).
               everything else -> fine (flagged, undecided or declared as an assumption).
Gold entries with no matching claim in the schema are listed, never silently dropped.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import edc_semantic_validator as sv

POSITIVE = ("DIRECT_SUPPORT", "PROCESS_LEVEL_SUPPORT")
ALARM = ("POTENTIAL_CROSS_EQUIPMENT_BORROWING", "WRONG_VARIABLE", "NUMERIC_OR_UNIT_CONFLICT")


def _close(a: Any, b: Any) -> bool:
    try:
        return abs(float(a) - float(b)) <= max(1e-6, 1e-3 * abs(float(b)))
    except (TypeError, ValueError):
        return False


def value_ok(claim_value: Any, gold: Dict[str, Any]) -> Optional[bool]:
    """True/False when the gold gives numbers to compare with, None when it does not (ratios, text)."""
    cands: List[Any] = []
    if isinstance(gold.get("correct_value"), (int, float)):
        cands.append(gold["correct_value"])
    cands += [v for v in gold.get("acceptable_values") or [] if isinstance(v, (int, float))]
    if not cands or not isinstance(claim_value, (int, float)):
        return None
    return any(_close(claim_value, c) for c in cands)


def score(report: Dict[str, Any], gold: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    claims = {c["id"]: c for c in report["claims"]}
    rows, tally = [], {"hit": 0, "unassessed": 0, "false_alarm": 0, "false_clearance": 0,
                       "wrong_value_clearance": 0, "ok_not_cleared": 0}
    for gid, g in gold.items():
        c = claims.get(gid)
        if c is None:
            rows.append({"id": gid, "gold": g["label"], "verdict": None, "outcome": "no_claim_in_schema"})
            continue
        rel, pos = c["evidence_relation"], c["evidence_relation"] in POSITIVE
        if g["label"] == "FIXED":
            if pos:
                vo = value_ok(c["value"], g)
                outcome = "wrong_value_clearance" if vo is False else "hit"
            elif rel in ALARM:
                outcome = "false_alarm"
            else:
                outcome = "unassessed"
        else:
            outcome = "false_clearance" if pos else "ok_not_cleared"
        tally[outcome] = tally.get(outcome, 0) + 1
        rows.append({"id": gid, "gold": g["label"], "confidence": g.get("confidence"), "value": c["value"],
                     "verdict": rel, "outcome": outcome})
    n_fixed = sum(1 for g in gold.values() if g["label"] == "FIXED")
    n_other = len(gold) - n_fixed
    return {"rows": rows, "tally": tally, "n_fixed": n_fixed, "n_open_or_absent": n_other,
            "fixed_recall": round(tally["hit"] / n_fixed, 3) if n_fixed else None,
            "false_clearances": tally["false_clearance"] + tally["wrong_value_clearance"]}


def main(argv: Optional[List[str]] = None) -> int:
    import yaml
    ap = argparse.ArgumentParser()
    ap.add_argument("schema", type=Path)
    ap.add_argument("paper", type=Path)
    ap.add_argument("gold", type=Path)
    ap.add_argument("case")
    ap.add_argument("--json", type=Path)
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    gold = yaml.safe_load(a.gold.read_text(encoding="utf-8"))[a.case]
    rep = sv.validate_schema(yaml.safe_load(a.schema.read_text(encoding="utf-8")),
                             a.paper.read_text(encoding="utf-8", errors="replace"))
    res = score(rep, gold)
    for r in res["rows"]:
        print(f'{r["id"]:<18}{r["gold"]:<8}{str(r.get("confidence") or ""):<8}{str(r["verdict"]):<38}{r["outcome"]}')
    print(json.dumps({k: res[k] for k in ("tally", "n_fixed", "n_open_or_absent", "fixed_recall", "false_clearances")},
                     ensure_ascii=False, indent=2))
    if a.json:
        a.json.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
