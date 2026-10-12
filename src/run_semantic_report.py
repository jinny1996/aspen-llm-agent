"""run_semantic_report.py -- P0-1 CLI: semantic evidence report for a saved Part1 schema (report-only).

  python run_semantic_report.py <Schema_*.yaml> <paper.md> [--out report.json]

Reads the schema and the paper, writes nothing next to them.  Not wired into the pipeline.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

import edc_semantic_validator as sv


def main(argv: Optional[List[str]] = None) -> int:
    import yaml
    ap = argparse.ArgumentParser(description="Semantic evidence report (report-only)")
    ap.add_argument("schema", type=Path)
    ap.add_argument("paper", type=Path)
    ap.add_argument("--out", type=Path, help="write the full report JSON here")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    schema = yaml.safe_load(args.schema.read_text(encoding="utf-8"))
    paper = args.paper.read_text(encoding="utf-8", errors="replace")
    rep = sv.validate_schema(schema, paper)
    for c in rep["claims"]:
        print(f'{c["id"]:<22} {str(c["value"]):<10} [{c["source"]}] {c["evidence_relation"]:<36} '
              f'{c["category"]:<22} edc={c["edc_policy_class"]}')
    print(json.dumps(rep["summary"], ensure_ascii=False, indent=2))
    if args.out:
        args.out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
