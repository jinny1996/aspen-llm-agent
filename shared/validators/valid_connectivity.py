# validator/connectivity/check.py
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from valid_common import make_issue, wrap_result


# =========================
# Aspen tree helpers
# =========================
def get_tree(doc):
    return doc.Application.Tree


def _find(doc, path: str):
    try:
        return get_tree(doc).FindNode(path)
    except Exception:
        return None


def _get(doc, path: str):
    node = _find(doc, path)
    if node is None:
        return None
    try:
        return node.Value
    except Exception:
        return None


def _is_blank(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _iter_elements(elems) -> List[Any]:
    """COM Elements iterator: Count/Item -> indexing 0 -> indexing 1"""
    if elems is None:
        return []

    try:
        n = elems.Count
        return [elems.Item(i) for i in range(1, n + 1)]
    except Exception:
        pass

    items = []
    try:
        i = 0
        while True:
            items.append(elems[i]); i += 1
    except Exception:
        if items:
            return items

    items = []
    try:
        i = 1
        while True:
            items.append(elems[i]); i += 1
    except Exception:
        return items


def _collect_child_names(node) -> List[str]:
    if node is None:
        return []
    out: List[str] = []

    # 1) Preferred: direct COM iteration (most stable in Aspen)
    elems = getattr(node, "Elements", None)
    if elems is not None:
        try:
            for it in elems:
                if it is None:
                    continue
                nm = getattr(it, "Name", None) or getattr(it, "NodeName", None)
                if nm:
                    s = str(nm).strip().upper()
                    if s:
                        out.append(s)
        except Exception:
            pass

    # 2) Fallback: Count/Item or index probing
    if not out:
        for it in _iter_elements(elems):
            nm = getattr(it, "Name", None) or getattr(it, "NodeName", None)
            if nm:
                s = str(nm).strip().upper()
                if s:
                    out.append(s)
    return sorted(set(out))


def list_blocks(doc) -> List[str]:
    """
    Robust block listing:
      1) \Data\Blocks children
      2) BLKTYPE key list
      3) keep only real nodes under \Data\Blocks\{b}
    """
    blocks = set()
    blocks.update(_collect_child_names(_find(doc, r"\Data\Blocks")))
    blocks.update(_collect_child_names(_find(doc, r"\Data\Flowsheet\Section\GLOBAL\Input\BLKTYPE")))

    real: List[str] = []
    for b in sorted(blocks):
        if _find(doc, rf"\Data\Blocks\{b}") is not None:
            real.append(b)

    if not real:
        raise RuntimeError("Failed to enumerate blocks from Aspen tree.")
    return real


def list_streams(doc) -> List[str]:
    """
    Robust stream listing:
      1) \Data\Streams children
      2) fallback: collect stream names from block port children
    """
    streams = set(_collect_child_names(_find(doc, r"\Data\Streams")))
    if streams:
        return sorted(streams)

    # Fallback when \Data\Streams is unavailable in current COM context.
    try:
        for b in list_blocks(doc):
            btype = get_block_type(doc, b)
            if not btype:
                continue
            for port in PORT_CANDIDATES.get(btype, []):
                pnode = _find(doc, rf"\Data\Blocks\{b}\Ports\{port}")
                for s in _collect_child_names(pnode):
                    streams.add(s)
    except Exception:
        pass

    if not streams:
        raise RuntimeError("Failed to enumerate streams from Aspen tree.")
    return sorted(streams)
# =========================
# Block type (BLKTYPE)
# =========================
def get_block_type(doc, block_name: str) -> Optional[str]:
    p = rf"\Data\Flowsheet\Section\GLOBAL\Input\BLKTYPE\{block_name}"
    v = _get(doc, p)
    return None if _is_blank(v) else str(v).upper().strip()


# =========================
# Port candidates by block type
# =========================
PORT_CANDIDATES = {
    "FLASH2": ["F(IN)", "V(OUT)", "L(OUT)"],
    "HEATER": ["F(IN)", "P(OUT)"],
    "MHEATX": ["HF(IN)", "CF(IN)", "HP(OUT)", "CP(OUT)"],
    "MIXER":  ["F(IN)", "P(OUT)"],
    "RSTOIC": ["F(IN)", "P(OUT)"],
    "RGIBBS": ["F(IN)", "P(OUT)"],
    "RPLUG":  ["F(IN)", "P(OUT)"],     # product multiple later
    "PUMP":   ["F(IN)", "P(OUT)"],
    "COMPR":  ["F(IN)", "P(OUT)"],
    "COMPRESSOR": ["F(IN)", "P(OUT)"],
}


def get_port_connections(doc, block_name: str, block_type: str, stream_names: List[str]) -> Dict[str, List[str]]:
    cand_ports = PORT_CANDIDATES.get(block_type, [])
    ports_map: Dict[str, List[str]] = {}
    for port in cand_ports:
        port_root = _find(doc, rf"\Data\Blocks\{block_name}\Ports\{port}")
        if port_root is None:
            continue

        connected: List[str] = []
        seen = set()

        # Prefer enumerating actual children under the port node so missing entries
        # in \Data\Streams do not cause false negatives.
        elems = getattr(port_root, "Elements", None)
        for it in _iter_elements(elems):
            nm = getattr(it, "Name", None) or getattr(it, "NodeName", None)
            if not nm:
                continue
            s = str(nm).strip()
            if s not in seen:
                connected.append(s)
                seen.add(s)

        # Fallback probe by candidate stream names.
        if not connected:
            for s in (stream_names or []):
                if _find(doc, rf"\Data\Blocks\{block_name}\Ports\{port}\{s}") is not None:
                    if s not in seen:
                        connected.append(s)
                        seen.add(s)

        ports_map[port] = connected

    return ports_map




# =========================
# Connectivity rule helpers
# =========================
def _count_check_exact(missing: List[Dict[str, Any]], block: str, role: str, count: int, exact: int, detail: Dict[str, Any]):
    if count != exact:
        missing.append({
            "rule_id": f"{role}_exact",
            "block": block,
            "role": role,
            "current": count,
            "required": exact,
            "detail": detail,
        })


def _count_check_min(missing: List[Dict[str, Any]], block: str, role: str, count: int, min_n: int, detail: Dict[str, Any]):
    if count < min_n:
        missing.append({
            "rule_id": f"{role}_min",
            "block": block,
            "role": role,
            "current": count,
            "required": f">={min_n}",
            "detail": detail,
        })


def validate_connectivity_one(doc, block_name: str, stream_names: List[str]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    btype = get_block_type(doc, block_name)
    ports_map = {} if btype is None else get_port_connections(doc, block_name, btype, stream_names)

    status = {"block": block_name, "type": btype, "ports_map": ports_map}
    missing: List[Dict[str, Any]] = []

    if btype is None:
        missing.append({"rule_id": "block_type_missing", "block": block_name})
        return status, missing

    if btype == "FLASH2":
        _count_check_min(missing, block_name, "feed",  len(ports_map.get("F(IN)", [])), 1, {"port": "F(IN)"})
        _count_check_exact(missing, block_name, "vapor", len(ports_map.get("V(OUT)", [])), 1, {"port": "V(OUT)"})
        _count_check_exact(missing, block_name, "liquid", len(ports_map.get("L(OUT)", [])), 1, {"port": "L(OUT)"})

    elif btype == "HEATER":
        _count_check_min(missing, block_name, "feed", len(ports_map.get("F(IN)", [])), 1, {"port": "F(IN)"})
        _count_check_exact(missing, block_name, "product", len(ports_map.get("P(OUT)", [])), 1, {"port": "P(OUT)"})

    elif btype == "MIXER":
        _count_check_min(missing, block_name, "feed", len(ports_map.get("F(IN)", [])), 1, {"port": "F(IN)"})
        _count_check_exact(missing, block_name, "product", len(ports_map.get("P(OUT)", [])), 1, {"port": "P(OUT)"})

    elif btype == "MHEATX":
        cold_in  = ports_map.get("CF(IN)", [])
        cold_out = ports_map.get("CP(OUT)", [])
        hot_in   = ports_map.get("HF(IN)", [])
        hot_out  = ports_map.get("HP(OUT)", [])

        _count_check_min(missing, block_name, "cold_feed", len(cold_in), 1, {"port": "CF(IN)"})
        _count_check_min(missing, block_name, "cold_product", len(cold_out), 1, {"port": "CP(OUT)"})
        _count_check_min(missing, block_name, "hot_feed", len(hot_in), 1, {"port": "HF(IN)"})
        _count_check_min(missing, block_name, "hot_product", len(hot_out), 1, {"port": "HP(OUT)"})

        if len(cold_in) != len(cold_out):
            missing.append({
                "rule_id": "cold_in_out_count_mismatch",
                "block": block_name,
                "current": {"cold_in": len(cold_in), "cold_out": len(cold_out)},
                "required": "cold_in == cold_out",
                "detail": {"cold_in_port": "CF(IN)", "cold_out_port": "CP(OUT)"},
            })

        if len(hot_in) != len(hot_out):
            missing.append({
                "rule_id": "hot_in_out_count_mismatch",
                "block": block_name,
                "current": {"hot_in": len(hot_in), "hot_out": len(hot_out)},
                "required": "hot_in == hot_out",
                "detail": {"hot_in_port": "HF(IN)", "hot_out_port": "HP(OUT)"},
            })

    elif btype in {"RSTOIC", "RGIBBS"}:
        _count_check_min(missing, block_name, "feed", len(ports_map.get("F(IN)", [])), 1, {"port": "F(IN)"})
        _count_check_min(missing, block_name, "product", len(ports_map.get("P(OUT)", [])), 1, {"port": "P(OUT)"})

    elif btype == "RPLUG":
        _count_check_exact(missing, block_name, "feed", len(ports_map.get("F(IN)", [])), 1, {"port": "F(IN)"})
        _count_check_exact(missing, block_name, "product", len(ports_map.get("P(OUT)", [])), 1, {"port": "P(OUT)", "note": "DB limitation: only 1 product supported now"})

    elif btype == "PUMP":
        _count_check_min(missing, block_name, "feed", len(ports_map.get("F(IN)", [])), 1, {"port": "F(IN)"})
        _count_check_exact(missing, block_name, "product", len(ports_map.get("P(OUT)", [])), 1, {"port": "P(OUT)"})

    elif btype in {"COMPR", "COMPRESSOR"}:
        _count_check_min(missing, block_name, "feed", len(ports_map.get("F(IN)", [])), 1, {"port": "F(IN)"})
        _count_check_exact(missing, block_name, "product", len(ports_map.get("P(OUT)", [])), 1, {"port": "P(OUT)"})

    else:
        missing.append({
            "rule_id": "block_type_not_supported",
            "block": block_name,
            "current": btype,
            "required": "Add PORT_CANDIDATES + rules",
            "detail": {"hint": "Extend connectivity DB"},
        })

    return status, missing


# =========================
# Public runner
# =========================
def run(doc, blocks: Optional[List[str]] = None, print_report: bool = True) -> Dict[str, Any]:
    blocks = blocks or list_blocks(doc)
    stream_names = list_streams(doc)

    issues: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []

    for b in blocks:
        st, miss = validate_connectivity_one(doc, b, stream_names)

        # 디버깅 편의: raw missing을 status에 붙여 저장
        st["missing_raw"] = miss
        results.append(st)

        for m in miss:
            issues.append(
                make_issue(
                    category="CONNECTIVITY",
                    target_type="BLOCK",
                    target=b,
                    rule_id=m.get("rule_id", "connectivity_rule"),
                    message=_make_connectivity_message(st, m),
                    path=None,
                    current=m.get("current"),
                    required=m.get("required"),
                    detail=m.get("detail", {}),
                )
            )

    res = wrap_result(
        validator="connectivity",
        issues=issues,
        targets={"blocks": blocks, "feeds": [], "products": []},
        raw={"results": results},
    )

    if print_report:
        from valid_common import print_report as _pr
        _pr(res)

    return res


def _make_connectivity_message(status: Dict[str, Any], miss: Dict[str, Any]) -> str:
    b = status.get("block")
    t = status.get("type")
    rid = miss.get("rule_id", "")
    detail = miss.get("detail", {}) or {}

    if rid.endswith("_min"):
        role = miss.get("role", "")
        req = miss.get("required")
        port = detail.get("port")
        return f"{b}({t}): {role} 연결이 부족합니다. (port={port}, required={req})"

    if rid.endswith("_exact"):
        role = miss.get("role", "")
        req = miss.get("required")
        port = detail.get("port")
        return f"{b}({t}): {role} 연결 개수가 맞지 않습니다. (port={port}, required={req})"

    if rid == "block_type_missing":
        return f"{b}: BLKTYPE를 읽지 못했습니다."

    if rid == "block_type_not_supported":
        return f"{b}({t}): 지원하지 않는 블록 타입입니다. PORT_CANDIDATES/룰 추가 필요."

    if rid in ("cold_in_out_count_mismatch", "hot_in_out_count_mismatch"):
        return f"{b}({t}): In/Out 스트림 개수가 불일치합니다. ({rid})"

    return f"{b}({t}): connectivity rule failed ({rid})"

