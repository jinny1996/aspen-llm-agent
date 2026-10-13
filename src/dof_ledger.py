"""dof_ledger.py -- EDC-1: schema-level degrees-of-freedom (DoF) ledger.

Takes a Part1 schema (Schema_proto.yaml / Schema_validated.yaml structure) and
produces a ledger of every specification variable the flowsheet needs, whether
it is provided, and with what provenance.  Pure analysis: no Aspen, no LLM, no
network, so it can run (and be unit-tested) for free.

Entry statuses
  PROVIDED           variable has a value in the schema
  MISSING            variable (or one-of group) required but absent/null
  DEFAULT_CANDIDATE  Aspen-side variable the schema does not model at all; if
                     the simulator needs it, it silently uses its own default.
                     This list is a STATIC, UNVERIFIED audit shortlist (names
                     only, no default values asserted); EDC-3 must confirm
                     against a real Aspen data tree.

Rule provenance (what is a rule based on)
  * block modes / required fields: valid_block_rules.yaml (Part2 validator) and
    Schema_v5.yaml block_specs_schema.  Schema aliases such as HEATER
    spec_mode OUTLET_T / DUTY are NOT interpreted anywhere in Part2 code (the
    LLM interprets them during code generation), so their mapping here is an
    assumption of this module.
  * RADFRAC is ANALYSIS-ONLY: Part2 / validators / codegen do not support it
    end to end.  Its field names beyond Schema_v5.1(rad) (nstage, condenser,
    reboiler) are PROPOSED here (feed_stage, P_top_bar, design_specs) and the
    "exactly two design specs" rule follows the RADFRAC guide in
    Kang et al., I&ECR 2026 (ACS Copilot, Table 5).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

PROVIDED = "PROVIDED"
MISSING = "MISSING"
DEFAULT_CANDIDATE = "DEFAULT_CANDIDATE"

SOURCES = ("paper", "inferred", "default", "llm", "user")

# --------------------------------------------------------------------------
# Rule tables
# --------------------------------------------------------------------------
_BLOCK_RULES: Dict[str, Dict[str, Any]] = {
    "FLASH2": {
        "kind": "modes", "mode_field": "spec_mode",
        "modes": {
            "TP": ["T_out_C", "P_out_bar"],
            "TD": ["T_out_C", "duty"],
            "PD": ["P_out_bar", "duty"],
        },
    },
    "HEATER": {
        "kind": "modes", "mode_field": "spec_mode",
        "modes": {
            "TP": ["T_out_C", "P_bar"],
            "OUTLET_T": ["T_out_C", "P_bar"],   # alias assumed = TP
            "TD": ["T_out_C", "duty"],
            "PD": ["P_bar", "duty"],
            "DUTY": ["duty", "P_bar"],          # alias assumed = PD
        },
    },
    "RGIBBS": {"kind": "all", "fields": ["T_C", "P_bar"]},
    "PUMP": {"kind": "one_of", "fields": ["P_out_bar", "dP_bar"]},
    "COMPRESSOR": {"kind": "one_of", "fields": ["P_out_bar", "ratio"]},
    "VALVE": {"kind": "one_of", "fields": ["P_out_bar", "dP_bar"]},
    "MIXER": {"kind": "all", "fields": []},
    "SPLITTER": {"kind": "splitter"},
    "MHEATX": {"kind": "mheatx"},
    "RADFRAC": {"kind": "radfrac", "analysis_only": True},
}

# Optional specs: if absent the simulator falls back to its own default.
_OPTIONAL_SPECS: Dict[str, List[str]] = {
    "PUMP": ["eff"],
    "COMPRESSOR": ["eff"],
    "MIXER": ["P_out_bar"],
}

# STATIC, UNVERIFIED shortlist of Aspen-side variables the schema never models.
_UNMODELED_DEFAULT_CANDIDATES: Dict[str, List[str]] = {
    "FLASH2": ["valid_phases"],
    "HEATER": ["valid_phases", "pressure_drop_when_P_not_given"],
    "RGIBBS": ["product_species_and_phases", "inert_species", "pressure_drop"],
    "MHEATX": ["hot_side_pressure_drop", "cold_side_pressure_drop", "flow_direction"],
    "PUMP": ["driver_efficiency"],
    "COMPRESSOR": ["model_type_isentropic_or_polytropic", "mechanical_efficiency"],
    "MIXER": ["valid_phases"],
    "SPLITTER": [],
    "VALVE": ["valid_phases"],
    "RADFRAC": ["stage_numbering_convention", "column_pressure_drop",
                "condenser_subcooling", "convergence_settings_MAXOL_TOLOL"],
}

_RADFRAC_DESIGN_SPEC_COUNT = 2


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _is_value_node(node: Any) -> bool:
    return isinstance(node, dict) and "value" in node


def _has(node: Any) -> bool:
    """True when a spec carries a usable (non-null, non-empty) value."""
    if node is None:
        return False
    if _is_value_node(node):
        return node.get("value") is not None and node.get("value") != ""
    if isinstance(node, (dict, list)):
        return len(node) > 0
    if isinstance(node, str):
        return node.strip() != ""
    return True


def _provenance(node: Any) -> Dict[str, Any]:
    """Extract provenance from a schema value node (or report that none exists)."""
    if not _is_value_node(node):
        return {"tracked": False}
    ev = node.get("evidence")
    quote = locator = None
    if isinstance(ev, dict):
        quote, locator = ev.get("quote"), ev.get("locator")
    elif isinstance(ev, str) and ev.strip():
        quote = ev
    prov = {
        "tracked": True,
        "source": str(node.get("source") or "").lower() or None,
        "confidence": node.get("confidence"),
        "quote": quote if isinstance(quote, str) and quote.strip() else None,
        "locator": locator,
        "has_rationale": bool(node.get("rationale")),
        "value": node.get("value"),
    }
    # optional fields (PART1_EDC_PROMPT): carried through untouched, judged in edc_policy
    for k in ("quote_scope", "alternatives", "derivation"):
        if node.get(k):
            prov[k] = node[k]
    return prov


def _entry(owner: str, variable: str, status: str, node: Any = None,
           group: Optional[str] = None, note: Optional[str] = None,
           required: bool = True, analysis_only: bool = False) -> Dict[str, Any]:
    e: Dict[str, Any] = {
        "id": f"{owner}.{variable}", "owner": owner, "variable": variable,
        "status": status, "required": required,
    }
    if status == PROVIDED:
        e["provenance"] = _provenance(node)
    if group:
        e["group"] = group
    if note:
        e["note"] = note
    if analysis_only:
        e["analysis_only"] = True
    return e


# --------------------------------------------------------------------------
# per-block evaluation
# --------------------------------------------------------------------------
def _outlet_count(block_id: str, edges: List[Dict[str, Any]]) -> int:
    n = 0
    for ed in edges:
        frm = ed.get("from")
        if isinstance(frm, dict) and frm.get("block") == block_id:
            n += 1
    return n


def _eval_block(block: Dict[str, Any], edges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bid = str(block.get("id"))
    btype = str(block.get("type") or "").upper()
    specs = block.get("specs") or {}
    rule = _BLOCK_RULES.get(btype)
    out: List[Dict[str, Any]] = []
    analysis_only = bool(rule and rule.get("analysis_only"))
    if rule is None:
        out.append(_entry(bid, "block_type", MISSING,
                          note=f"no DoF rule for block type {btype!r}"))
        return out
    kind = rule["kind"]

    if kind == "modes":
        modes = rule["modes"]
        mode = specs.get(rule["mode_field"])
        chosen = mode if mode in modes else None
        if chosen is None:
            for m, need in modes.items():
                if all(_has(specs.get(f)) for f in need):
                    chosen = m
                    break
        if chosen is None:
            out.append(_entry(bid, "spec_mode", MISSING,
                              group="|".join(modes),
                              note="no complete spec set for any allowed mode"
                                   + (f" (spec_mode={mode!r} unrecognised)" if mode else "")))
        else:
            for f in modes[chosen]:
                node = specs.get(f)
                out.append(_entry(bid, f, PROVIDED if _has(node) else MISSING, node,
                                  group=f"mode={chosen}"))

    elif kind == "all":
        for f in rule["fields"]:
            node = specs.get(f)
            out.append(_entry(bid, f, PROVIDED if _has(node) else MISSING, node))

    elif kind == "one_of":
        fields = rule["fields"]
        given = [f for f in fields if _has(specs.get(f))]
        grp = "one_of(" + "|".join(fields) + ")"
        if given:
            for f in given:
                out.append(_entry(bid, f, PROVIDED, specs.get(f), group=grp))
        else:
            out.append(_entry(bid, grp, MISSING, group=grp))

    elif kind == "splitter":
        n_out = _outlet_count(bid, edges)
        fr = specs.get("split_fractions")
        provided = [k for k, v in (fr.items() if isinstance(fr, dict) else []) if v is not None]
        need = max(n_out - 1, 0)
        if n_out < 2:
            out.append(_entry(bid, "outlets", MISSING,
                              note=f"splitter has {n_out} outlet edge(s); need >=2"))
        else:
            for k in provided[:need] if len(provided) >= need else provided:
                out.append(_entry(bid, f"split_fraction[{k}]", PROVIDED,
                                  fr[k], group=f"need {need} of {n_out}"))
            for i in range(max(need - len(provided), 0)):
                out.append(_entry(bid, f"split_fraction#{len(provided) + i + 1}", MISSING,
                                  group=f"need {need} of {n_out}"))

    elif kind == "mheatx":
        outs = specs.get("outlet_streams")
        pairs = len(outs) if isinstance(outs, dict) else 0
        if pairs == 0:
            out.append(_entry(bid, "outlet_streams", MISSING))
        else:
            out.append(_entry(bid, "outlet_streams", PROVIDED, outs))
            temps = specs.get("temp_specs") or []
            have = [t for t in temps if isinstance(t, dict) and _has(t.get("T_out_C"))]
            need = max(pairs - 1, 0)
            for t in have[:need] if len(have) >= need else have:
                out.append(_entry(bid, f"T_out_C[{t.get('out_stream')}]", PROVIDED,
                                  t.get("T_out_C"), group=f"need {need} of {pairs}"))
            for i in range(max(need - len(have), 0)):
                out.append(_entry(bid, f"T_out_C#{len(have) + i + 1}", MISSING,
                                  group=f"need {need} of {pairs}"))

    elif kind == "radfrac":
        for f in ("nstage", "feed_stage", "P_top_bar"):
            node = specs.get(f)
            out.append(_entry(bid, f, PROVIDED if _has(node) else MISSING, node,
                              analysis_only=True))
        for f in ("condenser", "reboiler"):
            node = specs.get(f)
            out.append(_entry(bid, f, PROVIDED if _has(node) else MISSING, node,
                              analysis_only=True))
        ds = specs.get("design_specs")
        n_ds = len(ds) if isinstance(ds, (list, dict)) else 0
        if n_ds == _RADFRAC_DESIGN_SPEC_COUNT:
            out.append(_entry(bid, "design_specs", PROVIDED, ds,
                              group="exactly 2", analysis_only=True))
        else:
            out.append(_entry(bid, "design_specs", MISSING, group="exactly 2",
                              note=f"{n_ds} given, need exactly {_RADFRAC_DESIGN_SPEC_COUNT}",
                              analysis_only=True))

    for f in _OPTIONAL_SPECS.get(btype, []):
        node = specs.get(f)
        if _has(node):
            out.append(_entry(bid, f, PROVIDED, node, required=False))
        else:
            out.append(_entry(bid, f, DEFAULT_CANDIDATE, required=False,
                              note="optional spec absent -> simulator default applies",
                              analysis_only=analysis_only))
    for f in _UNMODELED_DEFAULT_CANDIDATES.get(btype, []):
        out.append(_entry(bid, f, DEFAULT_CANDIDATE, required=False,
                          note="not modelled in schema (unverified shortlist)",
                          analysis_only=analysis_only))
    return out


# --------------------------------------------------------------------------
# feeds
# --------------------------------------------------------------------------
def _eval_feeds(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    fs = schema.get("flowsheet_schema") or {}
    feed_ids = [s.get("id") for s in (fs.get("streams") or []) if s.get("role") == "FEED"]
    feeds = ((schema.get("io") or {}).get("feeds")) or []
    by_stream = {f.get("stream"): f for f in feeds if isinstance(f, dict)}
    for sid in feed_ids:
        f = by_stream.get(sid)
        if f is None:
            out.append(_entry(str(sid), "feed_specs", MISSING,
                              note="FEED stream has no io.feeds entry"))
            continue
        sp = f.get("specs") or {}
        for var in ("T_C", "P_bar", "flow"):
            node = sp.get(var)
            out.append(_entry(str(sid), var, PROVIDED if _has(node) else MISSING, node))
        z = sp.get("z")
        if not isinstance(z, dict) or not z:
            out.append(_entry(str(sid), "z", MISSING))
        else:
            total = sum(float(v) for v in z.values() if isinstance(v, (int, float)))
            ok = abs(total - 1.0) <= 0.001      # tolerance from valid_feed_rules.yaml
            # `z_provenance` is a sibling key so the fractions dict itself stays as Part2 expects
            zp = sp.get("z_provenance")
            node = {**zp, "value": z} if isinstance(zp, dict) else z
            out.append(_entry(str(sid), "z", PROVIDED, node,
                              note=None if ok else f"fractions sum to {total:.4f}, not 1"))
    return out


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def build_ledger(schema: Dict[str, Any]) -> Dict[str, Any]:
    fs = schema.get("flowsheet_schema") or {}
    edges = ((fs.get("connectivity") or {}).get("edges")) or []
    entries: List[Dict[str, Any]] = []
    for b in fs.get("blocks") or []:
        entries.extend(_eval_block(b, edges))
    entries.extend(_eval_feeds(schema))
    return {"entries": entries, "summary": summarize(entries)}


def summarize(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    req = [e for e in entries if e["required"] and not e.get("analysis_only")]
    provided = [e for e in req if e["status"] == PROVIDED]
    missing = [e for e in req if e["status"] == MISSING]
    by_source: Dict[str, int] = {}
    untracked = 0
    for e in entries:
        if e["status"] != PROVIDED:
            continue
        p = e.get("provenance") or {}
        if not p.get("tracked"):
            untracked += 1
        else:
            by_source[p.get("source") or "none"] = by_source.get(p.get("source") or "none", 0) + 1
    defaults = [e for e in entries if e["status"] == DEFAULT_CANDIDATE and not e.get("analysis_only")]
    return {
        "required": len(req), "provided": len(provided), "missing": len(missing),
        "missing_ids": [e["id"] for e in missing],
        "provided_by_source": by_source, "provided_without_provenance": untracked,
        "default_candidates": len(defaults),
        "default_candidate_ids": [e["id"] for e in defaults],
    }


# --------------------------------------------------------------------------
# evidence quote verification (STATED must be traceable to the paper text)
# --------------------------------------------------------------------------
# \circ / \degree are the degree sign in MinerU-parsed papers ("$4 0 ~ ^ { \circ } C$" == 40 degC)
_LATEX_NOISE = re.compile(r"\\(mathrm|mathbf|mathit|text|left|right|operatorname|circ|degree)")


def _compact(text: str) -> str:
    t = text.lower()
    t = _LATEX_NOISE.sub("", t)
    return re.sub(r"[^0-9a-z]", "", t)


def _fragments(quote: str) -> List[str]:
    parts = re.split(r"\.\.\.|…|;|\|", quote)
    return [p for p in (x.strip() for x in parts) if len(_compact(p)) >= 10]


_UNIT_TOKENS = {"k", "bar"}   # units LLMs append to bare table numbers; dropped on both sides
_MAX_TOKEN_GAP = 6


def _tokens(text: str) -> List[str]:
    t = _LATEX_NOISE.sub("", text.lower())
    return [x for x in re.findall(r"[a-z0-9]+", t) if x not in _UNIT_TOKENS]


def _tokens_in_order(q: List[str], p: List[str], max_gap: int = _MAX_TOKEN_GAP) -> bool:
    """All quote tokens occur in the paper in order, <= max_gap tokens apart.

    Catches quotes reconstructed from table rows (tabs/units/headers removed or
    added) that are not literal substrings but are grounded in the source.
    Grounded != verified: it does not prove the quote supports the VALUE."""
    if not q:
        return False
    for s in (i for i, t in enumerate(p) if t == q[0]):
        pos, ok = s, True
        for t in q[1:]:
            nxt = None
            for j in range(pos + 1, min(pos + 2 + max_gap, len(p))):
                if p[j] == t:
                    nxt = j
                    break
            if nxt is None:
                ok = False
                break
            pos = nxt
        if ok:
            return True
    return False


def check_quote(quote: Optional[str], paper_text: str,
                _paper_compact: Optional[str] = None,
                _paper_tokens: Optional[List[str]] = None) -> str:
    """Quote grounding tier (whitespace/punct/LaTeX tolerant):

    EXACT           quote is a literal substring of the paper
    FRAGMENTS_ALL   quote stitched with '...'/';' from several literal substrings
    RECONSTRUCTED   not literal, but every fragment's tokens appear in order
                    nearby in the paper (e.g. rebuilt from a table row)
    PARTIAL         some fragments grounded, some not
    NOT_FOUND       nothing grounded
    NO_QUOTE        no quote given
    """
    if not quote:
        return "NO_QUOTE"
    pc = _paper_compact if _paper_compact is not None else _compact(paper_text)
    qc = _compact(quote)
    if qc and qc in pc:
        return "EXACT"
    pt = _paper_tokens if _paper_tokens is not None else _tokens(paper_text)
    frs = _fragments(quote) or [quote]
    marks: List[Optional[str]] = []
    for f in frs:
        if _compact(f) in pc:
            marks.append("EXACT")
        elif _tokens_in_order(_tokens(f), pt):
            marks.append("RECON")
        else:
            marks.append(None)
    if all(m == "EXACT" for m in marks):
        return "EXACT" if len(frs) == 1 else "FRAGMENTS_ALL"
    if all(marks):
        return "RECONSTRUCTED"
    if any(marks):
        return "PARTIAL"
    return "NOT_FOUND"


def _numbers(text: str) -> List[str]:
    return re.findall(r"\d+(?:\.\d+)?", text)


def numbers_grounded(quote: Optional[str], paper_numbers: set) -> Optional[bool]:
    """Secondary signal: every number in the quote occurs somewhere in the paper.

    Independent of word order, so it separates 'header+row merged' table quotes
    from invented ones.  None when there is no quote or the quote has no number.
    Weak evidence only: it does not show the number supports the VALUE."""
    if not quote:
        return None
    nums = _numbers(quote)
    if not nums:
        return None
    return all(n in paper_numbers for n in nums)


def verify_evidence(ledger: Dict[str, Any], paper_text: str) -> Dict[str, Any]:
    """Annotate PROVIDED entries with evidence_check; return counts by claimed source."""
    pc = _compact(paper_text)
    pt = _tokens(paper_text)
    pn = set(_numbers(paper_text))
    counts: Dict[str, Dict[str, int]] = {}
    for e in ledger["entries"]:
        if e["status"] != PROVIDED:
            continue
        p = e.get("provenance") or {}
        if not p.get("tracked"):
            continue
        status = check_quote(p.get("quote"), paper_text, pc, pt)
        e["evidence_check"] = status
        e["quote_numbers_grounded"] = numbers_grounded(p.get("quote"), pn)
        src = p.get("source") or "none"
        counts.setdefault(src, {}).setdefault(status, 0)
        counts[src][status] += 1
    ledger["summary"]["evidence_check_by_source"] = counts
    return counts


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _load_schema(path: Path) -> Dict[str, Any]:
    import yaml  # local import: keeps the analysis functions import-light
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="DoF ledger for a Part1 schema")
    ap.add_argument("schema", type=Path)
    ap.add_argument("--paper", type=Path, help="paper text/markdown for quote verification")
    ap.add_argument("--json", type=Path, help="write full ledger JSON here")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ledger = build_ledger(_load_schema(args.schema))
    if args.paper:
        verify_evidence(ledger, args.paper.read_text(encoding="utf-8", errors="replace"))
    print(json.dumps(ledger["summary"], ensure_ascii=False, indent=2))
    if args.json:
        args.json.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
