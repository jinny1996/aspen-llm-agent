from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from aspen_code_v1 import block_add, connect_stream, stream_add
import valid_connectivity as vc


PORT_ROLE_TO_ASPEN_PORT = {
    "IN": "F(IN)",
    "OUT": "P(OUT)",
    "COLD_IN": "CF(IN)",
    "COLD_OUT": "CP(OUT)",
    "HOT_IN": "HF(IN)",
    "HOT_OUT": "HP(OUT)",
    "V": "V(OUT)",
    "VAP": "V(OUT)",
    "VAP_OUT": "V(OUT)",
    "LIQ": "L(OUT)",
    "L": "L(OUT)",
    "LIQ_OUT": "L(OUT)",
}


def _flowsheet(schema: Dict[str, Any]) -> Dict[str, Any]:
    return (schema or {}).get("flowsheet_schema") or (schema or {}).get("flowsheet") or {}


def _schema_stream_ids(schema: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for s in (_flowsheet(schema).get("streams") or []):
        if isinstance(s, dict) and s.get("id"):
            out.append(str(s["id"]).strip())
    return [x for x in out if x]


def _schema_block_ids(schema: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for b in (_flowsheet(schema).get("blocks") or []):
        if isinstance(b, dict) and b.get("id"):
            out.append(str(b["id"]).strip())
    return [x for x in out if x]


def _schema_block_type_map(schema: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for b in (_flowsheet(schema).get("blocks") or []):
        if isinstance(b, dict) and b.get("id"):
            bid = str(b["id"]).strip()
            btype = str(b.get("type") or "").strip()
            if bid and btype:
                out[bid.upper()] = btype
    return out


def _aspen_block_type(btype: str) -> str:
    key = str(btype or "").strip().upper()
    return {
        "MIXER": "Mixer",
        "HEATER": "Heater",
        "FLASH2": "Flash2",
        "RGIBBS": "RGibbs",
        "RSTOIC": "RStoic",
        "RPLUG": "RPlug",
        "MHEATX": "MHeatX",
        "HEATX": "MHeatX",
        "PUMP": "Pump",
        "COMPR": "Compr",
        "COMPRESSOR": "Compr",
        "FSPLIT": "FSplit",
        "SPLITTER": "FSplit",
    }.get(key, btype)


def _schema_edges(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    conn = (_flowsheet(schema).get("connectivity") or {})
    return [e for e in (conn.get("edges") or []) if isinstance(e, dict) and e.get("stream")]


def _endpoint_to_conn(edge: Dict[str, Any], side: str) -> Optional[Tuple[str, str, str]]:
    endpoint = edge.get(side)
    if not isinstance(endpoint, dict):
        return None
    block = endpoint.get("block")
    if not block:
        return None
    role = str(endpoint.get("port_role") or "").upper().strip()
    port = PORT_ROLE_TO_ASPEN_PORT.get(role)
    if not port:
        return None
    return (str(block).strip().upper(), str(edge["stream"]).strip().upper(), port)


def expected_connections(schema: Dict[str, Any]) -> Set[Tuple[str, str, str]]:
    conns: Set[Tuple[str, str, str]] = set()
    for edge in _schema_edges(schema):
        for side in ("from", "to"):
            conn = _endpoint_to_conn(edge, side)
            if conn is not None:
                conns.add(conn)
    return conns


def _endpoint_child_blocks(doc, stream: str, endpoint: str) -> Set[str]:
    node = vc._find(doc, rf"\Data\Streams\{stream}\Ports\{endpoint}")
    return {str(x).strip().upper() for x in vc._collect_child_names(node)}


def _current_connections(
    doc,
    candidate_streams: Optional[List[str]] = None,
    schema: Optional[Dict[str, Any]] = None,
) -> Set[Tuple[str, str, str]]:
    conns: Set[Tuple[str, str, str]] = set()
    streams = sorted(_as_set_upper(_list_streams_for_repair(doc) + (candidate_streams or [])))
    for block in _list_blocks_for_repair(doc):
        btype = vc.get_block_type(doc, block)
        if not btype:
            continue
        for port in vc.PORT_CANDIDATES.get(btype, []):
            port_root = vc._find(doc, rf"\Data\Blocks\{block}\Ports\{port}")
            if port_root is None:
                continue

            # Use the robust child-name collector from valid_connectivity, then
            # always probe candidate stream paths as well. This avoids the Aspen
            # COM first/partial child enumeration issue on multi-stream ports
            # such as MIXER F(IN).
            for stream in vc._collect_child_names(port_root):
                conns.add((str(block).strip().upper(), str(stream).strip().upper(), str(port).strip()))

            for stream in streams:
                if vc._find(doc, rf"\Data\Blocks\{block}\Ports\{port}\{stream}") is not None:
                    conns.add((str(block).strip().upper(), str(stream).strip().upper(), str(port).strip()))

    # Stream endpoint fallback:
    # Some Aspen COM contexts under-report children from multi-stream block ports
    # (notably MIXER F(IN)). If the stream endpoint says SOURCE/DEST exists,
    # use the schema's expected endpoint to recover the connection.
    if schema is not None:
        for edge in _schema_edges(schema):
            for side in ("from", "to"):
                expected = _endpoint_to_conn(edge, side)
                if expected is None:
                    continue

                block, stream, port = expected
                endpoint = _connection_endpoint_type(port)
                if not _stream_has_endpoint(doc, stream, endpoint):
                    continue

                child_blocks = _endpoint_child_blocks(doc, stream, endpoint)
                if not child_blocks or block in child_blocks:
                    conns.add(expected)
    return conns


def _as_set_upper(values: List[str]) -> Set[str]:
    return {str(v).strip().upper() for v in values if str(v).strip()}


def _collect_child_names_union(node) -> List[str]:
    """Collect child names through every COM iteration path we know."""
    if node is None:
        return []

    out: Set[str] = set()
    elems = getattr(node, "Elements", None)

    if elems is not None:
        try:
            for it in elems:
                nm = getattr(it, "Name", None) or getattr(it, "NodeName", None)
                if nm:
                    out.add(str(nm).strip().upper())
        except Exception:
            pass

    for it in vc._iter_elements(elems):
        nm = getattr(it, "Name", None) or getattr(it, "NodeName", None)
        if nm:
            out.add(str(nm).strip().upper())

    return sorted(x for x in out if x)


def _list_blocks_for_repair(doc) -> List[str]:
    blocks: Set[str] = set()
    blocks.update(_collect_child_names_union(vc._find(doc, r"\Data\Blocks")))
    blocks.update(_collect_child_names_union(vc._find(doc, r"\Data\Flowsheet\Section\GLOBAL\Input\BLKTYPE")))

    try:
        blocks.update(_as_set_upper(vc.list_blocks(doc)))
    except Exception:
        pass

    real = [b for b in sorted(blocks) if vc._find(doc, rf"\Data\Blocks\{b}") is not None]
    if not real:
        raise RuntimeError("Failed to enumerate blocks for schema connectivity repair.")
    return real


def _list_streams_for_repair(doc) -> List[str]:
    streams: Set[str] = set()
    streams.update(_collect_child_names_union(vc._find(doc, r"\Data\Streams")))

    try:
        streams.update(_as_set_upper(vc.list_streams(doc)))
    except Exception:
        pass

    # Add connected streams from block ports as a union source. This does not help
    # with unconnected extra streams, so \Data\Streams union above remains primary.
    try:
        for block in _list_blocks_for_repair(doc):
            btype = vc.get_block_type(doc, block)
            if not btype:
                continue
            for port in vc.PORT_CANDIDATES.get(btype, []):
                streams.update(_collect_child_names_union(vc._find(doc, rf"\Data\Blocks\{block}\Ports\{port}")))
    except Exception:
        pass

    if not streams:
        raise RuntimeError("Failed to enumerate streams for schema connectivity repair.")
    return sorted(streams)


def _node_has_children(node) -> bool:
    if node is None:
        return False

    for key in ("Has Children", "HasChildren"):
        try:
            return int(node.AttributeValue(key)) == 1
        except Exception:
            pass

    for attr in ("Elements", "Children"):
        try:
            return int(getattr(node, attr).Count) > 0
        except Exception:
            pass

    return False


def _stream_has_endpoint(doc, stream: str, endpoint: str) -> bool:
    node = vc._find(doc, rf"\Data\Streams\{stream}\Ports\{endpoint}")
    return _node_has_children(node)


def _connection_endpoint_type(port: str) -> str:
    port_u = str(port or "").upper().strip()
    if "(IN)" in port_u or port_u in {"F(IN)", "CF(IN)", "HF(IN)", "INLET", "IN"}:
        return "DEST"
    return "SOURCE"


def repair_connectivity_from_schema(
    doc,
    schema: Dict[str, Any],
    apply: bool = True,
    remove_extra_streams: bool = False,
    remove_extra_connections: bool = False,
    print_report: bool = True,
) -> Dict[str, Any]:
    """
    Schema-driven connectivity repair.

    Current policy:
      - missing schema streams: add automatically when apply=True
      - missing schema connections: connect automatically when apply=True
      - extra streams/connections: report only by default
    """
    if remove_extra_streams or remove_extra_connections:
        raise NotImplementedError("Extra stream/connection removal is intentionally disabled in this test version.")

    schema_streams = _schema_stream_ids(schema)
    schema_blocks = _schema_block_ids(schema)
    schema_block_types = _schema_block_type_map(schema)
    schema_conn = expected_connections(schema)

    current_streams_before = _list_streams_for_repair(doc)
    current_blocks = _list_blocks_for_repair(doc)
    current_conn_before = _current_connections(doc, schema_streams, schema)

    schema_stream_set = _as_set_upper(schema_streams)
    schema_block_set = _as_set_upper(schema_blocks)
    current_stream_set = _as_set_upper(current_streams_before)
    current_block_set = _as_set_upper(current_blocks)

    missing_streams = [s for s in schema_streams if s.upper() not in current_stream_set]
    extra_streams = sorted(current_stream_set - schema_stream_set)
    missing_blocks = [b for b in schema_blocks if b.upper() not in current_block_set]
    extra_blocks = sorted(current_block_set - schema_block_set)

    actions: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    skipped_connections: List[Dict[str, Any]] = []

    if apply:
        for block in missing_blocks:
            btype = schema_block_types.get(block.upper())
            if not btype:
                errors.append({"action": "block_add", "block": block, "error": "schema_block_type_missing"})
                continue
            aspen_btype = _aspen_block_type(btype)
            try:
                block_add(doc, block, aspen_btype)
                actions.append({"action": "block_add", "block": block, "btype": aspen_btype, "schema_btype": btype})
            except Exception as e:
                errors.append({"action": "block_add", "block": block, "btype": aspen_btype, "schema_btype": btype, "error": repr(e)})

        for stream in missing_streams:
            try:
                stream_add(doc, stream, "MATERIAL")
                actions.append({"action": "stream_add", "stream": stream, "stype": "MATERIAL"})
            except Exception as e:
                errors.append({"action": "stream_add", "stream": stream, "error": repr(e)})

    current_conn_mid = _current_connections(doc, schema_streams, schema) if apply and (missing_streams or missing_blocks) else current_conn_before
    missing_connections = sorted(schema_conn - current_conn_mid)
    extra_connections = sorted(current_conn_mid - schema_conn)

    if apply:
        for block, stream, port in missing_connections:
            endpoint_type = _connection_endpoint_type(port)
            if _stream_has_endpoint(doc, stream, endpoint_type):
                skipped_connections.append({
                    "reason": f"stream_already_has_{endpoint_type.lower()}",
                    "block": block,
                    "stream": stream,
                    "port": port,
                    "policy": "report_only_until_extra_connection_removal_is_enabled",
                })
                continue

            try:
                connect_stream(doc, block, stream, port)
                actions.append({"action": "connect_stream", "block": block, "stream": stream, "port": port})
            except Exception as e:
                msg = repr(e)
                if "more than one sink" in msg.lower() or "more than one source" in msg.lower():
                    skipped_connections.append({
                        "reason": "aspen_endpoint_already_connected",
                        "block": block,
                        "stream": stream,
                        "port": port,
                        "error": msg,
                        "policy": "report_only_until_extra_connection_removal_is_enabled",
                    })
                else:
                    errors.append({"action": "connect_stream", "block": block, "stream": stream, "port": port, "error": msg})

    current_conn_after = _current_connections(doc, schema_streams, schema) if apply else current_conn_before
    current_blocks_after = _list_blocks_for_repair(doc) if apply else current_blocks
    current_block_set_after = _as_set_upper(current_blocks_after)
    remaining_missing_blocks = [b for b in schema_blocks if b.upper() not in current_block_set_after]
    remaining_missing_connections = sorted(schema_conn - current_conn_after)
    extra_connections_after = sorted(current_conn_after - schema_conn)

    repair_summary: List[str] = []
    for block, stream, port in missing_connections:
        repair_summary.append(f"Detected missing connection: {block} / {port} / {stream}")
    for act in actions:
        if act.get("action") == "stream_add":
            repair_summary.append(f"Applied stream_add: {act.get('stream')} ({act.get('stype')})")
        elif act.get("action") == "block_add":
            repair_summary.append(f"Applied block_add: {act.get('block')} ({act.get('btype')})")
        elif act.get("action") == "connect_stream":
            repair_summary.append(
                f"Applied connect_stream: {act.get('block')} / {act.get('port')} / {act.get('stream')}"
            )
    for block, stream, port in remaining_missing_connections:
        repair_summary.append(f"Remaining missing connection: {block} / {port} / {stream}")
    for block, stream, port in extra_connections_after:
        repair_summary.append(f"Extra connection reported only: {block} / {port} / {stream}")
    for stream in extra_streams:
        repair_summary.append(f"Extra stream reported only: {stream}")

    report = {
        "ok": not remaining_missing_blocks and not errors and not remaining_missing_connections,
        "applied": apply,
        "policy": {
            "missing_streams": "auto_add" if apply else "report_only",
            "missing_blocks": "auto_add" if apply else "report_only",
            "missing_connections": "auto_connect" if apply else "report_only",
            "extra_streams": "report_only",
            "extra_connections": "report_only",
        },
        "schema": {
            "streams": schema_streams,
            "blocks": schema_blocks,
            "n_expected_connections": len(schema_conn),
        },
        "current_before": {
            "streams": current_streams_before,
            "blocks": current_blocks,
            "n_connections": len(current_conn_before),
        },
        "missing_streams": missing_streams,
        "extra_streams": extra_streams,
        "missing_blocks": missing_blocks,
        "remaining_missing_blocks": remaining_missing_blocks,
        "extra_blocks": extra_blocks,
        "missing_connections_before_repair": missing_connections,
        "remaining_missing_connections": remaining_missing_connections,
        "extra_connections": extra_connections_after,
        "actions": actions,
        "skipped_connections": skipped_connections,
        "errors": errors,
        "repair_summary": repair_summary,
    }

    if print_report:
        print("[schema_connectivity_repair]")
        print(f"  missing_streams={missing_streams}")
        print(f"  extra_streams={extra_streams}")
        print(f"  missing_blocks={missing_blocks}")
        if remaining_missing_blocks:
            print(f"  remaining_missing_blocks={remaining_missing_blocks}")
        print(f"  missing_connections_before_repair={len(missing_connections)}")
        print(f"  remaining_missing_connections={len(remaining_missing_connections)}")
        print(f"  extra_connections={len(extra_connections_after)}")
        print(f"  actions={len(actions)}")
        if missing_connections:
            print("  detected:")
            for block, stream, port in missing_connections:
                print(f"    - missing connection: block={block}, port={port}, stream={stream}")
        if actions:
            print("  applied:")
            for act in actions:
                if act.get("action") == "stream_add":
                    print(f"    - stream_add(stream={act.get('stream')}, stype={act.get('stype')})")
                elif act.get("action") == "block_add":
                    print(f"    - block_add(block={act.get('block')}, btype={act.get('btype')})")
                elif act.get("action") == "connect_stream":
                    print(
                        "    - connect_stream("
                        f"block={act.get('block')}, stream={act.get('stream')}, port={act.get('port')})"
                    )
        if remaining_missing_connections:
            print("  remaining:")
            for block, stream, port in remaining_missing_connections:
                print(f"    - missing connection: block={block}, port={port}, stream={stream}")
        if extra_connections_after:
            print("  report_only:")
            for block, stream, port in extra_connections_after:
                print(f"    - extra connection: block={block}, port={port}, stream={stream}")
        if extra_streams:
            print("  report_only:")
            for stream in extra_streams:
                print(f"    - extra stream: stream={stream}")
        if skipped_connections:
            print(f"  skipped_connections={skipped_connections}")
        if errors:
            print(f"  errors={errors}")

    return report
