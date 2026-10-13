# validator/feeds/check.py
from __future__ import annotations

from typing import Any, Dict, List, Tuple, Optional

from valid_common import make_issue, wrap_result


# =========================
# Tree access (environment-fixed)
# =========================
def get_tree(doc):
    return doc.Application.Tree


def _find(doc, path: str):
    try:
        return get_tree(doc).FindNode(path)
    except Exception:
        return None


def _safe_value(node, default=None):
    if node is None:
        return default
    try:
        return node.Value
    except Exception:
        return default


def _iter_elements(elems):
    """COM Elements iterator: Count/Item -> indexing 0 -> indexing 1"""
    if elems is None:
        return []

    # 1) Count/Item
    try:
        n = elems.Count
        return [elems.Item(i) for i in range(1, n + 1)]
    except Exception:
        pass

    # 2) indexing 0
    items = []
    try:
        i = 0
        while True:
            items.append(elems[i])
            i += 1
    except Exception:
        if items:
            return items

    # 3) indexing 1
    items = []
    try:
        i = 1
        while True:
            items.append(elems[i])
            i += 1
    except Exception:
        return items


def _collect_child_names(node) -> List[str]:
    if node is None:
        return []

    out: List[str] = []
    elems = getattr(node, "Elements", None)

    # 1) Preferred: direct COM iteration
    if elems is not None:
        try:
            for it in elems:
                if it is None:
                    continue
                nm = getattr(it, "Name", None) or getattr(it, "NodeName", None)
                if nm:
                    s = str(nm).strip()
                    if s:
                        out.append(s)
        except Exception:
            pass

    # 2) Fallback: legacy iterator paths
    if not out:
        for it in _iter_elements(elems):
            nm = getattr(it, "Name", None) or getattr(it, "NodeName", None)
            if nm:
                s = str(nm).strip()
                if s:
                    out.append(s)

    seen = set()
    uniq: List[str] = []
    for x in out:
        if x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq
def list_streams_from_tree(doc) -> List[str]:
    node = _find(doc, r"\Data\Streams")
    if node is None:
        raise RuntimeError(r"\Data\Streams ?몃뱶瑜?紐?李얠븯?듬땲??")

    names = _collect_child_names(node)
    if not names:
        raise RuntimeError(r"\Data\Streams?먯꽌 ?ㅽ듃由쇱쓣 ?닿굅?섏? 紐삵뻽?듬땲??")
    return names


# =========================
# Node child existence (Ports SOURCE/DEST)
# =========================
def node_has_children(node) -> bool:
    """EO Tree node??child(?곌껐 ?뺣낫)媛 議댁옱?섎뒗吏 ?먮퀎."""
    if node is None:
        return False

    # 1) Variable Explorer??蹂댁씠??"Has Children" ?곗꽑
    for key in ("Has Children", "HasChildren"):
        try:
            return int(node.AttributeValue(key)) == 1
        except Exception:
            pass

    # 2) 李⑥꽑: ?섏쐞 ?몃뱶 媛쒖닔(踰꾩쟾留덈떎 Elements/Children???ㅻ쫫)
    for attr in ("Elements", "Children"):
        try:
            return int(getattr(node, attr).Count) > 0
        except Exception:
            pass

    return False


# =========================
# Stream role classification
# =========================
def classify_stream_by_ports(doc, sname: str) -> str:
    """
    Ports\\SOURCE / Ports\\DEST ?꾨옒 child 議댁옱 ?좊Т濡?FEED/PRODUCT ?먮퀎.
    """
    dest = _find(doc, fr"\Data\Streams\{sname}\Ports\DEST")
    src  = _find(doc, fr"\Data\Streams\{sname}\Ports\SOURCE")

    dest_conn = node_has_children(dest)
    src_conn  = node_has_children(src)

    if (not src_conn) and dest_conn:
        return "FEED"
    if src_conn and (not dest_conn):
        return "PRODUCT"
    if src_conn and dest_conn:
        return "INTERNAL"
    return "ISOLATED"


def get_system_feeds_products(doc) -> Tuple[List[str], List[str]]:
    streams = list_streams_from_tree(doc)

    feeds, prods = [], []
    for s in streams:
        c = classify_stream_by_ports(doc, s)
        if c == "FEED":
            feeds.append(s)
        elif c == "PRODUCT":
            prods.append(s)

    return feeds, prods


# =========================
# "Specified" detection
# =========================
def _is_specified(node) -> bool:
    if node is None:
        return False

    for key in ("Specified", "IsSpecified", "INPUT", "Input", "Status"):
        try:
            v = node.AttributeValue(key)
            if isinstance(v, (int, float)):
                return int(v) != 0
            if isinstance(v, bool):
                return v
            if isinstance(v, str):
                return v.strip().upper() in ("1", "Y", "YES", "TRUE", "T")
        except Exception:
            pass

    try:
        val = node.Value
        if val is None:
            return False
        if isinstance(val, str) and val.strip() == "":
            return False
        return True
    except Exception:
        return False


# =========================
# Path helpers
# =========================
def _p_T(s):           return rf"\Data\Streams\{s}\Input\TEMP\MIXED"
def _p_P(s):           return rf"\Data\Streams\{s}\Input\PRES\MIXED"
def _p_tot(s):         return rf"\Data\Streams\{s}\Input\TOTFLOW\MIXED"
def _p_basis(s):       return rf"\Data\Streams\{s}\Input\BASIS\MIXED"
def _p_comp_parent(s): return rf"\Data\Streams\{s}\Input\FLOW\MIXED"


def list_components_in_stream_input(doc, stream_name: str) -> List[str]:
    parent = _find(doc, _p_comp_parent(stream_name))
    return _collect_child_names(parent)


# =========================
# Composition validator
# =========================
def validate_feed_composition(doc, stream_name: str, tol_sum: float = 1e-3) -> Tuple[bool, Dict[str, Any]]:
    """
    Supported cases:
      - *-FRAC: requires TOTFLOW + positive component ratios/fractions
      - *-FLOW: requires component flows(sum>0), TOTFLOW optional
    """
    detail: Dict[str, Any] = {
        "basis": None,
        "totflow": None,
        "totflow_specified": False,
        "n_components_specified": 0,
        "sum_components": None,
        "reason": None,
    }

    # BASIS
    basis_node = _find(doc, _p_basis(stream_name))
    if basis_node is None:
        basis_val = None
    else:
        try:
            basis_val = (basis_node.Value or "").strip().upper()
        except Exception:
            basis_val = None
    detail["basis"] = basis_val

    # TOTFLOW
    tot_node = _find(doc, _p_tot(stream_name))
    detail["totflow"] = _safe_value(tot_node, None)
    detail["totflow_specified"] = _is_specified(tot_node)

    # component values (specified only)
    comps = list_components_in_stream_input(doc, stream_name)
    comp_vals: List[float] = []
    n_spec = 0
    comp_parent = _p_comp_parent(stream_name)

    for c in comps:
        node = _find(doc, rf"{comp_parent}\{c}")
        if _is_specified(node):
            n_spec += 1
            try:
                v = node.Value
                if v is not None:
                    comp_vals.append(float(v))
            except Exception:
                pass

    detail["n_components_specified"] = n_spec

    # decision
    if not basis_val:
        detail["reason"] = "BASIS 미지정"
        return False, detail

    is_frac = "FRAC" in basis_val
    is_flow = "FLOW" in basis_val

    # 1) FRAC basis: TOTFLOW + positive component ratios/fractions.
    # Aspen can normalize non-unity ratios (e.g. 1:3) against total flow, so
    # validation should not fail just because the entered sum is not exactly 1.
    if is_frac:
        if not detail["totflow_specified"]:
            detail["reason"] = "FRAC basis with missing TOTFLOW"
            return False, detail
        if n_spec == 0 or not comp_vals:
            detail["reason"] = "FRAC basis with no component values"
            return False, detail

        s = sum(comp_vals)
        detail["sum_components"] = s
        detail["sum_deviation_from_1"] = abs(s - 1.0)
        detail["aspen_normalizes_fraction_ratios"] = True
        if s <= 0:
            detail["reason"] = f"FRAC basis with no positive component values (sum={s:.6g})"
            return False, detail

        detail["reason"] = "OK (TOTFLOW + *-FRAC ratios)"
        return True, detail

    # 2) FLOW 기반: component flows(sum>0). TOTFLOW optional
    if is_flow:
        if n_spec == 0 or not comp_vals:
            detail["reason"] = "FLOW basis with no component flow values"
            return False, detail

        s = sum(comp_vals)
        detail["sum_components"] = s
        if s <= 0:
            detail["reason"] = f"FLOW basis with no positive component flow values (sum={s:.6g})"
            return False, detail

        detail["reason"] = "OK (*-FLOW)"
        return True, detail

    detail["reason"] = f"Unsupported BASIS: {basis_val}"
    return False, detail


def validate_feed_stream(doc, stream_name: str) -> Dict[str, Any]:
    """FEED ?ㅽ듃由??꾩닔 ?낅젰(T, P, Composition/Flow) 寃利?"""
    missing: List[str] = []
    details: Dict[str, Any] = {}

    # T
    T_node = _find(doc, _p_T(stream_name))
    T_ok = _is_specified(T_node)
    if not T_ok:
        missing.append("T")
    details["T"] = _safe_value(T_node, None)

    # P
    P_node = _find(doc, _p_P(stream_name))
    P_ok = _is_specified(P_node)
    if not P_ok:
        missing.append("P")
    details["P"] = _safe_value(P_node, None)

    # Composition / Flow
    comp_ok, comp_detail = validate_feed_composition(doc, stream_name)
    if not comp_ok:
        missing.append("COMPOSITION/FLOW")
    details["composition"] = comp_detail

    return {
        "stream": stream_name,
        "T_ok": T_ok,
        "P_ok": P_ok,
        "comp_ok": comp_ok,
        "missing": missing,
        "details": details,
    }


# =========================
# Public runner
# =========================
def run(doc, print_report: bool = True) -> Dict[str, Any]:
    feeds, prods = get_system_feeds_products(doc)
    results = [validate_feed_stream(doc, s) for s in feeds]

    issues: List[Dict[str, Any]] = []
    for r in results:
        if not r["missing"]:
            continue
        comp = r["details"]["composition"]
        paths = {
            "T": _p_T(r["stream"]),
            "P": _p_P(r["stream"]),
            "BASIS": _p_basis(r["stream"]),
            "TOTFLOW": _p_tot(r["stream"]),
            "COMP_PARENT": _p_comp_parent(r["stream"]),
        }

        issues.append(
            make_issue(
                category="FEED_INPUT",
                target_type="STREAM",
                target=r["stream"],
                rule_id="feed_missing_required_inputs",
                message=(
                    f"{r['stream']}: missing={','.join(r['missing'])} "
                    f"(basis={comp.get('basis')}, reason={comp.get('reason')})"
                ),
                path=None,
                current={
                    "T": r["details"]["T"],
                    "P": r["details"]["P"],
                    "basis": comp.get("basis"),
                    "totflow": comp.get("totflow"),
                },
                required="T, P, and valid composition/flow input",
                detail={
                    "missing": r["missing"],
                    "paths": paths,
                    **comp,
                },
            )
        )

    res = wrap_result(
        validator="feeds",
        issues=issues,
        targets={"blocks": [], "feeds": feeds, "products": prods},
        raw={"results": results},
    )

    if print_report:
        from valid_common import print_report as _pr
        _pr(res)

    return res


