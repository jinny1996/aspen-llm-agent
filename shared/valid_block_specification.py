# validator/block_specs/check.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from valid_common import make_issue, wrap_result


# ============================================================
# Aspen helpers (env-fixed)
# ============================================================
def get_tree(doc):
    return doc.Application.Tree


def _find(doc, path: str):
    try:
        return get_tree(doc).FindNode(path)
    except Exception:
        return None


def _get(doc, path: str):
    n = _find(doc, path)
    if n is None:
        return None
    try:
        return n.Value
    except Exception:
        return None


def _is_blank(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _iter_elements(elems) -> List[Any]:
    """
    Robust COM Elements iterator:
      1) Count/Item (1-index) but filters None
      2) try 0-index via __getitem__
      3) try 1-index via __getitem__
      4) 마지막: iterator protocol 시도
    """
    if elems is None:
        return []

    # 1) Count/Item (1-index) — BUT filter None, and if too many None, fallback
    try:
        n = int(elems.Count)
        items = []
        none_cnt = 0
        for i in range(1, n + 1):
            it = elems.Item(i)
            if it is None:
                none_cnt += 1
                continue
            items.append(it)

        # Count 대비 None이 섞여 있거나 items가 비정상적으로 적으면 fallback 시도
        if items and none_cnt == 0:
            return items
        # items가 아예 없거나 None이 섞이면 아래 방식도 시도
    except Exception:
        pass

    # 2) __getitem__ 0-index
    items = []
    try:
        i = 0
        while True:
            it = elems[i]
            if it is not None:
                items.append(it)
            i += 1
    except Exception:
        if items:
            return items

    # 3) __getitem__ 1-index
    items = []
    try:
        i = 1
        while True:
            it = elems[i]
            if it is not None:
                items.append(it)
            i += 1
    except Exception:
        if items:
            return items

    # 4) iterator protocol (for child in elems)
    try:
        items = [it for it in elems if it is not None]
        return items
    except Exception:
        return []


# # 이거 나중에 삭제 (at 260226)
# def list_blocks(doc) -> List[str]:
#     node = _find(doc, r"\Data\Blocks")
#     if node is None:
#         raise RuntimeError(r"\Data\Blocks 노드를 못 찾았습니다.")

#     elems = getattr(node, "Elements", None)
#     items = _iter_elements(elems)
#     names = [getattr(it, "Name", None) for it in items if getattr(it, "Name", None)]
#     if not names:
#         raise RuntimeError(r"\Data\Blocks에서 블록을 열거하지 못했습니다.")
#     return names

def list_blocks(doc) -> List[str]:
    """
    Robust block listing:
      - union(\Data\Blocks children, \Data\Flowsheet...\BLKTYPE keys)
      - keep only nodes that truly exist under \Data\Blocks\{b}
    """
    blocks = set()

    # 1) from \Data\Blocks (may drop first element in some Aspen COM cases)
    n1 = None
    try:
        n1 = doc.Tree.FindNode(r"\Data\Blocks")
    except Exception:
        n1 = None

    if n1 is not None:
        try:
            for ch in n1.Elements:
                if ch is None:
                    continue
                name = getattr(ch, "Name", None) or getattr(ch, "NodeName", None)
                if name:
                    blocks.add(str(name).upper().strip())
        except Exception:
            pass

    # 2) from BLKTYPE table (independent key source)
    n2 = None
    try:
        n2 = doc.Tree.FindNode(r"\Data\Flowsheet\Section\GLOBAL\Input\BLKTYPE")
    except Exception:
        n2 = None

    if n2 is not None:
        try:
            for ch in n2.Elements:
                if ch is None:
                    continue
                name = getattr(ch, "Name", None) or getattr(ch, "NodeName", None)
                if name:
                    blocks.add(str(name).upper().strip())
        except Exception:
            pass

    # 3) keep only real blocks existing under \Data\Blocks\{b}
    real = []
    for b in blocks:
        try:
            if doc.Tree.FindNode(rf"\Data\Blocks\{b}") is not None:
                real.append(b)
        except Exception:
            continue

    return sorted(set(real))


# 이거 나중에 삭제 (at 260226)
# def get_block_type(doc, block: str) -> Optional[str]:
#     p = rf"\Data\Flowsheet\Section\GLOBAL\Input\BLKTYPE\{block}"
#     v = _get(doc, p)
#     if _is_blank(v):
#         return None
#     return str(v).upper().strip()


def get_block_type(doc, block: str) -> Optional[str]:
    cands = [
        rf"\Data\Flowsheet\Section\GLOBAL\Input\BLKTYPE\{block}",
        rf"\Data\Blocks\{block}\Input\BLKTYPE",
        rf"\Data\Blocks\{block}\Input\BLOCKTYPE",
        rf"\Data\Blocks\{block}\Input\TYPE",
    ]
    for p in cands:
        v = _get(doc, p)
        if not _is_blank(v):
            return str(v).upper().strip()
    return None

# ============================================================
# Rule resolving (NO YAML loading here)
# - pipeline에서 rules_all(dict) 만들어서 run()에 넣으세요.
# ============================================================
def _resolve_rule(rules_all: Dict[str, Any], btype: str) -> Optional[Dict[str, Any]]:
    """
    Expected rules_all schema (example):
      {
        "defaults": {...},
        "blocks": {
          "FLASH2": { ... },
          "HEATER": { "inherits": "FLASH2", ... },
          ...
        }
      }
    """
    blocks = (rules_all or {}).get("blocks", {}) or {}
    if btype not in blocks:
        return None

    rule = dict(blocks[btype] or {})

    inherit = rule.get("inherits")
    if inherit:
        base = blocks.get(inherit)
        if base:
            merged = dict(base or {})
            merged.update(rule)
            rule = merged

    return rule


# ============================================================
# Generic rule engine
# ============================================================
def _apply_base(template: str, base: str) -> str:
    return template.replace("{base}", base)


def _read_fields(doc, base: str, fields: Dict[str, str]) -> Dict[str, Any]:
    vals: Dict[str, Any] = {}
    for k, templ in (fields or {}).items():
        try:
            vals[k] = _get(doc, _apply_base(templ, base))
        except Exception:
            vals[k] = None
    return vals


def _field_path(rule: Dict[str, Any], base: str, field: str) -> Optional[str]:
    """fields dict에 있으면 path 리턴, 없으면 None"""
    fields = rule.get("fields", {}) or {}
    templ = fields.get(field)
    if not templ:
        return None
    return _apply_base(templ, base)


def validate_generic(
    doc,
    block: str,
    btype: str,
    rules_all: Dict[str, Any],
    rule: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    defaults = (rules_all or {}).get("defaults", {}) or {}

    input_base = rule.get("input_base") or defaults.get("input_base") or r"\Data\Blocks\{B}\Input"
    base = input_base.replace("{B}", block)

    vals = _read_fields(doc, base, rule.get("fields", {}) or {})
    status = {"block": block, "type": btype, "values": vals}

    missing: List[Dict[str, Any]] = []

    for r in (rule.get("rules", []) or []):
        rid = r.get("id", "rule")
        rtype = (r.get("type") or "").lower()

        # --- enum_required ---
        if rtype == "enum_required":
            key = r.get("field")
            if not key:
                missing.append({"rule_id": "bad_rule", "current": r, "required": "field"})
                continue

            v = vals.get(key)
            allowed_raw = r.get("allowed") or []
            allowed = [str(x).upper() for x in allowed_raw]
            vu = str(v).upper().strip() if not _is_blank(v) else ""

            if _is_blank(v) or vu not in set(allowed):
                missing.append({
                    "rule_id": rid,
                    "field": key,
                    "path": _field_path(rule, base, key),
                    "current": v,
                    "required": {"allowed": allowed},
                    "detail": {"type": "enum_required"},
                })
            continue

        # --- required (all fields must be not blank) ---
        if rtype == "required":
            keys = r.get("fields") or []
            for key in keys:
                v = vals.get(key)
                if _is_blank(v):
                    missing.append({
                        "rule_id": rid,
                        "field": key,
                        "path": _field_path(rule, base, key),
                        "current": v,
                        "required": "not blank",
                        "detail": {"type": "required"},
                    })
            continue

        # --- require_when ---
        if rtype == "require_when":
            cond = r.get("when") or {}
            cfield = cond.get("field")
            ceq = str(cond.get("equals", "")).upper()
            if not cfield:
                missing.append({"rule_id": "bad_rule", "current": r, "required": "when.field"})
                continue

            cv = vals.get(cfield)
            cvu = str(cv).upper().strip() if not _is_blank(cv) else ""

            if cvu == ceq:
                req_keys = r.get("require") or []
                for key in req_keys:
                    v = vals.get(key)
                    if _is_blank(v):
                        missing.append({
                            "rule_id": rid,
                            "field": key,
                            "path": _field_path(rule, base, key),
                            "current": v,
                            "required": f"required when {cfield} == {ceq}",
                            "detail": {"type": "require_when", "when": {cfield: ceq}},
                        })
            continue

        # --- unknown ---
        missing.append({
            "rule_id": "unknown_rule_type",
            "current": rtype,
            "required": "supported: enum_required|required|require_when",
            "detail": {"rule": r},
        })

    return status, missing


# ============================================================
# Plugins (MHEATX / RPLUG) - 규칙 엔진으로 표현하기 어려운 케이스
# ============================================================
def _list_child_names(node) -> List[str]:
    if node is None:
        return []
    try:
        cnt = node.Elements.Count
    except Exception:
        return []
    names: List[str] = []
    for i in range(1, cnt + 1):
        ch = node.Elements.Item(i)
        nm = getattr(ch, "Name", None)
        if nm:
            names.append(nm)
    return names


def validate_mheatx_plugin(doc, block: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    base = rf"\Data\Blocks\{block}\Input"
    out_root  = rf"{base}\OUT"
    spec_root = rf"{base}\SPEC"
    val_root  = rf"{base}\VALUE"

    stream_names = _list_child_names(_find(doc, r"\Data\Streams"))
    in_streams = [s for s in stream_names if _find(doc, rf"{out_root}\{s}") is not None]
    n = len(in_streams)
    req = max(n - 1, 0)

    status = {
        "block": block,
        "type": "MHEATX",
        "in_streams": in_streams,
        "n_streams": n,
        "required_specs": req,
        "OUT_pairs": {},
        "n_out_filled": 0,
        "n_spec_filled": 0,
        "n_value_filled": 0,
    }
    missing: List[Dict[str, Any]] = []

    if n == 0:
        missing.append({"rule_id": "no_inlet_streams_detected", "path": out_root, "current": 0, "required": ">=1"})
        return status, missing

    out_filled = 0
    for s in in_streams:
        p = rf"{out_root}\{s}"
        v = _get(doc, p)
        status["OUT_pairs"][s] = v
        if _is_blank(v):
            missing.append({
                "rule_id": "outlet_stream_missing",
                "path": p,
                "current": v,
                "required": "out stream id",
                "detail": {"in_stream": s},
            })
        else:
            out_filled += 1
    status["n_out_filled"] = out_filled

    spec_filled = 0
    value_filled = 0
    for key in stream_names:
        ps = rf"{spec_root}\{key}"
        if _find(doc, ps) is not None and not _is_blank(_get(doc, ps)):
            spec_filled += 1
        pv = rf"{val_root}\{key}"
        if _find(doc, pv) is not None and not _is_blank(_get(doc, pv)):
            value_filled += 1

    status["n_spec_filled"] = spec_filled
    status["n_value_filled"] = value_filled

    if spec_filled < req:
        missing.append({"rule_id": "insufficient_specs", "path": spec_root, "current": spec_filled, "required": req})
    if value_filled < req:
        missing.append({"rule_id": "insufficient_values", "path": val_root, "current": value_filled, "required": req})
    if spec_filled != value_filled:
        missing.append({
            "rule_id": "spec_value_count_mismatch",
            "path": f"{spec_root} / {val_root}",
            "current": {"spec": spec_filled, "value": value_filled},
            "required": "spec == value",
        })

    return status, missing


def validate_rplug_plugin(doc, block: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    base = rf"\Data\Blocks\{block}\Input"

    p_type   = rf"{base}\TYPE"
    p_len    = rf"{base}\LENGTH"
    p_diam   = rf"{base}\DIAM"

    p_opt_t  = rf"{base}\OPT_TSPEC"
    p_tconst = rf"{base}\REAC_TEMP"

    p_cat    = rf"{base}\CAT_PRESENT"
    p_catwt  = rf"{base}\CATWT"
    p_catrho = rf"{base}\CAT_RHO"
    p_void   = rf"{base}\BED_VOIDAGE"

    status = {
        "block": block,
        "type": "RPLUG",
        "TYPE": _get(doc, p_type),
        "LENGTH": _get(doc, p_len),
        "DIAM": _get(doc, p_diam),
        "OPT_TSPEC": _get(doc, p_opt_t),
        "REAC_TEMP": _get(doc, p_tconst),
        "CAT_PRESENT": _get(doc, p_cat),
        "CATWT": _get(doc, p_catwt),
        "CAT_RHO": _get(doc, p_catrho),
        "BED_VOIDAGE": _get(doc, p_void),
    }
    missing: List[Dict[str, Any]] = []

    if _is_blank(status["TYPE"]):
        missing.append({"rule_id": "type_required", "path": p_type, "current": status["TYPE"], "required": "TYPE"})
        return status, missing

    type_u = str(status["TYPE"]).upper().strip()

    if _is_blank(status["LENGTH"]):
        missing.append({"rule_id": "length_required", "path": p_len, "current": status["LENGTH"], "required": "LENGTH"})
    if _is_blank(status["DIAM"]):
        missing.append({"rule_id": "diam_required", "path": p_diam, "current": status["DIAM"], "required": "DIAM"})

    if type_u == "T-SPEC":
        if not _is_blank(status["OPT_TSPEC"]):
            opt_u = str(status["OPT_TSPEC"]).upper().strip()
            allowed = {"INLET-TEMP", "CONST-TEMP", "TEMP-PROF"}
            if opt_u not in allowed:
                missing.append({"rule_id": "opt_tspec_invalid", "path": p_opt_t, "current": status["OPT_TSPEC"], "required": sorted(list(allowed))})
            else:
                if opt_u == "CONST-TEMP" and _is_blank(status["REAC_TEMP"]):
                    missing.append({"rule_id": "reac_temp_for_const_temp", "path": p_tconst, "current": status["REAC_TEMP"], "required": "REAC_TEMP"})
                if opt_u == "TEMP-PROF":
                    loc0 = _get(doc, rf"{base}\LOC\#0")
                    if _is_blank(loc0):
                        missing.append({"rule_id": "temp_profile_missing", "path": rf"{base}\LOC\#0", "current": loc0, "required": "LOC#0"})

    cat_u = str(status["CAT_PRESENT"]).upper().strip() if not _is_blank(status["CAT_PRESENT"]) else ""
    if cat_u == "YES":
        provided = sum([
            0 if _is_blank(status["CATWT"]) else 1,
            0 if _is_blank(status["CAT_RHO"]) else 1,
            0 if _is_blank(status["BED_VOIDAGE"]) else 1,
        ])
        if provided < 2:
            missing.append({
                "rule_id": "catalyst_props_need_two_of_three",
                "path": f"{base}\\CATWT / {base}\\CAT_RHO / {base}\\BED_VOIDAGE",
                "current": {"CATWT": status["CATWT"], "CAT_RHO": status["CAT_RHO"], "BED_VOIDAGE": status["BED_VOIDAGE"]},
                "required": "at least 2 of 3",
            })

    return status, missing


# ============================================================
# Public runner (NO YAML loading)
# ============================================================
def run(
    doc,
    rules_all: Dict[str, Any],
    blocks: Optional[List[str]] = None,
    print_report: bool = True,
) -> Dict[str, Any]:
    """
    Pipeline에서 rules_all(dict)을 만들어서 넘기는 구조가 정석입니다.
      - connectivity/feeds와 동일 철학: check.py는 "검증 엔진/함수"만 보유
      - DB(YAML) 로딩/선택은 pipeline(LLM 포함 루프)에서 처리
    """
    blocks = blocks or list_blocks(doc)

    issues: List[Dict[str, Any]] = []
    statuses: List[Dict[str, Any]] = []

    for b in blocks:
        btype = get_block_type(doc, b)
        if btype is None:
            issues.append(make_issue(
                category="BLOCK_SPEC",
                target_type="BLOCK",
                target=b,
                rule_id="block_type_missing",
                message=f"{b}: BLKTYPE를 읽지 못했습니다.",
            ))
            continue

        rule = _resolve_rule(rules_all, btype)
        if rule is None:
            issues.append(make_issue(
                category="BLOCK_SPEC",
                target_type="BLOCK",
                target=b,
                rule_id="no_rules_for_blktype",
                message=f"{b}({btype}): 룰 DB가 없습니다. rules.yaml에 추가 필요.",
                current=btype,
                required="rules.yaml entry",
            ))
            continue

        engine = (rule.get("engine") or "generic").lower()

        if engine == "plugin":
            if btype == "MHEATX":
                st, miss = validate_mheatx_plugin(doc, b)
            elif btype == "RPLUG":
                st, miss = validate_rplug_plugin(doc, b)
            else:
                st, miss = {"block": b, "type": btype}, [{"rule_id": "plugin_not_implemented", "current": btype, "required": "plugin"}]
        else:
            st, miss = validate_generic(doc, b, btype, rules_all, rule)

        statuses.append(st)

        for m in miss:
            issues.append(
                make_issue(
                    category="BLOCK_SPEC",
                    target_type="BLOCK",
                    target=b,
                    rule_id=m.get("rule_id", "block_spec_rule"),
                    message=_make_spec_message(b, btype, m),
                    path=m.get("path"),
                    current=m.get("current"),
                    required=m.get("required"),
                    detail=m.get("detail", {}),
                )
            )

    res = wrap_result(
        validator="block_specs",
        issues=issues,
        targets={"blocks": blocks, "feeds": [], "products": []},
        raw={"statuses": statuses},
    )

    if print_report:
        from valid_common import print_report as _pr
        _pr(res)

    return res


def _make_spec_message(block: str, btype: str, miss: Dict[str, Any]) -> str:
    rid = miss.get("rule_id", "")
    field = miss.get("field")
    if field:
        return f"{block}({btype}): {field} 누락/오류 ({rid})"
    return f"{block}({btype}): spec rule failed ({rid})"
