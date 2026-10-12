"""make_gold_sheet_holdout.py -- label sheet for one case, built from any set of Part1 runs (no manifest needed).

    python make_gold_sheet_holdout.py D                 # runs under phase14/run whose paper = case D's paper
    python make_gold_sheet_holdout.py A --run-root ../phase13/run --out-prefix /tmp/try_A

Writes <prefix>_sheet.md and <prefix>_labels.yaml (default prefix gold_<CASE>).  The sheet shows, per value id, the
distinct values the system produced, where it said they came from, and the paper sentences containing those numbers.
It shows NO validator verdicts, so the person labelling is not influenced by the system under test.
Labels (FIXED / OPEN / ABSENT, same meaning as phase13/2e_gold_sheet.md) are left empty; existing labels in the
yaml are never overwritten.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import yaml

import dof_ledger as dl
from make_gold_sheet import fmt_num, sentences_with
from run_case import CASES

HERE = Path(__file__).resolve().parent


def final_schema(run_dir: Path):
    """The schema Part 2 receives: Schema_validated.yaml when the completion step ran, else Schema_proto.yaml."""
    for name in ("Schema_validated.yaml", "Schema_proto.yaml"):
        p = run_dir / "part1" / name
        if p.exists():
            return p
    return None


def runs_for(case: str, run_root: Path):
    want = str(Path(CASES[case]["paper_text_path"]).resolve()).lower()
    out = []
    for d in sorted(run_root.glob("attempt_chat_*")):
        ri, sp = d / "runner_input.json", final_schema(d)
        if not (ri.exists() and sp is not None):
            continue
        try:
            data = json.loads(ri.read_text(encoding="utf-8"))
            pin = data.get("part1_input") or data
            p = pin.get("paper_text_path", "")
            if int(pin.get("paper_image_top_k") or 0) != int(CASES[case]["paper_image_top_k"]):
                continue                    # D and D2 share the paper; they differ in the images given
        except ValueError:
            continue
        if str(Path(p).resolve()).lower() == want:
            out.append(d)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("case", choices=sorted(CASES))
    ap.add_argument("--run-root", type=Path, default=HERE / "run")
    ap.add_argument("--out-prefix", type=Path, default=None)
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    prefix = a.out_prefix or (HERE / f"gold_{a.case}")
    paper = Path(CASES[a.case]["paper_text_path"]).read_text(encoding="utf-8", errors="replace")
    runs = runs_for(a.case, a.run_root)
    if not runs:
        print(f"no Part1 runs for case {a.case} under {a.run_root}")
        return 1
    seen = collections.defaultdict(list)
    maps = []
    for d in runs:
        sch = yaml.safe_load(final_schema(d).read_text(encoding="utf-8"))
        fs = sch.get("flowsheet_schema") or {}
        maps.append(tuple([("block", b.get("id"), b.get("type"), b.get("name")) for b in fs.get("blocks") or []]
                          + [("stream", x.get("id"), x.get("role"), x.get("name"))
                             for x in fs.get("streams") or [] if x.get("role") == "FEED"]))
        for e in dl.build_ledger(sch)["entries"]:
            pr = e.get("provenance") or {}
            if e["status"] == dl.PROVIDED and e["required"] and not e.get("analysis_only") and pr.get("value") is not None:
                seen[e["id"]].append({"run": d.name, "value": pr.get("value"), "source": pr.get("source"),
                                      "quote": pr.get("quote")})
    md = [f"# 정답 라벨 시트 — 케이스 {a.case}: {CASES[a.case]['label']}", "",
          f"관측한 실행 {len(runs)}회. 각 항목을 **논문만 보고** 판정해 주세요 "
          "(시스템이 낸 값이 맞는지가 아니라, *논문이 이 값을 하나로 정해 놓았는가*).", "",
          "- `FIXED`: 논문이 이 유닛/스트림의 값을 하나로 정해 놓음 → 물을 필요 없음",
          "- `OPEN`: 범위·여러 후보가 있거나 다른 케이스/유닛의 값만 있음 → 물어야 함",
          "- `ABSENT`: 논문에 없음 (가정 필요) → 물어야 함",
          "- 값이 논문 안에서 서로 다르게 적혀 있으면 `OPEN`으로 하고 note에 두 위치를 적어 주세요.", "",
          f"라벨은 `{prefix.name}_labels.yaml`에 적습니다 (`label`, `correct_value`, `note`).", ""]
    md += ["## 블록 / 공급 스트림 지도 (id가 무엇을 가리키는지)", ""]
    if len(set(maps)) == 1:
        md += [f"- `{i}` = {t} · {n}" + ("" if k == "block" else f" ({k})") for k, i, t, n in maps[0]]
    else:
        md += ["**주의: 실행마다 id 지정이 다릅니다.** 첫 실행 기준:"]
        md += [f"- `{i}` = {t} · {n}" for k, i, t, n in maps[0]]
    md += ["", "---", ""]
    labels = {a.case: {}}
    for rid in sorted(seen):
        obs = seen[rid]
        grouped = collections.OrderedDict()
        for o in obs:
            g = grouped.setdefault(json.dumps(o["value"], sort_keys=True, ensure_ascii=False),
                                   {"value": o["value"], "n": 0, "sources": collections.Counter(), "quote": None})
            g["n"] += 1
            g["sources"][o["source"]] += 1
            g["quote"] = g["quote"] or o["quote"]
        md += [f"### {a.case} · `{rid}`   (총 {len(obs)}회 관측)", "", "| 값 | 횟수 | 출처 표기 | 시스템이 댄 인용문 |", "|---|---|---|---|"]
        nums = []
        for g in grouped.values():
            q = (g["quote"] or "").replace("|", "/").replace("\n", " ")[:170]
            md.append(f"| {json.dumps(g['value'], ensure_ascii=False)[:60]} | {g['n']} | {dict(g['sources'])} | {q} |")
            if isinstance(g["value"], (int, float)) and not isinstance(g["value"], bool):
                nums.append(fmt_num(g["value"]))
                if "T" in rid.split(".")[-1][:2]:
                    nums.append(fmt_num(round(g["value"] + 273.15)))
        ctx = sentences_with(paper, nums)
        if ctx:
            md += ["", "논문에서 이 숫자가 나오는 문장:"] + [f"> {c}" for c in ctx]
        md.append("")
        labels[a.case][rid] = {"label": None, "correct_value": None, "note": ""}
    ypath = Path(str(prefix) + "_labels.yaml")
    if ypath.exists():
        old = (yaml.safe_load(ypath.read_text(encoding="utf-8")) or {}).get(a.case) or {}
        for rid in labels[a.case]:
            if rid in old:
                labels[a.case][rid] = old[rid]
    Path(str(prefix) + "_sheet.md").write_text("\n".join(md), encoding="utf-8")
    ypath.write_text("# label: FIXED | OPEN | ABSENT\n" + yaml.safe_dump(labels, allow_unicode=True, sort_keys=True),
                     encoding="utf-8")
    print(f"case {a.case}: {len(runs)} runs, {len(labels[a.case])} ids -> {prefix}_sheet.md / _labels.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
