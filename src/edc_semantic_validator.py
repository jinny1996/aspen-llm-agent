"""edc_semantic_validator.py -- P0-1: report-only semantic evidence validator (deterministic, no LLM).

Question it answers: does the text behind a schema value talk about THIS equipment and THIS kind of
quantity, in the units the schema uses?  dof_ledger / edc_policy only ask "is the quote in the paper"
and "does the quote contain the number".  A number that exists in the paper can still belong to another
unit (case B: the paper preheats the *feed* to 400 degC, the schema gave the *reactor* 400 degC).

It never changes the schema, never asks a question, and does not feed edc_policy's scoring.
Output: one verdict per claim, plus coverage (how many claims could actually be assessed).

Verdict labels (evidence_relation) -> reporting category
  DIRECT_SUPPORT                      SUPPORTED            owner equipment + quantity + value all found in the text
  PROCESS_LEVEL_SUPPORT               SUPPORTED_PROCESS_LEVEL  the value is stated as a process-wide chosen/constant condition
                                                           of the right quantity; no other equipment claims it
  WRONG_VARIABLE                      CLEAR_ERROR          paper-labelled quote has the number, but as another quantity
  NUMERIC_OR_UNIT_CONFLICT            CLEAR_ERROR          number only appears in another unit, or is absent from its own quote
  POTENTIAL_CROSS_EQUIPMENT_BORROWING EVIDENCE_GAP         the number is in the paper only as another equipment's condition
  INSUFFICIENT_EVIDENCE               EVIDENCE_GAP         text exists but does not decide (or an undeclared assumption)
  ASSUMPTION_DECLARED                 DECLARED_ASSUMPTION  value is labelled/declared as default or placeholder, no borrowing
                                                           signal.  NOT a statement that the value is right: in case B the declared
                                                           defaults (1.01325 bar) differ from the SI (3 / 2.95 / 2.85 bar).
  NOT_ASSESSED                        UNVERIFIABLE         nothing to check (no provenance, composition, user value, ...)

NOT implemented (reserved; always reported as unassessed, never silently "ok"):
  WRONG_EQUIPMENT (definite), WRONG_CASE, SOURCE_CONFLICT.  A deterministic lexicon cannot settle them.
"""
from __future__ import annotations

import bisect
import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import edc_claims
import edc_policy as ep

RELATION_CATEGORY = {
    "DIRECT_SUPPORT": "SUPPORTED",
    "WRONG_VARIABLE": "CLEAR_ERROR",
    "NUMERIC_OR_UNIT_CONFLICT": "CLEAR_ERROR",
    "POTENTIAL_CROSS_EQUIPMENT_BORROWING": "EVIDENCE_GAP",
    "INSUFFICIENT_EVIDENCE": "EVIDENCE_GAP",
    "ASSUMPTION_DECLARED": "DECLARED_ASSUMPTION",
    "NOT_ASSESSED": "UNVERIFIABLE",
    "PROCESS_LEVEL_SUPPORT": "SUPPORTED_PROCESS_LEVEL",
}
RESERVED_UNIMPLEMENTED = ("WRONG_EQUIPMENT", "WRONG_CASE", "SOURCE_CONFLICT")
# a verdict is "assessed" when the validator looked at real evidence and reached a position
ASSESSED = ("DIRECT_SUPPORT", "PROCESS_LEVEL_SUPPORT", "WRONG_VARIABLE", "NUMERIC_OR_UNIT_CONFLICT",
            "POTENTIAL_CROSS_EQUIPMENT_BORROWING", "ASSUMPTION_DECLARED")

_REAL_QUOTE_TIERS = ("EXACT", "FRAGMENTS_ALL", "RECONSTRUCTED")

# --------------------------------------------------------------------------
# text normalisation and number occurrences
# --------------------------------------------------------------------------
_RANGE_SPAN = ep._RANGE_RE


def clean_text(text: str) -> str:
    """ep._clean_math per line: an odd `$` early in the paper must not mis-pair every later formula."""
    return "\n".join(ep._clean_math(line) for line in (text or "").split("\n"))


_UNIT_AFTER = re.compile(
    r"^\s*(?:(?P<C>°\s*C|℃|deg\s*C)|(?P<K>K\b)|(?P<bar>bar[ag]?\b)|(?P<atm>atm\b)|(?P<MPa>MPa\b)"
    r"|(?P<kPa>kPa\b)|(?P<psi>psi\b)|(?P<Pa>Pa\b)"
    r"|(?P<flow>(?:kg|kmol|mol|t|ton|tons|Nm3|m3)\s*/\s*(?:h|hr|s|min)\b|(?:kg|kmol|mol|t|ton)\s*(?:h|hr|s)\s*[-−]\s*1\b))")
_UNIT_KIND = {"C": "temperature", "K": "temperature", "bar": "pressure", "atm": "pressure",
              "MPa": "pressure", "kPa": "pressure", "psi": "pressure", "Pa": "pressure", "flow": "flow"}
_KIND_WORDS = {
    "temperature": re.compile(r"\btemperature\b|\btemp\.", re.I),
    "pressure": re.compile(r"\bpressure\b", re.I),
    "flow": re.compile(r"\bflow\b|\bflow\s*rate\b|kg\s*/?\s*h|kmol|\bton\b|\bt\s*/\s*h\b", re.I),
    "duty": re.compile(r"\bduty\b|\bkW\b|\bMW\b", re.I),
}


def _is_negative(text: str, start: int) -> bool:
    """`- 25 degC` (MinerU spaces the sign) is -25; `773-1473` is a range, not a sign."""
    j = start
    while j > 0 and text[j - 1] == " ":
        j -= 1
    return j > 0 and text[j - 1] in "-−" and (j < 2 or not text[j - 2].isalnum())


def _unit_after(text: str, end: int) -> Optional[str]:
    m = _UNIT_AFTER.match(text[end:end + 14])
    return m.lastgroup if m else None


_HEADER_UNIT = re.compile(r"\((K|°\s*C|℃|C|bar|atm|MPa|kPa|psi|Pa)\)")


class _TableUnits:
    """Unit of a table cell from its column header: `Block<TAB>Component<TAB>Temperature (K)<TAB>Pressure (bar)`
    followed by rows whose numbers carry no unit of their own."""

    def __init__(self, text: str):
        self.lines = text.split("\n")
        self.offsets, off = [], 0
        for ln in self.lines:
            self.offsets.append(off)
            off += len(ln) + 1

    def unit_at(self, pos: int) -> Optional[str]:
        i = bisect.bisect_right(self.offsets, pos) - 1
        if i < 0 or "\t" not in self.lines[i]:
            return None
        col = self.lines[i][:pos - self.offsets[i]].count("\t")
        for k in range(i - 1, max(i - 25, -1), -1):
            ln = self.lines[k]
            if not ln.strip():
                continue
            if "\t" not in ln:
                return None
            cells = ln.split("\t")
            if _HEADER_UNIT.search(ln):
                if col < len(cells):
                    m = _HEADER_UNIT.search(cells[col])
                    if m:
                        u = re.sub(r"\s+", "", m.group(1))
                        return "C" if u in ("°C", "℃") else u
                return None
        return None


_HEADING = re.compile(r"^(?:\d+(?:\.\d+)*\.?\s+[A-Z]|Table\s+S?\d+\.|Fig\.\s*S?\d+\.)")


class _CaseMap:
    """Which case a line belongs to: a numbered heading ("2.3.2. Case 2: ...") or a table caption ("... C2B and C2H")
    that names cases opens a section about exactly those cases; the next heading closes it."""

    def __init__(self, text: str):
        self.lines = text.split("\n")
        self.offsets, off = [], 0
        for ln in self.lines:
            self.offsets.append(off)
            off += len(ln) + 1
        self.tags: List[Optional[set]] = []
        cur: Optional[set] = None
        for ln in self.lines:
            if _HEADING.match(ln.strip()) and len(ln) < 220:
                ids = edc_claims.case_ids(ln)
                cur = ids or None
            self.tags.append(cur)

    def tag_at(self, pos: int) -> Optional[set]:
        i = bisect.bisect_right(self.offsets, pos) - 1
        return self.tags[i] if i >= 0 else None


def _kinds_near(text: str, start: int, end: int, unit: Optional[str]) -> set:
    if unit in _UNIT_KIND:                       # a unit right behind the number is decisive
        return {_UNIT_KIND[unit]}
    window = text[max(0, start - 60):end + 40]
    return {k for k, rx in _KIND_WORDS.items() if rx.search(window)}


def _sentence_prefix(text: str, start: int, span: int = 160) -> str:
    before = text[max(0, start - span):start]
    cuts = [m.end() for m in re.finditer(r"[.!?;]\s+|\n", before)]
    return before[cuts[-1]:] if cuts else before


# --------------------------------------------------------------------------
# equipment lexicon (English).  Types are schema block types, plus FEED.
# --------------------------------------------------------------------------
_HT = frozenset({"HEATER", "MHEATX"})
_NOUNS: List[Tuple["re.Pattern", frozenset]] = [
    (re.compile(r"\b(?:reactor|reformer|gasifier|converter|gibbs)\w*", re.I), frozenset({"RGIBBS"})),
    (re.compile(r"\b(?:heat\s+exchanger|exchanger|heater|furnace|cooler|condenser|evaporator|vaporizer"
                r"|reboiler|boiler|preheater)\w*", re.I), _HT),
    (re.compile(r"\bpump\w*", re.I), frozenset({"PUMP"})),
    (re.compile(r"\bcompressor\w*", re.I), frozenset({"COMPRESSOR"})),
    (re.compile(r"\b(?:valve|throttl\w*)", re.I), frozenset({"VALVE"})),
    (re.compile(r"\b(?:flash\w*|separator|knock-?out|drum)\b", re.I), frozenset({"FLASH2"})),
    (re.compile(r"\bmixer\w*", re.I), frozenset({"MIXER"})),
    (re.compile(r"\bsplit\w*", re.I), frozenset({"SPLITTER"})),
    (re.compile(r"\b(?:feed(?:stock)?|raw\s+material)\b|\binput(?=\s*\()", re.I), frozenset({"FEED"})),
]
# verbs whose object is the temperature of a heat-transfer unit: "preheat the feed to 400"
_ACTIONS = re.compile(r"\bpreheat\w*|\bheated\s+to\b|\bheating\s+to\b|\bcooled\s+to\b|\bcooling\s+to\b"
                      r"|\bcondens\w+|\bvaporiz\w+|\bevaporat\w+", re.I)
_ACTION_REACH = 60
# an equipment word right after one of these is a LOCATION ("exiting the reactor"), not the subject
_LOCATION_BEFORE = re.compile(
    r"(?:before|prior\s+to|upstream\s+of|into|to|entering|enters|inlet\s+of|feeding|fed\s+to|from|exiting"
    r"|leaving|exits|after|downstream\s+of|out\s+of|through)\s+(?:the\s+|a\s+)?(?:[\w-]+\s+){0,1}$", re.I)


# "reactor exit stream", "entrance of all four reactors": a condition at the equipment's BOUNDARY (often
# a stream or a different configuration), not the equipment's own operating condition
_BOUNDARY_WORD = r"(?:entrance|exit|outlet|inlet|effluent|discharge|product)"
_BOUNDARY_AFTER = re.compile(rf"^\w*\s+{_BOUNDARY_WORD}", re.I)
_BOUNDARY_BEFORE = re.compile(rf"{_BOUNDARY_WORD}\s+of\s+(?:the\s+|all\s+|each\s+)?(?:[\w-]+\s+){{0,2}}$", re.I)


# "inlet temperature of 330", "set at ... exit temperature": the number is a condition at the equipment's
# boundary.  It supports the claim only when the claimed variable is that same boundary quantity.
_INLET_Q = re.compile(r"(?<![A-Za-z])(?:inlet|entrance)(?![A-Za-z])", re.I)
_OUTLET_Q = re.compile(r"(?<![A-Za-z])(?:outlet|exit)(?![A-Za-z])", re.I)


_CLAUSE_END = re.compile(r",|;|(?<![A-Za-z])(?:and|while|whereas|but|whereas)(?![A-Za-z])", re.I)


def _is_trivial(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) <= 10 and float(v).is_integer()


def _sentence_suffix(text: str, end: int, span: int = 60) -> str:
    after = text[end:end + span]
    m = re.search(r"[.!?;]\s|\n", after)
    return after[:m.start()] if m else after


def _boundary_mismatch(prefix: str, variable: str, owner_type: str, suffix: str = "") -> bool:
    tail = prefix[-60:] + " " + suffix        # "... reduced to 330 C at the entrance of all four reactors"
    inlet, outlet = bool(_INLET_Q.search(tail)), bool(_OUTLET_Q.search(tail))
    if inlet and owner_type != "FEED":
        return True                     # no block variable we model is an inlet quantity
    if outlet and "out" not in variable.lower():
        return True
    return False


def _attributed_types(prefix: str, owner_type: str = "", variable: str = "",
                      name_nouns: Optional[List[Tuple["re.Pattern", frozenset]]] = None,
                      pick: str = "last") -> frozenset:
    """Equipment types the number is being said about: nearest subject noun before it, plus any
    heating/cooling verb within reach (their object is a heater-type outlet).  `name_nouns` are the
    schema's own block / stream names ("@OWNER" for the claim's owner, "@OTHER" for any other)."""
    nouns: List[Tuple[int, frozenset]] = []
    for rx, types in _NOUNS + list(name_nouns or []):
        for m in rx.finditer(prefix):
            before = prefix[max(0, m.start() - 40):m.start()]
            if _LOCATION_BEFORE.search(before) or _BOUNDARY_BEFORE.search(before):
                continue
            ba = _BOUNDARY_AFTER.match(prefix[m.end():m.end() + 30])
            # "heater outlet temperature" is the heater's own outlet variable when that is what is claimed
            own_outlet = (ba is not None and (owner_type in types or "@OWNER" in types) and "out" in variable.lower()
                          and re.search(r"exit|outlet|effluent|discharge", ba.group(0), re.I))
            if ba and not own_outlet:
                continue
            nouns.append((m.start(), types))
    out = set()
    if nouns:
        out |= (max if pick == "last" else min)(nouns, key=lambda t: t[0])[1]     # "first": subject named after the number
    if pick == "last":
        for m in _ACTIONS.finditer(prefix):
            if len(prefix) - m.start() <= _ACTION_REACH:
                out |= _HT
    return frozenset(out)


def _owner_names(ctx: Dict[str, Any]) -> List[str]:
    names = []
    for k in ("name", "paper_id"):
        v = ctx.get(k)
        if isinstance(v, str) and len(v.strip()) >= 3 and not re.fullmatch(r"B\d+", v.strip()):
            names.append(v.strip())
    return names


# --------------------------------------------------------------------------
# occurrences of a claim's value in a text
# --------------------------------------------------------------------------
# a sentence that states a process-wide condition ("operating conditions of 923 K, 30 bar ... have been chosen")
_GLOBAL_CUE = re.compile(
    r"operating\s+conditions?|(?<![A-Za-z])chosen(?![A-Za-z])|(?<![A-Za-z])selected(?![A-Za-z])|throughout"
    r"|kept\s+constant|constant\s+at|fixed\s+at|base\s+case|reference\s+(?:pressure|temperature)", re.I)


def owner_reference_line(paper_clean: str, owner_id_rx: List["re.Pattern"], kind: str) -> Optional[str]:
    """A table row of the owner that defines its value by pointing at another unit: `E3 ... To the LTC Temperature`."""
    if not owner_id_rx:
        return None
    ref = re.compile(r"(?<![A-Za-z])to\s+the\s+(?:[\w-]+\s+){0,3}" + kind, re.I)
    for line in paper_clean.split("\n"):
        if "\t" in line and any(rx.search(line) for rx in owner_id_rx) and ref.search(line):
            return line
    return None


def _id_rx(letters: str, digits: str) -> "re.Pattern":
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(letters) + r"-?" + digits + r"(?![A-Za-z0-9])")


def _word_rx(word: str) -> "re.Pattern":
    return re.compile(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])", re.I)


def build_cues(claim: Dict[str, Any], ctx: Optional[Dict[str, Any]]):
    """-> (name_nouns, owner_id_patterns, case_filter) from the schema-derived context."""
    ctx = ctx or {}
    ent = ctx.get("entities") or {}
    own = ent.get(claim.get("owner")) or {"ids": [], "words": []}
    own_ids = {tuple(i) for i in own["ids"]}
    own_words = set(own["words"])
    nouns: List[Tuple["re.Pattern", frozenset]] = []
    owner_ids = [_id_rx(*i) for i in own["ids"]]
    nouns += [(rx, frozenset({"@OWNER"})) for rx in owner_ids]
    nouns += [(_word_rx(w), frozenset({"@OWNER"})) for w in own["words"]]
    for eid, t in ent.items():
        if eid == claim.get("owner"):
            continue
        nouns += [(_id_rx(*i), frozenset({"@OTHER"})) for i in t["ids"] if tuple(i) not in own_ids]
        nouns += [(_word_rx(w), frozenset({"@OTHER"})) for w in t["words"] if w not in own_words]
    sc = ctx.get("scope") or {}
    case = None
    if sc.get("excluded"):
        case = (re.compile("|".join(r"(?<![A-Za-z])" + re.escape(x) + r"(?![A-Za-z])" for x in sc["excluded"])),
                [re.compile(r"(?<![A-Za-z-])" + re.escape(x) + r"(?![A-Za-z-])") for x in sc.get("included") or []])
    return nouns, owner_ids, case


def _other_case_id(case_map: "_CaseMap", text: str, pos: int, sentence: str, target: set) -> bool:
    """Multi-case paper: the sentence names only other cases, or sits in a section about other cases."""
    named = edc_claims.case_ids(sentence)
    if named:
        return not (named & target)
    tag = case_map.tag_at(pos)
    return bool(tag) and not (tag & target)


def _other_case(sentence: str, case) -> bool:
    """The sentence is about a process the schema's scope excludes (and does not also name the included one)."""
    if not case:
        return False
    exc_rx, inc_rx = case
    if not exc_rx.search(sentence):
        return False
    rest = exc_rx.sub(" ", sentence)
    return not any(r.search(rest) for r in inc_rx)


def find_occurrences(text: str, claim: Dict[str, Any], cleaned: bool = False,
                     ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """-> {"occurrences": [...], "raw_matches": n}.  Each occurrence:
    {start, snippet, unit, kinds, kind_ok, unit_conflict, in_range, attributed, verdict}
    verdict: owner | other | neutral | range."""
    value = claim.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return {"occurrences": [], "raw_matches": 0}
    c = text if cleaned else clean_text(text)
    kind, owner_type = claim["kind"], claim["owner_type"]
    names = _owner_names(claim.get("owner_context") or {})
    name_nouns, owner_id_rx, case = build_cues(claim, ctx)
    range_spans = [m.span() for m in _RANGE_SPAN.finditer(c)]
    occs: List[Dict[str, Any]] = []
    raw = 0
    tables = _TableUnits(c)
    target = (ctx or {}).get("target_cases") or set()
    case_map = _CaseMap(c) if target else None
    for m in re.finditer(ep._NUM, c):
        n = float(m.group(0))
        if _is_negative(c, m.start()):
            n = -n
        sup = ep.value_support(value, [n], [], strict=True)
        if sup is None:
            continue
        unit = _unit_after(c, m.end()) or tables.unit_at(m.start())
        if sup == "conversion" and not ((kind == "temperature" and unit == "K")
                                        or (kind == "pressure" and unit == "atm")):
            continue                                    # %->fraction etc. is coincidence for T/P
        raw += 1
        kinds = _kinds_near(c, m.start(), m.end(), unit)
        kind_ok = (not kinds) or (kind not in _UNIT_KIND.values() and kind != "flow") or kind in kinds
        unit_conflict = (sup == "identity" and
                         ((kind == "temperature" and unit == "K")
                          or (kind == "pressure" and unit in ("MPa", "kPa", "psi", "Pa", "atm"))))
        in_range = any(a <= m.start() < b for a, b in range_spans)
        attributed = _attributed_types(_sentence_prefix(c, m.start()), owner_type, claim["variable"], name_nouns)
        if not attributed:      # "2.497 kW is the heat power supplied ... in an evaporator": the subject comes after the number
            same_clause = _CLAUSE_END.split(_sentence_suffix(c, m.end(), 100), maxsplit=1)[0]   # not the next clause's subject
            attributed = _attributed_types(same_clause, owner_type, claim["variable"], name_nouns, pick="first")
        boundary = False
        ls = c.rfind("\n", 0, m.start()) + 1                 # same line only: a table row must not see its neighbours
        le = c.find("\n", m.end())
        around = c[max(ls, m.start() - 100):min(le if le >= 0 else len(c), m.end() + 60)]
        owner_hit = (any(re.search(rf"(?<![A-Za-z0-9]){re.escape(nm)}(?![A-Za-z0-9])", around, re.I) for nm in names)
                     or any(rx.search(around) for rx in owner_id_rx))
        owner_hit = owner_hit or "@OWNER" in attributed
        prefix_here = _sentence_prefix(c, m.start())
        sentence = prefix_here + c[m.start():m.end()] + _sentence_suffix(c, m.end())
        if in_range:
            verdict = "range"
        elif _other_case(sentence, case) or (target and _other_case_id(case_map, c, m.start(), sentence, target)):
            verdict = "other_case"
        elif (owner_hit or owner_type in attributed) and _boundary_mismatch(prefix_here, claim["variable"], owner_type, _sentence_suffix(c, m.end())):
            verdict = "neutral"
            boundary = True
        elif owner_hit or owner_type in attributed:
            verdict = "owner"
        elif attributed - {"@OWNER"}:
            verdict = "other"
        else:
            verdict = "neutral"
        suffix_here = _sentence_suffix(c, m.end())
        boundary_any = _boundary_mismatch(prefix_here, claim["variable"], owner_type, suffix_here)
        occs.append({"start": m.start(), "snippet": re.sub(r"\s+", " ", c[max(0, m.start() - 90):m.end() + 40]).strip(),
                     "unit": unit, "kinds": sorted(kinds), "kind_ok": kind_ok, "unit_conflict": unit_conflict,
                     "in_range": in_range, "attributed": sorted(attributed), "verdict": verdict,
                     "boundary_condition": boundary, "boundary_any": boundary_any,
                     "explicit_kind": bool(kinds), "explicit_unit": unit is not None, "global_cue": bool(_GLOBAL_CUE.search(sentence))})
    return {"occurrences": occs, "raw_matches": raw}


# --------------------------------------------------------------------------
# rationale analysis
# --------------------------------------------------------------------------
_DECLARES_ASSUMPTION = re.compile(
    r"placeholder|assum\w+|does\s+not\s+(?:explicitly\s+)?(?:give|specify|state|include|provide)"
    r"|not\s+(?:explicitly\s+)?(?:specified|stated|provided|given|reported)|\bdefault\b|conservative"
    r"|typical|by\s+analogy|for\s+(?:an\s+)?executable", re.I)


def _rationale_view(claim: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    r = claim.get("rationale") or ""
    occ = find_occurrences(r, claim, ctx=ctx)["occurrences"] if r else []
    return {"declares_assumption": bool(_DECLARES_ASSUMPTION.search(r)),
            "cites_other_equipment": any(o["verdict"] == "other" and o["kind_ok"] for o in occ),
            "cites_owner_equipment": any(o["verdict"] == "owner" and o["kind_ok"] for o in occ)}


# --------------------------------------------------------------------------
# one claim
# --------------------------------------------------------------------------
def _verdict(rel: str, reasons: List[str], flags: Optional[List[str]] = None,
             occs: Optional[List[Dict[str, Any]]] = None, resolution: str = "UNRESOLVED") -> Dict[str, Any]:
    return {"evidence_relation": rel, "category": RELATION_CATEGORY[rel], "resolution_state": resolution,
            "reasons": reasons, "flags": flags or [],
            "occurrences": [{k: o[k] for k in ("snippet", "unit", "kinds", "verdict", "where")} for o in (occs or [])[:3]]}


def evaluate_claim(claim: Dict[str, Any], paper_clean: str, ep_row: Optional[Dict[str, Any]],
                   ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    src = claim.get("source")
    val = claim.get("value")
    ep_class = (ep_row or {}).get("class")
    if not claim["tracked"]:
        return _verdict("NOT_ASSESSED", ["MISSING_FIELD_PROVENANCE"])
    if src == "user":
        return _verdict("NOT_ASSESSED", ["USER_SUPPLIED_VALUE"], resolution="HUMAN_REVISED")
    if isinstance(val, dict) or isinstance(val, bool) or not isinstance(val, (int, float)):
        return _verdict("NOT_ASSESSED", ["NON_SCALAR_VALUE_NOT_SUPPORTED_IN_P0_1"])
    if claim["kind"] not in ("temperature", "pressure", "flow", "duty"):
        return _verdict("NOT_ASSESSED", [f"VARIABLE_KIND_{claim['kind'].upper()}_NOT_SUPPORTED_IN_P0_1"])

    flags: List[str] = []
    quote = claim.get("quote")
    quote_real = bool(quote) and ((ep_row or {}).get("quote_tier") in _REAL_QUOTE_TIERS
                                  or "table_merge_quote" in (ep_row or {}).get("flags", []))
    q_occ = find_occurrences(quote, claim, ctx=ctx)["occurrences"] if quote_real else []
    for o in q_occ:
        o["where"] = "quote"
    p_res = find_occurrences(paper_clean, claim, cleaned=True, ctx=ctx)
    p_occ = [o for o in p_res["occurrences"]]
    for o in p_occ:
        o["where"] = "paper"
    if _is_trivial(val):                       # small integers (1, 3, ...) match everything: demand a unit next to the number
        p_occ = [o for o in p_occ if o["explicit_unit"]]
        q_occ = [o for o in q_occ if o["explicit_unit"]]
    elif claim["kind"] in ("temperature", "pressure"):     # "Feed stream stage 25" is not a feed temperature: a bare number needs a unit or a T / P word
        p_occ = [o for o in p_occ if o["explicit_unit"] or o["explicit_kind"]]
        q_occ = [o for o in q_occ if o["explicit_unit"] or o["explicit_kind"]]
    rat =_rationale_view(claim, ctx)

    # (1) clear errors, only from the claim's OWN quote of a paper-labelled value
    if src == "paper" and q_occ:
        if not any(o["kind_ok"] for o in q_occ):
            return _verdict("WRONG_VARIABLE", [f"quote has the number only as {sorted({k for o in q_occ for k in o['kinds']})}, "
                                               f"claim is {claim['kind']}"], flags, q_occ)
        ok_q = [o for o in q_occ if o["kind_ok"]]
        if all(o["unit_conflict"] for o in ok_q):
            return _verdict("NUMERIC_OR_UNIT_CONFLICT", [f"quote gives the number in {sorted({o['unit'] for o in ok_q})}, "
                                                         f"schema variable is {claim['variable']}"], flags, ok_q)
    if src == "paper" and ep_class == "UNSUPPORTED_PAPER_CLAIM" and "value_not_in_quote" in (ep_row or {}).get("flags", []):
        return _verdict("NUMERIC_OR_UNIT_CONFLICT", ["paper-labelled value is absent from its own quote and the paper "
                                                     "does not state it elsewhere (edc_policy)"], flags)

    # (2) positive / negative position from where the number sits in the text
    usable = [o for o in q_occ + p_occ if o["kind_ok"] and not o["unit_conflict"]
              and o["verdict"] not in ("range", "other_case")]
    unit_bad = [o for o in q_occ + p_occ if o["kind_ok"] and o["unit_conflict"]]
    if any(o["verdict"] == "owner" for o in usable):
        if ep_class in ("ALTERNATIVE", "SWEPT"):
            flags.append("owner_context_but_multiple_values")
            return _verdict("INSUFFICIENT_EVIDENCE", [f"text names this equipment but edc_policy sees {ep_class}: "
                                                      "the paper gives several values / a range"], flags,
                            [o for o in usable if o["verdict"] == "owner"])
        if src != "paper":
            flags.append("source_label_understated")
        if not quote_real:
            flags.append("no_verifiable_quote")
        return _verdict("DIRECT_SUPPORT", ["text attributes the value to this equipment type / name"], flags,
                        [o for o in usable if o["verdict"] == "owner"])
    others_only = bool(usable) and all(o["verdict"] == "other" for o in usable)
    if others_only and "out" in claim["variable"].lower() and claim["owner_type"] in ("HEATER", "MHEATX")             and owner_reference_line(paper_clean, build_cues(claim, ctx)[1], claim["kind"]):
        # "E3 ... To the LTC Temperature": the owner's own table row defines its outlet as another unit's value
        if ep_class in ("ALTERNATIVE", "SWEPT"):
            flags.append("owner_context_but_multiple_values")
            return _verdict("INSUFFICIENT_EVIDENCE", [f"owner row points at another unit, but edc_policy sees {ep_class}"],
                            flags, usable)
        flags.append("by_reference_in_owner_row")
        return _verdict("DIRECT_SUPPORT", ["the owner's table row defines this value as another unit's temperature, "
                                           "and that unit has this value"], flags, usable)
    glob = [o for o in usable if o["verdict"] == "neutral" and o["explicit_kind"] and o["global_cue"]
            and not o["boundary_any"]]
    if glob and claim["kind"] in ("temperature", "pressure"):
        # other equipment stating the same number does not contradict a process-wide statement
        if ep_class in ("ALTERNATIVE", "SWEPT"):
            flags.append("process_level_but_multiple_values")
            return _verdict("INSUFFICIENT_EVIDENCE", [f"process-wide statement found, but edc_policy sees {ep_class}"],
                            flags, glob)
        return _verdict("PROCESS_LEVEL_SUPPORT", ["the value is stated as a process-wide chosen / constant "
                                                  f"{claim['kind']}; the text does not tie it to a specific equipment"],
                        flags, glob)
    if others_only:
        if rat["declares_assumption"]:
            flags.append("claim_declares_placeholder")
        return _verdict("POTENTIAL_CROSS_EQUIPMENT_BORROWING",
                        ["the number occurs in the text only as a condition of another equipment type: "
                         + ", ".join(sorted({t for o in usable for t in o["attributed"]}))], flags, usable)
    if usable:
        return _verdict("INSUFFICIENT_EVIDENCE", ["number found but the text does not say which equipment it belongs to"],
                        flags, usable)

    # (3) the paper does not carry the number; fall back on what the schema itself says
    if rat["cites_other_equipment"]:
        flags.append("rationale_only")
        return _verdict("POTENTIAL_CROSS_EQUIPMENT_BORROWING",
                        ["the claim's own rationale ties the number to another equipment type (paper text not matched)"], flags)
    if unit_bad:
        flags.append("unit_conflict_only_in_paper_text")
    if ep_class == "SWEPT" or any(o["verdict"] == "range" for o in q_occ + p_occ):
        return _verdict("INSUFFICIENT_EVIDENCE", ["the paper gives a range / sweep, not a single value"], flags)
    if src == "default" or rat["declares_assumption"]:
        return _verdict("ASSUMPTION_DECLARED", ["value is labelled default / rationale declares an assumption; "
                                                "no sign that it was borrowed from another equipment"], flags)
    if src == "paper":
        return _verdict("INSUFFICIENT_EVIDENCE", ["paper-labelled value, but no supporting text was found"], flags)
    return _verdict("INSUFFICIENT_EVIDENCE", ["undeclared assumption: not stated in the paper and not labelled as one"], flags)


# --------------------------------------------------------------------------
# schema level
# --------------------------------------------------------------------------
def validate_schema(schema: Dict[str, Any], paper_text: str) -> Dict[str, Any]:
    """Report-only.  Does not modify `schema`."""
    claims = edc_claims.extract_claims(schema)
    classified = ep.classify_schema(schema, paper_text, apply_v2=False)     # the validator judges the base classes
    rows = {r["id"]: r for r in classified["entries"]}
    paper_clean = clean_text(paper_text)
    ctx = {"entities": edc_claims.entity_tokens(schema), "scope": edc_claims.process_scope(schema),
           "target_cases": edc_claims.target_cases(schema)}
    out: List[Dict[str, Any]] = []
    for c in claims:
        row = rows.get(c["id"])
        v = evaluate_claim(c, paper_clean, row, ctx)
        out.append({"id": c["id"], "owner": c["owner"], "owner_type": c["owner_type"], "variable": c["variable"],
                    "value": c["value"], "source": c["source"], "edc_policy_class": (row or {}).get("class"), **v})
    return {"claims": out, "summary": summarize(out)}


def summarize(verdicts: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_rel: Dict[str, int] = {}
    by_cat: Dict[str, int] = {}
    for v in verdicts:
        by_rel[v["evidence_relation"]] = by_rel.get(v["evidence_relation"], 0) + 1
        by_cat[v["category"]] = by_cat.get(v["category"], 0) + 1
    n = len(verdicts)
    assessed = sum(1 for v in verdicts if v["evidence_relation"] in ASSESSED)
    return {"total_claims": n, "by_relation": by_rel, "by_category": by_cat,
            "assessed": assessed, "not_assessed": n - assessed,
            "coverage": round(assessed / n, 3) if n else None,
            "note": "A claim without a flag is NOT 'verified'. Only DIRECT_SUPPORT is positive evidence; "
                    "INSUFFICIENT_EVIDENCE / NOT_ASSESSED mean the validator could not decide.",
            "reserved_unimplemented": list(RESERVED_UNIMPLEMENTED)}


def schema_fingerprint(schema: Dict[str, Any]) -> str:
    return json.dumps(schema, sort_keys=True, default=str)


def validate_copy(schema: Dict[str, Any], paper_text: str) -> Dict[str, Any]:
    """Defensive wrapper for callers that must be sure the input stays untouched."""
    return validate_schema(copy.deepcopy(schema), paper_text)
