"""eval_summary.py -- the few p.7 metrics that can be read off existing runs (no LLM, no Aspen).

  1. Execution success (A, B, C) from the phase13 Part3 results
  2. Evidence detection precision / recall:   EDC only  vs  semantic validator  vs  both
  3. HITL: question precision / recall (current policy vs v2) and, for case B (truth in the SI), accuracy gain per question
  4. Parameter accuracy on the values the paper states (gold FIXED with a number)

Gold = the label files (A, B, D, E drafts by Claude; C by the user).  Everything here is PRELIMINARY (5 papers, small n).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml

import edc_policy as ep
import edc_semantic_validator as sv
from eval_dev import CASES, P13

HERE = Path(__file__).resolve().parent
POSITIVE_SUPPORT = ("DIRECT_SUPPORT", "PROCESS_LEVEL_SUPPORT")


def close(a, b):
    try:
        return abs(float(a) - float(b)) <= max(1e-6, 2e-3 * abs(float(b)))
    except (TypeError, ValueError):
        return False


def numeric_truth(g):
    c = []
    if isinstance(g.get("correct_value"), (int, float)):
        c.append(g["correct_value"])
    c += [v for v in g.get("acceptable_values") or [] if isinstance(v, (int, float))]
    return c


def execution():
    out = {}
    key = {"paper2": "A", "paper_main": "B", "paper3": "C", "paper5": "D", "paper6": "E"}
    for d in sorted(list((P13 / "run").glob("attempt_chat_*")) + list((HERE / "run").glob("attempt_chat_*"))):
        p2 = d / "part2"
        if not (p2.is_dir() and any(p2.iterdir())):
            continue
        try:
            pin = json.loads((d / "runner_input.json").read_text(encoding="utf-8"))["part1_input"]
            path = pin["paper_text_path"]
        except Exception:
            continue
        case = next((v for k, v in key.items() if k in path.replace("\\", "/")), None)
        if case == "E" and "Haber" in pin.get("user_input_text", ""):
            case = "F"
        if not case:
            continue
        r = out.setdefault(case, {"success": 0, "failed": 0, "no_part3_result": 0, "repairs": 0})
        f = d / "part3" / "part3_result.json"
        if not f.exists():
            p2j = sorted(p2.glob("part2_result_iter*.json"))
            p2_failed = False
            if p2j:
                try:
                    p2_failed = json.loads(p2j[-1].read_text(encoding="utf-8")).get("ok") is False
                except Exception:
                    pass
            r["failed" if p2_failed else "no_part3_result"] += 1       # Part 2 ended with ok=False -> a failed run, not an interrupted one
            continue
        j = json.loads(f.read_text(encoding="utf-8"))
        r["success" if j.get("success") else "failed"] += 1
        r["repairs"] += int(j.get("repair_cycle_count") or 0)
    return out


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    os.environ.pop("PART1_EDC_POLICY_V2", None)
    md = ["# 예비 평가 요약 (논문 5편, 작은 표본, 정답 라벨 대부분 초안)", ""]

    ex = execution()
    md += ["## 1. Simulation success (Part 2/3, phase13 기존 실행)", "", "| 논문 | 성공 | 실패(Part 2 또는 3) | 결과 없음(중단) | 수리 사이클 합 |", "|---|---|---|---|---|"]
    for k in "ABCDEF":
        r = ex.get(k, {})
        md.append(f'| {k} | {r.get("success", 0)} | {r.get("failed", 0)} | {r.get("no_part3_result", 0)} | {r.get("repairs", 0)} |')

    tp = {"EDC만": [0, 0, 0, 0], "의미 검증기": [0, 0, 0, 0], "둘 다": [0, 0, 0, 0]}      # TP FP FN TN
    q = {"무작위": [0, 0.0, 0], "현재": [0, 0, 0], "v2": [0, 0, 0]}                                          # asked, need_asked, need
    acc = {}
    per_case = {}
    for name, schemas, paper_path, gold_path in CASES:
        gold = yaml.safe_load(gold_path.read_text(encoding="utf-8"))[name]
        paper = paper_path.read_text(encoding="utf-8", errors="replace")
        for sp in schemas:
            schema = yaml.safe_load(sp.read_text(encoding="utf-8"))
            rep = {c["id"]: c for c in sv.validate_schema(schema, paper)["claims"]}
            base = ep.classify_schema(schema, paper, apply_v2=False)
            cls = {r["id"]: r["class"] for r in base["entries"]}
            for gid, g in gold.items():
                if gid not in rep:
                    continue
                pos = g["label"] != "FIXED"
                f_edc = cls.get(gid) in ep.CANDIDATE_CLASSES
                f_sem = rep[gid]["evidence_relation"] not in POSITIVE_SUPPORT
                for lab, flag in (("EDC만", f_edc), ("의미 검증기", f_sem), ("둘 다", f_edc or f_sem)):
                    i = (0 if flag else 2) if pos else (1 if flag else 3)
                    tp[lab][i] += 1
                tr = numeric_truth(g)
                if g["label"] == "FIXED" and tr and isinstance(rep[gid]["value"], (int, float)):
                    a = acc.setdefault(name, [0, 0])
                    a[1] += 1
                    a[0] += any(close(rep[gid]["value"], t) for t in tr)
            for ver, flag in (("현재", "0"), ("v2", "1")):
                os.environ["PART1_EDC_POLICY_V2"] = flag
                cl = ep.classify_schema(schema, paper)
                asked = {r["id"] for r in ep.select_questions(cl, 5)["asked"]}
                present = {r["id"] for r in cl["entries"]}
                need = {k for k, v in gold.items() if k in present and v["label"] != "FIXED"}
                q[ver][0] += len(asked)
                q[ver][1] += len(need & asked)
                q[ver][2] += len(need)
                if ver == "현재" and present:           # no-EDC baseline: the same number of questions drawn at random from all values (expected value)
                    q["무작위"][0] += len(asked)
                    q["무작위"][1] += len(asked) * len(need) / len(present)
                    q["무작위"][2] += len(need)
            os.environ.pop("PART1_EDC_POLICY_V2", None)

    md += ["", "## 2. Evidence detection: '확인이 필요한 값'(정답이 ABSENT/OPEN)을 걸러내는가", "",
           "검출 = 근거가 확인되지 않은 값으로 표시. 정답 확인필요 값 vs 정답 문헌명시(FIXED) 값, A–F 모든 실행 합산.", "",
           "| 방식 | Recall | Precision | 놓친 값(FN) | 불필요하게 표시한 FIXED 값(FP) |", "|---|---|---|---|---|"]
    for lab, (a, b, c, d) in tp.items():
        md.append(f"| {lab} | {a}/{a + c} = {a / (a + c):.2f} | {a}/{a + b} = {a / (a + b):.2f} | {c} | {b} / {b + d} |")

    md += ["", "## 3. HITL question efficiency (질문 상한 5개, A–F 모든 실행 합산)", "",
           "| 정책 | 질문 수 | 필요한 값 질문(Recall) | 질문 Precision |", "|---|---|---|---|"]
    for ver, (a, n, need) in q.items():
        ns = f"{n:.1f}" if isinstance(n, float) else str(n)
        md.append(f"| {ver} | {a} | {ns}/{need} = {n / need:.2f} | {ns}/{a} = {n / a:.2f} |")

    # case B: truth in the SI -> accuracy before / after oracle answers to the asked questions
    gold_b = yaml.safe_load((HERE / "gold_B_draft.yaml").read_text(encoding="utf-8"))["B"]
    sch_b = yaml.safe_load((HERE / "fixtures_semantic/caseB_Schema_proto.pre_review.yaml").read_text(encoding="utf-8"))
    paper_b = (P13.parent.parent / "jinny_DB/parsed/paper/auto/paper_main.md").read_text(encoding="utf-8", errors="replace")
    rep_b = {c["id"]: c for c in sv.validate_schema(sch_b, paper_b)["claims"]}
    asked_b = {r["id"] for r in ep.select_questions(ep.classify_schema(sch_b, paper_b, apply_v2=False), 5)["asked"]}
    items = [(k, g) for k, g in gold_b.items() if numeric_truth(g) and k in rep_b and isinstance(rep_b[k]["value"], (int, float))]
    ok_before = sum(any(close(rep_b[k]["value"], t) for t in numeric_truth(g)) for k, g in items)
    ok_after = sum((k in asked_b) or any(close(rep_b[k]["value"], t) for t in numeric_truth(g)) for k, g in items)
    md += ["", f"**케이스 B (SI에 정답 있음)**: 숫자 정답이 있는 값 {len(items)}개 중 질문 전 정확 {ok_before}개 → 질문 {len(asked_b)}개에 정답으로 답하면 {ok_after}개 "
           f"(질문 1개당 +{(ok_after - ok_before) / len(asked_b):.2f}개)", ""]

    md += ["## 4. Parameter accuracy: 논문이 값을 명시한(FIXED) 숫자값 중 Schema 값이 정답인 비율", "",
           "| 논문 | 정확 / 비교 가능 |", "|---|---|"]
    for k in "ABCDEF":
        a = acc.get(k, [0, 0])
        md.append(f"| {k} | {a[0]}/{a[1]} = {a[0] / a[1]:.2f} |" if a[1] else f"| {k} | - |")
    text = "\n".join(md) + "\n"
    print(text)
    (HERE / "meeting" / "EVAL_SUMMARY.md").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
