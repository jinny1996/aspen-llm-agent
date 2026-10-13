from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from aspen_code_v1 import (
    stream_set_basis,
    stream_set_flow_base,
    stream_set_pressure,
    stream_set_temperature,
    stream_set_total_flow,
)
import valid_feed as vf


def _io(schema: Dict[str, Any]) -> Dict[str, Any]:
    return (schema or {}).get("io") or (schema or {}).get("io_schema") or {}


def _aspen_components(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    comps = ((schema or {}).get("aspen_schema") or {}).get("components")
    return comps if isinstance(comps, list) else []


def _load_resolved_components(path: Optional[str]) -> List[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("resolved_components") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


def _component_alias_map(
    schema: Dict[str, Any],
    resolved_components: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, List[str]]:
    aliases: Dict[str, List[str]] = {}

    def add(key: Any, values: List[Any]) -> None:
        k = str(key or "").strip().upper()
        if not k:
            return
        aliases.setdefault(k, [])
        for raw in values:
            v = str(raw or "").strip().upper()
            if v and v != k and v not in aliases[k]:
                aliases[k].append(v)

    # 1) Highest priority: actual aliases selected by Part2 component resolver.
    for row in resolved_components or []:
        if not isinstance(row, dict):
            continue
        resolved_alias = row.get("resolved_alias")
        resolved_name = row.get("resolved_name")
        schema_name = row.get("schema_name")
        formula = row.get("formula")
        values = [resolved_alias, resolved_name, schema_name, formula]
        add(resolved_alias, values)
        add(resolved_name, values)
        add(schema_name, values)
        add(formula, values)

    # 2) Fallback: schema's own component table. This covers test notebooks even
    # when a resolved-components JSON was not produced next to the BKP.
    for comp in _aspen_components(schema):
        if not isinstance(comp, dict):
            continue
        aspen_id = comp.get("aspen_id")
        formula = comp.get("formula")
        name = comp.get("name")
        values = [aspen_id, formula, name]
        add(aspen_id, values)
        add(formula, values)
        add(name, values)

    return aliases


def _feed_specs(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [f for f in (_io(schema).get("feeds") or []) if isinstance(f, dict) and f.get("stream")]


def _value(x: Any, default: Any = None) -> Any:
    if isinstance(x, dict) and "value" in x:
        return x.get("value", default)
    return default if x is None else x


def _num(x: Any) -> Optional[float]:
    v = _value(x, None)
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return float(v)
    except Exception:
        return None


def _str_value(x: Any, default: str = "") -> str:
    v = _value(x, default)
    return str(v).strip() if v is not None else default


def _schema_feed_payload(feed: Dict[str, Any]) -> Dict[str, Any]:
    specs = feed.get("specs") or {}
    flow = specs.get("flow") or {}
    z_raw = specs.get("z") if isinstance(specs.get("z"), dict) else {}
    z = {str(k): _num(v) for k, v in z_raw.items() if _num(v) is not None}
    return {
        "stream": str(feed.get("stream")).strip(),
        "T_C": _num(specs.get("T_C")),
        "P_bar": _num(specs.get("P_bar")),
        "flow_value": _num(flow.get("value")),
        "flow_basis": _str_value(flow.get("basis"), "").upper(),
        "composition_basis": _str_value(specs.get("basis"), "").upper(),
        "z": z,
    }


def _schema_missing_for_feed(payload: Dict[str, Any], missing_inputs: set[str]) -> List[str]:
    missing_fields: List[str] = []

    if "T" in missing_inputs and payload["T_C"] is None:
        missing_fields.append("T_C")

    if "P" in missing_inputs and payload["P_bar"] is None:
        missing_fields.append("P_bar")

    if "COMPOSITION/FLOW" in missing_inputs:
        basis = str(payload.get("composition_basis") or "").upper()
        if not basis:
            missing_fields.append("composition_basis")

        if "FRAC" in basis:
            if payload["flow_value"] is None:
                missing_fields.append("flow_value")
            if not payload["flow_basis"]:
                missing_fields.append("flow_basis")
            if not payload["z"]:
                missing_fields.append("z")
        elif "FLOW" in basis:
            if not payload["z"]:
                missing_fields.append("component_flows")
        elif basis:
            missing_fields.append("supported_composition_basis")

    return missing_fields


def _component_parent_path(stream: str) -> str:
    return rf"\Data\Streams\{stream}\Input\FLOW\MIXED"


def _component_path(stream: str, comp: str) -> str:
    return rf"{_component_parent_path(stream)}\{comp}"


def _find_component_node(doc, stream: str, comp: str):
    node = vf._find(doc, _component_path(stream, comp))
    if node is not None:
        return node

    parent = vf._find(doc, _component_parent_path(stream))
    for child in vf._collect_child_names(parent):
        if child.upper() == comp.upper():
            return vf._find(doc, _component_path(stream, child))
    return None


def _component_candidates(comp: str, aliases: Optional[Dict[str, List[str]]] = None) -> List[str]:
    out: List[str] = []

    def add(x: Any) -> None:
        s = str(x or "").strip().upper()
        if s and s not in out:
            out.append(s)

    add(comp)
    for alias in (aliases or {}).get(str(comp or "").strip().upper(), []):
        add(alias)
    return out


def _find_component_node_any(doc, stream: str, comp: str, aliases: Optional[Dict[str, List[str]]] = None):
    for candidate in _component_candidates(comp, aliases):
        node = _find_component_node(doc, stream, candidate)
        if node is not None:
            return node, candidate
    return None, str(comp or "").strip().upper()


def _set_component_values_sparse(
    doc,
    stream: str,
    comp_dict: Dict[str, float],
    aliases: Optional[Dict[str, List[str]]] = None,
    zero_tol: float = 1e-12,
) -> Dict[str, Any]:
    """
    Set only component nodes that exist, skipping missing zero-valued components.

    Aspen sometimes omits/loses component value nodes after manual table deletion.
    A missing zero-fraction component does not need to be written for the feed to
    validate, but a missing nonzero component is a real repair failure.
    """
    set_components: Dict[str, float] = {}
    skipped_zero_components: List[str] = []
    missing_nonzero_components: Dict[str, float] = {}
    created_component_nodes: List[str] = []
    create_errors: Dict[str, str] = {}
    child_names_before = vf.list_components_in_stream_input(doc, stream)

    for comp, raw_val in comp_dict.items():
        val = float(raw_val)
        node, actual_comp = _find_component_node_any(doc, stream, comp, aliases)
        if node is None:
            if abs(val) <= zero_tol:
                skipped_zero_components.append(comp)
                continue

            parent = vf._find(doc, _component_parent_path(stream))
            try:
                elems = getattr(parent, "Elements", None)
                if elems is None:
                    raise RuntimeError("FLOW/MIXED parent has no Elements collection")

                add_errors: List[str] = []
                add_candidates: List[str] = []
                for candidate in _component_candidates(comp, aliases):
                    add_candidates.extend([candidate, f"{candidate}!REAL"])

                for candidate in add_candidates:
                    try:
                        elems.Add(candidate)
                        node, actual_comp = _find_component_node_any(doc, stream, comp, aliases)
                        if node is not None:
                            created_component_nodes.append(actual_comp)
                            break
                    except Exception as e:
                        add_errors.append(f"{candidate}: {repr(e)}")

                if node is None:
                    raise RuntimeError(
                        "component node still missing after Elements.Add attempts; "
                        + "; ".join(add_errors)
                    )
            except Exception as e:
                missing_nonzero_components[comp] = val
                create_errors[comp] = repr(e)
                continue

        node.Value = val
        set_components[actual_comp] = val

    return {
        "set_components": set_components,
        "skipped_zero_components": skipped_zero_components,
        "missing_nonzero_components": missing_nonzero_components,
        "created_component_nodes": created_component_nodes,
        "create_errors": create_errors,
        "child_names_before": child_names_before,
        "child_names_after": vf.list_components_in_stream_input(doc, stream),
    }


def _needs_composition_repair(result: Dict[str, Any]) -> bool:
    comp = ((result or {}).get("details") or {}).get("composition") or {}
    if not result or not result.get("comp_ok"):
        return True
    reason = str(comp.get("reason") or "")
    return not reason.upper().startswith("OK")


def repair_feeds_from_schema(
    doc,
    schema: Dict[str, Any],
    apply: bool = True,
    print_report: bool = True,
    resolved_components: Optional[List[Dict[str, Any]]] = None,
    resolved_components_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Schema-driven feed repair.

    Current policy:
      - For feed streams listed in schema io.feeds, repair missing/invalid
        T, P, total flow, flow basis, composition basis, and composition.
      - Extra feed-like Aspen streams are not deleted or modified here.
    """
    feeds = _feed_specs(schema)
    resolved_rows = resolved_components or _load_resolved_components(resolved_components_path)
    aliases = _component_alias_map(schema, resolved_rows)
    actions: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    inspected: List[Dict[str, Any]] = []
    schema_missing: List[Dict[str, Any]] = []
    assumptions_needed: List[Dict[str, Any]] = []

    for feed in feeds:
        payload = _schema_feed_payload(feed)
        stream = payload["stream"]

        try:
            before = vf.validate_feed_stream(doc, stream)
        except Exception as e:
            before = {"stream": stream, "missing": ["VALIDATION_ERROR"], "error": repr(e)}

        inspected.append({"stream": stream, "before": before, "schema": payload})

        def _record(action: str, **kwargs):
            actions.append({"action": action, "stream": stream, **kwargs})

        missing = set((before or {}).get("missing") or [])
        missing_schema_fields = _schema_missing_for_feed(payload, missing)
        if missing_schema_fields:
            item = {
                "stream": stream,
                "missing_schema_fields": missing_schema_fields,
                "missing_aspen_inputs": sorted(missing),
                "policy": "defer_to_llm_executable_fallback",
                "reason": "schema does not contain enough feed specs for deterministic hard repair",
            }
            schema_missing.append(item)
            assumptions_needed.append({
                "stream": stream,
                "phase": "feeds",
                "needed_for": sorted(missing),
                "missing_schema_fields": missing_schema_fields,
                "policy": "llm_may_choose_low_confidence_executable_placeholder",
            })

        if "T" in missing and payload["T_C"] is not None:
            if apply:
                try:
                    stream_set_temperature(doc, stream, payload["T_C"])
                except Exception as e:
                    errors.append({"action": "stream_set_temperature", "stream": stream, "error": repr(e)})
                    continue
            _record("stream_set_temperature", T_C=payload["T_C"])

        if "P" in missing and payload["P_bar"] is not None:
            if apply:
                try:
                    stream_set_pressure(doc, stream, payload["P_bar"])
                except Exception as e:
                    errors.append({"action": "stream_set_pressure", "stream": stream, "error": repr(e)})
                    continue
            _record("stream_set_pressure", P_bar=payload["P_bar"])

        if "COMPOSITION/FLOW" in missing or _needs_composition_repair(before):
            if payload["flow_basis"]:
                if apply:
                    try:
                        stream_set_flow_base(doc, stream, payload["flow_basis"])
                    except Exception as e:
                        errors.append({"action": "stream_set_flow_base", "stream": stream, "error": repr(e)})
                        continue
                _record("stream_set_flow_base", basis=payload["flow_basis"])

            if payload["flow_value"] is not None:
                if apply:
                    try:
                        stream_set_total_flow(doc, stream, payload["flow_value"], base=payload["flow_basis"] or "MOLE")
                    except Exception as e:
                        errors.append({"action": "stream_set_total_flow", "stream": stream, "error": repr(e)})
                        continue
                _record("stream_set_total_flow", value=payload["flow_value"], basis=payload["flow_basis"])

            if payload["composition_basis"]:
                if apply:
                    try:
                        stream_set_basis(doc, stream, payload["composition_basis"])
                    except Exception as e:
                        errors.append({"action": "stream_set_basis", "stream": stream, "error": repr(e)})
                        continue
                _record("stream_set_basis", basis=payload["composition_basis"])

            if payload["z"]:
                comp_dict = {str(k): float(v) for k, v in payload["z"].items() if v is not None}
                if apply:
                    try:
                        comp_result = _set_component_values_sparse(doc, stream, comp_dict, aliases=aliases)
                    except Exception as e:
                        errors.append({"action": "stream_set_components", "stream": stream, "error": repr(e)})
                        continue
                    if comp_result["missing_nonzero_components"]:
                        errors.append({
                            "action": "stream_set_components",
                            "stream": stream,
                            "error": "missing_nonzero_component_nodes",
                            "components": comp_result["missing_nonzero_components"],
                            "create_errors": comp_result.get("create_errors", {}),
                            "child_names_before": comp_result.get("child_names_before", []),
                            "child_names_after": comp_result.get("child_names_after", []),
                            "skipped_zero_components": comp_result.get("skipped_zero_components", []),
                            "created_component_nodes": comp_result.get("created_component_nodes", []),
                            "policy": "report_only_defer_to_llm_completion",
                        })
                        continue
                else:
                    comp_result = {
                        "set_components": comp_dict,
                        "skipped_zero_components": [],
                        "missing_nonzero_components": {},
                        "created_component_nodes": [],
                        "create_errors": {},
                        "child_names_before": [],
                        "child_names_after": [],
                    }
                _record(
                    "stream_set_components",
                    basis=payload["composition_basis"],
                    components=comp_result["set_components"],
                    skipped_zero_components=comp_result["skipped_zero_components"],
                    created_component_nodes=comp_result["created_component_nodes"],
                )

    after_results: List[Dict[str, Any]] = []
    remaining: List[Dict[str, Any]] = []
    for feed in feeds:
        stream = str(feed.get("stream")).strip()
        try:
            after = vf.validate_feed_stream(doc, stream)
        except Exception as e:
            after = {"stream": stream, "missing": ["VALIDATION_ERROR"], "error": repr(e)}
        after_results.append(after)
        if after.get("missing"):
            remaining.append({"stream": stream, "missing": after.get("missing"), "detail": after.get("details")})

    repair_summary: List[str] = []
    for act in actions:
        if act["action"] == "stream_set_temperature":
            repair_summary.append(f"Applied stream_set_temperature: {act['stream']} T_C={act['T_C']}")
        elif act["action"] == "stream_set_pressure":
            repair_summary.append(f"Applied stream_set_pressure: {act['stream']} P_bar={act['P_bar']}")
        elif act["action"] == "stream_set_total_flow":
            repair_summary.append(f"Applied stream_set_total_flow: {act['stream']} value={act['value']} basis={act.get('basis')}")
        elif act["action"] == "stream_set_components":
            repair_summary.append(f"Applied stream_set_components: {act['stream']} basis={act.get('basis')} n={len(act.get('components') or {})}")
        else:
            repair_summary.append(f"Applied {act['action']}: {act['stream']}")

    report = {
        "ok": not errors and not remaining and not schema_missing,
        "applied": apply,
        "policy": {
            "schema_feeds": "repair_missing_or_invalid_feed_inputs",
            "extra_feed_streams": "report_only",
            "schema_missing": "defer_to_llm_executable_fallback",
        },
        "schema_feeds": [str(f.get("stream")).strip() for f in feeds],
        "inspected": inspected,
        "schema_missing": schema_missing,
        "assumptions_needed": assumptions_needed,
        "actions": actions,
        "remaining": remaining,
        "errors": errors,
        "after_results": after_results,
        "repair_summary": repair_summary,
    }

    if print_report:
        print("[schema_feed_repair]")
        print(f"  schema_feeds={report['schema_feeds']}")
        print(f"  actions={len(actions)}")
        print(f"  remaining={len(remaining)}")
        if actions:
            print("  applied:")
            for line in repair_summary:
                print(f"    - {line}")
        if remaining:
            print("  remaining:")
            for item in remaining:
                print(f"    - stream={item.get('stream')} missing={item.get('missing')}")
        if schema_missing:
            print("  schema_missing:")
            for item in schema_missing:
                print(
                    "    - "
                    f"stream={item.get('stream')} missing_schema_fields={item.get('missing_schema_fields')}"
                )
        if errors:
            print(f"  errors={errors}")

    return report
