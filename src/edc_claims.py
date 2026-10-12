"""edc_claims.py -- P0-1: turn a Part1 schema into a flat list of "claims" for semantic validation.

A claim is one specification value the flowsheet needs (the same set dof_ledger.build_ledger marks
PROVIDED + required), enriched with what the semantic validator needs and the ledger does not carry:
the free-text rationale, the owner's equipment words (type / name / paper_id / equipment_evidence)
and the kind of quantity.  Pure analysis: no Aspen, no LLM, no network; the schema is never modified.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import dof_ledger as dl
import edc_policy as ep


def var_kind(variable: str) -> str:
    """Physical kind of a ledger variable: temperature / pressure / flow / duty / composition / other."""
    v = re.sub(r"\[.*\]$|#\d+$", "", variable)
    if v.startswith("T_"):
        return "temperature"
    if v.startswith("P_") or v in ("dP_bar",):
        return "pressure"
    if v == "flow":
        return "flow"
    if v == "duty":
        return "duty"
    if v == "z":
        return "composition"
    return "other"


def _owner_context(schema: Dict[str, Any], owner: str) -> Dict[str, Any]:
    """Equipment words attached to the owner (block or feed stream)."""
    fs = schema.get("flowsheet_schema") or {}
    for b in fs.get("blocks") or []:
        if str(b.get("id")) == owner:
            specs = b.get("specs") if isinstance(b.get("specs"), dict) else {}
            ee = specs.get("equipment_evidence")
            return {"kind": "block", "type": str(b.get("type") or "").upper(),
                    "name": b.get("name"), "paper_id": specs.get("paper_id"),
                    "equipment_text": ee.get("text") if isinstance(ee, dict) else None}
    return {"kind": "feed", "type": "FEED", "name": owner, "paper_id": None, "equipment_text": None}


def _rationale(schema: Dict[str, Any], owner: str, variable: str) -> Optional[str]:
    loc = ep.locate_node(schema, owner, variable)
    if not loc:
        return None
    node = loc["container"].get(loc["key"])
    if isinstance(node, dict):
        r = node.get("rationale")
        return r if isinstance(r, str) and r.strip() else None
    return None


# --------------------------------------------------------------------------
# names the paper may use for a block or stream (ids like E0102 / R-101, and distinctive words)
# --------------------------------------------------------------------------
_CHEMICALS = {"H2", "N2", "O2", "CO2", "CH4", "H2O", "NH3", "H2S", "SO2", "NO2", "C2H4", "C3H6"}
_GENERIC_WORDS = {
    "feed", "stream", "product", "mixture", "mixed", "reactor", "heater", "cooler", "shift", "converter",
    "convertor", "high", "temperature", "effluent", "heat", "recovery", "preheater", "preheated", "separator",
    "phase", "cooled", "heated", "vapor", "liquid", "final", "inter", "stage", "reformer", "gibbs", "pump",
    "valve", "outlet", "inlet", "internal", "material", "flash", "mixer", "splitter", "compressor", "exchanger",
    "block", "unit", "process", "recycle", "with", "from", "side", "cold", "hot", "water", "gas",
    "section", "column", "tank", "drum", "total", "low", "pressure",
    "plus", "placeholder", "recovered", "compressed", "residual", "cold", "outlet", "hydrogen-rich",
}


def name_tokens(*names: Any) -> Dict[str, List]:
    """-> {"ids": [(letters, digits)], "words": [lowercase word]} for one block / stream."""
    ids: List = []
    words: List[str] = []
    for n in names:
        s = str(n or "")
        for m in re.finditer(r"(?<![A-Za-z0-9])([A-Za-z]{1,4})-?(\d{1,5})(?![A-Za-z0-9])", s):
            if (m.group(1) + m.group(2)).upper() in _CHEMICALS:
                continue
            if (m.group(1), m.group(2)) not in ids:
                ids.append((m.group(1), m.group(2)))
        for w in re.findall(r"[A-Za-z]{4,}", s):
            lw = w.lower()
            if lw not in _GENERIC_WORDS and lw not in words:
                words.append(lw)
        # a 3-letter ALL-CAPS name is an abbreviation the paper uses for the stream / unit ("Cold MCH", "SEP")
        for w in re.findall(r"(?<![A-Za-z0-9])[A-Z]{3}(?![A-Za-z0-9])", s):
            lw = w.lower()
            if lw not in _GENERIC_WORDS and lw not in words and w not in _CHEMICALS                     and not re.search(re.escape(w) + r"-?\d", s):      # "HPC-1": the id pattern already covers it
                words.append(lw)
    return {"ids": ids, "words": words}


def entity_tokens(schema: Dict[str, Any]) -> Dict[str, Dict[str, List]]:
    """owner id (block id or stream id) -> name tokens, for every block and stream of the schema."""
    fs = schema.get("flowsheet_schema") or {}
    out: Dict[str, Dict[str, List]] = {}
    for b in fs.get("blocks") or []:
        specs = b.get("specs") if isinstance(b.get("specs"), dict) else {}
        out[str(b.get("id"))] = name_tokens(b.get("name"), specs.get("paper_id"))
    for s in fs.get("streams") or []:
        out[str(s.get("id"))] = name_tokens(s.get("name"), s.get("paper_stream_id"))
    return out


def process_scope(schema: Dict[str, Any]) -> Dict[str, List[str]]:
    """Process names the schema's own scope includes / excludes (e.g. ATR in, SMR / SE-SMR / SE-ATR out).
    Used to ignore sentences about a different process of a multi-process paper."""
    sc = schema.get("scope") or {}
    acr = r"(?<![A-Za-z0-9])[A-Z]{2,}(?:-[A-Z]{2,})*(?![A-Za-z0-9])"
    inc_text = " ".join([str(sc.get("target_unit") or "")] + [str(x) for x in sc.get("included_units") or []])
    exc_text = " ".join(str(x) for x in sc.get("excluded_units") or [])
    inc = set(re.findall(acr, inc_text))
    exc = set(re.findall(acr, exc_text)) - inc
    return {"included": sorted(inc), "excluded": sorted(exc, key=len, reverse=True)}


def extract_claims(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One claim per required, PROVIDED, non-analysis-only ledger entry.  Reuses dof_ledger."""
    ledger = dl.build_ledger(schema)
    claims: List[Dict[str, Any]] = []
    for e in ledger["entries"]:
        if e["status"] != dl.PROVIDED or not e["required"] or e.get("analysis_only"):
            continue
        prov = e.get("provenance") or {}
        owner_ctx = _owner_context(schema, e["owner"])
        claims.append({
            "id": e["id"], "owner": e["owner"], "owner_type": owner_ctx["type"],
            "owner_context": owner_ctx, "variable": e["variable"], "kind": var_kind(e["variable"]),
            "tracked": bool(prov.get("tracked")), "value": prov.get("value"),
            "source": prov.get("source"), "quote": prov.get("quote"),
            "quote_scope": prov.get("quote_scope"),
            "rationale": _rationale(schema, e["owner"], e["variable"]) if prov.get("tracked") else None,
        })
    return claims


# --------------------------------------------------------------------------
# multi-case papers: which case does the schema model?  which case does a section / sentence describe?
# --------------------------------------------------------------------------
_CASE_ID = re.compile(r"(?<![A-Za-z0-9])(?:Cases?\s+(\d+(?:-\d+)?)|C(\d+(?:-\d+)?)[BH](?![A-Za-z0-9]))")


def case_ids(text: str) -> set:
    """Case numbers a text names: "Case 3", "Case 4-1", "C2B", "C4-2H" -> {"3"}, {"4-1"}, {"2"}, {"4-2"}."""
    return {a or b for a, b in _CASE_ID.findall(text or "")}


def target_cases(schema: Dict[str, Any]) -> set:
    """Case numbers the schema's own scope names (empty when the paper has no cases)."""
    sc = schema.get("scope") or {}
    txt = " ".join([str(sc.get("target_unit") or ""), str(sc.get("target_unit_name") or "")]
                   + [str(x) for x in sc.get("target_unit_keywords") or []])
    return case_ids(txt)
