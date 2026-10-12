"""score_structure_aspen.py -- Structural F1 of the FINAL ASPEN FILE (read from the exported .inp), against gold_structure.yaml.

    python score_structure_aspen.py

Part1's Schema is scored by score_structure.py.  This script scores what actually exists in the Aspen model after Part 2 / 3
(codegen + repairs), whether or not the simulation then converged: the newest `*_run.inp` that Aspen wrote in part2/ or part3/ is
parsed (BLOCK <id> <model> headers + the FLOWSHEET section `BLOCK B1 IN=... OUT=...`).  Aspen block ids are the schema ids (B1, B2, ...),
so the Part1 schema maps them to the gold blocks.  A block missing from the Aspen file, a wrongly typed block or a lost connection
lowers F1 -- this is where a codegen / Aspen-side failure shows up even though the Schema looked complete.
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

import yaml

from eval_dev import HERE, P13
from make_gold_sheet_holdout import final_schema
from score_structure import GOLD, PAPER_OF, match_blocks, prf

ASPEN_TO_SCHEMA = {"MIXER": "MIXER", "MHEATX": "MHEATX", "HEATER": "HEATER", "RGIBBS": "RGIBBS", "FLASH2": "FLASH2", "COMPR": "COMPRESSOR",
                   "PUMP": "PUMP", "VALVE": "VALVE", "FSPLIT": "SPLITTER", "HEATX": "MHEATX"}


def parse_inp(text: str):
    types, ins, outs = {}, {}, {}
    in_flow = False
    for ln in text.splitlines():
        s = ln.strip()
        if re.match(r"^FLOWSHEET\b", s, re.I):
            in_flow = True
            continue
        if in_flow:
            m = re.match(r"^BLOCK\s+(\S+)\s+(?:IN=(.*?))?\s*(?:OUT=(.*))?$", s, re.I)
            if m:
                ins[m.group(1)] = (m.group(2) or "").split()
                outs[m.group(1)] = (m.group(3) or "").split()
                continue
            if s == "" or re.match(r"^[A-Z-]+\s*$", s):
                if s == "":
                    in_flow = False
                continue
        m = re.match(r"^BLOCK\s+(\S+)\s+([A-Za-z0-9-]+)\s*$", s, re.I)
        if m and not in_flow:
            types[m.group(1)] = m.group(2).upper()
    edges = set()
    for x, o in outs.items():
        for y, i in ins.items():
            if x != y and set(o) & set(i):
                edges.add((x, y))
    return types, edges


def final_inp(run: Path):
    cands = [p for d in ("part2", "part3") for p in (run / d).glob("*_run.inp")] if run.exists() else []
    return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def score_run(run: Path, case: str):
    inp = final_inp(run)
    sp = final_schema(run)
    if inp is None or sp is None:
        return None
    schema = yaml.safe_load(sp.read_text(encoding="utf-8"))
    g = GOLD[case]
    mapping = match_blocks(schema, g["blocks"])                       # schema/Aspen id -> gold id (type from the SCHEMA)
    types, edges = parse_inp(inp.read_text(encoding="utf-8", errors="replace"))
    gold_type = {b["id"]: b["type"] for b in g["blocks"]}
    ok = {a: m for a, m in mapping.items() if ASPEN_TO_SCHEMA.get(types.get(a, ""), "") == gold_type[m]}
    bf = prf(len(ok), len(types) - len(ok), len(g["blocks"]) - len(ok))
    pred = {(ok.get(a, f"extra:{a}"), ok.get(b, f"extra:{b}")) for a, b in edges}
    gold = {tuple(x) for x in g["edges"]}
    ef = prf(len(pred & gold), len(pred - gold), len(gold - pred))
    return {"run": run.name, "inp": inp.name, "block_f1": bf[0], "edge_f1": ef[0],
            "missing_blocks": sorted(set(gold_type) - set(ok.values())), "missing_edges": sorted(gold - pred)}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    rows = []
    for root in (P13 / "run", HERE / "run"):
        for d in sorted(root.glob("attempt_chat_*")):
            if not (d / "part2").is_dir() or not any((d / "part2").iterdir()):
                continue
            try:
                pin = json.loads((d / "runner_input.json").read_text(encoding="utf-8"))["part1_input"]
            except Exception:
                continue
            path = pin["paper_text_path"].replace("\\", "/")
            case = next((c for c in "ABCDEF" if PAPER_OF[c] in path and (c not in "EF" or ("Haber" in pin.get("user_input_text", "")) == (c == "F"))), None)
            if case is None or "->" not in pin.get("user_input_text", "") and case != "A":
                continue
            r = score_run(d, case)
            if r:
                rows.append((case, r))
    md = ["| Case | 실행 | Aspen 파일 | Block F1 | Edge F1 | 빠진 블록 / 연결 |", "|---|---|---|---|---|---|"]
    for case, r in rows:
        md.append(f'| {case} | {r["run"][-15:]} | {r["inp"]} | {r["block_f1"]:.2f} | {r["edge_f1"]:.2f} | '
                  f'{r["missing_blocks"] or "-"} / {r["missing_edges"][:3] or "-"} |')
    text = "\n".join(md)
    print(text)
    (HERE / "meeting" / "STRUCTURE_F1_ASPEN.md").write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
