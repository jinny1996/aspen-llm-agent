"""EDC-2e analysis of the Part1-only batch (run_2e_batch.ps1).  Offline: reads run/2e_manifest.csv and each
run's Schema_proto.yaml / part1_status.json / token_usage.jsonl; classifies with the CURRENT edc_policy.

    python analyze_2e.py              # prints the summary, writes 2e_analysis.json and 2e_review_table.md

Groups are (case, arm), arm = prompt off / on.  Rows of the manifest are used as they are, so a smoke
run that was repeated shows up as an extra repeat.  What is measured (no gold labels needed):
  * quote check pass rate        share of provenance-tracked values whose quote is EXACT/FRAGMENTS_ALL/RECONSTRUCTED
  * class stability              for each value id seen in >=2 runs of a group: share of runs that agree with
                                 the most common class (1.0 = the same class every time)
  * value stability              same for the numeric value itself
  * questions                    how many items the policy asks (before the cap) and after the cap K
  * topology signature           block types + stream count + edge count; differing signatures = structure changed
  * tokens                       total of token_usage.jsonl and the Part1 completion step
  * field fill rate (on only)    quote_scope / alternatives / z_provenance / property_method_note
Precision/recall of the questions needs gold labels: the review table lists every value of every run
side by side so the labels can be filled in by a person.
"""
import collections
import csv
import json
import statistics
import sys
from pathlib import Path

import yaml

import dof_ledger as dl
import edc_policy as ep
from run_case import CASES

HERE = Path(__file__).resolve().parent
RUN = HERE / "run"
OK_TIERS = ("EXACT", "FRAGMENTS_ALL", "RECONSTRUCTED")


def read_manifest():
    rows = []
    with open(RUN / "2e_manifest.csv", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("run_dir") and str(r.get("exit_code")) == "0":
                rows.append(r)
    return rows


def load_run(run_dir: str, paper_text: str):
    base = RUN / run_dir / "part1"
    sch_path = base / "Schema_proto.yaml"
    if not sch_path.exists():
        return None
    sch = yaml.safe_load(sch_path.read_text(encoding="utf-8"))
    res = ep.classify_schema(sch, paper_text)
    pol = ep.policy_review_items(sch, paper_text, k=5)
    uncapped = ep.select_questions(res, k=10 ** 6)
    toks, completion = 0, 0
    tf = base / "token_usage.jsonl"
    if tf.exists():
        for line in tf.read_text(encoding="utf-8").splitlines():
            try:
                t = json.loads(line)
            except ValueError:
                continue
            toks += int(t.get("total_tokens") or 0)
            if "completion" in str(t.get("section")):
                completion += int(t.get("total_tokens") or 0)
    fs = sch.get("flowsheet_schema") or {}
    topo = (tuple(sorted(str(b.get("type")) for b in fs.get("blocks") or [])), len(fs.get("streams") or []),
            len(((fs.get("connectivity") or {}).get("edges")) or []))
    fill = {"quote_scope": 0, "alternatives": 0, "z_provenance": 0, "property_method_note": 0, "values": 0}
    for e in dl.build_ledger(sch)["entries"]:
        p = e.get("provenance") or {}
        if p.get("tracked"):
            fill["values"] += 1
            fill["quote_scope"] += bool(p.get("quote_scope"))
            fill["alternatives"] += bool(p.get("alternatives"))
    for f in (sch.get("io") or {}).get("feeds") or []:
        fill["z_provenance"] += bool((f.get("specs") or {}).get("z_provenance"))
    fill["property_method_note"] = int(bool((sch.get("aspen_schema") or {}).get("property_method_note")))
    return {"run_dir": run_dir, "classified": res, "asked": [i["edc_id"] for i in pol["items"]],
            "asked_uncapped": [r["id"] for r in uncapped["asked"]], "tokens": toks, "completion_tokens": completion,
            "topology": topo, "fill": fill, "schema": sch}


def norm_value(v):
    """1 == 1.0, and a composition ignores zero fractions: formatting must not count as a different value."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 9)
    if isinstance(v, dict):
        return {str(k): norm_value(x) for k, x in v.items() if not (isinstance(x, (int, float)) and float(x) == 0.0)}
    return v


def agreement(values):
    c = collections.Counter(values)
    return max(c.values()) / len(values)


def summarize_group(runs):
    out = {"runs": len(runs), "run_dirs": [r["run_dir"] for r in runs]}
    tracked = ok_q = 0
    for r in runs:
        for e in r["classified"]["entries"]:
            if e["class"] != "NO_PROVENANCE" and e.get("quote_tier") not in (None, "NO_QUOTE"):
                tracked += 1
                ok_q += e["quote_tier"] in OK_TIERS
    out["quote_pass_rate"] = round(ok_q / tracked, 3) if tracked else None
    ids = collections.defaultdict(list)
    for r in runs:
        for e in r["classified"]["entries"]:
            ids[e["id"]].append(e)
    cls_stab, val_stab, per_id = [], [], {}
    for rid, es in sorted(ids.items()):
        if len(es) < 2:
            continue
        classes = [e["class"] for e in es]
        vals = [json.dumps(norm_value(e.get("value")), sort_keys=True) for e in es]
        cls_stab.append(agreement(classes))
        val_stab.append(agreement(vals))
        per_id[rid] = {"classes": dict(collections.Counter(classes)), "values": dict(collections.Counter(vals))}
    out["class_stability"] = round(statistics.mean(cls_stab), 3) if cls_stab else None
    out["value_stability"] = round(statistics.mean(val_stab), 3) if val_stab else None
    out["per_id"] = per_id
    out["questions_after_cap"] = [len(r["asked"]) for r in runs]
    out["questions_before_cap"] = [len(r["asked_uncapped"]) for r in runs]
    out["asked_ids"] = dict(collections.Counter(i for r in runs for i in r["asked"]))
    out["class_totals"] = dict(collections.Counter(e["class"] for r in runs for e in r["classified"]["entries"]))
    out["topologies"] = dict(collections.Counter(json.dumps(r["topology"], default=list) for r in runs))
    out["tokens"] = [r["tokens"] for r in runs]
    out["completion_tokens"] = [r["completion_tokens"] for r in runs]
    f = collections.Counter()
    for r in runs:
        f.update(r["fill"])
    out["fill_totals"] = dict(f)
    return out


def review_table(case, groups):
    """one block per value id: every run side by side, to be labelled by a person."""
    lines = [f"## case {case}", "",
             "라벨 칸: `질문이 필요했나?` (Y/N) 와 `정답 값/메모`를 직접 채우세요.", ""]
    ids = sorted({e["id"] for runs in groups.values() for r in runs for e in r["classified"]["entries"]})
    for rid in ids:
        lines += [f"### {case} · {rid}   | 질문 필요? [ ]  | 정답/메모: ", "",
                  "| 조건 | 실행 | 값 | 출처 | 분류 | 질문됨 | 후보(검증됨) | 인용 |", "|---|---|---|---|---|---|---|---|"]
        for arm in ("off", "on"):
            for r in groups.get(arm, []):
                e = next((x for x in r["classified"]["entries"] if x["id"] == rid), None)
                if e is None:
                    lines.append(f"| {arm} | {r['run_dir'][-6:]} | (없음) | | | | | |")
                    continue
                q = (e.get("quote") or "").replace("|", "/").replace("\n", " ")[:140]
                alts = ",".join(f"{a:g}" for a in e.get("alternatives_verified") or [])
                val = json.dumps(e.get("value"), ensure_ascii=False)[:40]
                lines.append(f"| {arm} | {r['run_dir'][-6:]} | {val} | {e.get('source')} | {e['class']} | "
                             f"{'Y' if rid in r['asked'] else ''} | {alts} | {q} |")
        lines.append("")
    return lines


def main():
    manifest = read_manifest()
    if not manifest:
        raise SystemExit("no finished runs in run/2e_manifest.csv")
    papers = {k: Path(v["paper_text_path"]).read_text(encoding="utf-8", errors="replace") for k, v in CASES.items()}
    data = collections.defaultdict(lambda: collections.defaultdict(list))
    for m in manifest:
        r = load_run(m["run_dir"], papers[m["case"]])
        if r:
            data[m["case"]][m["arm"]].append(r)
    summary, md = {}, ["# EDC-2e 검토 표 (자동 생성)", ""]
    for case, groups in sorted(data.items()):
        summary[case] = {arm: summarize_group(runs) for arm, runs in groups.items()}
        md += review_table(case, groups)
    (HERE / "2e_analysis.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=list),
                                           encoding="utf-8")
    (HERE / "2e_review_table.md").write_text("\n".join(md), encoding="utf-8")

    for case, arms in sorted(summary.items()):
        print(f"\n===== case {case} =====")
        for arm in ("off", "on"):
            s = arms.get(arm)
            if not s:
                continue
            print(f"[{arm}] runs={s['runs']}  quote_pass={s['quote_pass_rate']}  class_stability={s['class_stability']}"
                  f"  value_stability={s['value_stability']}")
            print(f"      questions after cap={s['questions_after_cap']} before cap={s['questions_before_cap']}")
            print(f"      class totals={s['class_totals']}")
            print(f"      topologies={len(s['topologies'])} distinct  tokens={s['tokens']}  completion={s['completion_tokens']}")
            if arm == "on":
                print(f"      fill (over {s['runs']} runs)={s['fill_totals']}")
            unstable = {k: v["classes"] for k, v in s["per_id"].items() if len(v["classes"]) > 1}
            if unstable:
                print(f"      unstable classes: {unstable}")
    print("\nwrote 2e_analysis.json, 2e_review_table.md")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
