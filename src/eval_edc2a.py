"""EDC-2a offline evaluation on EXISTING Part1 outputs (no API, no Aspen).

Compares, on the same schemas and the same draft gold labels:
  * the CURRENT human-review selector (collect_schema_human_review_items)
  * the EDC-2a classifier (edc_policy) -- flagged as "needs attention" when class is a candidate class

GOLD LABELS ARE A DRAFT written by the assistant from the papers' text (needs the user's review).
They are keyed by (paper, owner type, variable[, value]) because block ids differ across runs.
Papers: A = ATR (6 runs), B = methanol aromatization unit, main text only (13 runs),
        C = syngas methanation Fig. 2 (2 new runs; 16 old runs have no usable gold and are only counted).
"""
import collections
import itertools
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import edc_policy as ep  # noqa: E402
from part1_stage_functions import collect_schema_human_review_items as current_selector  # noqa: E402

ROOT = HERE.parent.parent           # ...\3. LLM (2601)
PARSED = ROOT / "jinny_DB" / "parsed"
ATTENTION_GOLD = {"SWEPT", "ALTERNATIVE", "ASSUMED", "BORROWED"}


# --------------------------------------------------------------------------
# draft gold labels
# --------------------------------------------------------------------------
def gold_label(paper, otype, var, value, feed_kind=None):
    var = re.sub(r"\[.*\]|#\d+$", "", var)
    key = (otype, var)
    if paper == "A":
        tbl = {("MIXER", "P_out_bar"): "STATED", ("MHEATX", "T_out_C"): "ASSUMED",
               ("HEATER", "T_out_C"): "DERIVED", ("HEATER", "P_bar"): "STATED",
               ("RGIBBS", "T_C"): "DERIVED", ("RGIBBS", "P_bar"): "STATED",
               ("FLASH2", "T_out_C"): "DERIVED", ("FLASH2", "P_out_bar"): "STATED",
               ("FEED", "T_C"): "DERIVED", ("FEED", "P_bar"): "STATED"}
        if key == ("FEED", "flow"):
            return {1: "ASSUMED", 3: "STATED", 2.142857: "DERIVED"}.get(round(float(value), 6) if value is not None else None)
        return tbl.get(key)
    if paper == "B":
        tbl = {("PUMP", "P_out_bar"): "ASSUMED", ("PUMP", "dP_bar"): "ASSUMED", ("PUMP", "eff"): "ASSUMED",
               ("MHEATX", "T_out_C"): "STATED", ("RGIBBS", "T_C"): "ASSUMED", ("RGIBBS", "P_bar"): "ASSUMED",
               ("HEATER", "T_out_C"): "STATED", ("HEATER", "P_bar"): "ASSUMED",
               ("FEED", "flow"): "STATED", ("FEED", "T_C"): "ASSUMED", ("FEED", "P_bar"): "ASSUMED"}
        return tbl.get(key)
    if paper == "C":
        if key == ("RGIBBS", "T_C"):
            return "SWEPT"
        if key == ("RGIBBS", "P_bar"):
            return "STATED"
        if key == ("FEED", "flow"):
            return {"SYN": "STATED", "H2": "ALTERNATIVE"}.get(feed_kind)
        if key == ("FEED", "T_C"):
            return {"SYN": "STATED", "H2": "ASSUMED"}.get(feed_kind)
        if key == ("FEED", "P_bar"):
            return "STATED"
    return None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def load_paper_from_run(run_dir):
    ri = json.loads((run_dir / "runner_input.json").read_text(encoding="utf-8"))
    return Path(ri["part1_input"]["paper_text_path"]).read_text(encoding="utf-8", errors="replace")


def selector_attention(schema):
    """set of (owner id, normalised variable) the CURRENT selector asks about"""
    fs = schema.get("flowsheet_schema") or {}
    blocks = fs.get("blocks") or []
    feeds = (schema.get("io") or {}).get("feeds") or []
    out = set()
    for it in current_selector(schema):
        path = it["field_path"]
        m = re.match(r"flowsheet_schema\.blocks\[(\d+)\]\.specs\..*?([A-Za-z_]+)$", path)
        if m and int(m.group(1)) < len(blocks):
            out.add((str(blocks[int(m.group(1))]["id"]), m.group(2)))
            continue
        m = re.match(r"io\.feeds\[(\d+)\]\.specs\.([A-Za-z_]+)", path)
        if m and int(m.group(1)) < len(feeds):
            out.add((str(feeds[int(m.group(1))]["stream"]), m.group(2)))
    return out


def norm_var(v):
    return re.sub(r"\[.*\]|#\d+$", "", v)


def collect_runs():
    runs = []
    for d in sorted((HERE.parent / "phase12" / "run").glob("attempt_*")):
        sp = d / "part1" / "Schema_proto.yaml"
        if sp.exists() and (d / "runner_input.json").exists():
            runs.append(("A", d.name, sp, load_paper_from_run(d)))
    d = HERE / "run" / "attempt_chat_20261006_225749"
    if (d / "part1" / "Schema_proto.yaml").exists():
        runs.append(("A", d.name, d / "part1" / "Schema_proto.yaml", load_paper_from_run(d)))
    paper_b = (PARSED / "paper" / "auto" / "paper.md").read_text(encoding="utf-8", errors="replace")
    for d in sorted((ROOT / "runs").glob("2026042*_Aroma_*")):
        if (d / "Schema_validated.yaml").exists():
            runs.append(("B", d.name, d / "Schema_validated.yaml", paper_b))
    paper_c = (PARSED / "paper3" / "paper3.md").read_text(encoding="utf-8", errors="replace")
    for tag in ("attempt_chat_20261006_214246", "attempt_chat_20261006_220801"):
        sp = HERE / "run" / tag / "part1" / "Schema_proto.yaml"
        if sp.exists():
            runs.append(("C", tag, sp, paper_c))
    for d in sorted((ROOT / "runs").glob("2026042*_CH4_*")):
        if (d / "Schema_validated.yaml").exists():
            runs.append(("C-old", d.name, d / "Schema_validated.yaml", paper_c))
    return runs


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    return round(p, 3), round(r, 3)


def jaccard(sets):
    vals = [len(a & b) / len(a | b) if (a | b) else 1.0 for a, b in itertools.combinations(sets, 2)]
    return round(sum(vals) / len(vals), 3) if vals else None


# --------------------------------------------------------------------------
def evaluate(runs, stated_elsewhere):
    per_paper = collections.defaultdict(lambda: {"runs": 0, "cls": collections.Counter(), "n_ask_policy": [],
                                                 "n_ask_current": [], "ask_sets_policy": [], "ask_sets_current": [],
                                                 "conf": {"policy": collections.Counter(), "current": collections.Counter()},
                                                 "fp": collections.Counter(), "fn": collections.Counter(),
                                                 "high_gold_att": 0, "high_asked_policy": 0, "high_asked_current": 0})
    known = {"C_reactor_T_300": collections.Counter(), "B_pressure_2.85": collections.Counter(),
             "B_reactor_T_400": collections.Counter()}
    for paper, name, sp, text in runs:
        schema = yaml.safe_load(Path(sp).read_text(encoding="utf-8"))
        cl = ep.classify_schema(schema, text, stated_elsewhere=stated_elsewhere)
        sel = ep.select_questions(cl)
        cur = selector_attention(schema)
        g = per_paper[paper]
        g["runs"] += 1
        asked_ids = {r["id"] for r in sel["asked"]}
        g["n_ask_policy"].append(len(sel["asked"]))
        g["n_ask_current"].append(len(cur))
        g["ask_sets_policy"].append({(r["owner_type"], norm_var(r["variable"])) for r in sel["asked"]})
        g["ask_sets_current"].append({(o, v) for o, v in cur})
        feed_kind = {}
        for r in cl["entries"]:
            if r["owner_type"] == "FEED" and r["variable"] == "flow" and isinstance(r.get("value"), (int, float)):
                feed_kind[r["owner"]] = "H2" if abs(r["value"] - 3.24) < 1e-6 else "SYN"
        for r in cl["entries"]:
            g["cls"][r["class"]] += 1
            lab = gold_label(paper if paper != "C-old" else "none", r["owner_type"], r["variable"],
                             r.get("value"), feed_kind.get(r["owner"]))
            pred_att = r["class"] in ep.CANDIDATE_CLASSES
            cur_att = (r["owner"], norm_var(r["variable"])) in cur
            if lab is not None:
                gold_att = lab in ATTENTION_GOLD
                for who, pred in (("policy", pred_att), ("current", cur_att)):
                    cell = ("TP" if pred and gold_att else "FP" if pred else "FN" if gold_att else "TN")
                    g["conf"][who][cell] += 1
                    if cell in ("FP", "FN") and who == "policy":
                        g["fp" if cell == "FP" else "fn"][(r["owner_type"], norm_var(r["variable"]), r["class"], lab)] += 1
                if gold_att and r["impact"] == "HIGH":
                    g["high_gold_att"] += 1
                    g["high_asked_policy"] += int(r["id"] in asked_ids)
                    g["high_asked_current"] += int(cur_att)
            # known cases
            if paper in ("C",) and r["owner_type"] == "RGIBBS" and r["variable"] == "T_C" and r.get("value") == 300:
                known["C_reactor_T_300"][(r["class"], "asked" if r["id"] in asked_ids else "not asked",
                                          "current asks" if cur_att else "current silent")] += 1
            if paper == "B" and isinstance(r.get("value"), (int, float)) and abs(r["value"] - 2.85) < 1e-9:
                known["B_pressure_2.85"][(r["class"], r["source"])] += 1
            if paper == "B" and r["owner_type"] == "RGIBBS" and r["variable"] == "T_C" and r.get("value") == 400:
                known["B_reactor_T_400"][(r["class"], "asked" if r["id"] in asked_ids else "not asked")] += 1

    summary = {}
    for paper in ("A", "B", "C", "C-old"):
        g = per_paper.get(paper)
        if not g:
            continue
        print("=" * 78)
        print(f"paper {paper}: {g['runs']} runs | classes: {dict(g['cls'])}")
        print(f"  questions per run  policy: {g['n_ask_policy']}   current selector: {g['n_ask_current']}")
        print(f"  stability (mean pairwise Jaccard of asked sets)  policy: {jaccard(g['ask_sets_policy'])}"
              f"   current: {jaccard(g['ask_sets_current'])}")
        row = {"runs": g["runs"], "classes": dict(g["cls"]), "ask_policy": g["n_ask_policy"],
               "ask_current": g["n_ask_current"]}
        if paper != "C-old":
            for who in ("policy", "current"):
                c = g["conf"][who]
                p, r = prf(c["TP"], c["FP"], c["FN"])
                print(f"  vs draft gold [{who:7}] TP={c['TP']} FP={c['FP']} FN={c['FN']} TN={c['TN']}  precision={p} recall={r}")
                row[f"conf_{who}"] = dict(c)
            if g["high_gold_att"]:
                print(f"  HIGH-impact gold-attention items asked: policy {g['high_asked_policy']}/{g['high_gold_att']}"
                      f"   current {g['high_asked_current']}/{g['high_gold_att']}")
            print("  policy false positives:", dict(g["fp"]))
            print("  policy false negatives:", dict(g["fn"]))
            row["fp"] = {str(k): v for k, v in g["fp"].items()}
            row["fn"] = {str(k): v for k, v in g["fn"].items()}
        summary[paper] = row
    print("=" * 78)
    print("known cases (how the classifier handled them):")
    for k, v in known.items():
        print(f"  {k}: {dict(v)}")
    return {"summary": summary, "known": {k: {str(a): b for a, b in v.items()} for k, v in known.items()}}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    runs = collect_runs()
    out = {}
    for tag, flag in (("v0 (quote-level only)", False), ("v1 (+ stated-elsewhere pass)", True)):
        print()
        print("#" * 78)
        print(f"# {tag}")
        print("#" * 78)
        out[tag] = evaluate(runs, flag)
    (HERE / "eval_edc2a.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
