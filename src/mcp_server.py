"""Unified MCP server for the Part1/2/3 Aspen pipeline.

Merged (2026-09-01, item 3) from three previously-separate files
(``mcp_server_part1.py``, ``mcp_server_part2.py``, ``mcp_server_part3.py``) into
one server exposing all 10 pipeline tools -- no tool-name collisions across the
three parts, so this is a pure reorganization: tool logic is unchanged, only its
location and the shared server-plumbing moved.

- Part1 tools (4): paper_context_tool, image_context_tool, schema_rule_tool,
  schema_validation_tool.
- Part2 tools (5): part2_config_tool, schema_context_tool, com_catalog_tool,
  validation_summary_tool, repair_assessment_tool.
- Part3 tools (1): part3_assessment_tool.

Run as an MCP stdio server:

    python mcp_server.py

Smoke-test without MCP transport:

    python mcp_server.py --smoke
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ModuleNotFoundError:  # Allows --smoke tests before MCP SDK is installed.
    Server = None
    stdio_server = None
    TextContent = None
    Tool = None


logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("aspen-pipeline-mcp")

SERVER_NAME = "aspen-pipeline-mcp"
MODE = "mcp_server"


def tool_envelope(tool_name: str, result: Any = None, ok: bool = True, error: str = "") -> Dict[str, Any]:
    return {
        "tool": tool_name,
        "mode": MODE,
        "ok": ok,
        "result": result,
        "error": error,
        "called_at": datetime.now().isoformat(timespec="seconds"),
    }


def as_text_content(payload: Dict[str, Any]) -> List[Any]:
    if TextContent is None:
        raise RuntimeError("MCP SDK is not installed; cannot create TextContent.")
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]


def load_yaml(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ===========================================================================
# Part1 tools (paper context, schema rules, schema precheck)
# ===========================================================================

def load_text(path: str, max_chars: int = 12000) -> str:
    return Path(path).read_text(encoding="utf-8", errors="ignore")[:max_chars]


def schema_path_get(obj: Any, parts: List[str]) -> Any:
    cur = obj
    for part in parts:
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def value_count(value: Any) -> Optional[int]:
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return None


def schema_quality_report(schema_obj: Dict[str, Any]) -> Dict[str, Any]:
    checks = {
        "property_method": ["aspen_schema", "property_method"],
        "components": ["aspen_schema", "components"],
        "blocks": ["flowsheet_schema", "blocks"],
        "streams": ["flowsheet_schema", "streams"],
        "connectivity_edges": ["flowsheet_schema", "connectivity", "edges"],
        "feeds": ["io", "feeds"],
        "port_policy": ["codegen", "port_policy"],
    }
    report: Dict[str, Any] = {}
    for name, path in checks.items():
        value = schema_path_get(schema_obj, path)
        ok = value is not None
        if name != "property_method":
            ok = ok and bool(value)
        item: Dict[str, Any] = {
            "ok": bool(ok),
            "canonical_path": ".".join(path),
            "canonical_present": value is not None,
            "found": value is not None,
            "found_path": ".".join(path) if value is not None else None,
        }
        count = value_count(value)
        if count is not None:
            item["count"] = count
        if name == "property_method" and value is not None:
            item["value"] = str(value)
        report[name] = item

    report["ok"] = all(v.get("ok") for v in report.values() if isinstance(v, dict))
    report["canonical_missing"] = [
        k for k, v in report.items() if isinstance(v, dict) and not v.get("ok")
    ]
    report["found_elsewhere"] = []
    return report


def validate_schema_for_execution(schema_obj: Dict[str, Any], schema_layer: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Lightweight server-side precheck.

    This mirrors the Part1 contract without trying to duplicate every notebook
    repair rule. The notebook can keep richer validators during the transition.
    """
    tasks: List[Dict[str, Any]] = []
    quality = schema_quality_report(schema_obj)
    for name in quality.get("canonical_missing", []):
        item = quality.get(name, {})
        tasks.append({
            "id": f"missing_{name}",
            "path": item.get("canonical_path"),
            "kind": "canonical_section",
            "required": True,
            "message": f"Missing required canonical schema section: {name}",
        })

    blocks = schema_path_get(schema_obj, ["flowsheet_schema", "blocks"]) or []
    streams = schema_path_get(schema_obj, ["flowsheet_schema", "streams"]) or []
    edges = schema_path_get(schema_obj, ["flowsheet_schema", "connectivity", "edges"]) or []
    block_ids = {b.get("id") for b in blocks if isinstance(b, dict)}
    stream_ids = {s.get("id") for s in streams if isinstance(s, dict)}

    for i, edge in enumerate(edges if isinstance(edges, list) else []):
        if not isinstance(edge, dict):
            continue
        stream = edge.get("stream")
        if stream not in stream_ids:
            tasks.append({
                "id": f"edge_{i}_unknown_stream",
                "path": f"flowsheet_schema.connectivity.edges[{i}].stream",
                "kind": "connectivity",
                "required": True,
                "message": f"Connectivity edge references undeclared stream: {stream}",
            })
        for side in ("from", "to"):
            endpoint = edge.get(side)
            if isinstance(endpoint, dict):
                block = endpoint.get("block")
                if block is not None and block not in block_ids:
                    tasks.append({
                        "id": f"edge_{i}_{side}_unknown_block",
                        "path": f"flowsheet_schema.connectivity.edges[{i}].{side}.block",
                        "kind": "connectivity",
                        "required": True,
                        "message": f"Connectivity edge references undeclared block: {block}",
                    })
    return tasks


def paper_context_tool(paper_text_path: str, max_chars: int = 12000) -> Dict[str, Any]:
    try:
        path = Path(paper_text_path)
        text = load_text(str(path), max_chars=max_chars) if path.exists() else ""
        return tool_envelope("paper_context_tool", {
            "paper_text_path": str(path),
            "paper_text_exists": path.exists(),
            "max_chars": max_chars,
            "text": text,
        })
    except Exception as exc:
        return tool_envelope("paper_context_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


def image_context_tool(paper_image_dir: str, paper_image_glob: str, paper_image_top_k: int = 4) -> Dict[str, Any]:
    return tool_envelope("image_context_tool", {
        "paper_image_dir": paper_image_dir,
        "paper_image_glob": paper_image_glob,
        "paper_image_top_k": paper_image_top_k,
    })


def schema_rule_tool(schema_layer_path: str) -> Dict[str, Any]:
    try:
        schema_layer = load_yaml(schema_layer_path)
        return tool_envelope("schema_rule_tool", {
            "schema_layer_path": schema_layer_path,
            "top_keys": list(schema_layer.keys()) if isinstance(schema_layer, dict) else [],
            "schema_layer": schema_layer,
        })
    except Exception as exc:
        return tool_envelope("schema_rule_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


def schema_validation_tool(
    schema_obj: Dict[str, Any],
    schema_layer: Optional[Dict[str, Any]] = None,
    schema_layer_path: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        if schema_layer is None and schema_layer_path:
            schema_layer = load_yaml(schema_layer_path)
        quality = schema_quality_report(schema_obj)
        missing = validate_schema_for_execution(schema_obj, schema_layer=schema_layer)
        return tool_envelope("schema_validation_tool", {
            "schema_quality": quality,
            "missing": missing,
            "n_missing": len(missing),
            "structural_error": "",
        })
    except Exception as exc:
        quality = schema_quality_report(schema_obj) if isinstance(schema_obj, dict) else {}
        return tool_envelope("schema_validation_tool", {
            "schema_quality": quality,
            "missing": [],
            "n_missing": 0,
            "structural_error": f"{type(exc).__name__}: {exc}",
        }, ok=False, error=f"{type(exc).__name__}: {exc}")


# ===========================================================================
# Part2 tools (schema context, COM catalog, validation/repair summaries)
# ===========================================================================

RUN_PHASES = ["property_method", "connectivity", "feeds", "block_specs"]

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

PHASE_FUNCTION_ALLOWLIST = {
    "property_method": {"set_property_method"},
    "connectivity": {"connect_stream", "disconnect_stream", "stream_add", "block_add"},
    "feeds": {
        "stream_set_temperature",
        "stream_set_pressure",
        "stream_set_flow_base",
        "stream_set_total_flow",
        "stream_set_basis",
        "stream_set_component_value",
        "stream_set_components",
        "stream_define",
    },
    "block_specs": {
        "heater_set_spec",
        "heater_set_temperature",
        "heater_set_pressure",
        "heater_set_duty",
        "heater_set_vapor_frac",
        "heater_config",
        "pump_set_type",
        "pump_set_spec",
        "pump_set_eff",
        "pump_set_nphase",
        "pump_configure",
        "rgibbs_set_opt_chemeq",
        "rgibbs_set_spec",
        "rgibbs_set_temperature",
        "rgibbs_set_pressure",
        "rgibbs_set_duty",
        "rgibbs_config",
        "rplug_set_type",
        "rplug_set_opt_tspec",
        "rplug_set_reactor_temp",
        "rplug_set_temp_profile",
        "rplug_set_multitube",
        "rplug_set_ntube",
        "rplug_set_length",
        "rplug_set_diameter",
        "rplug_set_phase",
        "rplug_set_pressure",
        "rplug_set_pressure_drop",
        "rplug_set_catalyst_presence",
        "rplug_set_catalyst_props",
        "mheatx_set_outlet_stream",
        "mheatx_set_spec",
        "mheatx_set_value",
        "mheatx_configure",
        "flash_set_spec",
        "flash_set_temperature",
        "flash_set_pressure",
        "flash_set_duty",
        "flash_set_vapor_frac",
        "flash_config",
        "splt_set_frac",
        "comp_set_model_type",
        "comp_set_type",
        "comp_set_spec",
        "comp_set_eff",
        "comp_set_nphase",
        "comp_configure",
        "radfrac_set_calc_mode",
        "radfrac_set_nstage",
        "radfrac_set_condenser",
        "radfrac_set_reboiler",
        "radfrac_set_no_phase",
        "radfrac_set_spec",
        "radfrac_set_feed_stage",
        "radfrac_set_stage_pressure",
        "radfrac_configure",
    },
}


def schema_flowsheet(schema_obj: Dict[str, Any]) -> Dict[str, Any]:
    return (schema_obj or {}).get("flowsheet_schema") or (schema_obj or {}).get("flowsheet") or {}


def schema_io(schema_obj: Dict[str, Any]) -> Dict[str, Any]:
    return (schema_obj or {}).get("io") or (schema_obj or {}).get("io_schema") or {}


def block_type_map(schema_obj: Dict[str, Any]) -> Dict[str, str]:
    out = {}
    for block in schema_flowsheet(schema_obj).get("blocks", []) or []:
        if isinstance(block, dict) and block.get("id"):
            out[str(block["id"])] = str(block.get("type", "")).upper()
    return out


def edge_with_ports(edge: Dict[str, Any], block_types: Dict[str, str]) -> Dict[str, Any]:
    item = dict(edge or {})
    for side in ("from", "to"):
        endpoint = item.get(side)
        if not isinstance(endpoint, dict):
            continue
        role = str(endpoint.get("port_role") or "").upper()
        block = endpoint.get("block")
        endpoint = dict(endpoint)
        endpoint["aspen_port"] = PORT_ROLE_TO_ASPEN_PORT.get(role)
        endpoint["block_type"] = block_types.get(str(block)) if block else None
        item[side] = endpoint
    return item


def schema_phase_context(phase: str, schema_obj: Dict[str, Any]) -> Dict[str, Any]:
    phase = str(phase).lower().strip()
    fs = schema_flowsheet(schema_obj)
    io = schema_io(schema_obj)
    block_types = block_type_map(schema_obj)

    if phase == "property_method":
        return {
            "property_method": ((schema_obj or {}).get("aspen_schema") or {}).get("property_method"),
            "instruction": "Set only the Aspen property method from aspen_schema.property_method.",
        }
    if phase == "connectivity":
        return {
            "blocks": fs.get("blocks", []) or [],
            "streams": fs.get("streams", []) or [],
            "expected_edges": [
                edge_with_ports(edge, block_types)
                for edge in ((fs.get("connectivity") or {}).get("edges", []) or [])
                if isinstance(edge, dict)
            ],
            "port_role_to_aspen_port": PORT_ROLE_TO_ASPEN_PORT,
            "instruction": "Use expected_edges and aspen_port values; do not invent block or stream IDs.",
        }
    if phase == "feeds":
        feed_streams = {str((feed or {}).get("stream")) for feed in (io.get("feeds", []) or []) if isinstance(feed, dict)}
        return {
            "feed_specs": io.get("feeds", []) or [],
            "feed_edges": [
                edge_with_ports(edge, block_types)
                for edge in ((fs.get("connectivity") or {}).get("edges", []) or [])
                if isinstance(edge, dict) and str(edge.get("stream")) in feed_streams
            ],
            "instruction": "Set only feed stream input values from feed_specs.",
        }
    if phase == "block_specs":
        return {
            "blocks": fs.get("blocks", []) or [],
            "instruction": "Set only block specifications from blocks[].specs.",
        }
    return {"schema": schema_obj}


def walk_catalog_items(node: Any):
    if isinstance(node, dict):
        if isinstance(node.get("name"), str):
            yield node
        for value in node.values():
            yield from walk_catalog_items(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_catalog_items(value)


def filter_catalog_by_functions(catalog: Any, allowed: Set[str]) -> Any:
    if isinstance(catalog, dict):
        if isinstance(catalog.get("name"), str):
            return catalog if catalog.get("name") in allowed else None
        out = {}
        for key, value in catalog.items():
            filtered = filter_catalog_by_functions(value, allowed)
            if filtered not in (None, {}, []):
                out[key] = filtered
        return out
    if isinstance(catalog, list):
        out = [filter_catalog_by_functions(value, allowed) for value in catalog]
        return [value for value in out if value not in (None, {}, [])]
    return catalog


def normalize_issues(validation_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    issues = []
    for validator_name, result in ((validation_result or {}).get("bundle", {}) or {}).items():
        for item in (result or {}).get("issues", []) or []:
            if isinstance(item, dict):
                normalized = dict(item)
                normalized["validator"] = validator_name
                issues.append(normalized)
    return issues


def compact_phase_result(phase_result: Dict[str, Any]) -> Dict[str, Any]:
    report = phase_result.get("schema_repair") or {}
    last = phase_result.get("last") or {}
    return {
        "phase": phase_result.get("phase"),
        "ok": bool(phase_result.get("ok")),
        "schema_repair_ok": report.get("ok"),
        "schema_repair_actions": len(report.get("actions") or []),
        "schema_repair_errors": len(report.get("errors") or []),
        "validation": last.get("summary", {}),
    }


def part2_config_tool(part2_input: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = dict(part2_input or {})
    return tool_envelope("part2_config_tool", {
        "run_phases": RUN_PHASES,
        "repair_max_iter": int(config.get("repair_max_iter", 3)),
        "has_schema_input_path": bool(config.get("schema_input_path")),
        "has_working_dir": bool(config.get("working_dir")),
        "known_paths": {k: v for k, v in config.items() if k.endswith("_path") or k.endswith("_dir")},
    })


def schema_context_tool(schema_obj: Optional[Dict[str, Any]] = None, schema_path: str = "", phase: str = "") -> Dict[str, Any]:
    try:
        if schema_obj is None:
            schema_obj = load_yaml(schema_path)
        phases = [phase] if phase else RUN_PHASES
        return tool_envelope("schema_context_tool", {
            "schema_path": schema_path,
            "phases": phases,
            "contexts": {ph: schema_phase_context(ph, schema_obj or {}) for ph in phases},
        })
    except Exception as exc:
        return tool_envelope("schema_context_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


def com_catalog_tool(db_com_path: str, phase: str = "") -> Dict[str, Any]:
    try:
        catalog = load_yaml(db_com_path)
        phases = [phase] if phase else RUN_PHASES
        result = {}
        for ph in phases:
            allowed = PHASE_FUNCTION_ALLOWLIST.get(ph, set())
            subset = filter_catalog_by_functions(catalog, allowed)
            found = sorted({item.get("name") for item in walk_catalog_items(subset) if isinstance(item, dict) and item.get("name")})
            result[ph] = {
                "allowed_functions": sorted(allowed),
                "found_functions": found,
                "missing_functions": sorted(allowed - set(found)),
                "catalog_subset": subset,
            }
        return tool_envelope("com_catalog_tool", {
            "db_com_path": db_com_path,
            "phases": phases,
            "phase_catalogs": result,
        })
    except Exception as exc:
        return tool_envelope("com_catalog_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


def validation_summary_tool(validation_result: Dict[str, Any], phase: str = "") -> Dict[str, Any]:
    try:
        issues = normalize_issues(validation_result or {})
        return tool_envelope("validation_summary_tool", {
            "phase": phase or (validation_result or {}).get("summary", {}).get("phase", ""),
            "ok": bool((validation_result or {}).get("ok")),
            "n_issues": len(issues),
            "issues_by_validator": (validation_result or {}).get("summary", {}).get("issues_by_validator", {}),
            "issues": issues,
        })
    except Exception as exc:
        return tool_envelope("validation_summary_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


def repair_assessment_tool(
    phase_results: Optional[List[Dict[str, Any]]] = None,
    final_validation: Optional[Dict[str, Any]] = None,
    run_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    try:
        phase_results = phase_results or []
        final_validation = final_validation or {}
        run_status = run_status or {}
        compact = [compact_phase_result(item) for item in phase_results]
        failed = [item for item in compact if not item.get("ok")]
        final_ok = bool(final_validation.get("ok"))
        run_executed = bool(run_status.get("run_executed"))
        needs_human_review = bool(failed) or (not final_ok)
        decision_hint = "enough" if final_ok and run_executed and not failed else "needs_human_review"
        return tool_envelope("repair_assessment_tool", {
            "decision_hint": decision_hint,
            "needs_human_review": needs_human_review,
            "phase_results": compact,
            "failed_phases": [item.get("phase") for item in failed],
            "final_validation_summary": final_validation.get("summary", {}),
            "run_status": run_status,
        })
    except Exception as exc:
        return tool_envelope("repair_assessment_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


# ===========================================================================
# Part3 tools (final repair-cycle assessment)
# ===========================================================================

def part3_assessment_tool(part3_state: Dict[str, Any], part3_result: Dict[str, Any]) -> Dict[str, Any]:
    try:
        part3_state = part3_state or {}
        part3_result = part3_result or {}
        counts = part3_state.get("parse_counts") or {}
        rerun = part3_state.get("rerun_summary") or {}
        issues = part3_state.get("issues") or []
        severe = int(counts.get("severe") or 0)
        errors = int(counts.get("errors") or 0)
        rerun_status = str(rerun.get("status") or "")
        success = bool(part3_result.get("success")) or rerun_status == "NO_ISSUE" or (severe == 0 and errors == 0)
        needs_human_review = (not success) and bool(issues or rerun_status)
        return tool_envelope("part3_assessment_tool", {
            "success": success,
            "needs_human_review": needs_human_review,
            "decision_hint": "write_result" if success else "needs_human_review",
            "parse_counts": counts,
            "rerun_status": rerun_status,
            "issue_count": len(issues),
            "final_bkp_path": part3_result.get("final_bkp_path") or part3_state.get("updated_bkp_path") or "",
        })
    except Exception as exc:
        return tool_envelope("part3_assessment_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


# ===========================================================================
# Shared server plumbing
# ===========================================================================

def register_tools(server: Any) -> None:
    if Tool is None:
        raise RuntimeError("MCP SDK is not installed; cannot register tools.")

    @server.list_tools()
    async def list_tools() -> List[Tool]:
        return [
            # --- Part1 ---
            Tool(
                name="paper_context_tool",
                description="Return paper text context for Part1 schema drafting.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "paper_text_path": {"type": "string"},
                        "max_chars": {"type": "integer", "default": 12000},
                    },
                    "required": ["paper_text_path"],
                },
            ),
            Tool(
                name="image_context_tool",
                description="Return image discovery hints for figure-aware Part1 generation.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "paper_image_dir": {"type": "string"},
                        "paper_image_glob": {"type": "string"},
                        "paper_image_top_k": {"type": "integer", "default": 4},
                    },
                    "required": ["paper_image_dir", "paper_image_glob"],
                },
            ),
            Tool(
                name="schema_rule_tool",
                description="Load schema-layer rules from YAML.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "schema_layer_path": {"type": "string"},
                    },
                    "required": ["schema_layer_path"],
                },
            ),
            Tool(
                name="schema_validation_tool",
                description="Run Part1 schema quality and lightweight execution precheck.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "schema_obj": {"type": "object"},
                        "schema_layer": {"type": "object"},
                        "schema_layer_path": {"type": "string"},
                    },
                    "required": ["schema_obj"],
                },
            ),
            # --- Part2 ---
            Tool(
                name="part2_config_tool",
                description="Return normalized Part2 run phases and path metadata.",
                inputSchema={"type": "object", "properties": {"part2_input": {"type": "object"}}},
            ),
            Tool(
                name="schema_context_tool",
                description="Return phase-specific schema context for Part2 repair.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "schema_obj": {"type": "object"},
                        "schema_path": {"type": "string"},
                        "phase": {"type": "string"},
                    },
                },
            ),
            Tool(
                name="com_catalog_tool",
                description="Return phase-filtered Aspen COM helper catalog.",
                inputSchema={
                    "type": "object",
                    "properties": {"db_com_path": {"type": "string"}, "phase": {"type": "string"}},
                    "required": ["db_com_path"],
                },
            ),
            Tool(
                name="validation_summary_tool",
                description="Normalize a Part2 validation result into issue summaries.",
                inputSchema={
                    "type": "object",
                    "properties": {"validation_result": {"type": "object"}, "phase": {"type": "string"}},
                    "required": ["validation_result"],
                },
            ),
            Tool(
                name="repair_assessment_tool",
                description="Summarize phase repair and final validation status for graph decisions.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "phase_results": {"type": "array"},
                        "final_validation": {"type": "object"},
                        "run_status": {"type": "object"},
                    },
                },
            ),
            # --- Part3 ---
            Tool(
                name="part3_assessment_tool",
                description="Summarize Part3 parse/rerun state and result into a human-review decision hint.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "part3_state": {"type": "object"},
                        "part3_result": {"type": "object"},
                    },
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: Any) -> List[TextContent]:
        arguments = arguments or {}
        try:
            # --- Part1 ---
            if name == "paper_context_tool":
                return as_text_content(paper_context_tool(
                    paper_text_path=arguments["paper_text_path"],
                    max_chars=int(arguments.get("max_chars", 12000)),
                ))
            if name == "image_context_tool":
                return as_text_content(image_context_tool(
                    paper_image_dir=arguments["paper_image_dir"],
                    paper_image_glob=arguments["paper_image_glob"],
                    paper_image_top_k=int(arguments.get("paper_image_top_k", 4)),
                ))
            if name == "schema_rule_tool":
                return as_text_content(schema_rule_tool(
                    schema_layer_path=arguments["schema_layer_path"],
                ))
            if name == "schema_validation_tool":
                return as_text_content(schema_validation_tool(
                    schema_obj=arguments["schema_obj"],
                    schema_layer=arguments.get("schema_layer"),
                    schema_layer_path=arguments.get("schema_layer_path"),
                ))
            # --- Part2 ---
            if name == "part2_config_tool":
                return as_text_content(part2_config_tool(arguments.get("part2_input")))
            if name == "schema_context_tool":
                return as_text_content(schema_context_tool(
                    schema_obj=arguments.get("schema_obj"),
                    schema_path=arguments.get("schema_path", ""),
                    phase=arguments.get("phase", ""),
                ))
            if name == "com_catalog_tool":
                return as_text_content(com_catalog_tool(arguments["db_com_path"], phase=arguments.get("phase", "")))
            if name == "validation_summary_tool":
                return as_text_content(validation_summary_tool(arguments["validation_result"], phase=arguments.get("phase", "")))
            if name == "repair_assessment_tool":
                return as_text_content(repair_assessment_tool(
                    phase_results=arguments.get("phase_results"),
                    final_validation=arguments.get("final_validation"),
                    run_status=arguments.get("run_status"),
                ))
            # --- Part3 ---
            if name == "part3_assessment_tool":
                return as_text_content(part3_assessment_tool(
                    arguments.get("part3_state"),
                    arguments.get("part3_result"),
                ))
            raise ValueError(f"Unknown tool: {name}")
        except Exception as exc:
            logger.exception("Tool call failed: %s", name)
            return as_text_content(tool_envelope(name, ok=False, error=f"{type(exc).__name__}: {exc}"))


async def run_server() -> None:
    if Server is None or stdio_server is None:
        raise RuntimeError("MCP SDK is not installed. Install package 'mcp' before running the server.")
    server = Server(SERVER_NAME)
    register_tools(server)
    logger.info("Starting %s", SERVER_NAME)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def smoke_test() -> None:
    # Diagnostic only: reads sample data from the shared data directory (see paths.py).
    # Real tool calls always receive absolute paths from their caller.
    from paths import data_dir, papers_dir, shared_file
    root = data_dir()

    # --- Part1 ---
    schema_path = shared_file("Schema_v5.yaml")
    paper_path = papers_dir() / "paper2" / "paper_h2.md"
    print(json.dumps(paper_context_tool(str(paper_path), max_chars=200), ensure_ascii=False, indent=2))
    print(json.dumps(image_context_tool(str(paper_path.parent), str(paper_path.parent / "*.jpg"), 4), ensure_ascii=False, indent=2))
    rules = schema_rule_tool(str(schema_path))
    print(json.dumps({k: v for k, v in rules.items() if k != "result"}, ensure_ascii=False, indent=2))
    minimal_schema = {
        "aspen_schema": {"property_method": "RK-SOAVE", "components": [{"aspen_id": "METHANE"}]},
        "flowsheet_schema": {"blocks": [{"id": "B1"}], "streams": [{"id": "S1"}], "connectivity": {"edges": []}},
        "io": {"feeds": [{"stream": "S1"}]},
        "codegen": {"port_policy": {"HEATER": {"IN": ["IN"], "OUT": ["OUT"]}}},
    }
    print(json.dumps(schema_validation_tool(minimal_schema, schema_layer_path=str(schema_path)), ensure_ascii=False, indent=2))

    # --- Part2 ---
    db_com_path = shared_file("aspen_code_v3.yaml")
    print(json.dumps(part2_config_tool({"schema_input_path": str(schema_path), "working_dir": str(root / "run(test)")}), ensure_ascii=False, indent=2))
    print(json.dumps(schema_context_tool(schema_path=str(schema_path), phase="connectivity"), ensure_ascii=False, indent=2)[:2500])
    print(json.dumps(com_catalog_tool(str(db_com_path), phase="feeds"), ensure_ascii=False, indent=2)[:2500])
    sample_validation = {"ok": False, "summary": {"phase": "feeds"}, "bundle": {"feeds": {"issues": [{"target": "S1", "message": "missing"}]}}}
    print(json.dumps(validation_summary_tool(sample_validation, phase="feeds"), ensure_ascii=False, indent=2))
    print(json.dumps(repair_assessment_tool([], {"ok": True, "summary": {"n_issues": 0}}, {"run_executed": True}), ensure_ascii=False, indent=2))

    # --- Part3 ---
    clean_state = {"parse_counts": {"severe": 0, "errors": 0, "warnings": 0}, "issues": [], "rerun_summary": {}}
    clean_result = {"success": True}
    print(json.dumps(part3_assessment_tool(clean_state, clean_result), ensure_ascii=False, indent=2))
    dirty_state = {
        "parse_counts": {"severe": 1, "errors": 2, "warnings": 0},
        "issues": [{"target": "B1", "message": "convergence failure"}],
        "rerun_summary": {"status": "ISSUE_REMAINS"},
    }
    dirty_result = {"success": False}
    print(json.dumps(part3_assessment_tool(dirty_state, dirty_result), ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true", help="Run local smoke test without MCP transport.")
    args = parser.parse_args()
    if args.smoke:
        smoke_test()
    else:
        asyncio.run(run_server())


if __name__ == "__main__":
    main()
