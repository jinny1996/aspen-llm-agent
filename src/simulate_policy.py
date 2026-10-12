"""simulate_policy.py -- OFFLINE what-if for the question policy; changes nothing in the pipeline.

Compares which values would be asked (K=5) by
  current      edc_policy.select_questions as it is
  validator    the same selector, but values the semantic validator found supported are not candidates and
               values it flagged as borrowed (POTENTIAL_CROSS_EQUIPMENT_BORROWING) are asked first
against gold labels: a value should be asked iff gold != FIXED.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

import edc_policy as ep
import edc_semantic_validator as sv
from score_semantic import POSITIVE


def asked_sets(schema, paper, k=5):
    classified = ep.classify_schema(schema, paper)
    present = {r["id"] for r in classified["entries"]}
    cur = {r["id"] for r in ep.select_questions(classified, k=k)["asked"]}
    rep = {c["id"]: c["evidence_relation"] for c in sv.validate_schema(schema, paper)["claims"]}
    mod = copy.deepcopy(classified)
    for r in mod["entries"]:
        rel = rep.get(r["id"])
        if rel in POSITIVE and r["class"] in ep.CANDIDATE_CLASSES:
            r["class"] = "STATED"                       # supported by the text: nothing to ask
        elif rel == "POTENTIAL_CROSS_EQUIPMENT_BORROWING":
            r["class"] = "BORROWED_SUSPECT"             # borrowed from another equipment: ask first
    new = {r["id"] for r in ep.select_questions(mod, k=k)["asked"]}
    return cur, new, present


def evaluate(asked, gold, present):
    """`present`: ids this schema actually has (gold may contain keys of other runs)."""
    gold = {g: v for g, v in gold.items() if g in present}
    need = {g for g, v in gold.items() if v["label"] != "FIXED"}
    fixed = {g for g, v in gold.items() if v["label"] == "FIXED"}
    return {"asked": len(asked), "needed_asked": len(asked & need), "needed_total": len(need),
            "needed_missed": sorted(need - asked), "fixed_asked(over-asking)": len(asked & fixed),
            "fixed_asked_ids": sorted(asked & fixed)}


def one(name, sp, pp, gp):
    schema = yaml.safe_load(Path(sp).read_text(encoding="utf-8"))
    paper = Path(pp).read_text(encoding="utf-8", errors="replace")
    gold = yaml.safe_load(Path(gp).read_text(encoding="utf-8"))[name]
    cur, new, present = asked_sets(schema, paper)
    print(f"== {name}  {Path(sp).parent.parent.name}")
    for label, s in (("current", cur), ("validator-aware", new)):
        print(f"  {label:<16}", evaluate(s, gold, present))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) == 5:                       # simulate_policy.py SCHEMA PAPER GOLD CASE
        one(sys.argv[4], *sys.argv[1:4])
        return
    here = Path(__file__).parent
    cases = {
        "A": (here / "../phase13/run/attempt_chat_20261007_204117/part1/Schema_proto.yaml",
              here / "../../jinny_DB/parsed/paper2/paper_h2.md", here / "gold_A_draft.yaml"),
        "C": (here / "../phase13/run/attempt_chat_20261007_204345/part1/Schema_proto.yaml",
              here / "../../jinny_DB/parsed/paper3/paper3.md", here / "../phase13/2e_gold_labels.yaml"),
    }
    for name, (sp, pp, gp) in cases.items():
        schema = yaml.safe_load(sp.read_text(encoding="utf-8"))
        paper = pp.read_text(encoding="utf-8", errors="replace")
        gold = yaml.safe_load(gp.read_text(encoding="utf-8"))[name]
        cur, new, present = asked_sets(schema, paper)
        print(f"== case {name}")
        for label, s in (("current", cur), ("validator-aware", new)):
            print(f"  {label:<16}", evaluate(s, gold, present))


if __name__ == "__main__":
    main()
