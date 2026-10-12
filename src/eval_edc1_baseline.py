"""EDC-1 preliminary evaluation on EXISTING Part1 outputs (no API/Aspen cost).

For every run under phase12/run/*/part1/Schema_proto.yaml:
  * build the DoF ledger
  * verify evidence quotes against the paper text used for that run
  * compare against what the CURRENT human-review selector would ask
    (part1_stage_functions.collect_schema_human_review_items)
Output: printed table + eval_edc1_baseline.json next to this file.
"""
import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
RUN_ROOT = HERE.parent / "phase12" / "run"

import dof_ledger as dl  # noqa: E402

try:
    sys.path.insert(0, str(HERE))
    from part1_stage_functions import collect_schema_human_review_items  # noqa: E402
except Exception as exc:  # pragma: no cover
    collect_schema_human_review_items = None
    print("WARN: could not import current review selector:", repr(exc))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    rows = []
    for run_dir in sorted(RUN_ROOT.glob("attempt_*")):
        sp = run_dir / "part1" / "Schema_proto.yaml"
        ri = run_dir / "runner_input.json"
        if not sp.exists() or not ri.exists():
            continue
        schema = yaml.safe_load(sp.read_text(encoding="utf-8"))
        paper_path = Path(json.loads(ri.read_text(encoding="utf-8"))["part1_input"]["paper_text_path"])
        paper_text = paper_path.read_text(encoding="utf-8", errors="replace") if paper_path.exists() else ""
        L = dl.build_ledger(schema)
        ungrounded = []
        if paper_text:
            dl.verify_evidence(L, paper_text)
            for e in L["entries"]:
                if e.get("evidence_check") in ("NOT_FOUND", "PARTIAL"):
                    ungrounded.append({
                        "id": e["id"], "check": e["evidence_check"],
                        "numbers_grounded": e.get("quote_numbers_grounded"),
                        "quote": e["provenance"]["quote"]})
        cur = (collect_schema_human_review_items(schema)
               if collect_schema_human_review_items else None)
        s = L["summary"]
        rows.append({
            "run": run_dir.name, "paper": paper_path.name, "paper_found": bool(paper_text),
            "required": s["required"], "provided": s["provided"], "missing": s["missing"],
            "missing_ids": s["missing_ids"],
            "provided_by_source": s["provided_by_source"],
            "provided_without_provenance": s["provided_without_provenance"],
            "default_candidates": s["default_candidates"],
            "evidence_check_by_source": s.get("evidence_check_by_source"),
            "strict_ungrounded_quotes": ungrounded,
            "current_review_items": None if cur is None else len(cur),
            "current_review_paths": None if cur is None else [c["field_path"] for c in cur],
        })
    for r in rows:
        print("=" * 78)
        print(r["run"], "| paper:", r["paper"], "| found:", r["paper_found"])
        print(f"  required={r['required']} provided={r['provided']} missing={r['missing']} {r['missing_ids']}")
        print("  provided_by_source:", r["provided_by_source"],
              "| no-provenance:", r["provided_without_provenance"])
        print("  evidence_check_by_source:", r["evidence_check_by_source"])
        for u in r["strict_ungrounded_quotes"]:
            print(f"    ungrounded[{u['check']}] {u['id']} numbers_grounded={u['numbers_grounded']} | {u['quote']}")
        print("  default candidates (unverified shortlist):", r["default_candidates"])
        print("  current HITL selector asks:", r["current_review_items"], r["current_review_paths"])
    (HERE / "eval_edc1_baseline.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
