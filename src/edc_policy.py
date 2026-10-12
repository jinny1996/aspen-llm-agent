"""edc_policy.py -- EDC-2a: evidence classification + question selection (offline).

Pure analysis on a Part1 schema and the paper text: no LLM, no Aspen, no network, and NOT wired
into the pipeline.  Builds on dof_ledger (EDC-1).

What it decides, per required value that carries provenance
  class  STATED                   the quoted text contains the value itself
         DERIVED                  the value follows from a quoted number by a unit conversion
                                  (K->degC, atm->bar, %->fraction)
         SWEPT                    the quote talks about a range / sweep and does not give the value
         BORROWED_SUSPECT         labelled inferred/default/llm, yet the quote contains the exact
                                  value -> the number exists in the paper but was transplanted to
                                  another variable (weak heuristic, a SUSPICION only)
         ASSUMED                  no usable evidence
         UNSUPPORTED_PAPER_CLAIM  labelled `paper` but the quote is missing/unverifiable or does
                                  not contain the value
         USER / NO_PROVENANCE     answered by the user / schema has no provenance field (e.g. z)
  The LLM's self-reported confidence is NOT used for any decision (it is kept for display only).

NOT implemented here (needs the Part1 prompt change of EDC-2e): ALTERNATIVE (several candidate
values in the paper, one chosen), and "derived by calculation" (e.g. 0.45/0.21 = 2.142857).

Question policy (draft, to be agreed): ask for candidates of HIGH impact, plus MED impact when the
class is BORROWED_SUSPECT or UNSUPPORTED_PAPER_CLAIM; at most K items; everything else is accepted
and only recorded.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import dof_ledger as dl

CANDIDATE_CLASSES = ("UNSUPPORTED_PAPER_CLAIM", "ALTERNATIVE", "SWEPT", "BORROWED_SUSPECT", "ASSUMED")
_CLASS_PRIORITY = {c: i for i, c in enumerate(CANDIDATE_CLASSES)}
_MED_ASKED = ("BORROWED_SUSPECT", "UNSUPPORTED_PAPER_CLAIM", "ALTERNATIVE")

# --------------------------------------------------------------------------
# number / range parsing of a quote
# --------------------------------------------------------------------------
_NUM = r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?(?![A-Za-z0-9])"        # not part of a tag like E2, P0101, O2
_RANGE_RE = re.compile(
    r"(?:between|from)\s*(\d+(?:\.\d+)?)\s*(?:°\s*C|K|bar|atm|%)?\s*(?:and|to)\s*(\d+(?:\.\d+)?)"
    r"|(\d+(?:\.\d+)?)\s*[–—−\-~]\s*(\d+(?:\.\d+)?)",
    re.I,
)
_CUE_RE = re.compile(r"\b(?:varied|varying|vary|sweep|swept|range of|ranging|sensitivity)\b", re.I)
_UNIT_EXP_RE = re.compile(r"(?<=[A-Za-z])\s*[−\-]\s*\d\b")      # 'kg h-1', 's-1'


_MATH_RE = re.compile(r"\$([^$]*)\$")


def _clean_math(text: str) -> str:
    """PDF->markdown (MinerU) writes numbers inside $...$ with spaced digits, e.g. `$4 0 0 ~ ^ { circ } mathrm { C }$` (LaTeX backslashes omitted here).
    Inside math only: drop the LaTeX decoration and join digits, giving `400 degC`."""
    def fix(m: "re.Match") -> str:
        t = m.group(1)
        t = re.sub(r"\\(?:circ|degree)", "°", t)
        t = re.sub(r"\\(?:mathrm|rm|text|mathbf)|[{}^~]|\\[ ,;!]", " ", t)
        for _ in range(3):
            t = re.sub(r"(?<=\d)\s+(?=\d)", "", t)
            t = re.sub(r"(?<=\d)\s+(?=\.\s*\d)|(?<=\.)\s+(?=\d)", "", t)
        return " " + re.sub(r"\s+", " ", t).strip() + " "
    return _MATH_RE.sub(fix, text) if "$" in text else text


def parse_quote(quote: str) -> Tuple[List[float], List[Tuple[float, float]], bool]:
    """-> (numbers outside any range, ranges, has_sweep_cue)"""
    q = _UNIT_EXP_RE.sub("", _clean_math(quote))
    ranges: List[Tuple[float, float]] = []
    spans: List[Tuple[int, int]] = []
    for m in _RANGE_RE.finditer(q):
        lo, hi = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        ranges.append((float(lo), float(hi)))
        spans.append(m.span())
    rest = q
    for s, e in reversed(spans):
        rest = rest[:s] + " " + rest[e:]
    nums = [float(x) for x in re.findall(_NUM, rest)]
    return nums, ranges, bool(_CUE_RE.search(q))


def value_support(value: Any, nums: List[float], ranges: List[Tuple[float, float]],
                  strict: bool = False) -> Optional[str]:
    """'identity' | 'conversion' | 'range_endpoint' | None.
    strict=True uses tight tolerances; used when matching against the WHOLE paper, where a
    loose match would hit by coincidence."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    v = float(value)
    rel = 1e-6 if strict else 1e-3
    tol = max(1e-9, rel * abs(v))
    k_tol = 0.006 if strict else 0.06
    if any(abs(n - v) <= tol for n in nums):
        return "identity"
    for n in nums:
        if abs((n - 273.15) - v) <= k_tol:                # K -> degC
            return "conversion"
        if abs(n * 1.01325 - v) <= (1e-4 * abs(v) if strict else tol):      # atm -> bar
            return "conversion"
        if abs(n / 100.0 - v) <= tol + 1e-6:              # % -> fraction
            return "conversion"
    for lo, hi in ranges:
        if abs(lo - v) <= tol or abs(hi - v) <= tol:
            return "range_endpoint"
    return None


# --------------------------------------------------------------------------
# paper context (computed once per paper)
# --------------------------------------------------------------------------
class PaperContext:
    def __init__(self, paper_text: str):
        self.text = paper_text
        self.compact = dl._compact(paper_text)
        self.tokens = dl._tokens(paper_text)
        self.numbers = set(dl._numbers(paper_text))
        self.nums_nr, self.ranges, _ = parse_quote(paper_text)      # numbers outside ranges, whole paper

    def states_value(self, value: Any) -> Optional[str]:
        """does the paper itself state this value (identity or unit conversion), anywhere?"""
        k = value_support(value, self.nums_nr, [], strict=True)
        return k if k in ("identity", "conversion") else None

    def states_all_fractions(self, z: Any) -> Optional[str]:
        """composition: the paper states EVERY non-zero fraction somewhere (a table the quote only
        captioned).  Needs >= 2 non-zero fractions: a pure stream {H2: 1.0} would match any '1.0'."""
        if not isinstance(z, dict):
            return None
        vals = [float(v) for v in z.values()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) != 0.0]
        if len(vals) < 2:
            return None
        kinds = [value_support(v, self.nums_nr, [], strict=True) for v in vals]
        if all(k in ("identity", "conversion") for k in kinds):
            return "conversion" if "conversion" in kinds else "identity"
        return None

    values: Dict[str, Any] = {}          # id -> provided numeric value; set by classify_schema (derivations)

    def quote_tier(self, quote: Optional[str]) -> str:
        return dl.check_quote(quote, self.text, self.compact, self.tokens)

    def numbers_grounded(self, quote: Optional[str]) -> Optional[bool]:
        return dl.numbers_grounded(quote, self.numbers)


# --------------------------------------------------------------------------
# EDC-2e: checks of the structured evidence the Part1 prompt asks for
# --------------------------------------------------------------------------
_SCOPE_BORROWED = ("other_config",)
_SCOPE_SWEPT = ("sweep_range",)
# constants a derivation may use without the paper stating them (unit conversions, percent, ...)
_FREE_CONSTANTS = {0.0, 1.0, 2.0, 3.0, 10.0, 60.0, 100.0, 1000.0, 3600.0, 273.15, 1.01325, 0.001, 0.01}


_COMMIT_RE = re.compile(
    r"\b(?:(?:is|was|were|are|been|be)\s+)?(?:set|fixed|kept|held|maintained)\s+(?:constant\s+)?(?:to|at)\b"
    r"|\b(?:adopted|chosen|selected|specified)\b|\bkept\s+constant\b", re.I)
_CASE_RE = re.compile(r"\b(?:case|scenario|configuration|option|system)\s+(?:[IVX]+|\d+|[A-Z])\b", re.I)


def _commits_to_value(quote: Optional[str]) -> bool:
    """The (verified) quote itself says the value was set/fixed/chosen, and it is not tied to one
    named case/scenario/configuration (then other cases' values may still be open)."""
    return bool(quote) and bool(_COMMIT_RE.search(quote)) and not _CASE_RE.search(quote)


_UNIT_FACTORS = (1e3, 60.0, 3600.0)       # ton/h vs kg/h, kmol vs mol, per minute / hour / second


def _same_quantity(a: float, b: float) -> bool:
    """Equal, or the same quantity written in another unit (303823.76 kg/h and the rounded 303.8 ton/h).
    A real alternative differs by more than a unit factor plus rounding."""
    if abs(a - b) <= 1e-9 + 1e-6 * abs(b):
        return True
    for f in _UNIT_FACTORS:
        for x, y in ((a * f, b), (a / f, b)):
            if y != 0 and abs(x - y) <= 5e-3 * abs(y):
                return True
    return False


def verified_alternatives(prov: Dict[str, Any], ctx: PaperContext) -> List[float]:
    """Distinct candidate values the LLM listed AND the paper confirms: the quote exists in the
    paper and contains the value.  The chosen value counts as a candidate when its own quote is real."""
    vals: List[float] = []

    def add(v: Any, q: Optional[str]) -> None:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not q:
            return
        if ctx.quote_tier(q) not in ("EXACT", "FRAGMENTS_ALL", "RECONSTRUCTED"):
            return
        nums, ranges, _ = parse_quote(q)
        if value_support(v, nums, ranges) != "identity":
            return
        if not any(_same_quantity(float(v), x) for x in vals):
            vals.append(float(v))

    for alt in prov.get("alternatives") or []:
        if isinstance(alt, dict):
            add(alt.get("value"), alt.get("quote"))
    return vals


def _safe_eval(expr: str, names: Dict[str, float]) -> Tuple[Optional[float], List[float]]:
    """Arithmetic only (+ - * / ** parentheses, numbers, names).  -> (value, constants used)."""
    import ast
    consts: List[float] = []

    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            consts.append(float(n.value))
            return float(n.value)
        if isinstance(n, ast.Name) and n.id in names:
            return float(names[n.id])
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                return a / b
            if abs(b) > 10:
                raise ValueError("exponent too large")
            return a ** b
        raise ValueError("unsupported expression")

    try:
        return ev(ast.parse(expr.strip(), mode="eval")), consts
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None, consts


def check_derivation(prov: Dict[str, Any], ctx: PaperContext) -> Dict[str, Any]:
    """Recompute a value the LLM says it calculated: {formula: "x*y/2", inputs: {x: "S1.flow", ...}}.
    -> {"status": "ok"|"mismatch"|"unresolved"|"constant_not_in_paper"|"malformed", ...}
    Only plain arithmetic is recomputed; a derivation can never turn into DERIVED by assertion."""
    d = prov.get("derivation")
    if not isinstance(d, dict) or not isinstance(d.get("formula"), str) or not isinstance(d.get("inputs") or {}, dict):
        return {"status": "malformed"}
    names, missing = {}, []
    for name, rid in (d.get("inputs") or {}).items():
        v = ctx.values.get(str(rid))
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            names[str(name)] = float(v)
        else:
            missing.append(str(rid))
    if missing:
        return {"status": "unresolved", "missing_inputs": missing}
    res, consts = _safe_eval(d["formula"], names)
    if res is None:
        return {"status": "malformed"}
    odd = [c for c in consts if c not in _FREE_CONSTANTS and not any(abs(c - n) <= 1e-9 for n in ctx.nums_nr)]
    val = prov.get("value")
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return {"status": "malformed"}
    if abs(res - float(val)) > max(1e-9, 1e-3 * abs(float(val))):
        return {"status": "mismatch", "recomputed": res}
    if odd:
        return {"status": "constant_not_in_paper", "constants": odd, "recomputed": res}
    return {"status": "ok", "recomputed": res, "inputs": sorted(str(r) for r in (d.get("inputs") or {}).values())}


def _z_support(z: Any, nums: List[float], ranges: List[Tuple[float, float]]) -> Optional[str]:
    """Composition: every non-zero fraction must be supported by the quote (a table row of fractions)."""
    if not isinstance(z, dict):
        return None
    vals = [float(v) for v in z.values() if isinstance(v, (int, float)) and not isinstance(v, bool) and float(v) != 0.0]
    if not vals:
        return None
    # fractions are usually printed as percentages in the paper (15.9 -> 0.159), so a conversion counts
    return "identity" if all(value_support(v, nums, ranges) in ("identity", "conversion") for v in vals) else None


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------
def classify_entry(entry: Dict[str, Any], ctx: PaperContext, stated_elsewhere: bool = True) -> Dict[str, Any]:
    prov = entry.get("provenance") or {}
    out: Dict[str, Any] = {"id": entry["id"], "owner": entry["owner"], "variable": entry["variable"],
                           "flags": []}
    if not prov.get("tracked"):
        out.update({"class": "NO_PROVENANCE"})
        return out
    src, val, quote = prov.get("source"), prov.get("value"), prov.get("quote")
    tier = ctx.quote_tier(quote) if quote else "NO_QUOTE"
    ng = ctx.numbers_grounded(quote) if quote else None
    strict_ok = tier in ("EXACT", "FRAGMENTS_ALL", "RECONSTRUCTED")
    # a paper-labelled quote merged from a table header + row fails the strict check although its
    # numbers are in the paper; accept it for `paper` claims only
    real_quote = strict_ok or (src == "paper" and tier in ("NOT_FOUND", "PARTIAL") and ng is True)
    if real_quote and not strict_ok:
        out["flags"].append("table_merge_quote")
    nums, ranges, cue = parse_quote(quote) if (quote and real_quote) else ([], [], False)
    if isinstance(val, dict):                                  # composition z
        support = _z_support(val, nums, ranges) if (quote and real_quote) else None
    else:
        support = value_support(val, nums, ranges) if (quote and real_quote) else None
    out.update({"source": src, "value": val, "confidence": prov.get("confidence"), "quote": quote,
                "quote_tier": tier, "support": support})

    if src == "user":
        cls = "USER"
    elif src == "paper":
        if not real_quote:
            cls = "UNSUPPORTED_PAPER_CLAIM"
            out["flags"].append("no_verifiable_quote")
        elif support == "identity":
            cls = "STATED"
        elif support == "conversion":
            cls = "DERIVED"
        elif ranges or cue or support == "range_endpoint":
            cls = "SWEPT"
        else:
            cls = "UNSUPPORTED_PAPER_CLAIM"
            out["flags"].append("value_not_in_quote")
        # v1: the cited quote is weak, but the paper does state the value somewhere (e.g. a table
        # gives only the allowed range while the abstract states the chosen value).  Hygiene issue,
        # not a missing fact.  Only for `paper` claims; inferred/default values never get this pass.
        if stated_elsewhere and cls in ("SWEPT", "UNSUPPORTED_PAPER_CLAIM"):
            where = ctx.states_all_fractions(val) if isinstance(val, dict) else ctx.states_value(val)
            if where:
                out["flags"].append("quote_does_not_support_value")
                out["flags"].append("stated_elsewhere_" + where)
                cls = "STATED_ELSEWHERE"
    else:   # inferred / default / llm / anything else
        if real_quote and support == "identity":
            cls = "BORROWED_SUSPECT"
        elif real_quote and support == "conversion":
            cls = "DERIVED"
            out["flags"].append("inferred_by_conversion")
        elif real_quote and (ranges or cue or support == "range_endpoint"):
            cls = "SWEPT"
        else:
            cls = "ASSUMED"
    # ---- EDC-2e overlays: only act on the optional fields the 2e prompt asks Part1 to fill ----
    if src != "user":
        scope = prov.get("quote_scope")
        if cls == "STATED" and scope in _SCOPE_BORROWED:        # self-reported: can only add a question
            cls = "BORROWED_SUSPECT"
            out["flags"].append("declared_scope_" + scope)
        elif cls == "STATED" and scope in _SCOPE_SWEPT:
            cls = "SWEPT"
            out["flags"].append("declared_scope_" + scope)
        if prov.get("alternatives") and cls != "DERIVED":
            alts = verified_alternatives(prov, ctx)
            out["alternatives_verified"] = alts
            if len(alts) >= 2:
                if cls == "STATED" and _commits_to_value(quote):
                    # the cited sentence itself fixes the value ("set to 25 °C in the following
                    # sections"): the other candidates are context, not an open choice
                    out["flags"].append("alternatives_resolved_by_quote")
                else:
                    cls = "ALTERNATIVE"
        if prov.get("derivation"):
            chk = check_derivation(prov, ctx)
            out["derivation_check"] = chk
            if chk["status"] == "ok" and cls in ("ASSUMED", "UNSUPPORTED_PAPER_CLAIM", "STATED_ELSEWHERE", "SWEPT"):
                cls = "DERIVED"
                out["flags"].append("derived_by_calculation")
            elif chk["status"] == "mismatch" and src == "paper":
                cls = "UNSUPPORTED_PAPER_CLAIM"
                out["flags"].append("derivation_mismatch")
            elif chk["status"] == "constant_not_in_paper":
                out["flags"].append("derivation_constant_not_in_paper")
    out["class"] = cls
    return out


# --------------------------------------------------------------------------
# impact (draft table -- needs chemical-engineering review)
# --------------------------------------------------------------------------
_IMPACT = {
    ("RGIBBS", "T_C"): "HIGH", ("RGIBBS", "P_bar"): "HIGH",
    ("FLASH2", "T_out_C"): "HIGH", ("FLASH2", "P_out_bar"): "HIGH", ("FLASH2", "duty"): "HIGH",
    ("HEATER", "T_out_C"): "HIGH", ("HEATER", "duty"): "HIGH", ("HEATER", "P_bar"): "MED",
    ("MHEATX", "T_out_C"): "HIGH",
    ("FEED", "flow"): "HIGH", ("FEED", "z"): "HIGH", ("FEED", "T_C"): "MED", ("FEED", "P_bar"): "MED",
    ("PUMP", "P_out_bar"): "MED", ("PUMP", "dP_bar"): "MED", ("PUMP", "eff"): "LOW",
    ("COMPRESSOR", "P_out_bar"): "MED", ("COMPRESSOR", "ratio"): "MED", ("COMPRESSOR", "eff"): "LOW",
    ("VALVE", "P_out_bar"): "MED", ("VALVE", "dP_bar"): "MED",
    ("MIXER", "P_out_bar"): "LOW", ("SPLITTER", "split_fraction"): "HIGH",
}


def impact(owner_type: str, variable: str) -> str:
    return _IMPACT.get((owner_type.upper(), re.sub(r"\[.*\]|#\d+$", "", variable)), "MED")


def owner_types(schema: Dict[str, Any]) -> Dict[str, str]:
    fs = schema.get("flowsheet_schema") or {}
    m = {str(b.get("id")): str(b.get("type") or "").upper() for b in fs.get("blocks") or []}
    for f in ((schema.get("io") or {}).get("feeds")) or []:
        if isinstance(f, dict) and f.get("stream"):
            m[str(f["stream"])] = "FEED"
    return m


# --------------------------------------------------------------------------
# schema-level API
# --------------------------------------------------------------------------
def classify_schema(schema: Dict[str, Any], paper_text: str, stated_elsewhere: bool = True,
                    apply_v2: bool = True) -> Dict[str, Any]:
    ctx = PaperContext(paper_text)
    ledger = dl.build_ledger(schema)
    ctx.values = {e["id"]: (e.get("provenance") or {}).get("value") for e in ledger["entries"]
                  if e["status"] == dl.PROVIDED}
    types = owner_types(schema)
    rows = []
    for e in ledger["entries"]:
        if e["status"] != dl.PROVIDED or not e["required"] or e.get("analysis_only"):
            continue
        row = classify_entry(e, ctx, stated_elsewhere=stated_elsewhere)
        row["owner_type"] = types.get(e["owner"], "?")
        row["impact"] = impact(row["owner_type"], e["variable"])
        rows.append(row)
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["class"]] = counts.get(r["class"], 0) + 1
    out = {"entries": rows, "class_counts": counts, "ledger_summary": ledger["summary"]}
    note = (schema.get("aspen_schema") or {}).get("property_method_note")
    if isinstance(note, dict) and note.get("paper_method") and note.get("used_method") \
            and str(note["paper_method"]).strip().upper() != str(note["used_method"]).strip().upper():
        out["property_method_substitution"] = {k: note.get(k) for k in ("paper_method", "used_method", "reason")}
    import edc_policy_v2 as _v2            # P0 refinements; inert unless PART1_EDC_POLICY_V2 is set
    if apply_v2 and _v2.enabled():
        out = _v2.relabel(schema, out, paper_text)
    return out


def select_questions(classified: Dict[str, Any], k: int = 5) -> Dict[str, Any]:
    import edc_policy_v2 as _v2
    if _v2.enabled():
        return _v2.select_questions_v2(classified, k)
    cand = []
    for r in classified["entries"]:
        if r["class"] not in CANDIDATE_CLASSES:
            continue
        if r["impact"] == "HIGH" or (r["impact"] == "MED" and r["class"] in _MED_ASKED):
            cand.append(r)
    cand.sort(key=lambda r: (_CLASS_PRIORITY[r["class"]], 0 if r["impact"] == "HIGH" else 1, r["id"]))
    asked, overflow = cand[:k], cand[k:]
    # budget fill: when the question cap is not used up, also ask MED values the paper does not support at
    # all (ASSUMED).  They were left silent only to save questions; with budget left that saving is moot
    # (case B: feed and cooler pressures defaulted to 1.013 bar while the paper's unit runs at ~3 bar).
    if len(asked) < k:
        spare = sorted((r for r in classified["entries"]
                        if r["class"] == "ASSUMED" and r["impact"] == "MED" and r not in cand), key=lambda r: r["id"])
        asked += spare[:k - len(asked)]
    return {"asked": asked, "overflow": overflow}


_REASON = {
    "UNSUPPORTED_PAPER_CLAIM": "논문 근거로 표시됐지만 인용이 없거나 값을 뒷받침하지 않음",
    "SWEPT": "논문이 하나의 값이 아니라 범위/스윕으로 다룸",
    "BORROWED_SUSPECT": "논문의 숫자와 같지만 추정으로 표시됨 (다른 변수의 값을 옮겨 썼을 수 있음)",
    "ASSUMED": "논문에 근거가 없어 가정한 값",
    "ALTERNATIVE": "논문에 후보 값이 여럿 있고 그중 하나를 골랐음 (후보는 원문에서 확인됨)",
    "NAME_IMPLIED": "공급 스트림 이름으로 주성분은 짐작되지만 순도는 논문에 적혀 있지 않음",   # PART1_EDC_POLICY_V2
}


def format_question(selected: Dict[str, Any], will_apply: bool = False) -> str:
    """will_apply=True when the reply is really applied to the schema (PART1_EDC_POLICY on)."""
    asked = selected["asked"]
    if not asked:
        return ""
    lines = ["다음 값은 논문에서 하나로 정해져 있지 않거나 근거가 약합니다. 그대로 쓰려면 `ok`, "
             "바꾸려면 `ID=값` 형식으로 답해 주세요 (예: `B2.T_C=350`)."]
    if will_apply:
        lines.append("입력한 값은 스키마에 그대로 반영됩니다. 물리적으로 불가능하거나 논문이 다룬 범위를 벗어나면 "
                     "경고만 하고 입력한 값을 사용합니다.")
    for r in asked:
        q = f' | 인용: "{r["quote"][:90]}"' if r.get("quote") else ""
        alts = r.get("alternatives_verified")
        extra = f' | 후보: {", ".join(f"{a:g}" for a in alts)}' if r["class"] == "ALTERNATIVE" and alts else ""
        note = " | 조성은 `ok`만 가능(값 변경은 논문 확인 후 직접 수정)" if isinstance(r.get("value"), dict) else ""
        lines.append(f'- {r["id"]} = {r["value"]}  [{r["class"]}] {_REASON[r["class"]]}{extra}{q}{note}')
    if selected["overflow"]:
        lines.append(f'(상한으로 {len(selected["overflow"])}개는 묻지 않고 가정 장부에만 기록: '
                     + ", ".join(r["id"] for r in selected["overflow"]) + ")")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# EDC-2c: apply the user's reply to the schema (used when PART1_EDC_POLICY is on)
# --------------------------------------------------------------------------
_ID_PATTERN = r"[A-Za-z][A-Za-z0-9_]*\.[A-Za-z0-9_]+(?:\[[^\]\s;,]+\])?"
_ASSIGN_RE = re.compile(rf"({_ID_PATTERN})\s*=\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
_OK_WORDS = {"ok", "okay", "approve", "approved", "yes", "y", "네", "예", "응", "그대로", "승인", "좋아"}


def locate_node(schema: Dict[str, Any], owner: str, variable: str) -> Optional[Dict[str, Any]]:
    """Find where a ledger entry lives in the schema.
    -> {"path": str, "container": dict, "key": str} or None.  Handles plain specs
    (T_C, P_bar, flow, ...), MHeatX `T_out_C[<out stream>]` and SPLITTER `split_fraction[<stream>]`."""
    fs = schema.get("flowsheet_schema") or {}
    for i, b in enumerate(fs.get("blocks") or []):
        if str(b.get("id")) != owner:
            continue
        specs = b.get("specs") if isinstance(b.get("specs"), dict) else {}
        m = re.match(r"^T_out_C\[(.+)\]$", variable)
        if m:
            for j, t in enumerate(specs.get("temp_specs") or []):
                if isinstance(t, dict) and str(t.get("out_stream")) == m.group(1):
                    return {"path": f"flowsheet_schema.blocks[{i}].specs.temp_specs[{j}].T_out_C",
                            "container": t, "key": "T_out_C"}
            return None
        m = re.match(r"^split_fraction\[(.+)\]$", variable)
        if m:
            fr = specs.get("split_fractions")
            if isinstance(fr, dict) and m.group(1) in fr:
                return {"path": f"flowsheet_schema.blocks[{i}].specs.split_fractions.{m.group(1)}",
                        "container": fr, "key": m.group(1)}
            return None
        if variable in specs:
            return {"path": f"flowsheet_schema.blocks[{i}].specs.{variable}", "container": specs, "key": variable}
        return None
    for i, f in enumerate(((schema.get("io") or {}).get("feeds")) or []):
        if isinstance(f, dict) and str(f.get("stream")) == owner:
            specs = f.get("specs") if isinstance(f.get("specs"), dict) else {}
            if variable in specs:
                return {"path": f"io.feeds[{i}].specs.{variable}", "container": specs, "key": variable}
    return None


def policy_review_items(schema: Dict[str, Any], paper_text: str, k: int = 5) -> Dict[str, Any]:
    """Classify + select; items are shaped like the old selector's so the Part1 flow can reuse them."""
    classified = classify_schema(schema, paper_text)
    selected = select_questions(classified, k=k)
    items = []
    for r in selected["asked"]:
        loc = locate_node(schema, r["owner"], r["variable"])
        items.append({
            "kind": "edc_policy", "edc_id": r["id"], "field_path": (loc or {}).get("path") or r["id"],
            "source": r.get("source"), "confidence": r.get("confidence"), "value": r.get("value"),
            "class": r["class"], "impact": r["impact"], "reason": _REASON[r["class"]], "quote": r.get("quote"),
        })
    return {"classified": classified, "selected": selected, "items": items}


# Lenient forms of `OWNER.VAR=number` seen in real replies: `B2=T_C=350`, `B2. T_C=350`,
# `B2 T_C = 350`, `b2:t_c=350`.  Only the separator between owner and variable is relaxed.
_OWNER_RE = r"[A-Za-z][A-Za-z0-9_]*"
_VAR_RE = r"[A-Za-z][A-Za-z0-9_]*(?:\[[^\]\s;,]+\])?"
_NUM_RE = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_LENIENT_RE = re.compile(rf"(?<![\w.])({_OWNER_RE})\s*(?:\.|=|:|\s)\s*({_VAR_RE})\s*=\s*({_NUM_RE})")
_VARONLY_RE = re.compile(rf"(?<![\w.\[])({_VAR_RE})\s*=\s*({_NUM_RE})")


def _resolve_id(owner: str, var: str, known_ids: Optional[Iterable[str]]) -> Optional[str]:
    cand = f"{owner}.{var}"
    if not known_ids:
        return cand
    known = list(known_ids)
    if cand in known:
        return cand
    low = {k.lower(): k for k in known}
    return low.get(cand.lower())


def parse_review_answer(text: Optional[str], known_ids: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """`ok`, or one/more `ID=number` (separated by ; , or newlines).  Tolerates the usual typing
    variants of the ID (separator, letter case) and, when `known_ids` is given, a bare
    `T_C=350` if exactly one asked item has that variable.  Free text is never turned into a value;
    it is returned as `leftover`.  Each override carries `interpreted_as` when the text was
    normalised, so the user can be shown what was understood."""
    raw = (text or "").strip()
    known = list(known_ids) if known_ids else None
    overrides: List[Dict[str, Any]] = []
    spans: List[Tuple[int, int]] = []
    for m in _LENIENT_RE.finditer(raw):
        rid = _resolve_id(m.group(1), m.group(2), known)
        if rid is None:
            continue
        overrides.append({"id": rid, "value": float(m.group(3)), "text": m.group(0).strip(),
                          "interpreted_as": (None if re.sub(r"\s*=\s*", "=", m.group(0).strip()) == f"{rid}={m.group(3)}"
                                             else f"{rid}={m.group(3)}")})
        spans.append(m.span())
    if known:
        for m in _VARONLY_RE.finditer(raw):
            if any(a <= m.start() < b for a, b in spans):
                continue
            hits = [k for k in known if k.split(".", 1)[1].lower() == m.group(1).lower()]
            if len(hits) == 1:
                overrides.append({"id": hits[0], "value": float(m.group(2)), "text": m.group(0).strip(),
                                  "interpreted_as": f"{hits[0]}={m.group(2)}"})
                spans.append(m.span())
    leftover = raw
    for a, b in sorted(spans, reverse=True):
        leftover = leftover[:a] + " " + leftover[b:]
    leftover = re.sub(r"[;,\s]+", " ", leftover).strip()
    words = {w.lower() for w in re.findall(r"[A-Za-zㄱ-힝]+", leftover)}
    is_ok = (raw.lower() in _OK_WORDS) or bool(words & _OK_WORDS and not overrides)
    # `OWNER.VAR=number` the user typed that is not one of the asked items (e.g. an id from an earlier run)
    unknown = []
    if known:
        for m in _LENIENT_RE.finditer(raw):
            if _resolve_id(m.group(1), m.group(2), known) is None:
                typed = f"{m.group(1)}.{m.group(2)}"
                if typed not in unknown:
                    unknown.append(typed)
    return {"overrides": overrides, "ok": is_ok, "leftover": "" if is_ok and not overrides else leftover,
            "unknown_ids": unknown}


def _override_warnings(row: Dict[str, Any], new_value: float) -> List[str]:
    var = re.sub(r"\[.*\]$", "", row["variable"])
    w: List[str] = []
    if var.startswith("T_") and new_value < -273.15:
        w.append(f"{new_value} °C는 절대영도(-273.15 °C) 미만")
    if var in ("P_bar", "P_out_bar", "P_top_bar") and new_value <= 0:
        w.append(f"압력 {new_value} bar는 0 이하")
    if var == "flow" and new_value <= 0:
        w.append(f"유량 {new_value}는 0 이하")
    if var == "split_fraction" and not (0.0 <= new_value <= 1.0):
        w.append(f"분할 비율 {new_value}가 0~1 범위 밖")
    if var == "eff" and not (0.0 < new_value <= 1.0):
        w.append(f"효율 {new_value}가 (0, 1] 범위 밖")
    quote = row.get("quote")
    if quote:
        _, ranges, _ = parse_quote(quote)
        for lo, hi in ranges:
            if not (min(lo, hi) <= new_value <= max(lo, hi)):
                w.append(f"논문이 다룬 범위({lo:g}–{hi:g}) 밖")
                break
    return w


def apply_overrides(schema: Dict[str, Any], overrides: List[Dict[str, Any]],
                    classified: Dict[str, Any]) -> Dict[str, Any]:
    """Apply `ID=value` replies IN PLACE.  Policy: warn and accept (never reject a number).
    Unknown ids are ignored and reported.  -> {"applied": [...], "ignored": [...], "warnings": [...]}"""
    rows = {r["id"]: r for r in classified["entries"]}
    applied, ignored, warnings = [], [], []
    for ov in overrides:
        row = rows.get(ov["id"])
        loc = locate_node(schema, row["owner"], row["variable"]) if row else None
        if not row or not loc:
            ignored.append({"id": ov["id"], "reason": "알 수 없는 ID (이 스키마에 없는 값)"})
            continue
        cont, key = loc["container"], loc["key"]
        old = cont.get(key)
        if isinstance(old, dict) and "value" not in old:                 # composition etc.: not a number
            ignored.append({"id": ov["id"], "reason": "숫자 하나로 바꿀 수 없는 값(조성 등)"})
            continue
        warns = _override_warnings(row, ov["value"])
        if isinstance(old, dict) and "value" in old:
            old_value = old.get("value")
            old.update({"value": ov["value"], "source": "user", "confidence": 1.0,
                        "evidence": {"doc_id": "chat", "locator": "schema_review", "quote": ov["text"]},
                        "rationale": f"사용자 지정값 (이전 값 {old_value}, 출처 {old.get('source')})"})
        else:
            old_value = old
            cont[key] = ov["value"]
        applied.append({"id": ov["id"], "path": loc["path"], "old": old_value, "new": ov["value"],
                        "warnings": warns, "interpreted_as": ov.get("interpreted_as")})
        warnings.extend(f"{ov['id']}: {x}" for x in warns)
    return {"applied": applied, "ignored": ignored, "warnings": warnings}


def assumption_ledger(classified: Dict[str, Any], selected: Dict[str, Any],
                      review: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Everything that is not stated by the paper, whether or not the user was asked."""
    asked = {r["id"] for r in selected["asked"]}
    rows = []
    for r in classified["entries"]:
        if r["class"] in ("STATED", "DERIVED", "STATED_ELSEWHERE", "USER", "NO_PROVENANCE"):
            continue
        rows.append({"id": r["id"], "value": r.get("value"), "class": r["class"], "impact": r["impact"],
                     "source": r.get("source"), "quote": r.get("quote"), "asked": r["id"] in asked,
                     **({"alternatives_verified": r["alternatives_verified"]} if r.get("alternatives_verified") else {}),
                     **({"flags": r["flags"]} if r.get("flags") else {})})
    led = {"class_counts": classified["class_counts"], "not_stated": rows, "review": review or {}}
    if classified.get("property_method_substitution"):
        led["property_method_substitution"] = classified["property_method_substitution"]
    return led


# --------------------------------------------------------------------------
# user-facing summary at the end of a chat run (flag PART1_EDC_POLICY on)
# --------------------------------------------------------------------------
_SHORT_REASON = {
    "UNSUPPORTED_PAPER_CLAIM": "논문 근거로 표시됐지만 인용이 값을 뒷받침하지 않음",
    "ALTERNATIVE": "논문에 후보가 여럿이고 하나를 골랐음",
    "SWEPT": "논문은 범위로만 다룸",
    "BORROWED_SUSPECT": "다른 값의 숫자를 옮겨 왔을 수 있음",
    "ASSUMED": "논문에 근거가 없음",
    "NAME_IMPLIED": "스트림 이름에서 짐작한 조성",                  # PART1_EDC_POLICY_V2
    "RATIO_DERIVED": "논문의 비율에서 유도",                         # PART1_EDC_POLICY_V2
}


def _fmt_value(v: Any) -> str:
    if isinstance(v, dict):
        parts = [f"{k} {x:g}" for k, x in v.items() if isinstance(x, (int, float)) and not isinstance(x, bool) and x != 0]
        return "조성 {" + ", ".join(parts) + "}" if parts else "조성"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def format_assumption_summary(ledger: Dict[str, Any], max_items: int = 12) -> str:
    """Plain-language list of everything the paper did not settle, for the end of the chat.
    Reads the dict written by assumption_ledger().  Empty string when there is nothing to report."""
    review = ledger.get("review") or {}
    applied = {a["id"]: a for a in review.get("applied") or []}
    rows = ledger.get("not_stated") or []
    changed = [a for a in applied.values()]
    # `asked` only says the policy SELECTED the value for a question.  The question may never have been put
    # (e.g. the draft needed completion and no review ran): then nothing was confirmed by anyone.
    reviewed = "reply_was_ok" in review
    confirmed = [r for r in rows if reviewed and r.get("asked") and r["id"] not in applied]
    silent = [r for r in rows if not (reviewed and r.get("asked")) and r["id"] not in applied]
    sub = ledger.get("property_method_substitution")
    if not (changed or confirmed or silent or sub):
        return ""

    def line(r: Dict[str, Any]) -> str:
        return f"  - {r['id']} = {_fmt_value(r.get('value'))}  ({_SHORT_REASON.get(r['class'], r['class'])})"

    out = ["[가정 요약] 논문이 직접 정하지 않은 값입니다. 결과를 해석할 때 참고하세요."]
    if changed:
        out.append("직접 바꾸신 값:")
        out += [f"  - {a['id']}: {_fmt_value(a.get('old'))} → {_fmt_value(a.get('new'))}"
                + (f"  (경고: {'; '.join(a['warnings'])})" if a.get("warnings") else "") for a in changed]
    if confirmed:
        out.append("질문에서 확인하고 그대로 쓴 값:")
        out += [line(r) for r in confirmed[:max_items]]
    if silent:
        out.append("묻지 않고 가정한 값:")
        out += [line(r) for r in silent[:max_items]]
        if len(silent) > max_items:
            out.append(f"  … 외 {len(silent) - max_items}개 (전체는 edc_assumption_ledger.json)")
    if sub:
        out.append(f"물성 방법: 논문 {sub.get('paper_method')} → 실제 {sub.get('used_method')}"
                   + (f"  ({sub.get('reason')})" if sub.get("reason") else ""))
    return "\n".join(out)


def summary_from_file(path: Any) -> str:
    """Never raises: a missing/corrupt ledger just means no summary."""
    import json
    from pathlib import Path
    try:
        p = Path(path)
        if not p.exists():
            return ""
        return format_assumption_summary(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return ""
