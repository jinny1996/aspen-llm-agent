"""collect_results.py -- one results table over finished pipeline runs (read-only; no LLM, no Aspen).

    python collect_results.py                      # default run list below -> MEETING_TABLE.md / meeting_table.json
    python collect_results.py --run "A=../phase13/run/attempt_chat_20261006_225749" --run "D=run/attempt_chat_20261008_182632"

Per run: what Part1 built (blocks / feeds), how many specification values it needed and where they came from, what the semantic
validator says about them, how many questions the EDC policy would ask, tokens and wall time per stage, and -- when the run went
on -- the Part2 / Part3 outcome (Aspen run, severe / error / warning counts, repair cycles).
Runs that stopped after Part1 show "-" for Part2 / Part3.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

import dof_ledger as dl
import edc_policy as ep
import edc_semantic_validator as sv
from make_gold_sheet_holdout import final_schema

HERE = Path(__file__).resolve().parent
P13 = HERE.parent / "phase13"

DEFAULT_RUNS = [   # label, run dir  (latest successful full run per case; D/E/F: Part1 runs, Part2/3 only if they were run)
    ("A ATR", P13 / "run/attempt_chat_20261006_225749"),
    ("B methanol aromatization", P13 / "run/attempt_chat_20261008_134658"),
    ("C methanation", P13 / "run/attempt_chat_20261007_222124"),
    ("D LOHC (MCH)", HERE / "run/attempt_chat_20261008_223038"),
    ("E ammonia / SMR", HERE / "run/attempt_chat_20261008_230526"),
    ("F ammonia / Haber-Bosch", HERE / "run/attempt_chat_20261009_004412"),   # Part 2 stopped at validation (Valve/FSplit unsupported in shared validator)
]


def _jl(path: Path) -> List[Dict[str, Any]]:
    out = []
    if path.exists():
        for ln in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


def _stage_usage(run: Path, part: str) -> Dict[str, Any]:
    rows = _jl(run / part / "token_usage.jsonl")
    if not rows:
        return {"tokens": None, "seconds": None}
    tok = sum(int(r.get("total_tokens") or 0) for r in rows)
    ts = []
    for r in rows:
        for k in ("started_at", "finished_at"):
            try:
                ts.append(datetime.fromisoformat(r[k]))
            except (KeyError, ValueError, TypeError):
                pass
    return {"tokens": tok, "seconds": int((max(ts) - min(ts)).total_seconds()) if len(ts) > 1 else None}


def collect(label: str, run: Path) -> Dict[str, Any]:
    ri = json.loads((run / "runner_input.json").read_text(encoding="utf-8"))["part1_input"]
    paper_path = Path(ri["paper_text_path"])
    paper = paper_path.read_text(encoding="utf-8", errors="replace")
    sp = final_schema(run)
    schema = yaml.safe_load(sp.read_text(encoding="utf-8"))
    fs = schema.get("flowsheet_schema") or {}
    types = collections.Counter(str(b.get("type")) for b in fs.get("blocks") or [])
    ledger = dl.build_ledger(schema)
    prov = [e for e in ledger["entries"] if e["status"] == dl.PROVIDED and e["required"] and not e.get("analysis_only")]
    src = collections.Counter(((e.get("provenance") or {}).get("source") or "none") for e in prov)
    base = ep.classify_schema(schema, paper, apply_v2=False)
    asked = ep.select_questions(base, 5)["asked"]
    rep = sv.validate_schema(schema, paper)["summary"]
    st = {}
    try:
        st = json.loads((run / "part1" / "part1_status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    p3 = None
    if (run / "part3" / "part3_result.json").exists():
        j = json.loads((run / "part3" / "part3_result.json").read_text(encoding="utf-8"))
        p3 = {"success": bool(j.get("success")), "counts": j.get("parse_counts"), "repairs": j.get("repair_cycle_count"),
              "bkp": bool(j.get("final_bkp_path"))}
    p2_files = sorted(p.name for p in (run / "part2").glob("*")) if (run / "part2").exists() else []
    return {
        "label": label, "run": run.name, "paper": paper_path.name, "schema_file": sp.name,
        "blocks": sum(types.values()), "block_types": dict(types),
        "feeds": len((schema.get("io") or {}).get("feeds") or []),
        "values_needed": len(prov), "value_sources": dict(src),
        "edc_classes": base["class_counts"], "edc_questions": len(asked), "edc_question_ids": [r["id"] for r in asked],
        "validator": {"by_relation": rep["by_relation"], "coverage": rep["coverage"]},
        "part1_stage": st.get("stage"),
        "usage": {p: _stage_usage(run, p) for p in ("part1", "part2", "part3")},
        "part2_files": len(p2_files), "part2_bkp": any(f.endswith(".bkp") for f in p2_files),
        "part3": p3,
    }


def _fmt_usage(u: Dict[str, Any]) -> str:
    if not u or u["tokens"] is None:
        return "-"
    return f'{u["tokens"] / 1000:.0f}k tok' + (f' / {u["seconds"] // 60}m{u["seconds"] % 60:02d}s' if u["seconds"] is not None else "")


def to_markdown(rows: List[Dict[str, Any]]) -> str:
    md = ["| 논문 | 블록 | 필요한 사양값 | 출처(paper / inferred / default / llm) | 검증기: 근거 확인 / 가정 선언 / 불확정 | EDC 질문 | Part1 비용 | Part2/3 결과 |",
          "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        s = r["value_sources"]
        vr = r["validator"]["by_relation"]
        sup = vr.get("DIRECT_SUPPORT", 0) + vr.get("PROCESS_LEVEL_SUPPORT", 0)
        decl = vr.get("ASSUMPTION_DECLARED", 0)
        und = sum(vr.values()) - sup - decl
        p3 = r["part3"]
        if p3:
            c = p3["counts"] or {}
            res = (f'Aspen 실행 {"성공" if p3["success"] else "실패"} (severe {c.get("severe", "?")}, error {c.get("errors", "?")}, '
                   f'warning {c.get("warnings", "?")}; 수리 {p3["repairs"]}회)')
        else:
            res = "Part1까지만 실행"
        md.append(f'| {r["label"]} | {r["blocks"]} ({", ".join(f"{k} {v}" for k, v in r["block_types"].items())}) | {r["values_needed"]} | '
                  f'{s.get("paper", 0)} / {s.get("inferred", 0)} / {s.get("default", 0)} / {s.get("llm", 0)} | '
                  f'{sup} / {decl} / {und} | {r["edc_questions"]} | {_fmt_usage(r["usage"]["part1"])} | {res} |')
    return "\n".join(md)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append", help='"label=path/to/run_dir" (repeatable); default: built-in list')
    ap.add_argument("--out-prefix", type=Path, default=HERE / "meeting")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    runs = [(x.split("=", 1)[0], Path(x.split("=", 1)[1])) for x in a.run] if a.run else DEFAULT_RUNS
    rows = []
    for label, d in runs:
        if not d.exists():
            print("skip (missing):", d)
            continue
        rows.append(collect(label, d))
    md = to_markdown(rows)
    print(md)
    Path(str(a.out_prefix) + "_table.md").write_text(md + "\n", encoding="utf-8")
    Path(str(a.out_prefix) + "_table.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
