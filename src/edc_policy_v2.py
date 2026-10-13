"""edc_policy_v2.py -- question-policy refinements, OFF by default (env PART1_EDC_POLICY_V2=1 turns them on).

They only reorder or silence questions; no value is ever changed.  Everything they hide is still written to the
assumption ledger / final summary, so nothing becomes invisible.

  R1 NAME_IMPLIED   a pure-species FEED composition whose stream is named after that species ("CH4 feed" with
                    {METHANE: 1.0}).  The name implies the MAIN component, not an exact purity, and how far that holds
                    depends on the paper -> it is still asked, but only after everything else, i.e. when budget is left.
  R2 RATIO_DERIVED  a FEED flow whose quoted number is a ratio the paper states (S/C = 3.0, X/Y = ...).  The value
                    only fixes the stream relative to another feed; the absolute basis is asked through that other
                    feed -> not asked.
  R3 validator      values the semantic validator found supported by the text (DIRECT / PROCESS_LEVEL) are not
                    asked; values it found borrowed from another equipment are asked first.

Developed on cases A and C; NOT validated on held-out papers.
"""
from __future__ import annotations

import copy
import os
import re
from typing import Any, Dict, List, Optional

import edc_policy as ep

FLAG = "PART1_EDC_POLICY_V2"


def enabled() -> bool:
    return os.environ.get(FLAG, "0").strip().lower() in ("1", "true", "on", "yes")


# formula written in a stream name -> component ids the schema may use
_ALIAS = {
    "CH4": {"METHANE"}, "H2O": {"WATER"}, "H2": {"H2", "HYDROGEN"}, "N2": {"N2", "NITROGEN"},
    "O2": {"O2", "OXYGEN"}, "CO": {"CO"}, "CO2": {"CO2"}, "NH3": {"NH3", "AMMONIA"},
    "CH3OH": {"METHANOL"}, "C2H4": {"ETHYLENE"}, "C3H6": {"PROPYLENE"},
}
_RATIO_CUE = re.compile(r"(?<![A-Za-z])S\s*/\s*C(?![A-Za-z])|steam[- ]to[- ]carbon|(?<![A-Za-z])ratio(?![A-Za-z])"
                        r"|(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*\s*/\s*[A-Za-z][A-Za-z0-9]*(?![A-Za-z0-9])", re.I)
NAME_IMPLIED = "NAME_IMPLIED"
RATIO_DERIVED = "RATIO_DERIVED"


def _stream_tokens(schema: Dict[str, Any], sid: str) -> set:
    for s in (schema.get("flowsheet_schema") or {}).get("streams") or []:
        if str(s.get("id")) == sid:
            txt = f'{s.get("name") or ""} {s.get("paper_stream_id") or ""}'
            return {t.upper() for t in re.findall(r"[A-Za-z][A-Za-z0-9]*", txt)}
    return set()


def _component_tokens(schema: Dict[str, Any], comp: str) -> set:
    """Names a component may carry in a stream name: aspen id, formula, name words, and the ALL-CAPS abbreviations
    the schema's own notes mention ("abbreviated MCH in the paper")."""
    toks = {comp.upper()}
    for c in (schema.get("aspen_schema") or {}).get("components") or []:
        if not isinstance(c, dict) or str(c.get("aspen_id", "")).upper() != comp.upper():
            continue
        for key in ("formula", "name"):
            toks |= {t.upper() for t in re.findall(r"[A-Za-z][A-Za-z0-9]*", str(c.get(key) or ""))}
        toks |= set(re.findall(r"(?<![A-Za-z0-9])[A-Z][A-Z0-9]{1,5}(?![A-Za-z0-9])", str(c.get("notes") or "")))
    return toks


def name_implied_pure(row: Dict[str, Any], schema: Dict[str, Any]) -> bool:
    z = row.get("value")
    if row.get("variable") != "z" or row.get("owner_type") != "FEED" or not isinstance(z, dict):
        return False
    major = [k for k, v in z.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
    if len(major) != 1 or abs(float(z[major[0]]) - 1.0) > 1e-9:
        return False
    toks = _stream_tokens(schema, row["owner"])
    comp = str(major[0]).upper()
    if comp in toks or _component_tokens(schema, comp) & toks:
        return True
    return any(comp in names and f in toks for f, names in _ALIAS.items())


def ratio_derived(row: Dict[str, Any]) -> bool:
    if row.get("variable") != "flow" or row.get("owner_type") != "FEED":
        return False
    q, v = row.get("quote"), row.get("value")
    if not q or isinstance(v, bool) or not isinstance(v, (int, float)) or not _RATIO_CUE.search(q):
        return False
    nums, _, _ = ep.parse_quote(q)
    return ep.value_support(v, nums, [], strict=False) == "identity"


def relabel(schema: Dict[str, Any], classified: Dict[str, Any], paper_text: Optional[str] = None,
            use_validator: bool = True) -> Dict[str, Any]:
    """Returns a relabelled COPY of `classified`; the input is untouched."""
    out = copy.deepcopy(classified)
    verdicts: Dict[str, str] = {}
    if use_validator and paper_text is not None:
        import edc_semantic_validator as sv          # lazy: sv imports edc_policy
        verdicts = {c["id"]: c["evidence_relation"] for c in sv.validate_schema(schema, paper_text)["claims"]}
    for r in out["entries"]:
        cls = r["class"]
        if cls not in ep.CANDIDATE_CLASSES:
            continue
        rel = verdicts.get(r["id"])
        if rel in ("DIRECT_SUPPORT", "PROCESS_LEVEL_SUPPORT"):
            r["class"] = "STATED"
            r["flags"].append("v2_supported_by_text:" + rel)
        elif rel == "POTENTIAL_CROSS_EQUIPMENT_BORROWING" and cls != "BORROWED_SUSPECT":
            r["class"] = "BORROWED_SUSPECT"
            r["flags"].append("v2_borrowed_from_other_equipment")
        elif ratio_derived(r):
            r["class"] = RATIO_DERIVED
            r["flags"].append("v2_ratio_derived")
        elif name_implied_pure(r, schema):
            r["class"] = NAME_IMPLIED
            r["flags"].append("v2_name_implied")
    counts: Dict[str, int] = {}
    for r in out["entries"]:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    out["class_counts"] = counts
    return out


_PRIORITY = {**ep._CLASS_PRIORITY, NAME_IMPLIED: 99}


def select_questions_v2(classified: Dict[str, Any], k: int = 5) -> Dict[str, Any]:
    """ep.select_questions with NAME_IMPLIED as a last-resort candidate.  Same shape of result."""
    cands_classes = tuple(ep.CANDIDATE_CLASSES) + (NAME_IMPLIED,)
    cand = []
    for r in classified["entries"]:
        if r["class"] not in cands_classes:
            continue
        if r["impact"] == "HIGH" or (r["impact"] == "MED" and r["class"] in ep._MED_ASKED):
            cand.append(r)
    cand.sort(key=lambda r: (_PRIORITY[r["class"]], 0 if r["impact"] == "HIGH" else 1, r["id"]))
    asked, overflow = cand[:k], cand[k:]
    if len(asked) < k:
        spare = sorted((r for r in classified["entries"]
                        if r["class"] == "ASSUMED" and r["impact"] == "MED" and r not in cand), key=lambda r: r["id"])
        asked += spare[:k - len(asked)]
    return {"asked": asked, "overflow": overflow}


REASONS = {NAME_IMPLIED: "공급 스트림 이름으로 주성분은 짐작되지만 순도는 논문에 적혀 있지 않음",
           RATIO_DERIVED: "논문이 말한 비율에서 나온 값 (절대 기준은 다른 공급 스트림에서 정해짐)"}
SHORT_REASONS = {NAME_IMPLIED: "스트림 이름에서 짐작한 조성", RATIO_DERIVED: "논문의 비율에서 유도"}
