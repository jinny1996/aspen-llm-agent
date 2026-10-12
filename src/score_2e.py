"""Score the questions of the EDC-2e runs against the person-made gold labels (2e_gold_labels.yaml).

    python score_2e.py

A value is "needed" (the user should be asked) when its label is OPEN or ABSENT; FIXED means the paper
settles it.  For every run of a case: the questions the system asks are compared with the labelled needed
set (only ids that exist in that run and carry a label).
    precision = asked & needed / asked        (asked questions that were really needed)
    recall    = asked & needed / needed       (needed values that were asked)
Both are given with the question cap K=5 and without it.  Unlabelled cases/ids are skipped, not guessed.
Only a few labelled values per case: read the numbers as counts, not as statistics.
"""
import collections
import sys
from pathlib import Path

import yaml

import analyze_2e as a
from run_case import CASES

HERE = Path(__file__).resolve().parent
NEEDED = ("OPEN", "ABSENT")


def main():
    gold = yaml.safe_load((HERE / "2e_gold_labels.yaml").read_text(encoding="utf-8")) or {}
    papers = {k: Path(v["paper_text_path"]).read_text(encoding="utf-8", errors="replace") for k, v in CASES.items()}
    runs = collections.defaultdict(lambda: collections.defaultdict(list))
    for m in a.read_manifest():
        r = a.load_run(m["run_dir"], papers[m["case"]])
        if r:
            runs[m["case"]][m["arm"]].append(r)
    for case in sorted(runs):
        labels = {k: v["label"] for k, v in (gold.get(case) or {}).items() if v.get("label")}
        if not labels:
            print(f"\ncase {case}: no labels yet, skipped")
            continue
        n_needed_all = sum(1 for v in labels.values() if v in NEEDED)
        print(f"\n===== case {case}: {len(labels)} labelled values, {n_needed_all} need a question "
              f"({', '.join(sorted(k for k, v in labels.items() if v in NEEDED))}) =====")
        for arm in ("off", "on"):
            rs = runs[case].get(arm, [])
            if not rs:
                continue
            for key, name in (("asked", "with cap K=5"), ("asked_uncapped", "without cap")):
                P, R, rows = [], [], []
                for r in rs:
                    present = {e["id"] for e in r["classified"]["entries"]} & set(labels)
                    needed = {i for i in present if labels[i] in NEEDED}
                    asked = set(r[key]) & present            # unlabelled questions cannot be scored
                    unscored = set(r[key]) - present
                    tp = asked & needed
                    P.append(len(tp) / len(asked) if asked else None)
                    R.append(len(tp) / len(needed) if needed else None)
                    rows.append((sorted(asked - needed), sorted(needed - asked), sorted(unscored)))
                ps = [x for x in P if x is not None]
                rc = [x for x in R if x is not None]
                fp = collections.Counter(i for x in rows for i in x[0])
                fn = collections.Counter(i for x in rows for i in x[1])
                un = collections.Counter(i for x in rows for i in x[2])
                print(f"[{arm}] {name:13} runs={len(rs)}  precision={sum(ps)/len(ps):.2f}" if ps else
                      f"[{arm}] {name:13} runs={len(rs)}  precision=n/a (no labelled question asked)", end="")
                print(f"  recall={sum(rc)/len(rc):.2f}" if rc else "  recall=n/a")
                if fp:
                    print(f"        asked but the paper fixes it (FIXED): {dict(fp)}")
                if fn:
                    print(f"        needed but not asked: {dict(fn)}")
                if un:
                    print(f"        asked, not labelled (not scored): {dict(un)}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
