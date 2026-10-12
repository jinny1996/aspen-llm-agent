"""score_structure.py -- Structural F1 of Part1 schemas against gold_structure.yaml (draft gold, read from the figures).

    python score_structure.py             # table over every Part1 run found for cases A-F

Block F1 : a gold block counts as found when a schema block carries its figure label (name / paper_id) AND the right block type.
Edge F1  : block -> block connections of the schema (stream edges with a block on both ends), mapped onto gold block ids.
Per case the mean and the min-max over runs are reported.  Final schema of a run = Schema_validated.yaml if present, else Schema_proto.yaml.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

import yaml

from eval_dev import DB, HERE, P13
from make_gold_sheet_holdout import final_schema

GOLD = yaml.safe_load((HERE / "gold_structure.yaml").read_text(encoding="utf-8"))
PAPER_OF = {"A": "paper2", "B": "paper/auto/paper_main", "C": "paper3", "D": "paper5", "E": "paper6", "F": "paper6"}


def norm_type(t):
    return str(t or "").upper()


def match_blocks(schema, gold_blocks):
    """-> {schema block id: gold id}.  A schema block is mapped to the first gold block whose pattern hits its label and type."""
    out = {}
    for b in (schema.get("flowsheet_schema") or {}).get("blocks") or []:
        specs = b.get("specs") if isinstance(b.get("specs"), dict) else {}
        label = f'{b.get("name") or ""} {specs.get("paper_id") or ""}'.replace("_", " ")     # E1_feed_preheater -> E1 feed preheater
        for g in gold_blocks:
            if any(re.search(p, label, re.I) for p in g["match"]) and norm_type(b.get("type")) == g["type"]:
                if g["id"] not in out.values():
                    out[str(b.get("id"))] = g["id"]
                    break
    return out


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return (2 * p * r / (p + r)) if p + r else 0.0, p, r


def score(schema, case):
    g = GOLD[case]
    m = match_blocks(schema, g["blocks"])
    n_schema_blocks = len((schema.get("flowsheet_schema") or {}).get("blocks") or [])
    bf = prf(len(m), n_schema_blocks - len(m), len(g["blocks"]) - len(m))
    pred = set()
    for e in (schema.get("flowsheet_schema") or {}).get("connectivity", {}).get("edges") or []:
        fb = (e.get("from") or {}).get("block") if isinstance(e.get("from"), dict) else None
        tb = (e.get("to") or {}).get("block") if isinstance(e.get("to"), dict) else None
        if fb and tb:
            pred.add((m.get(str(fb), f"extra:{fb}"), m.get(str(tb), f"extra:{tb}")))
    gold = {tuple(x) for x in g["edges"]}
    ef = prf(len(pred & gold), len(pred - gold), len(gold - pred))
    return {"block_f1": bf[0], "block_p": bf[1], "block_r": bf[2], "edge_f1": ef[0], "edge_p": ef[1], "edge_r": ef[2],
            "missing_blocks": sorted({x["id"] for x in g["blocks"]} - set(m.values())),
            "missing_edges": sorted(gold - pred), "extra_edges": sorted(pred - gold)}


def runs_for(case, with_sequence=None):
    """with_sequence: True = first request lists the units in order ("->"), False = it does not, None = any."""
    dirs = []
    for root in (P13 / "run", HERE / "run"):
        for d in sorted(root.glob("attempt_chat_*")):
            sp = final_schema(d)
            ri = d / "runner_input.json"
            if sp is None or not ri.exists():
                continue
            try:
                pin = json.loads(ri.read_text(encoding="utf-8"))["part1_input"]
            except Exception:
                continue
            path = pin["paper_text_path"].replace("\\", "/")
            has_seq = "->" in pin.get("user_input_text", "")
            if with_sequence is not None and has_seq != with_sequence:
                continue
            if PAPER_OF[case] not in path:
                continue
            if case in ("E", "F"):
                prompt = pin.get("user_input_text", "")
                if ("Haber" in prompt) != (case == "F") or "paper6.md" not in path:
                    continue
            if case == "B" and "paper_main" not in path:
                continue
            if case == "D" and "paper5" not in path:
                continue
            dirs.append((d, sp))
    return dirs


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    md = ["| Case | 첫 요청 | 실행 수 | Block F1 (평균, 범위) | Edge F1 (평균, 범위) | 빠진 블록 / 연결 (최악 사례) |", "|---|---|---|---|---|---|"]
    detail = {}
    for case, seq in [(c, s) for c in "ABCDEF" for s in (True, False)]:
        res = []
        for d, sp in runs_for(case, seq):
            res.append((d.name, score(yaml.safe_load(sp.read_text(encoding="utf-8")), case)))
        label = "유닛 순서 포함" if seq else "유닛 순서 없음"
        if not res:
            if not seq or case != "A":
                md.append(f"| {case} | {label} | 0 | - | - | - |")
            continue
        bf = [r["block_f1"] for _, r in res]
        ef = [r["edge_f1"] for _, r in res]
        worst = min(res, key=lambda x: x[1]["edge_f1"] + x[1]["block_f1"])[1]
        miss = f'블록 {worst["missing_blocks"] or "-"} / 연결 {worst["missing_edges"][:3] or "-"}' if (worst["missing_blocks"] or worst["missing_edges"]) else "-"
        md.append(f"| {case} | {label} | {len(res)} | {statistics.mean(bf):.2f} ({min(bf):.2f}–{max(bf):.2f}) | "
                  f"{statistics.mean(ef):.2f} ({min(ef):.2f}–{max(ef):.2f}) | {miss} |")
        detail[f"{case}{'' if seq else 'n'}"] = res
    text = "\n".join(md)
    print(text)
    (HERE / "meeting" / "STRUCTURE_F1.md").write_text(text + "\n", encoding="utf-8")
    (HERE / "meeting" / "structure_f1_detail.json").write_text(
        json.dumps({c: [{"run": n, **r} for n, r in rs] for c, rs in detail.items()}, ensure_ascii=False, indent=1, default=list), encoding="utf-8")


if __name__ == "__main__":
    main()
