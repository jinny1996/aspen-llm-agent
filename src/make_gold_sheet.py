"""Gold-label sheet for the EDC-2e experiment (offline).

    python make_gold_sheet.py        # -> 2e_gold_sheet.md  (+ 2e_gold_labels.yaml, the file the person fills in)

For every value id of case A / C it lists the distinct values the system produced over ALL runs (both prompt
arms, batch 1 included), where they came from, and the sentences of the paper that contain those numbers.
A person then decides, from the paper only, what the correct handling is:
    FIXED     the paper fixes this value for this unit/stream            -> asking the user is NOT needed
    OPEN      the paper gives a range, several candidates, or a value only for another case/unit
              -> asking the user IS needed
    ABSENT    the paper does not give it at all                          -> asking IS needed (assumption)
and, when known, the correct value.  `2e_gold_labels.yaml` is the machine-readable result; analysis
(precision/recall of the questions) reads it once the labels are filled in.  The labels are NOT
prefilled: a label written by the system that is then used to score the system would be circular.
"""
import collections
import json
import re
import sys
from pathlib import Path

import yaml

import dof_ledger as dl
from analyze_2e import RUN, read_manifest
from run_case import CASES

HERE = Path(__file__).resolve().parent


def all_runs():
    rows = list(read_manifest())
    b1 = RUN / "2e_manifest_batch1.csv"
    if b1.exists():
        import csv
        with open(b1, encoding="utf-8-sig", newline="") as f:
            rows += [r for r in csv.DictReader(f) if r.get("run_dir") and str(r.get("exit_code")) == "0"]
    seen, out = set(), []
    for r in rows:
        if r["run_dir"] not in seen:
            seen.add(r["run_dir"])
            out.append(r)
    return out


def sentences_with(text: str, numbers, limit=2):
    out = []
    for n in numbers:
        # the number must carry a unit (K, °C, bar, kg, mol, %, ...) so that "30-TW" does not match "30 bar"
        unit = r'(?:K|C|bar|atm|kPa|MPa|kg|g|mol|kmol|%|kW|MW|h)(?![A-Za-z])'
        gap = r'(?:\s|\$|\^|\{|\}|°|\\circ|\\mathrm)*'
        pat = re.compile(r'(?<![\d.])' + re.escape(n) + r'(?:\.0+)?' + gap + unit)
        for m in pat.finditer(text):
            a = max(text.rfind(".", 0, m.start()), text.rfind("\n", 0, m.start())) + 1
            b_candidates = [x for x in (text.find(". ", m.end()), text.find("\n", m.end())) if x >= 0]
            b = min(b_candidates) if b_candidates else len(text)
            s = re.sub(r"\s+", " ", text[a:b]).strip()
            if 20 <= len(s) and s not in out:
                out.append(s[:260])
            if len(out) >= limit:
                return out
    return out


def fmt_num(v):
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def main():
    papers = {k: Path(v["paper_text_path"]).read_text(encoding="utf-8", errors="replace") for k, v in CASES.items()}
    per = {"A": collections.defaultdict(list), "C": collections.defaultdict(list)}
    for m in all_runs():
        if m["case"] not in per:
            continue
        p = RUN / m["run_dir"] / "part1" / "Schema_proto.yaml"
        if not p.exists():
            continue
        sch = yaml.safe_load(p.read_text(encoding="utf-8"))
        for e in dl.build_ledger(sch)["entries"]:
            pr = e.get("provenance") or {}
            if e["status"] == dl.PROVIDED and e["required"] and not e.get("analysis_only"):
                per[m["case"]][e["id"]].append({"arm": m["arm"], "value": pr.get("value"), "source": pr.get("source"),
                                                "quote": pr.get("quote")})
    md = ["# EDC-2e 정답 라벨 시트", "",
          "각 항목을 **논문만 보고** 판정해 주세요. 시스템이 낸 값이 맞는지가 아니라, *논문이 이 값을 하나로 정해 놓았는가*입니다.", "",
          "- `FIXED`: 논문이 이 유닛/스트림의 값을 하나로 정해 놓음 → 사용자에게 물을 필요 없음",
          "- `OPEN`: 범위·여러 후보가 있거나, 다른 케이스/유닛의 값만 있음 → 물어야 함",
          "- `ABSENT`: 논문에 아예 없음 (가정 필요) → 물어야 함",
          "",
          "라벨은 `2e_gold_labels.yaml`에 적습니다 (`label`과 `correct_value`, 메모는 `note`).", ""]
    labels = {}
    for case in ("A", "C"):
        md += [f"## 케이스 {case}: {CASES[case]['label']}", ""]
        labels[case] = {}
        for rid in sorted(per[case]):
            obs = per[case][rid]
            if all(o["value"] is None for o in obs):          # structural entries (e.g. outlet_streams)
                continue
            grouped = collections.OrderedDict()
            for o in obs:
                key = json.dumps(o["value"], sort_keys=True, ensure_ascii=False)
                g = grouped.setdefault(key, {"value": o["value"], "n": 0, "sources": collections.Counter(), "quote": None})
                g["n"] += 1
                g["sources"][o["source"]] += 1
                g["quote"] = g["quote"] or o["quote"]
            md += [f"### {case} · `{rid}`   (총 {len(obs)}회 관측)", "", "| 값 | 횟수 | 출처 표기 | 시스템이 댄 인용문 |", "|---|---|---|---|"]
            nums = []
            for g in grouped.values():
                q = (g["quote"] or "").replace("|", "/").replace("\n", " ")[:170]
                val = json.dumps(g["value"], ensure_ascii=False)[:60]
                md.append(f"| {val} | {g['n']} | {dict(g['sources'])} | {q} |")
                if isinstance(g["value"], (int, float)) and not isinstance(g["value"], bool):
                    nums.append(fmt_num(g["value"]))
                    if "T" in rid.split(".")[-1][:2]:                       # papers often give K, schema is degC
                        nums.append(fmt_num(round(g["value"] + 273.15)))
            ctx = sentences_with(papers[case], nums)
            if ctx:
                md += ["", "논문에서 이 숫자가 나오는 문장:"] + [f"> {c}" for c in ctx]
            md.append("")
            labels[case][rid] = {"label": None, "correct_value": None, "note": ""}
    (HERE / "2e_gold_sheet.md").write_text("\n".join(md), encoding="utf-8")
    ypath = HERE / "2e_gold_labels.yaml"
    if ypath.exists():
        old = yaml.safe_load(ypath.read_text(encoding="utf-8")) or {}
        for c in labels:                       # never overwrite labels a person already wrote
            for rid in labels[c]:
                if rid in (old.get(c) or {}):
                    labels[c][rid] = old[c][rid]
    ypath.write_text("# label: FIXED | OPEN | ABSENT   (see 2e_gold_sheet.md)\n"
                     + yaml.safe_dump(labels, allow_unicode=True, sort_keys=True), encoding="utf-8")
    print({c: len(v) for c, v in labels.items()}, "ids -> 2e_gold_sheet.md, 2e_gold_labels.yaml")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
