"""EDC-1 evaluation on EXISTING runs of the methanol-aromatization paper (unit of Fig. 2:
P0101 -> E0101 -> R0101 -> E0102).  No API/Aspen cost: it only re-reads old schemas.

Caveat: these runs come from earlier Part1 versions (April 2026), not the phase12/13
pipeline.  The current HITL selector is applied post hoc to those schemas.
Output: printed summary + eval_edc1_aroma.json
"""
import collections
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import dof_ledger as dl  # noqa: E402
from part1_stage_functions import collect_schema_human_review_items as current_selector  # noqa: E402

RUNS = HERE.parent.parent / "runs"
PAPER = HERE.parent.parent / "jinny_DB" / "parsed" / "paper" / "auto" / "paper.md"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    paper = PAPER.read_text(encoding="utf-8", errors="replace")
    runs = [d for d in sorted(RUNS.glob("2026042*_Aroma_*")) if (d / "Schema_validated.yaml").exists()]
    per_run, per_var = [], collections.defaultdict(list)
    paper_quote = collections.Counter()
    for d in runs:
        schema = yaml.safe_load((d / "Schema_validated.yaml").read_text(encoding="utf-8"))
        L = dl.build_ledger(schema)
        dl.verify_evidence(L, paper)
        non_paper = 0
        for e in L["entries"]:
            if e["status"] != dl.PROVIDED or not e["required"]:
                continue
            p = e.get("provenance") or {}
            if not p.get("tracked"):
                continue
            per_var[e["id"]].append({"source": p["source"], "value": p["value"],
                                     "confidence": p["confidence"], "quote": p["quote"],
                                     "run": d.name})
            if p["source"] == "paper":
                paper_quote[e.get("evidence_check")] += 1   # grounding only judged for 'paper' claims
            else:
                non_paper += 1
        asked = current_selector(schema)
        per_run.append({"run": d.name, "required": L["summary"]["required"],
                        "missing": L["summary"]["missing"], "non_paper_required": non_paper,
                        "current_selector_questions": len(asked),
                        "by_source": L["summary"]["provided_by_source"]})

    print(f"runs analysed: {len(runs)} (validated schemas)")
    print("non-paper required values per run:", sorted({r['non_paper_required'] for r in per_run}))
    print("current selector questions per run:", [r["current_selector_questions"] for r in per_run])
    print("'paper' quote grounding:", dict(paper_quote))
    print()
    summary_vars = {}
    for var, rows in per_var.items():
        srcs = collections.Counter(r["source"] for r in rows)
        vals = collections.Counter(str(float(r["value"])) if isinstance(r["value"], (int, float))
                                   else str(r["value"]) for r in rows)
        summary_vars[var] = {"n": len(rows), "sources": dict(srcs), "values": dict(vals)}
        print(f"{var:16} n={len(rows):2} sources={dict(srcs)} values={dict(vals)}")

    # values taken from a different unit of the paper (2.85 bar is the F0102 organic-phase release pressure)
    cross = [dict(var=v, **r) for v, rows in per_var.items() for r in rows
             if isinstance(r["value"], (int, float)) and abs(float(r["value"]) - 2.85) < 1e-9]
    print("\nvalues equal to 2.85 (pressure the paper states for ANOTHER unit):", len(cross))
    for c in cross:
        print(f"  {c['var']} source={c['source']} conf={c['confidence']} quote={c['quote']!r}")

    (HERE / "eval_edc1_aroma.json").write_text(
        json.dumps({"per_run": per_run, "per_variable": summary_vars,
                    "paper_quote_grounding": dict(paper_quote), "cross_unit_values": cross},
                   ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
