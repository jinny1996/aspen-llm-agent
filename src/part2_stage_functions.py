"""Part2 stage logic, refactored out of part2_v1.ipynb's inline cells into
real functions (Phase C2, 2026-09-02 -- gradio-chatbot-architecture-plan item C),
mirroring part1_stage_functions.py/part3_stage_functions.py.

Behavior is preserved from the original notebook cells -- this is a
restructuring, not a rewrite. Part2 is the most complex of the three parts to
extract because of its Aspen COM `doc` lifecycle:
  - `doc` is created inside _execute_com_skeleton() (cell 14's old body) as a
    side effect of exec'ing generated com_skeleton.py statements into this
    module's own globals() -- exec(..., globals(), globals()) always uses the
    globals of whatever module the executing function is DEFINED in, so this
    works correctly once relocated here, exactly as it did in the notebook.
  - `doc` is read (not reassigned) by most repair/validation functions.
  - `doc` is closed and REOPENED inside the fixed _run_save_and_check()
    (ported from the notebook's cell 30, which used to monkeypatch a cell-24
    stub after the fact -- a module has no such run-order dependency, so the
    fixed version is just written once, directly, from the start).
  - `doc` is finally closed in write_result_node().
Every doc-touching call MUST happen on the same thread that first created it
(see agent_loop_utils.py's docstring on why the tool-calling loop is a plain
while loop, not LangGraph's create_react_agent).

Design choice (confirmed against the notebook, not assumed): unlike
Part1/Part3, Part2's cross-cutting state (`doc`, `schema`, `part2_state`,
`part2_loop_status`, `com_completion_saved`, `action_plan`, ...) was NEVER
part of `Part2GraphState`/`PART2_SESSION` in the original notebook -- it was
always bare notebook-kernel globals, read/written by many functions across
many cells. Rather than forcing all of that into a "session" dict (which
would mean rewriting every one of those reads across ~1500 lines), this
module keeps the exact same architecture, just relocated: these become real
module-level globals here, set once by build_session() (mirroring
part1_stage_functions.py's simpler TOKEN_USAGE_PATH-style module global, just
with many more names). `session["state"]` is `Part2GraphState` (renamed
`PART2_SESSION` in the notebook, exactly as before) and node functions still
take a plain `state` dict and return an updated one -- their signatures are
completely unchanged from the notebook, right down to already having Phase
B's `human_answer` parameter on human_review_node.

session shape:
    session = {
        "config": {...},   # snapshot of the same values also set as module
                            # globals below, for external inspection
        "state": {...},    # Part2GraphState -- exactly the notebook's
                            # PART2_SESSION, unchanged
        "llm_model": "...", "llm_temperature": 1,
        "part2_component_resolver_model": "...", ... (5 more per-stage models),
        "token_usage_path": "...", "agent_events_path": "...",
    }
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

try:
    from typing import TypedDict
except ImportError:
    from typing_extensions import TypedDict

import win32com.client  # kept for parity with the notebook's own cell 2 import

from agent_loop_utils import HumanInputRequired
from experiment_logging import token_usage
from part3_patch_safety import _walk_catalog_items, filter_catalog_by_functions, build_fn_index, safety_check_patchplan

from paths import data_dir, shared_dirs, shared_file

_HERE = Path(__file__).resolve().parent
_PROJECT_ROOT = data_dir()
for _p in (*shared_dirs(), _HERE):
    _p_str = str(_p)
    if _p_str not in sys.path:
        sys.path.insert(0, _p_str)

# aspen_code_v1.py / valid_*.py live in the shared dirs (paths.shared_dirs()) -- must be importable
# before these run. importlib.reload() calls the notebook made right after
# each `import ... as _x_repair_mod` (cells 24) are dropped here: they were
# always a no-op in that position (nothing could invalidate the cache between
# an import and the reload two lines later), and are a genuine no-op here too
# for the same reason -- not a behavior change, just dropping dead motion.
from aspen_code_v1 import *
from valid_common import *
from valid_connectivity import run as run_connectivity
from valid_feed import run as run_feeds
import valid_block_specification as vbs
from valid_block_specification import run as run_block_specs
from valid_property_schema_repair import validate_property_method_from_schema
from valid_property_schema_repair import repair_property_method_from_schema
from valid_connectivity_schema_repair import repair_connectivity_from_schema
from valid_feed_schema_repair import repair_feeds_from_schema
from valid_block_schema_repair import repair_block_specs_from_schema


# ---------------------------------------------------------------------------
# fix: mass-basis total flow was silently entered as kmol/hr.
#
# aspen_code_v1.stream_set_total_flow() ignores its `base` argument and only does
# `node.Value = total_flow`, so the number lands in the node's current unit (kmol/hr).
# Probed against Aspen V14 via COM: with FLOWBASE='MASS' set, UnitString stays 'kmol/hr' and
# the exported INP says MOLE-FLOW=<same number>; SetValueUnitAndBasis(value, 3, 'MASS') gives
# UnitString 'kg/hr' and MASS-FLOW=<value>.  Defined here (not in the shared aspen_code_v1.py)
# so only phase13 changes; generated COM code runs in this module's globals().
# ---------------------------------------------------------------------------
_KG_PER_HR_UNIT_CODE = 3          # kg/hr in the default unit set; verified at runtime below


def _is_kg_per_hr(unit_string) -> bool:
    return str(unit_string).strip().lower().replace(" ", "") in {"kg/hr", "kg/h"}


def stream_set_total_flow(doc, stream_name, total_flow, base="MOLE"):
    """Set total flow.  MOLE keeps the old behaviour; MASS sets an explicit kg/hr unit
    and raises if the unit did not take (never silently falls back to kmol/hr)."""
    node = doc.Tree.FindNode(rf"\Data\Streams\{stream_name}\Input\TOTFLOW\MIXED")
    if node is None:
        raise RuntimeError(f"TOTFLOW node not found for stream {stream_name!r}")
    if str(base).strip().upper() != "MASS":
        node.Value = total_flow
        return
    for code in (_KG_PER_HR_UNIT_CODE, *range(0, 20)):
        node.SetValueUnitAndBasis(float(total_flow), code, "MASS")
        if _is_kg_per_hr(node.UnitString):
            break
    else:
        raise RuntimeError(f"could not set a kg/hr unit on TOTFLOW of stream {stream_name!r}")
    if abs(float(node.Value) - float(total_flow)) > 1e-9 * max(1.0, abs(float(total_flow))):
        raise RuntimeError(f"TOTFLOW of {stream_name!r} reads {node.Value}, expected {total_flow}")


import aspen_code_v1 as _aspen_code_v1_mod
_aspen_code_v1_mod.stream_set_total_flow = stream_set_total_flow   # helpers inside that module use it too

# the schema block type COMPRESSOR is not an Aspen block type (Aspen rejects block_add(..., "COMPRESSOR") with
# "Invalid Block/Model/Library Specification"; verified on this PC with test_aspen_block_types.py: Compr / COMPR / MCompr are
# accepted, Compressor / COMPRESSOR are not).  "COMPRESSOR" stays the right value of the block's MODEL_TYPE option, which
# comp_set_model_type sets after the block exists.  Defined here, not in the shared aspen_code_v1.py (see the note above).
_orig_normalize_block_type = _aspen_code_v1_mod._normalize_block_type


def _normalize_block_type(btype):
    if str(btype or "").strip().upper() in {"COMPRESSOR", "COMPR"}:
        return "Compr"
    return _orig_normalize_block_type(btype)


_aspen_code_v1_mod._normalize_block_type = _normalize_block_type    # block_add looks it up in its module at call time

# valid_feed_schema_repair did `from aspen_code_v1 import stream_set_total_flow` at import time (above),
# so it kept the OLD function; its repair path (line ~365) would re-introduce the kmol/hr error.
import valid_feed_schema_repair as _valid_feed_schema_repair_mod
_valid_feed_schema_repair_mod.stream_set_total_flow = stream_set_total_flow


# ---------------------------------------------------------------------------
# post-build read-back of the feed total-flow unit.
# The unit bug above passed every validator because they only look at "is a value present".
# This compares the unit FAMILY (mass vs mole) that Aspen actually holds with the schema's flow
# basis.  A mismatch is corrected once with the fixed setter and re-read; if it still differs it
# becomes a validation issue.  Only the unit family is judged; value differences are reported
# as information only (repair loops may legitimately change values).
# ---------------------------------------------------------------------------
def _unit_is_mole(unit_string) -> bool:
    return "mol" in str(unit_string).lower()


def _schema_flow_basis(specs: dict) -> Optional[str]:
    flow = specs.get("flow") if isinstance(specs, dict) else None
    fb = str(flow.get("basis") or "").strip().upper() if isinstance(flow, dict) else ""
    if fb in {"MASS", "MOLE"}:
        return fb
    b = str((specs or {}).get("basis") or "").strip().upper()
    if b.startswith("MASS"):
        return "MASS"
    if b.startswith("MOLE"):
        return "MOLE"
    return None


def check_feed_flow_units(doc, schema_obj: dict, fix: bool = True) -> Dict[str, Any]:
    """-> {"checked": [...], "mismatches": [...], "fixed": [...]} (never raises for one stream)."""
    out: Dict[str, Any] = {"checked": [], "mismatches": [], "fixed": []}
    for f in _iter_feeds(schema_obj):
        specs = f.get("specs") if isinstance(f.get("specs"), dict) else {}
        sname = str(f.get("stream") or "").strip()
        basis = _schema_flow_basis(specs)
        flow = specs.get("flow")
        value = _to_float_or_none(flow)
        if not sname or basis is None or value is None:
            continue
        try:
            node = doc.Tree.FindNode(rf"\Data\Streams\{sname}\Input\TOTFLOW\MIXED")
            if node is None:
                out["mismatches"].append({"stream": sname, "reason": "TOTFLOW node not found",
                                          "schema_basis": basis})
                continue
            unit = str(node.UnitString)
            row = {"stream": sname, "schema_basis": basis, "aspen_unit": unit,
                   "schema_value": value, "aspen_value": node.Value}
            out["checked"].append(row)
            if _unit_is_mole(unit) == (basis == "MOLE"):
                continue
            if fix:
                try:
                    stream_set_total_flow(doc, sname, value, base=basis)
                    unit_after = str(node.UnitString)
                    if _unit_is_mole(unit_after) == (basis == "MOLE"):
                        out["fixed"].append({**row, "aspen_unit_after": unit_after})
                        continue
                except Exception as exc:
                    row = {**row, "fix_error": repr(exc)}
            out["mismatches"].append({**row, "reason": "unit family differs from schema flow basis"})
        except Exception as exc:
            out["mismatches"].append({"stream": sname, "reason": f"read-back failed: {exc!r}",
                                      "schema_basis": basis})
    return out


def _with_feed_unit_check(doc, result: Dict[str, Any]) -> Dict[str, Any]:
    """Attach the unit read-back to a `feeds` validator result; mismatches become issues."""
    try:
        chk = check_feed_flow_units(doc, schema)
    except Exception as exc:
        result["unit_check"] = {"error": repr(exc)}
        return result
    result["unit_check"] = chk
    if chk["checked"] and not chk["fixed"] and not chk["mismatches"]:
        print(f"[PART2] feed unit check: {len(chk['checked'])} feed(s) OK "
              f"({', '.join(r['stream'] + '=' + r['aspen_unit'] for r in chk['checked'])})")
    for fx in chk["fixed"]:
        print(f"[PART2] feed unit check: {fx['stream']} was {fx['aspen_unit']} but schema basis is "
              f"{fx['schema_basis']} -> corrected to {fx['aspen_unit_after']}")
    for mm in chk["mismatches"]:
        issue = {
            "category": "FEED_UNIT", "target_type": "STREAM", "target": mm["stream"],
            "rule_id": "feed_flow_unit_mismatch",
            "message": f"total-flow unit {mm.get('aspen_unit')!r} does not match schema flow basis "
                       f"{mm.get('schema_basis')} ({mm.get('reason')})",
            "path": rf"\Data\Streams\{mm['stream']}\Input\TOTFLOW\MIXED",
            "current": mm.get("aspen_unit"),
            "required": "mass unit (e.g. kg/hr)" if mm.get("schema_basis") == "MASS" else "mole unit (e.g. kmol/hr)",
            "detail": mm,
        }
        result.setdefault("issues", []).append(issue)
        result["ok"] = False
        if isinstance(result.get("summary"), dict):
            result["summary"]["n_issues"] = int(result["summary"].get("n_issues", 0) or 0) + 1
        print(f"[PART2] feed unit check FAILED for {mm['stream']}: {issue['message']}")
    return result


# ===========================================================================
# Module-level shared state.
# Defaults here matter only for a pre-build_session() import; build_session()
# always overwrites them for a real run, and _reset_module_state() clears
# them at the start of every build_session() call (a fresh notebook kernel
# used to give this for free; a long-lived module -- e.g. a future chat
# process building multiple sessions -- needs it done explicitly).
# ===========================================================================
doc = None
schema: Dict[str, Any] = {}
aspen_code: Dict[str, Any] = {}
function_db: Dict[str, Any] = {}
function_db_json: str = "{}"
action_plan: Optional[Dict[str, Any]] = None
action_plan_staged: Optional[Dict[str, Any]] = None
resolved_out_path: Optional[str] = None
part2_state: Dict[str, Any] = {"ok": True, "errors": []}
com_initial_saved: bool = False
com_completion_saved: bool = False
v_val: Dict[str, Any] = {"ok": False, "n_issues": 0, "bundle": {}, "summary": {"note": "phase-aware validation deferred to repair loop"}}
issues_norm: List[Dict[str, Any]] = []
RUN_PHASES = ["property_method", "connectivity", "feeds", "block_specs"]
PHASE_MAX_ITER = 3  # build_session() overrides from part2_input.repair_max_iter
ENABLE_COMPLETION = True
PRINT_SCHEMA_REPAIR_DETAIL = False
PRINT_PATCHPLAN_JSON = False
PRINT_PHASE_RESULTS_JSON = False
part2_loop_status: Dict[str, Any] = {
    "attempts": 0, "run_executed": False, "his_path": "", "his_exists": False,
    "his_nonempty": False, "counts_reliable": False, "error_count": None,
    "warning_count": None, "severe_blocks": None, "error_blocks": None,
    "warning_blocks": None, "reason": "", "phase": "",
}
part2_hitl_mode = os.environ.get("PART2_HITL_MODE", "bypass").strip().lower()
part2_token_usage_path: Optional[str] = None
part2_agent_events_path: Optional[str] = None


def _reset_module_state() -> None:
    """Zero out cross-cutting module globals at the start of every
    build_session() call -- see the module docstring for why this is needed
    now that this code can live in one long-running process instead of a
    fresh-per-run notebook kernel."""
    global doc, schema, aspen_code, function_db, function_db_json
    global action_plan, action_plan_staged, resolved_out_path
    global part2_state, com_initial_saved, com_completion_saved
    global v_val, issues_norm, part2_loop_status
    doc = None
    schema = {}
    aspen_code = {}
    function_db = {}
    function_db_json = "{}"
    action_plan = None
    action_plan_staged = None
    resolved_out_path = None
    part2_state = {"ok": True, "errors": []}
    com_initial_saved = False
    com_completion_saved = False
    v_val = {"ok": False, "n_issues": 0, "bundle": {}, "summary": {"note": "phase-aware validation deferred to repair loop"}}
    issues_norm = []
    part2_loop_status = {
        "attempts": 0, "run_executed": False, "his_path": "", "his_exists": False,
        "his_nonempty": False, "counts_reliable": False, "error_count": None,
        "warning_count": None, "severe_blocks": None, "error_blocks": None,
        "warning_blocks": None, "reason": "", "phase": "",
    }




# ===========================================================================
# MCP gating (cell 22, moved into the module -- Part1/Part3 pattern)
# ===========================================================================
# MCP adapter layer
# Default to local adapters while developing. Switch to "mcp_client" for integration checks.
import subprocess
import time

# Default local_adapter; export MCP_ADAPTER_MODE=mcp_client to exercise the real
# MCP stdio path for integration checks.
MCP_ADAPTER_MODE = os.environ.get("MCP_ADAPTER_MODE", "local_adapter").strip().lower()
MCP_CLIENT_FALLBACK_TO_LOCAL = True
# Diagnostic-only: records why a real MCP call fell back to local_adapter (the
# print below is easy to miss under nbconvert, this survives into part2_result).
mcp_fallback_log: List[Dict[str, Any]] = []


def mcp_tool_result(tool_name: str, result: Any = None, ok: bool = True, error: str = "", mode: Optional[str] = None) -> Dict[str, Any]:
    return {
        "tool": tool_name,
        "mode": mode or MCP_ADAPTER_MODE,
        "ok": bool(ok),
        "result": result or {},
        "error": error or "",
        "called_at": datetime.now().isoformat(timespec="seconds"),
    }


def mcp_client_tool_call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    helper_path = _HERE / "mcp_call_tool.py"
    if not helper_path.exists():
        return mcp_tool_result(tool_name, ok=False, error=f"MCP helper script not found: {helper_path}", mode="mcp_client")
    args_path = Path(working_dir) / f"part2_mcp_args_{tool_name}.json"
    out_path = Path(working_dir) / f"part2_mcp_out_{tool_name}.json"
    # Root-caused: the
    # child process always completes and flushes its stdout print successfully,
    # but the parent's subprocess.run(capture_output=True) intermittently lost
    # the piped stdout anyway for some tools when spawned from inside a busy
    # nbconvert-launched Jupyter kernel (retries and PYTHONUNBUFFERED=1 did not
    # help -- the child was never the problem). Fix: have the child write its
    # result to a plain file and read that instead of proc.stdout, which has no
    # such pipe race. proc.stdout is kept only as a last-resort fallback.
    max_attempts = 2
    last_error = ""
    try:
        args_path.write_text(json.dumps(arguments, ensure_ascii=False), encoding="utf-8")
        for attempt in range(1, max_attempts + 1):
            try:
                out_path.unlink()
            except FileNotFoundError:
                pass
            proc = subprocess.run(
                [sys.executable, str(helper_path), tool_name, "--arguments-file", str(args_path), "--output-file", str(out_path)],
                cwd=str(_HERE.parent),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                text=True,
                encoding="utf-8",  # without this,
                # subprocess.run() decodes the child's stdout/stderr with
                # locale.getpreferredencoding() (cp949 on this Korean Windows
                # setup), which raises UnicodeDecodeError on non-ASCII bytes
                # and silently forces a fallback to local_adapter in some
                # shells. Part1's mcp_client_tool_call already had this fixed.
                capture_output=True,
                check=False,
                timeout=60,
            )
            if proc.returncode != 0:
                last_error = proc.stderr or proc.stdout or "MCP helper failed"
                time.sleep(0.5)
                continue
            raw = out_path.read_text(encoding="utf-8") if out_path.exists() else (proc.stdout or "")
            if not raw:
                last_error = (
                    f"no output from output-file or stdout (attempt {attempt}/{max_attempts}) "
                    f"| stderr={proc.stderr!r:.300}"
                )
                time.sleep(0.5)
                continue
            try:
                payload = json.loads(raw)
            except Exception as parse_exc:
                last_error = (
                    f"{type(parse_exc).__name__}: {parse_exc} | raw={raw!r:.300} "
                    f"| stderr={proc.stderr!r:.300}"
                )
                time.sleep(0.5)
                continue
            payload["mode"] = "mcp_client"
            if attempt > 1:
                payload["retries"] = attempt - 1
            return payload
        return mcp_tool_result(tool_name, ok=False, error=f"gave up after {max_attempts} attempts: {last_error}", mode="mcp_client")
    except Exception as exc:
        return mcp_tool_result(tool_name, ok=False, error=f"{type(exc).__name__}: {exc}", mode="mcp_client")
    finally:
        for _p in (args_path, out_path):
            try:
                _p.unlink()
            except FileNotFoundError:
                pass


def maybe_mcp_client_call(tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if MCP_ADAPTER_MODE != "mcp_client":
        return None
    call = mcp_client_tool_call(tool_name, arguments)
    if call.get("ok") or not MCP_CLIENT_FALLBACK_TO_LOCAL:
        return call
    print(f"[MCP][WARN] {tool_name}: {call.get('error')} Falling back to local_adapter.")
    mcp_fallback_log.append({"tool": tool_name, "error": call.get("error", ""), "at": call.get("called_at")})
    return None


def mcp_trace_append(state: Dict[str, Any], *calls: Dict[str, Any]) -> List[Dict[str, Any]]:
    trace = list(state.get("mcp_trace", []) or [])
    for call in calls:
        if not call:
            continue
        trace.append({
            "tool": call.get("tool"),
            "mode": call.get("mode"),
            "ok": call.get("ok"),
            "error": call.get("error", ""),
            "called_at": call.get("called_at"),
        })
    return trace


def mcp_part2_config() -> Dict[str, Any]:
    client_call = maybe_mcp_client_call("part2_config_tool", {"part2_input": part2_input})
    if client_call:
        return client_call
    try:
        from mcp_server import part2_config_tool
        payload = part2_config_tool(part2_input)
        payload["mode"] = "local_adapter"
        return payload
    except Exception as exc:
        return mcp_tool_result("part2_config_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


def mcp_schema_context(phase: str = "") -> Dict[str, Any]:
    client_call = maybe_mcp_client_call("schema_context_tool", {"schema_obj": schema, "schema_path": out_validated_schema_path, "phase": phase})
    if client_call:
        return client_call
    try:
        from mcp_server import schema_context_tool
        payload = schema_context_tool(schema_obj=schema, schema_path=out_validated_schema_path, phase=phase)
        payload["mode"] = "local_adapter"
        return payload
    except Exception as exc:
        return mcp_tool_result("schema_context_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


def mcp_com_catalog(phase: str = "") -> Dict[str, Any]:
    client_call = maybe_mcp_client_call("com_catalog_tool", {"db_com_path": db_com_path, "phase": phase})
    if client_call:
        return client_call
    try:
        from mcp_server import com_catalog_tool
        payload = com_catalog_tool(db_com_path, phase=phase)
        payload["mode"] = "local_adapter"
        return payload
    except Exception as exc:
        return mcp_tool_result("com_catalog_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


def mcp_validation_summary(validation_result: Dict[str, Any], phase: str = "") -> Dict[str, Any]:
    client_call = maybe_mcp_client_call("validation_summary_tool", {"validation_result": validation_result, "phase": phase})
    if client_call:
        return client_call
    try:
        from mcp_server import validation_summary_tool
        payload = validation_summary_tool(validation_result, phase=phase)
        payload["mode"] = "local_adapter"
        return payload
    except Exception as exc:
        return mcp_tool_result("validation_summary_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


def mcp_repair_assessment(phase_results: List[Dict[str, Any]], final_validation: Dict[str, Any], run_status: Dict[str, Any]) -> Dict[str, Any]:
    arguments = {"phase_results": phase_results, "final_validation": final_validation, "run_status": run_status}
    client_call = maybe_mcp_client_call("repair_assessment_tool", arguments)
    if client_call:
        return client_call
    try:
        from mcp_server import repair_assessment_tool
        payload = repair_assessment_tool(phase_results, final_validation, run_status)
        payload["mode"] = "local_adapter"
        return payload
    except Exception as exc:
        return mcp_tool_result("repair_assessment_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


# ===========================================================================
# Error tracking + generated-script execution (cell 4's exec-running helpers)
# ===========================================================================
def _record_error(stage: str, err: Exception):
    part2_state["ok"] = False
    part2_state["errors"].append({"stage": stage, "error": str(err)})
    print(f"[PART2][ERROR] {stage}: {err}")

def _run_exec_script_continue(path: str, stage: str = "exec_script"):
    import ast

    p = Path(path)
    if not p.exists():
        _record_error(stage, FileNotFoundError(f"Script not found: {p}"))
        return False

    code = p.read_text(encoding="utf-8")
    try:
        mod = ast.parse(code, filename=str(p))
    except Exception as e:
        _record_error(stage, RuntimeError(f"AST parse failed: {e}"))
        return False

    n_total = len(mod.body)
    n_ok = 0
    n_fail = 0

    for i, node in enumerate(mod.body, start=1):
        stmt_mod = ast.Module(body=[node], type_ignores=[])
        ast.fix_missing_locations(stmt_mod)
        snippet = (ast.get_source_segment(code, node) or f"<stmt {i}>").strip().replace("\n", " ")

        try:
            exec(compile(stmt_mod, str(p), "exec"), globals(), globals())
            n_ok += 1
        except Exception as e:
            n_fail += 1
            _record_error(
                stage,
                RuntimeError(f"stmt {i}/{n_total} failed: {e} | code: {snippet[:220]}")
            )
            continue

    print(f"[PART2] {stage}: statements ok={n_ok}, failed={n_fail}, total={n_total}")
    return n_fail == 0


def run_exec_script(path: str, stage: str = "exec_script", continue_on_error: bool = True):
    if continue_on_error:
        return _run_exec_script_continue(path, stage=stage)

    p = Path(path)
    if not p.exists():
        _record_error(stage, FileNotFoundError(f"Script not found: {p}"))
        return False
    try:
        code = p.read_text(encoding="utf-8")
        exec(compile(code, str(p), "exec"), globals(), globals())
        return True
    except Exception as e:
        _record_error(stage, e)
        return False


# Duplicated from cell10 (item below, _build_com_action_plan) verbatim --
# cell12's _generate_com_skeleton_code() needs to call merge_feed_spec_code()
# directly, but cell10's own body is wrapped into a nested function scope
# below, so a standalone copy lives here at true module level instead. Both
# are pure functions of their arguments -- no divergence risk.
def _to_float_or_none(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        vv = v.get("value")
        if isinstance(vv, (int, float)):
            return float(vv)
    return None


def _iter_feeds(schema_obj: dict):
    if not isinstance(schema_obj, dict):
        return []
    feeds = []

    io = schema_obj.get("io")
    if isinstance(io, dict) and isinstance(io.get("feeds"), list):
        feeds.extend(io.get("feeds") or [])

    io_schema = schema_obj.get("io_schema")
    if isinstance(io_schema, dict) and isinstance(io_schema.get("feeds"), list):
        feeds.extend(io_schema.get("feeds") or [])

    sp = schema_obj.get("schema_proto")
    if isinstance(sp, dict):
        sp_io = sp.get("io")
        if isinstance(sp_io, dict) and isinstance(sp_io.get("feeds"), list):
            feeds.extend(sp_io.get("feeds") or [])

    v = schema_obj.get("value")
    if isinstance(v, dict):
        vio = v.get("io")
        if isinstance(vio, dict) and isinstance(vio.get("feeds"), list):
            feeds.extend(vio.get("feeds") or [])

    # De-duplicate by stream name to avoid repeated feed-spec code.
    uniq = []
    seen = set()
    for f in feeds:
        if not isinstance(f, dict):
            continue
        s = str(f.get("stream") or "").strip().upper()
        if not s or s in seen:
            continue
        seen.add(s)
        uniq.append(f)
    return uniq


def _norm_basis(v: str) -> str:
    return str(v or "").strip().upper().replace("_", "-")


def _feed_flow_value(specs: dict):
    flow = specs.get("flow")
    if isinstance(flow, dict):
        return _to_float_or_none(flow.get("value"))
    return _to_float_or_none(flow)


def _feed_flow_basis(specs: dict) -> str:
    flow = specs.get("flow")
    fb = ""
    if isinstance(flow, dict):
        fb = str(flow.get("basis") or "").strip().upper()
    if fb in {"MASS", "MOLE"}:
        return fb
    b = _norm_basis(specs.get("basis"))
    if b.startswith("MASS"):
        return "MASS"
    return "MOLE"


def _feed_comp_dict(specs: dict) -> dict:
    z = specs.get("z")
    if not isinstance(z, dict):
        return {}
    out = {}
    for k, v in z.items():
        val = _to_float_or_none(v)
        key = str(k or "").strip()
        if key and (val is not None):
            out[key] = val
    return out


def build_feed_spec_code(schema_obj: dict) -> list:
    lines = []
    for f in _iter_feeds(schema_obj):
        if not isinstance(f, dict):
            continue
        sname = str(f.get("stream") or "").strip()
        specs = f.get("specs") if isinstance(f.get("specs"), dict) else {}
        if (not sname) or (not specs):
            continue

        t_val = _to_float_or_none(specs.get("T_C"))
        p_val = _to_float_or_none(specs.get("P_bar"))
        flow_val = _feed_flow_value(specs)
        flow_basis = _feed_flow_basis(specs)
        basis = _norm_basis(specs.get("basis"))
        comp = _feed_comp_dict(specs)

        if t_val is not None:
            lines.append(f"stream_set_temperature(doc, {sname!r}, {t_val})")
        if p_val is not None:
            lines.append(f"stream_set_pressure(doc, {sname!r}, {p_val})")

        if flow_val is not None:
            lines.append(f"stream_set_flow_base(doc, {sname!r}, {flow_basis!r})")
            lines.append(f"stream_set_total_flow(doc, {sname!r}, {flow_val}, base={flow_basis!r})")
            if "FLOW" in basis:
                comp_basis = basis
            elif "FRAC" in basis:
                comp_basis = basis
            else:
                comp_basis = f"{flow_basis}-FRAC"
            lines.append(f"stream_set_basis(doc, {sname!r}, {comp_basis!r})")
            if comp:
                lines.append(f"stream_set_components(doc, {sname!r}, {repr(comp)}, basis={comp_basis!r})")
        else:
            if "FLOW" in basis:
                comp_basis = basis
            elif "FRAC" in basis:
                comp_basis = f"{flow_basis}-FLOW"
            else:
                comp_basis = f"{flow_basis}-FLOW"
            lines.append(f"stream_set_basis(doc, {sname!r}, {comp_basis!r})")
            if comp:
                lines.append(f"stream_set_components(doc, {sname!r}, {repr(comp)}, basis={comp_basis!r})")
    return lines


def merge_feed_spec_code(com_code: str, schema_obj: dict) -> str:
    extra = build_feed_spec_code(schema_obj)
    if not extra:
        return com_code

    def _canon_call(line: str) -> str:
        # Normalize quote/space style to avoid duplicate calls that differ only by formatting.
        s = str(line or "").strip()
        s = s.replace("'", '"')
        s = re.sub(r"\s+", "", s)
        return s

    existing = set()
    for raw in str(com_code or "").splitlines():
        line = raw.strip()
        if line.startswith("stream_set_"):
            existing.add(_canon_call(line))

    merged = com_code.rstrip() + "\n"
    for ln in extra:
        c = _canon_call(ln)
        if c in existing:
            continue
        merged += ln + "\n"
        existing.add(c)
    return merged


def merge_compressor_defaults_code(com_code: str, schema_obj: dict) -> str:
    """phase14: make sure every COMPRESSOR block of the schema gets the settings Aspen requires.

    The COM code is written by an LLM and varies from run to run: one run set the model type, the calculation TYPE and the pressure
    (Aspen accepted it), another set only the pressure, so Aspen ignored the block ("VALUE IS REQUIRED FOR TERTIARY KEYWORD: TYPE")
    and Part3 had no patch for it.  Missing calls are added right after the block's block_add line:
      comp_set_model_type(.., "COMPRESSOR"), comp_set_type(.., "ISENTROPIC")  [Aspen's default calculation type]
      comp_set_eff(.., seff=<schema eff>)  only when the schema carries a numeric `eff` (the LLM code dropped it in both D and E).
    A block already configured through comp_configure(...) or the individual calls is left alone."""
    fs = (schema_obj or {}).get("flowsheet_schema") or {}
    lines = str(com_code or "").splitlines()
    for b in fs.get("blocks") or []:
        if str(b.get("type") or "").upper() != "COMPRESSOR":
            continue
        bid = str(b.get("id"))
        arg = r"\(\s*doc\s*,\s*[\"']" + re.escape(bid) + r"[\"']"
        has = lambda fn: any(re.search(fn + arg, ln) for ln in lines)          # noqa: E731
        specs = b.get("specs") if isinstance(b.get("specs"), dict) else {}
        eff = specs.get("eff")
        eff = eff.get("value") if isinstance(eff, dict) else eff
        add = []
        if not (has("comp_configure") or has("comp_set_model_type")):
            add.append(f'comp_set_model_type(doc, "{bid}", "COMPRESSOR")')
        if not (has("comp_configure") or has("comp_set_type")):
            add.append(f'comp_set_type(doc, "{bid}", "ISENTROPIC")')
        if isinstance(eff, (int, float)) and not isinstance(eff, bool) and 0 < float(eff) <= 1                 and not (has("comp_configure") or has("comp_set_eff")):
            add.append(f'comp_set_eff(doc, "{bid}", seff={float(eff)})')
        if not add:
            continue
        at = next((i for i, ln in enumerate(lines) if re.search(r"block_add" + arg, ln)), None)
        lines[(at + 1) if at is not None else len(lines):(at + 1) if at is not None else len(lines)] = add
    return "\n".join(lines).rstrip() + "\n"


def merge_splitter_fractions_code(com_code: str, schema_obj: dict) -> str:
    """phase14: Aspen accepts split fractions for all outlets of an FSplit but ONE ("Flow Split Specifications for Outlet Streams are
    allowed for all stream but one").  The LLM-written code set every outlet.  When a splitter has as many splt_set_frac calls as
    outlet streams, drop the call with the largest fraction (Aspen takes the remainder), keeping the others (e.g. the user's 1 % purge)."""
    fs = (schema_obj or {}).get("flowsheet_schema") or {}
    n_out = {}
    for e in (fs.get("connectivity") or {}).get("edges") or []:
        src = e.get("from")
        if isinstance(src, dict) and src.get("block"):
            n_out[str(src["block"])] = n_out.get(str(src["block"]), 0) + 1
    pat = re.compile(r"^\s*splt_set_frac\(\s*doc\s*,\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']+)[\"']\s*,\s*([-+0-9.eE]+)\s*\)\s*$")
    lines = str(com_code or "").splitlines()
    calls = {}
    for i, ln in enumerate(lines):
        m = pat.match(ln)
        if m:
            calls.setdefault(m.group(1), []).append((i, float(m.group(3))))
    drop = set()
    for blk, cs in calls.items():
        outs = n_out.get(blk)
        if outs and len(cs) >= outs:
            drop.add(max(cs, key=lambda c: (c[1], c[0]))[0])
    if not drop:
        return str(com_code or "")
    return "\n".join(ln for i, ln in enumerate(lines) if i not in drop).rstrip() + "\n"


def safe_json_loads(text: str) -> dict:
    """Module-level so every stage function can reach it (Phase C2 extraction
    originally nested this inside _generate_inp_skeleton only, which left
    _build_com_action_plan's call to it unresolved -- NameError at runtime)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    return json.loads(t)


# ===========================================================================
# Deterministic bootstrap steps (cells 6/8/10/12/14), called in order from
# build_session() below -- never individually agent-selectable, matching the
# notebook's original unconditional cell-by-cell execution.
# ===========================================================================
def _generate_inp_skeleton() -> None:
    """Verbatim body of part2_v1.ipynb cell 6 (INP skeleton generation),
    wrapped into a function so build_session() controls when it runs
    (needs config globals already resolved). Sets module globals `schema`
    and `resolved_out_path` for later cells to read."""
    global schema, resolved_out_path
    ## inp_skeleton

    # =========================
    # Helpers
    # =========================
    def load_yaml(path: str):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)


    def load_text(path: str, max_chars: int = 40000) -> str:
        if not path or not os.path.isfile(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        return txt[:max_chars]


    def slim(obj, max_chars: int = 25000) -> str:
        s = json.dumps(obj, ensure_ascii=False, indent=2)
        return s[:max_chars]


    def load_aspen_component_db(comp_db_path: str) -> dict:
        # Deterministic lookup: use explicit component DB path from runner input.
        comp_db_path = os.path.abspath(comp_db_path)
        if not os.path.isfile(comp_db_path):
            return {"components": {}, "_meta": {"path": comp_db_path, "exists": False}}
        db = load_yaml(comp_db_path) or {}
        if not isinstance(db, dict):
            db = {}
        db.setdefault("components", {})
        db["_meta"] = {"path": comp_db_path, "exists": True}
        return db


    def _formula_of_alias(alias: str) -> str:
        m = re.match(r"^([A-Z0-9\(\)]+?)(?:-[A-Z0-9]+)?$", (alias or "").upper())
        return m.group(1) if m else (alias or "").upper()


    def _norm(s: str) -> str:
        return re.sub(r"[^A-Z0-9]+", "", (s or "").upper())


    def safe_json_loads(text: str) -> dict:
        t = (text or "").strip()
        if t.startswith("```"):
            t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
            t = re.sub(r"\n?```$", "", t)
        return json.loads(t)


    SCHEMA_COMPONENT_PATHS = [
        ("aspen_schema", "components"),
        ("schema_proto", "aspen", "components"),
        ("schema_proto", "aspen_input", "components"),
        ("schema_proto", "aspen_schema", "components"),
        ("schema_proto", "components"),
        ("aspen", "components"),
        ("simulation", "aspen", "components"),
        ("value", "components"),
        ("components",),
    ]


    def _get_path(obj: dict, path: tuple):
        cur = obj
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                return None
        return cur


    def _first_non_empty_list_at_paths(obj: dict, paths: list) -> tuple:
        first_empty = None
        for path in paths:
            cur = _get_path(obj, path)
            if isinstance(cur, list):
                if cur:
                    return cur, path
                if first_empty is None:
                    first_empty = (cur, path)
        return first_empty if first_empty is not None else ([], None)


    def extract_schema_components(schema: dict) -> list:
        if not isinstance(schema, dict):
            return []
        components, source_path = _first_non_empty_list_at_paths(schema, SCHEMA_COMPONENT_PATHS)
        if source_path:
            print(f"[SCHEMA] components source: {'.'.join(source_path)} count={len(components)}")
        return components


    def build_candidate_component_db(schema_components: list, aspen_comp_db: dict, max_per_formula: int = 20) -> dict:
        raw = (aspen_comp_db or {}).get("components", {}) or {}
        schema_formulas = {
            str(c.get("formula") or "").strip().upper()
            for c in (schema_components or [])
            if isinstance(c, dict) and str(c.get("formula") or "").strip()
        }
        schema_name_tokens = {
            _norm(str(c.get("name") or ""))
            for c in (schema_components or [])
            if isinstance(c, dict)
        }

        out = {}
        counts = {}
        for alias, meta in raw.items():
            a = str(alias).strip().upper()
            if not a:
                continue
            fm = _formula_of_alias(a)
            nm = str((meta or {}).get("name", "") if isinstance(meta, dict) else "")

            keep = False
            if fm in schema_formulas:
                keep = True
            elif _norm(nm) in schema_name_tokens:
                keep = True

            if not keep:
                continue
            if counts.get(fm, 0) >= max_per_formula:
                continue
            counts[fm] = counts.get(fm, 0) + 1
            out[a] = {"name": nm, "formula_family": fm}
        return {"components": out}


    def resolve_components_with_llm(schema_components: list, candidate_db: dict,
                                    model: str = "gpt-5-mini", temperature: float = 1) -> dict:
        prompt = PromptTemplate.from_template(
            """
    You are an Aspen component alias resolver.

    Task:
    Given schema components and Aspen candidate components, resolve each schema component to ONE Aspen alias.

    Rules:
    - Prefer candidates with same formula family (e.g., C8H10-* matches C8H10).
    - Use chemical naming knowledge for synonyms (e.g., n-butane == butane, diethyl is not C4H10 in normal naming; avoid wrong synonym).
    - If exact confidence is low, still return best guess but set confidence < 0.70 and explain briefly.
    - Keep one output row per input component.
    - Do not drop components.

    Return JSON only, no markdown:
    {{
      "resolved_components": [
        {{
          "schema_name": "Methanol",
          "formula": "CH4O",
          "resolved_alias": "CH4O",
          "resolved_name": "METHANOL",
          "confidence": 0.99,
          "reason": "exact formula/name family match"
        }}
      ]
    }}

    Inputs:
    schema_components (JSON):
    {schema_components_json}

    candidate_aspen_components (JSON):
    {candidate_db_json}
            """.strip()
        )

        llm = ChatOpenAI(model=model, temperature=temperature)
        parser = StrOutputParser()
        chain = prompt | llm | parser
        with token_usage("part2_component_resolver", model=model, log_path=part2_token_usage_path) as _callbacks:
            raw = chain.invoke({
                "schema_components_json": slim(schema_components, max_chars=25000),
                "candidate_db_json": slim(candidate_db, max_chars=25000),
            }, config={"callbacks": _callbacks})
        obj = safe_json_loads(raw)
        if not isinstance(obj, dict) or not isinstance(obj.get("resolved_components"), list):
            raise RuntimeError("Resolver output invalid JSON schema.")
        return obj


    def generate_components_inp_with_llm(resolved_components: list, inp_db: dict,
                                         components_line_format: str,
                                         model: str = "gpt-5.4-mini",
                                         temperature: float = 1) -> str:
        prompt = PromptTemplate.from_template(
            """
    You are an expert in Aspen Plus input (.inp) authoring.

    Goal:
    Generate a minimal Aspen Plus .inp text that contains ONLY:
    1) standard header content from inp_db (if available),
    2) a COMPONENTS section.

    Rules:
    - Output plain .inp text only. No markdown, no explanations.
    - COMPONENTS must use EXACT alias+formula from resolved_components. Do not reinterpret.
    - Component line format: {components_line_format}
    - Do not add unrelated sections.

    Inputs:
    resolved_components (JSON):
    {resolved_components_json}

    inp_db (JSON, may be truncated):
    {inp_db_json}
            """.strip()
        )

        llm = ChatOpenAI(model=model, temperature=temperature)
        parser = StrOutputParser()
        chain = prompt | llm | parser

        with token_usage("part2_inp_codegen", model=model, log_path=part2_token_usage_path) as _callbacks:
            inp_text = chain.invoke({
                "resolved_components_json": slim(resolved_components, max_chars=20000),
                "inp_db_json": slim(inp_db, max_chars=30000),
                "components_line_format": components_line_format,
            }, config={"callbacks": _callbacks})
        up = inp_text.upper()
        if "COMPONENTS" not in up:
            raise RuntimeError("LLM output missing COMPONENTS section.")
        if len(inp_text.strip()) < 50:
            raise RuntimeError("LLM output too short; likely failed.")
        return inp_text


    def build_components_lines_from_resolved(resolved_components: list, line_format: str) -> list:
        lines = []
        seen = set()
        for r in (resolved_components or []):
            if not isinstance(r, dict):
                continue
            alias = str(r.get("resolved_alias") or "").strip().upper()
            formula = str(r.get("formula") or "").strip().upper()
            if not alias or not formula:
                continue
            key = (alias, formula)
            if key in seen:
                continue
            seen.add(key)
            # the component ID (first token of a COMPONENTS line) must be the same string the generated COM code uses
            # for stream compositions and the reaction / block specs, i.e. the resolved alias.  The shared format puts the
            # FORMULA first ("C7H14 C7H14-6 /"), which is harmless while formula == alias (CH4, H2, H2O ...) but made
            # stream_set_components(doc, "S1", {"C7H14-6": 1.0, ...}) fail for methylcyclohexane (no such component ID).
            try:
                lines.append(line_format.format(alias=alias, formula=alias))
            except Exception:
                lines.append(f"{alias} {alias} /")
        return lines


    def replace_components_section(inp_text: str, component_lines: list) -> str:
        lines = inp_text.splitlines()
        start = None
        end = None
        for i, line in enumerate(lines):
            if line.strip().upper() == "COMPONENTS":
                start = i
                j = i + 1
                while j < len(lines) and lines[j].strip() != "":
                    j += 1
                end = j
                break
        if start is None:
            if lines and lines[-1].strip() != "":
                lines.append("")
            lines.append("COMPONENTS")
            lines.extend(component_lines)
            lines.append("")
            return "\n".join(lines) + "\n"
        new_lines = lines[: start + 1] + component_lines + [""] + lines[end + 1 :]
        return "\n".join(new_lines) + "\n"


    def enforce_components_indent(inp_text: str, indent: str = "    ") -> str:
        lines = inp_text.splitlines()
        new_lines = []
        in_components = False
        for line in lines:
            stripped = line.strip()
            if stripped.upper() == "COMPONENTS":
                in_components = True
                new_lines.append("COMPONENTS")
                continue
            if in_components and stripped == "":
                in_components = False
                new_lines.append(line)
                continue
            if in_components and stripped.endswith("/"):
                new_lines.append(indent + stripped)
            else:
                new_lines.append(line)
        return "\n".join(new_lines) + "\n"


    # =========================
    # Run
    # =========================
    schema = load_yaml(out_validated_schema_path)
    inp_db = load_yaml(db_inp_path)
    aspen_comp_db = load_aspen_component_db(component_db_path)
    components_line_format = (
        ((inp_db or {}).get("InpFormat", {}) or {})
        .get("COMPONENTS", {})
        .get("line_format", "{formula} {alias} /")
    )

    schema_components = extract_schema_components(schema)
    if not schema_components:
        searched = [".".join(p) for p in SCHEMA_COMPONENT_PATHS]
        raise RuntimeError("No schema components found. Searched paths: " + ", ".join(searched))
    candidate_db = build_candidate_component_db(schema_components, aspen_comp_db, max_per_formula=25)
    resolved_obj = resolve_components_with_llm(
        schema_components=schema_components,
        candidate_db=candidate_db,
        model=part2_component_resolver_model,
        temperature=llm_temperature
    )
    resolved_components = resolved_obj.get("resolved_components", [])
    if not resolved_components:
        raise RuntimeError("Component resolver returned no components; stopping before writing an empty COMPONENTS section.")

    inp_text = generate_components_inp_with_llm(
        resolved_components=resolved_components,
        inp_db=inp_db,
        components_line_format=components_line_format,
        model=part2_inp_codegen_model,
        temperature=llm_temperature
    )

    # Final deterministic patch: COMPONENTS section follows resolved result.
    resolved_component_lines = build_components_lines_from_resolved(
        resolved_components,
        line_format=components_line_format
    )
    if resolved_component_lines:
        inp_text = replace_components_section(inp_text, resolved_component_lines)

    indent = inp_db["InpFormat"]["general"]["indent"]
    inp_text = enforce_components_indent(inp_text, indent=indent)

    with open(out_inp_path, "w", encoding="utf-8") as f:
        f.write(inp_text)

    # Save resolver artifact for debugging/reproducibility.
    resolved_out_path = os.path.splitext(out_inp_path)[0] + "_resolved_components.json"
    with open(resolved_out_path, "w", encoding="utf-8") as f:
        json.dump(resolved_obj, f, ensure_ascii=False, indent=2)

    meta = aspen_comp_db.get("_meta", {})
    print(f"[OK] wrote: {out_inp_path}")
    print("chars:", len(inp_text))
    print(f"[COMP_DB] path={meta.get('path')} exists={meta.get('exists')} candidates={len((candidate_db or {}).get('components', {}))} resolved={len(resolved_components)}")
    print(f"[RESOLVE] wrote: {resolved_out_path}")


def _convert_inp_to_bkp() -> None:
    """Verbatim body of part2_v1.ipynb cell 8 (INP -> BKP conversion)."""
    ## inp -> bkp conversion
    # Batch file is written without Korean/space-heavy absolute working paths.
    # It runs from its own directory and references only local filenames.

    bat_path = os.path.join(working_dir, "convert_inp_to_bkp.bat")
    inp_filename = Path(out_inp_path).name
    expected_bkp_path = Path(out_bkp_path)

    bat_content = f"""@echo off
    call "{aspen_setup_bat}"
    cd /d "%~dp0"
    Aspen "{inp_filename}" /mmbackup
    """

    with open(bat_path, "w", encoding="ascii") as f:
        f.write(bat_content)

    try:
        subprocess.run(['cmd.exe', '/c', bat_path], cwd=working_dir, check=True)
        if not expected_bkp_path.exists() or expected_bkp_path.stat().st_size == 0:
            raise RuntimeError(f"BKP conversion produced missing/empty file: {expected_bkp_path}")
        print("INP -> BKP conversion completed")
    except Exception as e:
        _record_error("inp_to_bkp", e)
        raise


def _build_com_action_plan() -> None:
    """Verbatim body of part2_v1.ipynb cell 10 (staged COM action plan).
    All of this cell's own helper functions (including its own copy of
    merge_feed_spec_code, unused from in here) are nested inside this
    function's scope -- only the run-level statements actually need to
    execute after config is resolved; the helper defs are harmless to
    nest since nothing outside this cell called them directly (confirmed
    by inspection) except merge_feed_spec_code, which has its own
    standalone module-level duplicate above for that reason."""
    global function_db, schema, function_db_json, action_plan, action_plan_staged
    # com_skeleton generation (1/2): staged action plans

    # =========================
    # Load DB / Schema
    # =========================
    with open(db_com_path, "r", encoding="utf-8") as f:
        function_db = yaml.safe_load(f)

    with open(out_validated_schema_path, "r", encoding="utf-8") as f:
        schema = yaml.safe_load(f)


    def _to_float_or_none(v):
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, dict):
            vv = v.get("value")
            if isinstance(vv, (int, float)):
                return float(vv)
        return None


    def _norm(s: str) -> str:
        return re.sub(r"[^A-Z0-9]+", "", str(s or "").upper())


    def _get_component_list(s: dict) -> list:
        paths = globals().get("SCHEMA_COMPONENT_PATHS") or [
            ("aspen_schema", "components"),
            ("schema_proto", "aspen", "components"),
            ("schema_proto", "aspen_input", "components"),
            ("schema_proto", "aspen_schema", "components"),
            ("schema_proto", "components"),
            ("aspen", "components"),
            ("simulation", "aspen", "components"),
            ("value", "components"),
            ("components",),
        ]

        first_empty = None
        for path in paths:
            cur = s
            ok = True
            for k in path:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, list):
                if cur:
                    return cur
                if first_empty is None:
                    first_empty = cur
        return first_empty if first_empty is not None else []


    def _iter_feeds(schema_obj: dict):
        if not isinstance(schema_obj, dict):
            return []
        feeds = []

        io = schema_obj.get("io")
        if isinstance(io, dict) and isinstance(io.get("feeds"), list):
            feeds.extend(io.get("feeds") or [])

        io_schema = schema_obj.get("io_schema")
        if isinstance(io_schema, dict) and isinstance(io_schema.get("feeds"), list):
            feeds.extend(io_schema.get("feeds") or [])

        sp = schema_obj.get("schema_proto")
        if isinstance(sp, dict):
            sp_io = sp.get("io")
            if isinstance(sp_io, dict) and isinstance(sp_io.get("feeds"), list):
                feeds.extend(sp_io.get("feeds") or [])

        v = schema_obj.get("value")
        if isinstance(v, dict):
            vio = v.get("io")
            if isinstance(vio, dict) and isinstance(vio.get("feeds"), list):
                feeds.extend(vio.get("feeds") or [])

        # De-duplicate by stream name to avoid repeated feed-spec code.
        uniq = []
        seen = set()
        for f in feeds:
            if not isinstance(f, dict):
                continue
            s = str(f.get("stream") or "").strip().upper()
            if not s or s in seen:
                continue
            seen.add(s)
            uniq.append(f)
        return uniq


    def normalize_feed_z_for_codegen(schema_obj: dict) -> dict:
        comps = _get_component_list(schema_obj)
        key_to_id = {}
        for c in comps:
            if not isinstance(c, dict):
                continue
            aspen_id = str(c.get("aspen_id") or "").strip()
            formula = str(c.get("formula") or "").strip()
            name = str(c.get("name") or "").strip()
            pref = aspen_id or formula
            if not pref:
                continue
            for k in [aspen_id, formula, name]:
                nk = _norm(k)
                if nk:
                    key_to_id[nk] = pref

        for f in _iter_feeds(schema_obj):
            if not isinstance(f, dict):
                continue
            specs = f.get("specs")
            if not isinstance(specs, dict):
                continue
            z = specs.get("z")
            if not isinstance(z, dict):
                continue

            new_z = {}
            for k, v in z.items():
                val = _to_float_or_none(v)
                if val is None:
                    continue
                nk = _norm(k)
                pref = key_to_id.get(nk, str(k).strip())
                if not pref:
                    continue
                new_z[pref] = new_z.get(pref, 0.0) + val
            if new_z:
                specs["z"] = new_z
        return schema_obj


    def apply_resolved_aliases_to_schema(schema_obj: dict, out_inp_path: str) -> dict:
        resolved_path = os.path.splitext(out_inp_path)[0] + "_resolved_components.json"
        if not os.path.isfile(resolved_path):
            return schema_obj
        try:
            data = json.loads(Path(resolved_path).read_text(encoding="utf-8"))
        except Exception:
            return schema_obj

        rows = data.get("resolved_components") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return schema_obj

        alias_by_key = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            alias = str(r.get("resolved_alias") or "").strip()
            if not alias:
                continue
            for k in [r.get("schema_name"), r.get("formula"), r.get("resolved_name"), alias]:
                nk = _norm(k)
                if nk:
                    alias_by_key[nk] = alias

        comps = _get_component_list(schema_obj)
        comp_pref = {}
        for c in comps:
            if not isinstance(c, dict):
                continue
            keys = [
                _norm(c.get("name")),
                _norm(c.get("formula")),
                _norm(c.get("aspen_id")),
            ]
            picked = ""
            for k in keys:
                if k and k in alias_by_key:
                    picked = alias_by_key[k]
                    break
            if picked:
                # fix: remember the ORIGINAL aspen_id before overwriting it. The feed
                # compositions (z) are keyed by that original id; without this, an id that is not
                # also a resolved_name (e.g. 'CARBON' for solid carbon, resolved to 'C') stayed
                # unmapped in z and the generated stream_set_components used a component that
                # does not exist in the Aspen model.
                orig_id = _norm(c.get("aspen_id"))
                c["aspen_id"] = picked
                if orig_id:
                    comp_pref[orig_id] = picked
                if c.get("formula"):
                    comp_pref[_norm(c.get("formula"))] = picked
                if c.get("name"):
                    comp_pref[_norm(c.get("name"))] = picked
                comp_pref[_norm(picked)] = picked

        for f in _iter_feeds(schema_obj):
            if not isinstance(f, dict):
                continue
            specs = f.get("specs")
            if not isinstance(specs, dict):
                continue
            z = specs.get("z")
            if not isinstance(z, dict):
                continue
            new_z = {}
            for k, v in z.items():
                nk = _norm(k)
                alias = comp_pref.get(nk) or alias_by_key.get(nk) or str(k).strip()
                val = _to_float_or_none(v)
                if val is None:
                    continue
                new_z[alias] = new_z.get(alias, 0.0) + val
            if new_z:
                specs["z"] = new_z
        return schema_obj


    schema = normalize_feed_z_for_codegen(schema)
    schema = apply_resolved_aliases_to_schema(schema, out_inp_path)


    def _norm_basis(v: str) -> str:
        return str(v or "").strip().upper().replace("_", "-")


    def _feed_flow_value(specs: dict):
        flow = specs.get("flow")
        if isinstance(flow, dict):
            return _to_float_or_none(flow.get("value"))
        return _to_float_or_none(flow)


    def _feed_flow_basis(specs: dict) -> str:
        flow = specs.get("flow")
        fb = ""
        if isinstance(flow, dict):
            fb = str(flow.get("basis") or "").strip().upper()
        if fb in {"MASS", "MOLE"}:
            return fb
        b = _norm_basis(specs.get("basis"))
        if b.startswith("MASS"):
            return "MASS"
        return "MOLE"


    def _feed_comp_dict(specs: dict) -> dict:
        z = specs.get("z")
        if not isinstance(z, dict):
            return {}
        out = {}
        for k, v in z.items():
            val = _to_float_or_none(v)
            key = str(k or "").strip()
            if key and (val is not None):
                out[key] = val
        return out


    def build_feed_spec_code(schema_obj: dict) -> list:
        lines = []
        for f in _iter_feeds(schema_obj):
            if not isinstance(f, dict):
                continue
            sname = str(f.get("stream") or "").strip()
            specs = f.get("specs") if isinstance(f.get("specs"), dict) else {}
            if (not sname) or (not specs):
                continue

            t_val = _to_float_or_none(specs.get("T_C"))
            p_val = _to_float_or_none(specs.get("P_bar"))
            flow_val = _feed_flow_value(specs)
            flow_basis = _feed_flow_basis(specs)
            basis = _norm_basis(specs.get("basis"))
            comp = _feed_comp_dict(specs)

            if t_val is not None:
                lines.append(f"stream_set_temperature(doc, {sname!r}, {t_val})")
            if p_val is not None:
                lines.append(f"stream_set_pressure(doc, {sname!r}, {p_val})")

            if flow_val is not None:
                lines.append(f"stream_set_flow_base(doc, {sname!r}, {flow_basis!r})")
                lines.append(f"stream_set_total_flow(doc, {sname!r}, {flow_val}, base={flow_basis!r})")
                if "FLOW" in basis:
                    comp_basis = basis
                elif "FRAC" in basis:
                    comp_basis = basis
                else:
                    comp_basis = f"{flow_basis}-FRAC"
                lines.append(f"stream_set_basis(doc, {sname!r}, {comp_basis!r})")
                if comp:
                    lines.append(f"stream_set_components(doc, {sname!r}, {repr(comp)}, basis={comp_basis!r})")
            else:
                if "FLOW" in basis:
                    comp_basis = basis
                elif "FRAC" in basis:
                    comp_basis = f"{flow_basis}-FLOW"
                else:
                    comp_basis = f"{flow_basis}-FLOW"
                lines.append(f"stream_set_basis(doc, {sname!r}, {comp_basis!r})")
                if comp:
                    lines.append(f"stream_set_components(doc, {sname!r}, {repr(comp)}, basis={comp_basis!r})")
        return lines


    def merge_feed_spec_code(com_code: str, schema_obj: dict) -> str:
        extra = build_feed_spec_code(schema_obj)
        if not extra:
            return com_code

        def _canon_call(line: str) -> str:
            # Normalize quote/space style to avoid duplicate calls that differ only by formatting.
            s = str(line or "").strip()
            s = s.replace("'", '"')
            s = re.sub(r"\s+", "", s)
            return s

        existing = set()
        for raw in str(com_code or "").splitlines():
            line = raw.strip()
            if line.startswith("stream_set_"):
                existing.add(_canon_call(line))

        merged = com_code.rstrip() + "\n"
        for ln in extra:
            c = _canon_call(ln)
            if c in existing:
                continue
            merged += ln + "\n"
            existing.add(c)
        return merged


    def _coerce_actions(plan_obj: dict) -> list:
        if not isinstance(plan_obj, dict):
            return []
        acts = plan_obj.get("actions")
        if not isinstance(acts, list):
            return []
        out = []
        for a in acts:
            if not isinstance(a, dict):
                continue
            fn = str(a.get("fn") or "").strip()
            args = a.get("args") if isinstance(a.get("args"), dict) else {}
            if not fn:
                continue
            out.append({
                "id": str(a.get("id") or ""),
                "action": "call",
                "fn": fn,
                "args": args,
                "reason": str(a.get("reason") or "").strip(),
            })
        return out


    def _json_safe_obj(obj):
        if isinstance(obj, float):
            if obj != obj or obj in (float("inf"), float("-inf")):
                return None
            return obj
        if isinstance(obj, dict):
            return {k: _json_safe_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_json_safe_obj(v) for v in obj]
        return obj


    def _json_dumps_safe(obj, indent=2):
        return json.dumps(_json_safe_obj(obj), ensure_ascii=False, indent=indent, allow_nan=False)


    function_db_json = _json_dumps_safe(function_db, indent=2)
    schema_json = _json_dumps_safe(schema, indent=2)


    def _extract_function_names(obj, out=None):
        if out is None:
            out = set()
        if isinstance(obj, list):
            for x in obj:
                _extract_function_names(x, out)
        elif isinstance(obj, dict):
            for k in ("name", "fn", "function", "function_name"):
                v = obj.get(k)
                if isinstance(v, str):
                    vv = v.strip()
                    if vv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", vv):
                        out.add(vv)
            for v in obj.values():
                _extract_function_names(v, out)
        return out


    def _allowed_functions_for_stage(function_db_obj, stage_key: str) -> list:
        all_fns = sorted(_extract_function_names(function_db_obj))
        if not all_fns:
            return []

        def pick(pred):
            return [f for f in all_fns if pred(f.lower())]

        if stage_key == "property_method":
            out = pick(lambda x: ("property" in x) or ("thermo" in x and "set" in x) or ("method" in x and "set" in x))
        elif stage_key == "stream_block_create":
            out = pick(lambda x: x in {"stream_add", "block_add", "stream_define"})
        elif stage_key == "stream_block_connectivity":
            out = pick(lambda x: ("connect" in x) or ("link" in x and "stream" in x))
        elif stage_key == "stream_spec":
            out = pick(lambda x: x.startswith("stream_set_"))
        elif stage_key == "block_spec":
            out = pick(
                lambda x:
                (
                    (("heater_" in x) or ("flash_" in x) or ("rgibbs_" in x) or ("rstoic_" in x) or ("rplug_" in x)
                     or ("radfrac_" in x) or ("pump_" in x) or ("comp_" in x) or ("splt_" in x) or ("mheatx_" in x))
                    and (("set_" in x) or ("config" in x))
                )
            )
        else:
            out = []

        return sorted(set(out))


    PROPERTY_METHOD_PATHS = [
        ("aspen_schema", "property_method"),
        ("aspen", "property_method"),
        ("aspen_input", "property_method"),
        ("schema_proto", "aspen", "property_method"),
        ("schema_proto", "aspen_input", "property_method"),
        ("schema_proto", "aspen_schema", "property_method"),
        ("schema_proto", "property_method"),
        ("simulation", "aspen", "property_method"),
        ("value", "aspen_schema", "property_method"),
        ("value", "aspen", "property_method"),
        ("property_method",),
    ]


    def _get_scalar_at_path(obj: dict, path: tuple):
        cur = obj
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                return None
        if isinstance(cur, str):
            v = cur.strip()
            return v or None
        if isinstance(cur, (int, float)):
            return str(cur)
        return None


    def extract_property_method(schema_obj: dict) -> tuple:
        if not isinstance(schema_obj, dict):
            return None, None
        for path in PROPERTY_METHOD_PATHS:
            val = _get_scalar_at_path(schema_obj, path)
            if val:
                return val, path
        return None, None

    def _stage_context(schema_obj: dict, stage_key: str) -> dict:
        property_method, property_method_source = extract_property_method(schema_obj)
        flowsheet = {}
        aspen_schema_obj = None
        codegen_obj = None

        if isinstance(schema_obj, dict):
            # Prefer already-flattened top-level keys.
            if isinstance(schema_obj.get("flowsheet"), dict):
                flowsheet = schema_obj.get("flowsheet")
            elif isinstance(schema_obj.get("flowsheet_schema"), dict):
                flowsheet = schema_obj.get("flowsheet_schema")

            aspen_schema_obj = schema_obj.get("aspen_schema") if isinstance(schema_obj.get("aspen_schema"), dict) else None
            codegen_obj = schema_obj.get("codegen") if isinstance(schema_obj.get("codegen"), dict) else None

            # Fallback: schema_proto envelope (from Part1 validated output)
            if (not flowsheet) or aspen_schema_obj is None or codegen_obj is None:
                sp = schema_obj.get("schema_proto")
                if isinstance(sp, dict):
                    if not flowsheet:
                        if isinstance(sp.get("flowsheet"), dict):
                            flowsheet = sp.get("flowsheet")
                        elif isinstance(sp.get("flowsheet_schema"), dict):
                            flowsheet = sp.get("flowsheet_schema")
                    if aspen_schema_obj is None and isinstance(sp.get("aspen"), dict):
                        aspen_schema_obj = sp.get("aspen")
                    if codegen_obj is None and isinstance(sp.get("codegen"), dict):
                        codegen_obj = sp.get("codegen")

            # Legacy fallback under value
            if not flowsheet:
                v = schema_obj.get("value")
                if isinstance(v, dict):
                    if isinstance(v.get("flowsheet"), dict):
                        flowsheet = v.get("flowsheet")
                    elif isinstance(v.get("flowsheet_schema"), dict):
                        flowsheet = v.get("flowsheet_schema")

        blocks = flowsheet.get("blocks") if isinstance(flowsheet.get("blocks"), list) else []
        streams = flowsheet.get("streams") if isinstance(flowsheet.get("streams"), list) else []
        connectivity = flowsheet.get("connectivity")
        if isinstance(connectivity, dict) and isinstance(connectivity.get("edges"), list):
            edges = connectivity.get("edges")
        elif isinstance(connectivity, list):
            edges = connectivity
        else:
            edges = []
        feeds = _iter_feeds(schema_obj)

        if stage_key == "property_method":
            if property_method:
                print(f"[SCHEMA] property_method source: {'.'.join(property_method_source)} value={property_method}")
            return {
                "meta": schema_obj.get("meta"),
                "codegen": codegen_obj,
                "aspen_schema": aspen_schema_obj,
                "property_method": property_method,
                "property_method_source": ".".join(property_method_source) if property_method_source else None,
            }
        if stage_key == "stream_block_create":
            return {
                "blocks": blocks,
                "streams": streams,
            }
        if stage_key == "stream_block_connectivity":
            return {
                "blocks": blocks,
                "streams": streams,
                "edges": edges,
                "codegen": codegen_obj,
            }
        if stage_key == "stream_spec":
            return {
                "feeds": feeds,
                "streams": streams,
                "components": _get_component_list(schema_obj),
            }
        if stage_key == "block_spec":
            return {
                "blocks": blocks,
                "codegen": codegen_obj,
            }
        return {"schema": schema_obj}


    PLAN_STAGES = [
        {"key": "property_method", "title": "property method setup"},
        {"key": "stream_block_create", "title": "stream and block creation"},
        {"key": "stream_block_connectivity", "title": "stream-block connectivity"},
        {"key": "stream_spec", "title": "stream specifications"},
        {"key": "block_spec", "title": "block specifications"},
    ]


    prompt_plan_property_method = PromptTemplate.from_template(
    """
    You are planning Aspen Plus COM calls for stage: PROPERTY_METHOD.

    Return ONLY valid JSON object with key "actions".
    Each action item fields: id, action, fn, args, reason.
    No markdown. No extra keys.

    Goal:
    - Decide property-method related calls only.
    - Use stage_context.property_method first when it is present.
    - Otherwise use stage_context.aspen_schema.property_method when present.
    - If neither is present, return empty actions.

    Must do:
    - Use functions only from allowed_functions.
    - If stage_context.property_method is present, emit exactly one property-method setup action using that value.
    - Respect function signatures in function_db.
    - Avoid duplicates against already_planned_actions.

    Must NOT do:
    - Create blocks or streams.
    - Connect streams/ports.
    - Set stream or block operating specs.

    Inputs:
    allowed_functions:
    {allowed_functions_json}

    already_planned_actions:
    {existing_actions_json}

    function_db:
    {function_db}

    stage_context:
    {stage_context_json}
    """.strip()
    )


    prompt_plan_stream_block_create = PromptTemplate.from_template(
    """
    You are planning Aspen Plus COM calls for stage: STREAM_BLOCK_CREATE.

    Return ONLY valid JSON object with key "actions".
    Each action item fields: id, action, fn, args, reason.
    No markdown. No extra keys.

    Goal:
    - Generate creation calls for streams and blocks only.
    - Use IDs/names exactly from stage_context.
    - Prefer deterministic order: create streams first, then blocks.

    Must do:
    - Use functions only from allowed_functions.
    - Respect function signatures in function_db.
    - Avoid duplicates against already_planned_actions.

    Must NOT do:
    - Connectivity calls.
    - Stream spec calls.
    - Block spec calls.
    - Property-method calls.

    Inputs:
    allowed_functions:
    {allowed_functions_json}

    already_planned_actions:
    {existing_actions_json}

    function_db:
    {function_db}

    stage_context:
    {stage_context_json}
    """.strip()
    )


    prompt_plan_stream_block_connectivity = PromptTemplate.from_template(
    """
    You are planning Aspen Plus COM calls for stage: STREAM_BLOCK_CONNECTIVITY.

    Return ONLY valid JSON object with key "actions".
    Each action item fields: id, action, fn, args, reason.
    No markdown. No extra keys.

    Goal:
    - Generate only connectivity calls from explicit topology.
    - Skip uncertain port mapping instead of guessing.

    Must do:
    - Use functions only from allowed_functions.
    - Respect function signatures in function_db.
    - Avoid duplicates against already_planned_actions.
    - Use stage_context.edges as the primary topology source.
    - For feed edges where "from" is null and "to.block" exists, emit connect_stream to the destination block using an IN port compatible with block type/policy.
    - For product edges where "to.block" is null, do not emit connect_stream for that missing destination.

    Must NOT do:
    - Stream/block creation calls.
    - Stream spec calls.
    - Block spec calls.
    - Property-method calls.

    Inputs:
    allowed_functions:
    {allowed_functions_json}

    already_planned_actions:
    {existing_actions_json}

    function_db:
    {function_db}

    stage_context:
    {stage_context_json}
    """.strip()
    )


    prompt_plan_stream_spec = PromptTemplate.from_template(
    """
    You are planning Aspen Plus COM calls for stage: STREAM_SPEC.

    Return ONLY valid JSON object with key "actions".
    Each action item fields: id, action, fn, args, reason.
    No markdown. No extra keys.

    Goal:
    - Generate stream/feed spec calls only (flow, basis, composition, T, P).
    - Use only values present in stage_context.
    - If a required value is missing, skip that action.

    Must do:
    - Use functions only from allowed_functions.
    - Respect function signatures in function_db.
    - Avoid duplicates against already_planned_actions.

    Must NOT do:
    - Stream/block creation calls.
    - Connectivity calls.
    - Block spec calls.
    - Property-method calls.

    Inputs:
    allowed_functions:
    {allowed_functions_json}

    already_planned_actions:
    {existing_actions_json}

    function_db:
    {function_db}

    stage_context:
    {stage_context_json}
    """.strip()
    )


    prompt_plan_block_spec = PromptTemplate.from_template(
    """
    You are planning Aspen Plus COM calls for stage: BLOCK_SPEC.

    Return ONLY valid JSON object with key "actions".
    Each action item fields: id, action, fn, args, reason.
    No markdown. No extra keys.

    Goal:
    - Generate only block-level spec calls from stage_context blocks.
    - Follow block type compatibility. If incompatible/uncertain, skip.
    - MHeatX-specific rule:
      - Apply this rule only when stage_context.blocks contains type MHEATX (HEATX alias allowed). If none exists, emit zero mheatx_* actions.
      - For each MHeatX block, define all outlet mappings first as outlet_streams mapping (in_stream -> out_stream).
      - If an MHeatX has N connected in/out stream pairs, set exactly N-1 specification values.
      - For MHeatX, use only function names and argument schema available in function_db (BlockMHeatX); if unavailable or mismatched, emit no MHeatX action.
      - Do not emit any mheatx_* spec/value action when outlet mappings are incomplete.
    Must do:
    - Use functions only from allowed_functions.
    - Respect function signatures in function_db.
    - Avoid duplicates against already_planned_actions.

    Must NOT do:
    - Stream specs.
    - Stream/block creation.
    - Connectivity.
    - Property-method calls.

    Inputs:
    allowed_functions:
    {allowed_functions_json}

    already_planned_actions:
    {existing_actions_json}

    function_db:
    {function_db}

    stage_context:
    {stage_context_json}
    """.strip()
    )


    PLAN_PROMPT_BY_STAGE = {
        "property_method": prompt_plan_property_method,
        "stream_block_create": prompt_plan_stream_block_create,
        "stream_block_connectivity": prompt_plan_stream_block_connectivity,
        "stream_spec": prompt_plan_stream_spec,
        "block_spec": prompt_plan_block_spec,
    }

    plan_llm = ChatOpenAI(model=part2_plan_model, temperature=1)
    action_plan_staged = {"stages": []}
    combined_actions = []

    for idx, stage in enumerate(PLAN_STAGES, start=1):
        stage_key = stage["key"]
        stage_prompt = PLAN_PROMPT_BY_STAGE.get(stage_key)
        if stage_prompt is None:
            raise RuntimeError(f"Missing plan prompt for stage: {stage_key}")

        stage_allowed = _allowed_functions_for_stage(function_db, stage_key)
        stage_context_obj = _stage_context(schema, stage_key)

        stage_chain = stage_prompt | plan_llm | StrOutputParser()
        with token_usage("part2_plan_generation", model=part2_plan_model, log_path=part2_token_usage_path, extra={"stage_key": stage_key}) as _callbacks:
            stage_raw = stage_chain.invoke({
                "allowed_functions_json": _json_dumps_safe(stage_allowed, indent=2),
                "existing_actions_json": _json_dumps_safe(combined_actions, indent=2),
                "function_db": function_db_json,
                "stage_context_json": _json_dumps_safe(stage_context_obj, indent=2),
            }, config={"callbacks": _callbacks})
        parsed = safe_json_loads(stage_raw)
        stage_actions = _coerce_actions(parsed)

        action_plan_staged["stages"].append({
            "index": idx,
            "key": stage_key,
            "title": stage["title"],
            "allowed_functions": stage_allowed,
            "stage_context": stage_context_obj,
            "actions": stage_actions,
        })
        combined_actions.extend(stage_actions)
        print(f"[PLAN] stage={stage_key} allowed={len(stage_allowed)} actions={len(stage_actions)}")

    action_plan = {"actions": combined_actions}
    if not isinstance(action_plan.get("actions"), list):
        raise RuntimeError("Invalid staged action plan schema")

    out_com_action_plan_path = os.path.splitext(out_com_path)[0] + "_action_plan.json"
    Path(out_com_action_plan_path).write_text(
        _json_dumps_safe(action_plan, indent=2),
        encoding="utf-8"
    )

    out_com_action_plan_staged_path = os.path.splitext(out_com_path)[0] + "_action_plan_staged.json"
    Path(out_com_action_plan_staged_path).write_text(
        _json_dumps_safe(action_plan_staged, indent=2),
        encoding="utf-8"
    )

    print("[OK] action plan generated:", out_com_action_plan_path)
    print("[OK] staged action plan generated:", out_com_action_plan_staged_path)
    print("actions_total:", len(action_plan.get("actions", [])))


def _generate_com_skeleton_code() -> None:
    """Verbatim body of part2_v1.ipynb cell 12 (COM skeleton code
    generation from the staged action plan)."""
    # com_skeleton generation (2/2): staged code draft and merge into single .py
    global action_plan, action_plan_staged

    if "action_plan" not in globals() or not isinstance(action_plan, dict):
        raise RuntimeError("action_plan not prepared. Run previous cell first.")

    if "action_plan_staged" not in globals() or not isinstance(action_plan_staged, dict):
        action_plan_staged = {
            "stages": [
                {
                    "index": 1,
                    "key": "all",
                    "title": "all actions",
                    "actions": action_plan.get("actions", []),
                }
            ]
        }


    def _sanitize_stage_code(text: str) -> str:
        out = []
        for raw in str(text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("```"):
                continue
            if line in {
                "doc = start_aspen_doc()",
                "open_aspen(doc, bkp_path, visible=False)",
            }:
                continue
            if line.startswith("import ") or line.startswith("from "):
                continue
            if line.startswith("def ") or line.startswith("class "):
                continue
            out.append(line)
        return "\n".join(out).strip()


    prompt_codegen_property_method = PromptTemplate.from_template(
    """
    You are generating Aspen Plus COM Python calls for stage: PROPERTY_METHOD.

    Output requirements:
    - Return ONLY Python top-level call statements.
    - No markdown, comments, imports, defs, classes, or helper assignments.
    - Do not emit start lines (they are added by caller):
      doc = start_aspen_doc()
      open_aspen(doc, bkp_path, visible=False)

    Logic:
    - Convert stage_actions to executable call lines in given order.
    - fn must be in allowed_functions.
    - Keep only property-method-related calls.
    - Skip invalid actions silently.
    - Avoid duplicates with existing_code.

    Inputs:
    allowed_functions:
    {allowed_functions_json}

    function_db:
    {function_db}

    stage_context:
    {stage_context_json}

    existing_code:
    {existing_code}

    stage_actions:
    {stage_actions_json}
    """.strip()
    )

    prompt_codegen_stream_block_create = PromptTemplate.from_template(
    """
    You are generating Aspen Plus COM Python calls for stage: STREAM_BLOCK_CREATE.

    Output requirements:
    - Return ONLY Python top-level call statements.
    - No markdown, comments, imports, defs, classes, or helper assignments.
    - Do not emit start lines (they are added by caller):
      doc = start_aspen_doc()
      open_aspen(doc, bkp_path, visible=False)

    Logic:
    - Convert stage_actions to executable call lines in given order.
    - fn must be in allowed_functions.
    - Keep only stream/block creation calls.
    - Never emit connectivity or spec calls.
    - Avoid duplicates with existing_code.

    Inputs:
    allowed_functions:
    {allowed_functions_json}

    function_db:
    {function_db}

    stage_context:
    {stage_context_json}

    existing_code:
    {existing_code}

    stage_actions:
    {stage_actions_json}
    """.strip()
    )

    prompt_codegen_stream_block_connectivity = PromptTemplate.from_template(
    """
    You are generating Aspen Plus COM Python calls for stage: STREAM_BLOCK_CONNECTIVITY.

    Output requirements:
    - Return ONLY Python top-level call statements.
    - No markdown, comments, imports, defs, classes, or helper assignments.
    - Do not emit start lines (they are added by caller):
      doc = start_aspen_doc()
      open_aspen(doc, bkp_path, visible=False)

    Logic:
    - Convert stage_actions to executable call lines in given order.
    - fn must be in allowed_functions.
    - Keep only connectivity calls.
    - Never emit creation/property/spec calls.
    - Avoid duplicates with existing_code.

    Inputs:
    allowed_functions:
    {allowed_functions_json}

    function_db:
    {function_db}

    stage_context:
    {stage_context_json}

    existing_code:
    {existing_code}

    stage_actions:
    {stage_actions_json}
    """.strip()
    )

    prompt_codegen_stream_spec = PromptTemplate.from_template(
    """
    You are generating Aspen Plus COM Python calls for stage: STREAM_SPEC.

    Output requirements:
    - Return ONLY Python top-level call statements.
    - No markdown, comments, imports, defs, classes, or helper assignments.
    - Do not emit start lines (they are added by caller):
      doc = start_aspen_doc()
      open_aspen(doc, bkp_path, visible=False)

    Logic:
    - Convert stage_actions to executable call lines in given order.
    - fn must be in allowed_functions.
    - Keep only stream/feed spec calls (flow, basis, composition, T, P).
    - Never emit creation/connectivity/property/block-spec calls.
    - Avoid duplicates with existing_code.

    Inputs:
    allowed_functions:
    {allowed_functions_json}

    function_db:
    {function_db}

    stage_context:
    {stage_context_json}

    existing_code:
    {existing_code}

    stage_actions:
    {stage_actions_json}
    """.strip()
    )

    prompt_codegen_block_spec = PromptTemplate.from_template(
    """
    You are generating Aspen Plus COM Python calls for stage: BLOCK_SPEC.

    Output requirements:
    - Return ONLY Python top-level call statements.
    - No markdown, comments, imports, defs, classes, or helper assignments.
    - Do not emit start lines (they are added by caller):
      doc = start_aspen_doc()
      open_aspen(doc, bkp_path, visible=False)

    Logic:
    - Convert stage_actions to executable call lines in given order.
    - fn must be in allowed_functions.
    - Keep only block-level spec calls.
    - Never emit creation/connectivity/property/stream-spec calls.
    - If a block spec action is incompatible with block type, skip it.
    - MHeatX-specific rule:
      - Apply this rule only when stage_context.blocks contains type MHEATX (HEATX alias allowed). If none exists, emit zero mheatx_* actions.
      - For each MHeatX block, define all outlet mappings first as outlet_streams mapping (in_stream -> out_stream).
      - If an MHeatX has N connected in/out stream pairs, set exactly N-1 specification values.
      - For MHeatX, use only function names and argument schema available in function_db (BlockMHeatX); if unavailable or mismatched, emit no MHeatX action.
      - Do not emit any mheatx_* spec/value action when outlet mappings are incomplete.
    - Avoid duplicates with existing_code.

    Inputs:
    allowed_functions:
    {allowed_functions_json}

    function_db:
    {function_db}

    stage_context:
    {stage_context_json}

    existing_code:
    {existing_code}

    stage_actions:
    {stage_actions_json}
    """.strip()
    )

    CODEGEN_PROMPT_BY_STAGE = {
        "property_method": prompt_codegen_property_method,
        "stream_block_create": prompt_codegen_stream_block_create,
        "stream_block_connectivity": prompt_codegen_stream_block_connectivity,
        "stream_spec": prompt_codegen_stream_spec,
        "block_spec": prompt_codegen_block_spec,
    }

    code_llm = ChatOpenAI(model=part2_code_model, temperature=1)

    stage_chunks = []
    accum_code = ""
    for stage in action_plan_staged.get("stages", []):
        if not isinstance(stage, dict):
            continue
        stage_key = str(stage.get("key") or "")
        stage_actions = stage.get("actions") if isinstance(stage.get("actions"), list) else []
        if not stage_actions:
            continue

        stage_prompt = CODEGEN_PROMPT_BY_STAGE.get(stage_key)
        if stage_prompt is None:
            print(f"[CODEGEN] stage={stage_key} skipped (no stage prompt)")
            continue
        stage_chain = stage_prompt | code_llm | StrOutputParser()

        allowed_functions = stage.get("allowed_functions") if isinstance(stage.get("allowed_functions"), list) else []
        stage_context_obj = stage.get("stage_context") if isinstance(stage.get("stage_context"), dict) else {}

        with token_usage("part2_com_codegen", model=part2_code_model, log_path=part2_token_usage_path, extra={"stage_key": stage_key}) as _callbacks:
            chunk_raw = stage_chain.invoke({
                "allowed_functions_json": json.dumps(allowed_functions, ensure_ascii=False, indent=2),
                "function_db": function_db_json,
                "stage_context_json": json.dumps(stage_context_obj, ensure_ascii=False, indent=2),
                "existing_code": accum_code,
                "stage_actions_json": json.dumps(stage_actions, ensure_ascii=False, indent=2),
            }, config={"callbacks": _callbacks})
        chunk = _sanitize_stage_code(chunk_raw)
        if not chunk:
            print(f"[CODEGEN] stage={stage_key} skipped (empty chunk)")
            continue

        stage_chunks.append({
            "key": stage_key,
            "code": chunk,
        })
        accum_code = (accum_code + "\n" + chunk).strip()
        print(f"[CODEGEN] stage={stage_key} lines={len(chunk.splitlines())}")

    header = "doc = start_aspen_doc()\nopen_aspen(doc, bkp_path, visible=False)"
    body = "\n\n".join([c["code"] for c in stage_chunks if c.get("code")])
    com_code = (header + "\n\n" + body).strip() + "\n"

    com_code = merge_feed_spec_code(com_code, schema)
    com_code = merge_compressor_defaults_code(com_code, schema)
    com_code = merge_splitter_fractions_code(com_code, schema)

    with open(out_com_path, "w", encoding="utf-8") as f:
        f.write(com_code)

    print("[OK] wrote:", out_com_path)
    print("chars:", len(com_code))


def _execute_com_skeleton() -> None:
    """Verbatim body of part2_v1.ipynb cell 14 -- executes com_skeleton.py
    statement-by-statement via run_exec_script(), which is how `doc` (the
    Aspen COM handle) first comes into existence, created on whatever
    thread calls this function. Never call this from a different thread
    than every other doc-touching function runs on."""
    global doc, com_initial_saved, bkp_path
    # inp에서 변환한 bkp 파일 경로
    # NOTE: must be a module global, not a local -- run_exec_script() execs
    # com_skeleton.py's generated statements (e.g. `open_aspen(doc, bkp_path, ...)`)
    # against globals(), so a local bkp_path here would be invisible to them
    # (this caused "name 'bkp_path' is not defined" -> doc never initialized
    # -> every subsequent COM statement failed).
    bkp_path = out_bkp_path
    print(f"[PART2] bkp_path: {bkp_path}")


    # com_skeleton 실행 (safe)
    com_skeleton_ok = run_exec_script(out_com_path, stage="com_skeleton_exec")
    print(f"[PART2] com_skeleton_ok={com_skeleton_ok} | path={out_com_path}")

    com_initial_saved = False
    if "doc" in globals() and doc is not None:
        try:
            save_aspen(doc, out_com_initial_bkp_path)
            com_initial_saved = Path(out_com_initial_bkp_path).exists()
            print(f"[PART2] com_proto_initial saved={com_initial_saved} | path={out_com_initial_bkp_path}")
        except Exception as e:
            _record_error("com_initial_save", e)
    else:
        _record_error("com_initial_save", RuntimeError("doc unavailable after com_skeleton_exec"))


# ===========================================================================
# Phase-aware validation helpers (cell 16, verbatim)
# ===========================================================================
# Phase-aware validation helpers.
from valid_common import *
import valid_connectivity as _vconn                              # Valve / FSplit support (shared 2608 validator has neither)
from valid_connectivity import run as run_connectivity
from valid_feed import run as run_feeds
import valid_block_specification as vbs
from valid_block_specification import run as run_block_specs
from valid_property_schema_repair import validate_property_method_from_schema

# the shared connectivity validator reports block_type_not_supported for VALVE and FSPLIT, and the block-spec rules have no entry
# for them, so every flowsheet with a valve or a splitter failed Part 2 validation.  Same port rules as Aspen enforces: valve = 1 in / 1 out;
# splitter = >= 1 in, >= 2 outs (last outlet fraction is left to Aspen).  The shared files are not touched.
_vconn.PORT_CANDIDATES.setdefault("VALVE", ["F(IN)", "P(OUT)"])
_vconn.PORT_CANDIDATES.setdefault("FSPLIT", ["F(IN)", "P(OUT)"])
_orig_validate_connectivity_one = _vconn.validate_connectivity_one


def _validate_connectivity_one_p14(doc, block_name, stream_names):
    btype = _vconn.get_block_type(doc, block_name)
    if btype not in {"VALVE", "FSPLIT"}:
        return _orig_validate_connectivity_one(doc, block_name, stream_names)
    ports_map = _vconn.get_port_connections(doc, block_name, btype, stream_names)
    missing: List[Dict[str, Any]] = []
    _vconn._count_check_min(missing, block_name, "feed", len(ports_map.get("F(IN)", [])), 1, {"port": "F(IN)"})
    if btype == "VALVE":
        _vconn._count_check_exact(missing, block_name, "product", len(ports_map.get("P(OUT)", [])), 1, {"port": "P(OUT)"})
    else:
        _vconn._count_check_min(missing, block_name, "product", len(ports_map.get("P(OUT)", [])), 2, {"port": "P(OUT)"})
    return {"block": block_name, "type": btype, "ports_map": ports_map}, missing


_vconn.validate_connectivity_one = _validate_connectivity_one_p14
_PHASE14_BLOCK_RULES = {"VALVE": {"engine": "generic", "fields": {}, "rules": []},
                        "FSPLIT": {"engine": "generic", "fields": {}, "rules": []}}


def _load_block_rules(base_dir: str, rules_filename: str = "valid_block_rules.yaml") -> Dict[str, Any]:
    path = Path(base_dir) / rules_filename
    if not path.exists():   # shared files may sit in a sub-folder of the data dir (see paths.py)
        path = shared_file(rules_filename)
    if not path.exists():
        raise FileNotFoundError(f"Block rules YAML not found: {path}")
    rules = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    blocks = rules.setdefault("blocks", {})
    for name, rule in _PHASE14_BLOCK_RULES.items():
        blocks.setdefault(name, dict(rule))
    return rules


def _safe_validator_result(name: str, fn, strict: bool = False) -> Dict[str, Any]:
    try:
        return fn()
    except Exception as e:
        if strict:
            raise
        return {
            "validator": name,
            "ok": False,
            "summary": {"n_targets": 0, "n_issues": 1, "fatal_error": str(e)},
            "targets": {"blocks": [], "feeds": [], "products": []},
            "issues": [{
                "category": "VALIDATOR_FATAL",
                "target_type": "SYSTEM",
                "target": name,
                "rule_id": "validator_exception",
                "message": str(e),
                "path": None,
                "current": None,
                "required": "validator should run without exception",
                "detail": {"traceback": traceback.format_exc()},
            }],
            "raw": {},
        }


def run_validator_phase(
    doc,
    phase: str,
    base_dir: str,
    blocks: Optional[List[str]] = None,
    print_report: bool = True,
    strict: bool = False,
) -> Dict[str, Any]:
    phase = str(phase).lower().strip()
    bundle: Dict[str, Any] = {}

    if phase == "property_method":
        bundle["property_method"] = _safe_validator_result(
            "property_method",
            lambda: validate_property_method_from_schema(doc=doc, schema=schema, print_report=print_report),
            strict=strict,
        )
    elif phase == "connectivity":
        bundle["connectivity"] = _safe_validator_result(
            "connectivity",
            lambda: run_connectivity(doc=doc, blocks=blocks, print_report=print_report),
            strict=strict,
        )
    elif phase == "feeds":
        bundle["feeds"] = _with_feed_unit_check(doc, _safe_validator_result(
            "feeds",
            lambda: run_feeds(doc=doc, print_report=print_report),
            strict=strict,
        ))
    elif phase == "block_specs":
        rules_all = _load_block_rules(base_dir)
        resolved_blocks = blocks if blocks is not None else vbs.list_blocks(doc)
        bundle["block_specs"] = _safe_validator_result(
            "block_specs",
            lambda: run_block_specs(doc=doc, rules_all=rules_all, blocks=resolved_blocks, print_report=print_report),
            strict=strict,
        )
    elif phase in {"all", "final_all"}:
        for ph in ["property_method", "connectivity", "feeds", "block_specs"]:
            sub = run_validator_phase(doc, ph, base_dir=base_dir, blocks=blocks, print_report=print_report, strict=strict)
            bundle.update(sub.get("bundle", {}))
    else:
        raise ValueError(f"Unknown validation phase: {phase}")

    def _issues(d: Any) -> List[dict]:
        return (d or {}).get("issues", []) or []

    n_issues = sum(len(_issues(v)) for v in bundle.values())
    ok = (n_issues == 0) and all(bool((v or {}).get("ok", False)) for v in bundle.values())
    summary = {
        "phase": phase,
        "ok": ok,
        "n_issues": n_issues,
        "issues_by_validator": {k: len(_issues(v)) for k, v in bundle.items()},
    }
    return {"ok": ok, "n_issues": n_issues, "bundle": bundle, "summary": summary}


def run_all_validators(
    doc,
    base_dir: str,
    blocks: Optional[List[str]] = None,
    print_report: bool = True,
    rules_filename: str = "valid_block_rules.yaml",
    strict: bool = False,
    unify_blocks: bool = True,
    debug_blocks: bool = True,
) -> Dict[str, Any]:
    return run_validator_phase(doc, "final_all", base_dir=base_dir, blocks=blocks, print_report=print_report, strict=strict)


def normalize_issues_from_v(v: dict) -> list[dict]:
    issues = []
    for validator_name, d in ((v or {}).get("bundle", {}) or {}).items():
        for it in (d or {}).get("issues", []) or []:
            if isinstance(it, dict):
                x = dict(it)
                x["validator"] = validator_name
                issues.append(x)
    return issues


def print_phase_summary(v: Dict[str, Any], max_issues: int = 20) -> None:
    print(json.dumps(v.get("summary", {}), ensure_ascii=False, indent=2))
    issues = normalize_issues_from_v(v)
    for it in issues[:max_issues]:
        print(f" - [{it.get('validator')}] {it.get('target_type')}:{it.get('target')} | {it.get('rule_id')} | {it.get('message')}")


v_val = {
    "ok": False,
    "n_issues": 0,
    "bundle": {},
    "summary": {"note": "phase-aware validation deferred to repair loop"},
}
print("[PART2] phase-aware validation helpers loaded; run deferred to phase repair loop")


# ===========================================================================
# Phase context + completion helpers (cell 18, minus the 4 patch-safety
# functions now imported from part3_patch_safety.py instead) + the schema/
# aspen_code reload wrapped into a function.
# ===========================================================================
def _load_phase_context_data() -> None:
    """Verbatim body of part2_v1.ipynb cell 18's 2-line schema/aspen_code
    reload (yes, this re-reads schema from disk a 4th time, discarding
    cell10's in-memory alias-resolved/Z-normalized version -- preserved
    as-is, matching current behavior exactly, not a bug for this
    refactor to fix)."""
    global aspen_code, schema
    aspen_code = yaml.safe_load(Path(db_com_path).read_text(encoding="utf-8"))
    schema = yaml.safe_load(Path(out_validated_schema_path).read_text(encoding="utf-8")) if Path(out_validated_schema_path).exists() else {}




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


def _schema_flowsheet(schema_obj: Dict[str, Any]) -> Dict[str, Any]:
    return (schema_obj or {}).get("flowsheet_schema") or (schema_obj or {}).get("flowsheet") or {}


def _schema_io(schema_obj: Dict[str, Any]) -> Dict[str, Any]:
    return (schema_obj or {}).get("io") or (schema_obj or {}).get("io_schema") or {}


def _block_type_map(schema_obj: Dict[str, Any]) -> Dict[str, str]:
    fs = _schema_flowsheet(schema_obj)
    out = {}
    for b in fs.get("blocks", []) or []:
        if isinstance(b, dict) and b.get("id"):
            out[str(b["id"])] = str(b.get("type", "")).upper()
    return out


def _edge_with_ports(edge: Dict[str, Any], block_types: Dict[str, str]) -> Dict[str, Any]:
    e = dict(edge or {})
    for side in ["from", "to"]:
        endpoint = e.get(side)
        if not isinstance(endpoint, dict):
            continue
        role = str(endpoint.get("port_role") or "").upper()
        block = endpoint.get("block")
        endpoint = dict(endpoint)
        endpoint["aspen_port"] = PORT_ROLE_TO_ASPEN_PORT.get(role)
        endpoint["block_type"] = block_types.get(str(block)) if block else None
        e[side] = endpoint
    return e


def schema_phase_context(phase: str, schema_obj: Dict[str, Any]) -> Dict[str, Any]:
    phase = str(phase).lower().strip()
    fs = _schema_flowsheet(schema_obj)
    io = _schema_io(schema_obj)
    block_types = _block_type_map(schema_obj)

    if phase == "property_method":
        return {
            "property_method": ((schema_obj or {}).get("aspen_schema") or {}).get("property_method"),
            "instruction": "Set only the Aspen property method from aspen_schema.property_method. Do not change components, streams, blocks, or specs in this phase.",
        }

    if phase == "connectivity":
        return {
            "blocks": fs.get("blocks", []) or [],
            "streams": fs.get("streams", []) or [],
            "expected_edges": [
                _edge_with_ports(e, block_types)
                for e in ((fs.get("connectivity") or {}).get("edges", []) or [])
                if isinstance(e, dict)
            ],
            "port_role_to_aspen_port": PORT_ROLE_TO_ASPEN_PORT,
            "instruction": "For missing stream objects, call stream_add before connect_stream. For each edge endpoint, use endpoint.aspen_port as connect_stream port_name. Do not invent IDs; use expected_edges.",
        }

    if phase == "feeds":
        feed_streams = {str((f or {}).get("stream")) for f in (io.get("feeds", []) or []) if isinstance(f, dict)}
        return {
            "feed_specs": io.get("feeds", []) or [],
            "feed_edges": [
                _edge_with_ports(e, block_types)
                for e in ((fs.get("connectivity") or {}).get("edges", []) or [])
                if isinstance(e, dict) and str(e.get("stream")) in feed_streams
            ],
            "instruction": "Set only feed stream input values from feed_specs. Do not modify internal/product streams in this phase.",
        }

    if phase == "block_specs":
        return {
            "blocks": fs.get("blocks", []) or [],
            "instruction": "Set only block specifications from blocks[].specs. Do not change connectivity or feed stream specs in this phase.",
        }

    return {"schema": schema_obj}


PHASE_FUNCTION_ALLOWLIST = {
    "property_method": {"set_property_method"},
    "connectivity": {"connect_stream", "disconnect_stream", "stream_add", "block_add"},
    "feeds": {
        "stream_set_temperature", "stream_set_pressure", "stream_set_flow_base",
        "stream_set_total_flow", "stream_set_basis", "stream_set_component_value",
        "stream_set_components", "stream_define",
    },
    "block_specs": {
        "heater_set_spec", "heater_set_temperature", "heater_set_pressure", "heater_set_duty", "heater_set_vapor_frac", "heater_config",
        "pump_set_type", "pump_set_spec", "pump_set_eff", "pump_set_nphase", "pump_configure",
        "rgibbs_set_opt_chemeq", "rgibbs_set_spec", "rgibbs_set_temperature", "rgibbs_set_pressure", "rgibbs_set_duty", "rgibbs_config",
        "rplug_set_type", "rplug_set_opt_tspec", "rplug_set_reactor_temp", "rplug_set_temp_profile", "rplug_set_multitube", "rplug_set_ntube", "rplug_set_length", "rplug_set_diameter", "rplug_set_phase", "rplug_set_pressure", "rplug_set_pressure_drop", "rplug_set_catalyst_presence", "rplug_set_catalyst_props",
        "mheatx_set_outlet_stream", "mheatx_set_spec", "mheatx_set_value", "mheatx_configure",
        "flash_set_spec", "flash_set_temperature", "flash_set_pressure", "flash_set_duty", "flash_set_vapor_frac", "flash_config",
        "splt_set_frac",
        "comp_set_model_type", "comp_set_type", "comp_set_spec", "comp_set_eff", "comp_set_nphase", "comp_configure",
        "radfrac_set_calc_mode", "radfrac_set_nstage", "radfrac_set_condenser", "radfrac_set_reboiler", "radfrac_set_no_phase", "radfrac_set_spec", "radfrac_set_feed_stage", "radfrac_set_stage_pressure", "radfrac_configure",
    },
}


def write_com_completion_script(code_text: str, out_path: str) -> str:
    Path(out_path).write_text(code_text.strip() + "\n", encoding="utf-8")
    return out_path


print("[PART2] phase completion helpers loaded")


# ===========================================================================
# Phase-based schema repair engine (cell 24, with the fixed _run_save_and_check
# from cell 30 in place of cell 24's original -- see final_validate_node's
# docstring below for why no monkeypatch is needed here).
# ===========================================================================
# Phase-based schema repair -> validation -> optional LLM fallback -> final Aspen run/save.
import importlib
import re
import valid_property_schema_repair as _prop_repair_mod
import valid_connectivity_schema_repair as _conn_repair_mod
import valid_feed_schema_repair as _feed_repair_mod
import valid_block_schema_repair as _block_repair_mod
importlib.reload(_prop_repair_mod)
importlib.reload(_conn_repair_mod)
importlib.reload(_feed_repair_mod)
importlib.reload(_block_repair_mod)
from valid_property_schema_repair import repair_property_method_from_schema
from valid_connectivity_schema_repair import repair_connectivity_from_schema
from valid_feed_schema_repair import repair_feeds_from_schema
from valid_block_schema_repair import repair_block_specs_from_schema

RUN_PHASES = ["property_method", "connectivity", "feeds", "block_specs"]
PHASE_MAX_ITER = 3  # placeholder -- build_session() sets this for real from part2_input
ENABLE_COMPLETION = True
PRINT_SCHEMA_REPAIR_DETAIL = False
PRINT_PATCHPLAN_JSON = False
PRINT_PHASE_RESULTS_JSON = False

part2_loop_status = {
    "attempts": 0,
    "run_executed": False,
    "his_path": "",
    "his_exists": False,
    "his_nonempty": False,
    "counts_reliable": False,
    "error_count": None,
    "warning_count": None,
    "severe_blocks": None,
    "error_blocks": None,
    "warning_blocks": None,
    "reason": "",
    "phase": "",
}


def inspect_his_status_local(working_dir: str, preferred_his: str = ""):
    wd = Path(working_dir)
    preferred_path = Path(preferred_his) if preferred_his else None
    his_files = sorted(wd.glob("*.his"), key=lambda x: x.stat().st_mtime, reverse=True)

    if preferred_path:
        if preferred_path.exists() and preferred_path.suffix.lower() == ".his":
            his_files = [preferred_path] + [p for p in his_files if p.resolve() != preferred_path.resolve()]
        else:
            return {
                "run_executed": False,
                "his_path": str(preferred_path),
                "his_exists": False,
                "his_nonempty": False,
                "counts_reliable": False,
                "error_count": None,
                "warning_count": None,
                "severe_blocks": None,
                "error_blocks": None,
                "warning_blocks": None,
                "reason": "missing_preferred_his",
            }

    if not his_files:
        return {
            "run_executed": False,
            "his_path": "",
            "his_exists": False,
            "his_nonempty": False,
            "counts_reliable": False,
            "error_count": None,
            "warning_count": None,
            "severe_blocks": None,
            "error_blocks": None,
            "warning_blocks": None,
            "reason": "no_his_file",
        }

    his_path = his_files[0]
    text = his_path.read_text(encoding="utf-8", errors="ignore")
    his_nonempty = bool(text.strip())
    if not his_nonempty:
        return {
            "run_executed": False,
            "his_path": str(his_path),
            "his_exists": True,
            "his_nonempty": False,
            "counts_reliable": False,
            "error_count": None,
            "warning_count": None,
            "severe_blocks": None,
            "error_blocks": None,
            "warning_blocks": None,
            "reason": "empty_his_file",
        }

    real_run_patterns = [
        r"(?im)RUN\s+STATUS",
        r"(?im)NORMAL\s+COMPLETION",
        r"(?im)TERMINATED\s+NORMALLY",
        r"(?im)CALCULATIONS?\s+BEGIN",
        r"(?im)UOS\s+BLOCK",
        r"(?im)RESULTS?\s+GENERATED",
        r"(?im)\bRUN\s+SAVED\b",
    ]
    has_real_run_trace = any(re.search(pat, text) for pat in real_run_patterns)
    has_no_error_warn_banner = bool(re.search(r"(?im)NO\s+ERRORS?\s+OR\s+WARNINGS?\s+GENERATED", text))
    run_executed = bool(has_real_run_trace)

    m_err = re.search(r"(?im)\bERRORS?\b\s*[:=]\s*(\d+)", text)
    m_warn = re.search(r"(?im)\bWARNINGS?\b\s*[:=]\s*(\d+)", text)
    severe_blocks = len(re.findall(r"(?im)^\s*\*\*\s*SEVERE\s+ERRORS?\b", text))
    error_blocks = len(re.findall(r"(?im)^\s*\*\*\s*ERRORS?\b", text))
    warning_blocks = len(re.findall(r"(?im)^\s*\*\*\s*WARNINGS?\b", text))
    counts_reliable = bool(
        run_executed and (
            m_err or m_warn or severe_blocks > 0 or error_blocks > 0 or warning_blocks > 0 or has_no_error_warn_banner
        )
    )

    return {
        "run_executed": run_executed,
        "his_path": str(his_path),
        "his_exists": True,
        "his_nonempty": True,
        "counts_reliable": counts_reliable,
        "error_count": int(m_err.group(1)) if m_err else None,
        "warning_count": int(m_warn.group(1)) if m_warn else None,
        "severe_blocks": severe_blocks if counts_reliable else None,
        "error_blocks": error_blocks if counts_reliable else None,
        "warning_blocks": warning_blocks if counts_reliable else None,
        "reason": "ok" if run_executed else ("no_real_run_trace" if has_no_error_warn_banner else "no_run_trace_in_his"),
    }


def _phase_completion_paths(phase: str, attempt: int) -> str:
    base = Path(out_com_compl_path)
    return str(base.with_name(f"{base.stem}_{phase}_attempt{attempt}{base.suffix}"))


def _phase_completion_bkp_path(phase: str, attempt: int) -> str:
    base = Path(out_com_completion_bkp_path)
    return str(base.with_name(f"{base.stem}_{phase}_attempt{attempt}{base.suffix}"))


def _print_lines(title: str, lines: List[str], max_lines: int = 12) -> None:
    if not lines:
        return
    print(f"  {title}:")
    for line in lines[:max_lines]:
        print(f"    - {line}")
    if len(lines) > max_lines:
        print(f"    ... +{len(lines) - max_lines} more")


def _format_action(action: Dict[str, Any]) -> str:
    name = action.get("action", "action")
    if name == "block_add":
        return f"block_add: {action.get('block')} ({action.get('btype')})"
    if name == "stream_add":
        return f"stream_add: {action.get('stream')} ({action.get('stype', 'MATERIAL')})"
    if name == "connect_stream":
        return f"connect_stream: {action.get('block')} / {action.get('port')} / {action.get('stream')}"
    if name == "set_property_method":
        return f"set_property_method: {action.get('method')}"
    if "stream" in action:
        return f"{name}: {action.get('stream')}"
    if "block" in action:
        return f"{name}: {action.get('block')}"
    return name


def _print_schema_repair_summary(phase: str, report: Optional[Dict[str, Any]]) -> None:
    if not report:
        return
    actions = report.get("actions") or []
    errors = report.get("errors") or []
    remaining = report.get("remaining") or report.get("remaining_missing_connections") or []
    schema_missing = report.get("schema_missing") or []
    status = "ok" if report.get("ok") else "needs follow-up"
    print(f"[{phase}] schema repair: {status} | actions={len(actions)} | errors={len(errors)}")

    if phase == "property_method":
        expected = report.get("expected")
        before = report.get("current_before")
        after = report.get("current_after")
        if expected or before or after:
            print(f"  method: current={before} -> {after} | expected={expected}")
    elif phase == "connectivity":
        detected = []
        detected += [f"missing stream: {x}" for x in report.get("missing_streams", [])]
        detected += [f"missing block: {x}" for x in report.get("missing_blocks", [])]
        detected += [f"missing connection: {b} / {p} / {s}" for b, s, p in report.get("missing_connections_before_repair", [])]
        detected += [f"extra stream: {x}" for x in report.get("extra_streams", [])]
        detected += [f"extra connection: {x}" for x in report.get("extra_connections", [])]
        _print_lines("detected", detected)
    elif phase == "feeds":
        feed_issues = []
        for item in report.get("inspected") or []:
            before = item.get("before", {})
            missing = before.get("missing") or []
            if missing:
                feed_issues.append(f"{item.get('stream')}: missing={missing}")
        _print_lines("detected", feed_issues)
    elif phase == "block_specs":
        targets = report.get("target_blocks") or []
        if targets:
            print(f"  target blocks: {', '.join(targets)}")

    _print_lines("applied", [_format_action(a) for a in actions])
    _print_lines("remaining", [str(x) for x in remaining])
    _print_lines("schema_missing", [str(x) for x in schema_missing])
    _print_lines("errors", [str(x) for x in errors], max_lines=6)


def _print_validation_summary(phase: str, attempt: int, v_phase: Dict[str, Any], max_issues: int = 10) -> None:
    summary = v_phase.get("summary", {})
    print(f"[{phase}] validation {attempt}: ok={summary.get('ok')} | issues={summary.get('n_issues', 0)}")
    issues = normalize_issues_from_v(v_phase)
    for item in issues[:max_issues]:
        print(f"  - {item.get('target_type')}:{item.get('target')} | {item.get('rule_id')} | {item.get('message')}")
    if len(issues) > max_issues:
        print(f"  ... +{len(issues) - max_issues} more issues")


def _compact_phase_result(result: Dict[str, Any]) -> Dict[str, Any]:
    report = result.get("schema_repair") or {}
    last = result.get("last") or {}
    completion_attempts = []
    for item in result.get("history") or []:
        if item.get("completion_path") or item.get("completion_bkp_path") or item.get("patchplan"):
            completion_attempts.append({
                "attempt": item.get("attempt"),
                "completion_path": item.get("completion_path"),
                "completion_bkp_path": item.get("completion_bkp_path"),
                "exec_ok": item.get("exec_ok"),
            })
    return {
        "phase": result.get("phase"),
        "ok": result.get("ok"),
        "schema_repair_ok": report.get("ok"),
        "schema_repair_actions": len(report.get("actions") or []),
        "schema_repair_errors": len(report.get("errors") or []),
        "validation": last.get("summary", {}),
        "completion_attempts": completion_attempts,
    }


def _generate_patchplan_for_phase(phase: str, v_phase: Dict[str, Any], catalog_subset: Dict[str, Any], schema_repair_report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    issues_norm = normalize_issues_from_v(v_phase)
    prompt = PromptTemplate.from_template("""
You are an Aspen Plus COM code patch planner.

Return ONLY valid JSON. No markdown. No explanation.

Output schema:
{{"patches":[{{"id":str,"action":"call","fn":str,"args":object,"units":object,"reason":str}}]}}

Phase: {phase}

Rules:
- action must be "call".
- Use ONLY function names appearing in aspen_code_catalog_subset.
- Args must EXACTLY match the args defined in catalog except "doc". Never include "doc" in args.
- Every numeric argument must have a unit in "units".
- Do not invent new blocks or streams unless the issue explicitly requires a missing object.
- Do not change IDs.
- Prefer the smallest patch that fixes the current phase only.

VALIDATION SUMMARY:
{validation_summary}

ISSUES:
{issues_norm}

SCHEMA REPAIR REPORT:
{schema_repair_report}

Executable fallback policy:
- If phase is feeds and SCHEMA REPAIR REPORT contains schema_missing or assumptions_needed, choose minimum executable feed inputs only for the missing items.
- If phase is block_specs and SCHEMA REPAIR REPORT contains schema_missing, choose minimum executable block specifications only for the missing items.
- Mark every such fallback patch reason with "low-confidence executable fallback; schema_missing".
- Do not claim fallback values are paper-validated.
- Do not change values that are already valid unless required to make the current phase executable.

ASPEN CODE CATALOG SUBSET:
{catalog_yaml}

SCHEMA PHASE CONTEXT:
{schema_phase_context}

FULL SCHEMA YAML:
{schema_yaml}

Return JSON now.
""".strip())
    llm = ChatOpenAI(model=part2_repair_plan_model, temperature=1)
    response = (prompt | llm).invoke({
        "phase": phase,
        "validation_summary": json.dumps(v_phase.get("summary", {}), ensure_ascii=False, indent=2),
        "issues_norm": json.dumps(issues_norm, ensure_ascii=False, indent=2),
        "schema_repair_report": json.dumps(schema_repair_report or {}, ensure_ascii=False, indent=2),
        "catalog_yaml": yaml.safe_dump(catalog_subset, sort_keys=False, allow_unicode=True),
        "schema_phase_context": yaml.safe_dump(schema_phase_context(phase, schema), sort_keys=False, allow_unicode=True),
        "schema_yaml": yaml.safe_dump(schema, sort_keys=False, allow_unicode=True),
    })
    return json.loads(response.content.strip())


def _generate_completion_code(patchplan: Dict[str, Any], catalog_subset: Dict[str, Any]) -> str:
    prompt_codegen = PromptTemplate.from_template("""
You are generating a Python script file for Aspen COM completion.

Output ONLY raw Python code. No markdown. No explanation.

Requirements:
- Do NOT define functions.
- Do NOT create/open/save/close Aspen document.
- Assume `doc` already exists in globals().
- Output ONLY patch call lines.
- Use ONLY helper functions in the catalog.

patchplan JSON:
{patchplan_json}

aspen_code catalog subset YAML:
{catalog_yaml}

Output Python code now.
""".strip())
    llm_codegen = ChatOpenAI(model=part2_repair_codegen_model, temperature=1)
    resp = (prompt_codegen | llm_codegen).invoke({
        "patchplan_json": json.dumps(patchplan, ensure_ascii=False, indent=2),
        "catalog_yaml": yaml.safe_dump(catalog_subset, sort_keys=False, allow_unicode=True),
    })
    return resp.content.strip()


def repair_phase(phase: str, max_iter: int = 3) -> Dict[str, Any]:
    allowed = PHASE_FUNCTION_ALLOWLIST[phase]
    catalog_subset = filter_catalog_by_functions(aspen_code, allowed)
    fn_index = build_fn_index(catalog_subset)
    history = []
    schema_repair_report = None
    print(f"\n========== PHASE: {phase} ==========")

    if phase == "property_method":
        schema_repair_report = repair_property_method_from_schema(doc, schema, apply=True, print_report=PRINT_SCHEMA_REPAIR_DETAIL)
        history.append({"attempt": 0, "schema_repair": schema_repair_report})
        _print_schema_repair_summary(phase, schema_repair_report)
    elif phase == "connectivity":
        schema_repair_report = repair_connectivity_from_schema(
            doc,
            schema,
            apply=True,
            remove_extra_streams=False,
            remove_extra_connections=False,
            print_report=PRINT_SCHEMA_REPAIR_DETAIL,
        )
        history.append({"attempt": 0, "schema_repair": schema_repair_report})
        _print_schema_repair_summary(phase, schema_repair_report)
    elif phase == "feeds":
        schema_repair_report = repair_feeds_from_schema(
            doc,
            schema,
            apply=True,
            print_report=PRINT_SCHEMA_REPAIR_DETAIL,
            resolved_components_path=globals().get("resolved_out_path"),
        )
        history.append({"attempt": 0, "schema_repair": schema_repair_report})
        _print_schema_repair_summary(phase, schema_repair_report)
    elif phase == "block_specs":
        pre_v_phase = run_validator_phase(doc, phase, base_dir=rules_base_dir, print_report=False, strict=False)
        pre_issues = normalize_issues_from_v(pre_v_phase)
        target_blocks = sorted({
            str(it.get("target")).strip()
            for it in pre_issues
            if it.get("target_type") == "BLOCK" and str(it.get("target") or "").strip()
        })
        history.append({"attempt": 0, "pre_validation": pre_v_phase.get("summary", {})})
        if target_blocks:
            print(f"[block_specs] pre-validation issues -> target blocks: {target_blocks}")
            schema_repair_report = repair_block_specs_from_schema(doc, schema, apply=True, print_report=PRINT_SCHEMA_REPAIR_DETAIL, target_blocks=target_blocks)
        else:
            schema_repair_report = {
                "ok": True,
                "applied": False,
                "policy": {"targeting": "validation_issue_blocks_only"},
                "target_blocks": [],
                "actions": [],
                "skipped": [],
                "schema_missing": [],
                "errors": [],
                "repair_summary": [],
            }
            print("[schema_block_spec_repair] no validation issue blocks -> skipped")
        history.append({"attempt": 0, "schema_repair": schema_repair_report})
        _print_schema_repair_summary(phase, schema_repair_report)

    for attempt in range(1, max_iter + 1):
        v_phase = run_validator_phase(doc, phase, base_dir=rules_base_dir, print_report=False, strict=False)
        _print_validation_summary(phase, attempt, v_phase)
        history.append({"attempt": attempt, "validation": v_phase.get("summary", {})})

        if v_phase.get("ok"):
            print(f"[{phase}] ok -> phase complete")
            return {"phase": phase, "ok": True, "history": history, "last": v_phase, "schema_repair": schema_repair_report}
        if not ENABLE_COMPLETION:
            print(f"[{phase}] completion disabled")
            return {"phase": phase, "ok": False, "history": history, "last": v_phase, "schema_repair": schema_repair_report}

        patchplan = _generate_patchplan_for_phase(phase, v_phase, catalog_subset, schema_repair_report=schema_repair_report)
        safety_check_patchplan(patchplan, fn_index)
        print(f"[{phase}] patches:", len(patchplan.get("patches", [])))
        if PRINT_PATCHPLAN_JSON:
            print(json.dumps(patchplan, ensure_ascii=False, indent=2))
        code_text = _generate_completion_code(patchplan, catalog_subset)
        out_path = _phase_completion_paths(phase, attempt)
        write_com_completion_script(code_text, out_path)
        print(f"[{phase}] wrote: {out_path}")
        ok_exec = run_exec_script(out_path, stage=f"completion_{phase}_{attempt}")
        print(f"[{phase}] exec_ok={ok_exec}")
        attempt_bkp_path = _phase_completion_bkp_path(phase, attempt)
        try:
            save_aspen(doc, attempt_bkp_path)
            globals()["com_completion_saved"] = Path(attempt_bkp_path).exists()
            print(f"[{phase}] com_completion saved={globals().get('com_completion_saved')} | path={attempt_bkp_path}")
        except Exception as e:
            _record_error(f"completion_{phase}_{attempt}_save", e)
            attempt_bkp_path = ""
        if history:
            history[-1]["completion_bkp_path"] = attempt_bkp_path
        if not ok_exec:
            return {"phase": phase, "ok": False, "history": history, "last": v_phase, "exec_failed": out_path, "completion_bkp_path": attempt_bkp_path, "schema_repair": schema_repair_report}

    v_phase = run_validator_phase(doc, phase, base_dir=rules_base_dir, print_report=False, strict=False)
    _print_validation_summary(phase, max_iter + 1, v_phase)
    return {"phase": phase, "ok": bool(v_phase.get("ok")), "history": history, "last": v_phase, "schema_repair": schema_repair_report}


def _wait_for_his_candidates_part2(candidates, started_at, timeout_seconds=30.0, interval_seconds=0.5):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        for candidate in candidates:
            path = Path(candidate)
            if path.exists() and path.stat().st_size > 0 and path.stat().st_mtime >= started_at - 1.0:
                return str(path)
        time.sleep(interval_seconds)
    return None


def _run_save_and_check(doc, save_bkp_path: str, phase: str):
    """Run -> save -> close -> poll HIS -> reopen saved BKP.

    The fallback preserves the existing project convention of exporting an INP
    and invoking Aspen with a run stem when COM Run2 does not materialize HIS.
    """
    global part2_state
    started_at = time.time()
    save_path = Path(save_bkp_path)
    run_stem_local = f"{save_path.stem}_run"
    direct_his = save_path.with_suffix(".his")
    stem_his = save_path.parent / f"{run_stem_local}.his"
    run_error = None
    try:
        run_aspen(doc)
    except Exception as exc:
        run_error = str(exc)
        _record_error(f"{phase}_run", exc)
    try:
        save_aspen(doc, str(save_path))
    except Exception as exc:
        _record_error(f"{phase}_save", exc)
        raise
    finally:
        try: close_aspen(doc)
        except Exception as exc: _record_error(f"{phase}_close", exc)

    his_path = _wait_for_his_candidates_part2([direct_his, stem_his], started_at)
    reopened = None
    try:
        reopened = start_aspen_doc()
        open_aspen(reopened, str(save_path), visible=False)
    except Exception as exc:
        _record_error(f"{phase}_reopen", exc)
        raise RuntimeError(f"Saved BKP cannot be reopened: {save_path}: {exc}") from exc

    if not his_path:
        try:
            export_stem = str(save_path.parent / run_stem_local)
            reopened.Export(4, export_stem)
            close_aspen(reopened)
            reopened = None
            bat_path = save_path.parent / f"run_{run_stem_local}.bat"
            bat_path.write_text("@echo off\n" + f"call \"{aspen_setup_bat}\"\n" + f"cd /d \"{save_path.parent}\"\n" + f"Aspen \"{run_stem_local}\" \"{run_stem_local}\"\n", encoding="utf-8")
            subprocess.run(["cmd.exe", "/c", str(bat_path)], cwd=str(save_path.parent), check=True)
            his_path = _wait_for_his_candidates_part2([stem_his], started_at)
        except Exception as exc:
            _record_error(f"{phase}_his_fallback", exc)
        finally:
            if reopened is None:
                try:
                    reopened = start_aspen_doc()
                    open_aspen(reopened, str(save_path), visible=False)
                except Exception as exc:
                    _record_error(f"{phase}_final_reopen", exc)

    # Keep a reopened document available for an optional Part 2 human-feedback repair.
    globals()["doc"] = reopened
    hs = inspect_his_status_local(working_dir, preferred_his=his_path or str(direct_his))
    hs.update({"phase": phase, "final_bkp_path": str(save_path), "his_generation_fallback": bool(his_path and Path(his_path) == stem_his), "run_exception": run_error})
    print(f"[PART2] HIS({phase}) run_executed={hs['run_executed']} | his={hs.get('his_path')} | reason={hs.get('reason')}")
    return hs


# ===========================================================================
# LangGraph-era node functions (cell 26), called directly now instead of via
# graph edges -- signatures/bodies unchanged (Phase B already added
# human_answer to human_review_node), except final_validate_node has cell 30's
# fix inlined directly instead of monkeypatched.
# ===========================================================================
from agent_loop_utils import HumanInputRequired


class Part2GraphState(TypedDict, total=False):
    ok: bool
    stage: str
    errors: List[Dict[str, Any]]
    mcp_trace: List[Dict[str, Any]]
    run_phases: List[str]
    schema_phase_contexts: Dict[str, Any]
    phase_catalogs: Dict[str, Any]
    phase_results: List[Dict[str, Any]]
    final_validation: Dict[str, Any]
    final_issues: List[Dict[str, Any]]
    part2_loop_status: Dict[str, Any]
    assessment: Dict[str, Any]
    human_review_required: bool
    human_intervention_type: Optional[str]
    human_question: str
    human_answer: Optional[Dict[str, Any]]
    human_review_decision: str
    human_repair_requested: bool
    human_repair_rounds: int
    human_stop_requested: bool
    human_review_bypassed: bool
    part2_result_path: str
    final_output_ready: bool
    com_initial_saved: bool
    com_completion_saved: bool


def initial_part2_state() -> Part2GraphState:
    return {
        "ok": True,
        "stage": "initialized",
        "errors": list(part2_state.get("errors", []) or []),
        "mcp_trace": [],
        "run_phases": list(RUN_PHASES),
        "phase_results": [],
        "final_validation": {},
        "final_issues": [],
        "part2_loop_status": dict(part2_loop_status),
        "human_review_required": False,
        "human_intervention_type": None,
        "human_question": "",
        "human_answer": None,
        "human_review_decision": "",
        "human_repair_requested": False,
        "human_repair_rounds": 0,
        "human_stop_requested": False,
        "part2_result_path": part2_result_path,
        "final_output_ready": False,
        "com_initial_saved": bool(globals().get("com_initial_saved", False)),
        "com_completion_saved": bool(globals().get("com_completion_saved", False)),
    }


def prepare_node(state: Part2GraphState) -> Part2GraphState:
    print("[GRAPH][prepare] Part2 paths and globals are ready")
    return {**state, "stage": "prepared", "errors": list(part2_state.get("errors", []) or [])}


def mcp_context_node(state: Part2GraphState) -> Part2GraphState:
    config_call = mcp_part2_config()
    schema_call = mcp_schema_context()
    catalog_call = mcp_com_catalog()
    mcp_trace = mcp_trace_append(state, config_call, schema_call, catalog_call)
    run_phases = (config_call.get("result") or {}).get("run_phases") or list(RUN_PHASES)
    schema_contexts = (schema_call.get("result") or {}).get("contexts", {})
    phase_catalogs = (catalog_call.get("result") or {}).get("phase_catalogs", {})
    print(f"[GRAPH][mcp_context] phases={run_phases} tools={len(mcp_trace)}")
    return {**state, "stage": "mcp_context_ready", "run_phases": run_phases, "schema_phase_contexts": schema_contexts, "phase_catalogs": phase_catalogs, "mcp_trace": mcp_trace}


def _run_single_phase_repair_node(state: Part2GraphState, phase: str) -> Part2GraphState:
    phase_results_local = list(state.get("phase_results", []) or [])
    if "doc" not in globals() or doc is None:
        err = RuntimeError(f"doc unavailable before {phase} repair")
        _record_error(f"repair_{phase}", err)
        return {**state, "ok": False, "stage": f"repair_{phase}_failed", "phase_results": phase_results_local, "errors": list(part2_state.get("errors", []))}

    if phase not in (state.get("run_phases", RUN_PHASES) or RUN_PHASES):
        print(f"[GRAPH][repair_{phase}] skipped: not in run_phases")
        return {**state, "stage": f"repair_{phase}_skipped", "phase_results": phase_results_local, "errors": list(part2_state.get("errors", []))}

    if any(not result.get("ok") for result in phase_results_local):
        print(f"[GRAPH][repair_{phase}] skipped: previous phase failed")
        return {**state, "stage": f"repair_{phase}_skipped_after_failure", "phase_results": phase_results_local, "errors": list(part2_state.get("errors", []))}

    result = repair_phase(phase, max_iter=PHASE_MAX_ITER)
    phase_results_local.append(result)
    v_last = result.get("last") or {}
    summary_call = mcp_validation_summary(v_last, phase=phase)
    print(f"[GRAPH][repair_{phase}] ok={bool(result.get('ok'))}")
    # repair_phase() may have set the module-level com_completion_saved global
    # (see cell 24 save logic). Capture it into state right here at the node
    # boundary so write_result_node never has to read globals() itself -- it only
    # ever sees whatever the most recent node put in state.
    return {
        **state,
        "ok": bool(state.get("ok", True)) and bool(result.get("ok")),
        "stage": f"repair_{phase}_done" if result.get("ok") else f"repair_{phase}_failed",
        "phase_results": phase_results_local,
        "mcp_trace": mcp_trace_append(state, summary_call),
        "com_completion_saved": bool(globals().get("com_completion_saved", state.get("com_completion_saved", False))),
        "errors": list(part2_state.get("errors", [])),
    }


def repair_property_method_node(state: Part2GraphState) -> Part2GraphState:
    return _run_single_phase_repair_node(state, "property_method")


def repair_connectivity_node(state: Part2GraphState) -> Part2GraphState:
    return _run_single_phase_repair_node(state, "connectivity")


def repair_feeds_node(state: Part2GraphState) -> Part2GraphState:
    return _run_single_phase_repair_node(state, "feeds")


def repair_block_specs_node(state: Part2GraphState) -> Part2GraphState:
    result_state = _run_single_phase_repair_node(state, "block_specs")
    print("[GRAPH][phase_repair] summary:")
    print(json.dumps([_compact_phase_result(r) for r in (result_state.get("phase_results", []) or [])], ensure_ascii=False, indent=2))
    return result_state

def _phase_results_have_repairs(phase_results: List[Dict[str, Any]]) -> bool:
    for result in phase_results or []:
        report = result.get("schema_repair") or {}
        if report.get("actions") or report.get("errors"):
            return True
        history = result.get("history") or []
        for item in history:
            if item.get("patchplan") or item.get("completion_path") or item.get("exec_failed"):
                return True
    return False


def final_validate_node(state: Part2GraphState) -> Part2GraphState:
    # this is cell26's original
    # final_validate_node body, with cell30's fix already applied inline --
    # the notebook used to run cell26's version first and then monkeypatch it
    # with this fixed version from a later cell (see cell30's comment history
    # for why: the original "no repair needed" branch never actually ran
    # Aspen, only saved a BKP, so Part3 got no .his file to consume). A
    # module has no such run-order dependency, so this is just written once,
    # correctly, from the start -- no monkeypatch.
    global v_val, issues_norm, part2_loop_status
    if "doc" not in globals() or doc is None:
        _record_error("final_validate", RuntimeError("doc unavailable before final validation"))
        return {**state, "ok": False, "stage": "final_validation_failed", "errors": list(part2_state.get("errors", []))}
    v_val = run_validator_phase(doc, "final_all", base_dir=rules_base_dir, print_report=True, strict=False)
    issues_norm = normalize_issues_from_v(v_val)
    print("[GRAPH][final_validate] final validation issues:", len(issues_norm))

    phase_results = state.get("phase_results", []) or []
    has_repairs = _phase_results_have_repairs(phase_results)
    ran_baseline = False
    if v_val.get("ok"):
        if has_repairs:
            final_status = _run_save_and_check(doc, out_com_repaired_bkp_path, phase="phase_repaired")
            final_status.update({
                "final_output_kind": "repaired",
                "final_bkp_path": out_com_repaired_bkp_path,
                "repair_applied": True,
            })
            ran_baseline = True
        else:
            # Fixed (cell30, folded in): actually run the baseline Aspen
            # execution here too, instead of only recording a synthetic
            # "no_repair_required" status with run_executed=False and no
            # .his file -- Part3 needs a real .his to consume.
            final_status = _run_save_and_check(doc, out_com_initial_bkp_path, phase="phase_initial_validated")
            final_status.update({
                "final_output_kind": "initial",
                "final_bkp_path": out_com_initial_bkp_path,
                "repair_applied": False,
            })
            ran_baseline = True
            print(f"[PART2] no repair required -> final BKP: {out_com_initial_bkp_path}")
        part2_loop_status.update(final_status)
    else:
        part2_loop_status.update({"attempts": PHASE_MAX_ITER, "phase": "validation_failed", "reason": "final_validation_failed"})
    part2_loop_status["attempts"] = len(phase_results)
    summary_call = mcp_validation_summary(v_val, phase="final_all")
    return {**state, "stage": "final_validated", "final_validation": v_val, "final_issues": issues_norm, "part2_loop_status": dict(part2_loop_status), "mcp_trace": mcp_trace_append(state, summary_call), "errors": list(part2_state.get("errors", []))}


def assess_final_node(state: Part2GraphState) -> Part2GraphState:
    assessment_call = mcp_repair_assessment(state.get("phase_results", []) or [], state.get("final_validation", {}) or {}, state.get("part2_loop_status", {}) or {})
    result = assessment_call.get("result", {}) or {}
    mcp_trace = mcp_trace_append(state, assessment_call)
    needs_review = bool(result.get("needs_human_review")) or bool(part2_state.get("errors"))
    print(f"[GRAPH][assess_final] decision_hint={result.get('decision_hint')} needs_review={needs_review}")
    return {
        **state,
        "stage": "final_assessed",
        "assessment": result,
        "human_review_required": needs_review,
        "human_intervention_type": "part2_final_review" if needs_review else None,
        "human_question": "Part2 repair/final validation needs review. Reply with approve, stop, or revise plus notes.",
        "human_repair_requested": False,
        "mcp_trace": mcp_trace,
    }


def _normalize_human_review_response(response: Any) -> Dict[str, Any]:
    if isinstance(response, dict):
        response_obj = dict(response)
    elif isinstance(response, str):
        raw = response.strip()
        decision = raw
        notes = ""
        if ":" in raw:
            head, tail = raw.split(":", 1)
            decision = head.strip()
            notes = tail.strip()
        response_obj = {"decision": decision, "notes": notes, "raw": response}
    else:
        response_obj = {"decision": "", "notes": "", "raw": response}
    response_obj["decision"] = str(response_obj.get("decision", "")).lower().strip()
    response_obj["notes"] = str(response_obj.get("notes") or response_obj.get("message") or response_obj.get("instruction") or "").strip()
    return response_obj


def human_review_node(state: Part2GraphState, human_answer: Optional[Any] = None) -> Part2GraphState:
    if part2_hitl_mode == "bypass":
        print("[GRAPH][human_review] bypassed (PART2_HITL_MODE=bypass): auto-approving")
        return {
            **state,
            "stage": "human_review_bypassed",
            "human_answer": {
                "decision": "approve",
                "notes": "Auto-approved because PART2_HITL_MODE=bypass (human review disabled).",
            },
            "human_review_decision": "approve",
            "human_review_required": False,
            "human_review_bypassed": True,
            "human_repair_requested": False,
            "human_stop_requested": False,
            "final_output_ready": True,
        }

    payload = {
        "type": "part2_final_review",
        "question": state.get("human_question"),
        "failed_phases": (state.get("assessment") or {}).get("failed_phases", []),
        "final_validation_summary": (state.get("assessment") or {}).get("final_validation_summary", {}),
        "run_status": (state.get("assessment") or {}).get("run_status", {}),
        "errors": state.get("errors", []),
        "allowed_responses": ["approve", "complete", "revise", "stop", "revise_stop"],
        "response_schema": {"decision": "approve|complete|revise|stop|revise_stop", "notes": "required when decision=revise"},
    }

    if part2_hitl_mode == "chat":
        if human_answer is None:
            print("[GRAPH][human_review] pausing (PART2_HITL_MODE=chat) for final review")
            raise HumanInputRequired(payload)
        response = human_answer
    else:
        # Any other PART2_HITL_MODE value (legacy "interactive"): this used to
        # call LangGraph's interrupt() here, but that only works inside a
        # compiled StateGraph -- this function is now called directly from a
        # plain @tool in the bind_tools()+while-loop agent, where
        # interrupt() has nothing to suspend and would just raise. Treat any
        # non-bypass/non-chat mode as bypass instead of crashing.
        print(f"[GRAPH][human_review] unrecognized PART2_HITL_MODE={part2_hitl_mode!r}, treating as bypass")
        response = {"decision": "approve", "notes": "Auto-approved: unrecognized PART2_HITL_MODE."}
    response_obj = _normalize_human_review_response(response)
    decision = response_obj.get("decision", "")
    if decision in {"approve", "complete"}:
        return {
            **state,
            "stage": "human_review_approved",
            "human_answer": response_obj,
            "human_review_decision": decision,
            "human_review_required": False,
            "human_repair_requested": False,
            "human_stop_requested": False,
            "final_output_ready": True,
        }
    if decision == "revise":
        return {
            **state,
            "stage": "human_review_revise_requested",
            "human_answer": response_obj,
            "human_review_decision": decision,
            "human_review_required": False,
            "human_repair_requested": True,
            "human_stop_requested": False,
            "final_output_ready": False,
        }
    return {
        **state,
        "ok": False,
        "stage": "human_review_stopped",
        "human_answer": response_obj,
        "human_review_decision": decision or "stop",
        "human_review_required": False,
        "human_repair_requested": False,
        "human_stop_requested": True,
        "final_output_ready": False,
    }


def _generate_human_feedback_patchplan(state: Part2GraphState, catalog_subset: Dict[str, Any]) -> Dict[str, Any]:
    human_answer = state.get("human_answer") or {}
    notes = human_answer.get("notes") or human_answer.get("raw") or ""
    prompt = PromptTemplate.from_template("""
You are revising an Aspen Plus COM model based on explicit human feedback.

Return ONLY valid JSON. No markdown. No explanation.

Output schema:
{{"patches":[{{"id":str,"action":"call","fn":str,"args":object,"units":object,"reason":str}}]}}

Rules:
- action must be "call".
- Use ONLY function names appearing in aspen_code_catalog_subset.
- Args must EXACTLY match the args defined in catalog except "doc". Never include "doc" in args.
- Every numeric argument must have a unit in "units".
- Do not invent new blocks or streams unless the human feedback explicitly requires it.
- Prefer the smallest patch that reflects the human feedback.
- If the feedback is not actionable, return {{"patches":[]}}.

HUMAN FEEDBACK:
{human_feedback}

FINAL VALIDATION SUMMARY:
{final_validation_summary}

FINAL ISSUES:
{final_issues}

PHASE RESULTS:
{phase_results}

SCHEMA YAML:
{schema_yaml}

ASPEN CODE CATALOG SUBSET:
{catalog_yaml}

Return JSON now.
""".strip())
    llm = ChatOpenAI(model=part2_repair_plan_model, temperature=llm_temperature)
    response = (prompt | llm).invoke({
        "human_feedback": str(notes),
        "final_validation_summary": json.dumps((state.get("final_validation") or {}).get("summary", {}), ensure_ascii=False, indent=2),
        "final_issues": json.dumps(state.get("final_issues", []) or [], ensure_ascii=False, indent=2),
        "phase_results": json.dumps([_compact_phase_result(r) for r in (state.get("phase_results", []) or [])], ensure_ascii=False, indent=2),
        "schema_yaml": yaml.safe_dump(schema, sort_keys=False, allow_unicode=True),
        "catalog_yaml": yaml.safe_dump(catalog_subset, sort_keys=False, allow_unicode=True),
    })
    return json.loads(response.content.strip())


def human_feedback_repair_node(state: Part2GraphState) -> Part2GraphState:
    global part2_loop_status
    max_rounds = int((part2_input or {}).get("human_repair_max_rounds", 1) or 1)
    current_round = int(state.get("human_repair_rounds", 0) or 0)
    if current_round >= max_rounds:
        print(f"[GRAPH][human_feedback_repair] max rounds reached: {current_round}/{max_rounds}")
        return {**state, "ok": False, "stage": "human_feedback_repair_max_rounds", "human_repair_requested": False, "human_stop_requested": True}
    if "doc" not in globals() or doc is None:
        err = RuntimeError("doc unavailable before human feedback repair")
        _record_error("human_feedback_repair", err)
        return {**state, "ok": False, "stage": "human_feedback_repair_failed", "human_repair_requested": False, "errors": list(part2_state.get("errors", []))}

    allowed = set().union(*PHASE_FUNCTION_ALLOWLIST.values())
    catalog_subset = filter_catalog_by_functions(aspen_code, allowed)
    fn_index = build_fn_index(catalog_subset)
    round_no = current_round + 1
    try:
        patchplan = _generate_human_feedback_patchplan(state, catalog_subset)
        safety_check_patchplan(patchplan, fn_index)
        print(f"[GRAPH][human_feedback_repair] patches={len(patchplan.get('patches', []))}")
        if not patchplan.get("patches"):
            return {**state, "ok": False, "stage": "human_feedback_no_actionable_patch", "human_repair_requested": False, "human_repair_rounds": round_no}
        code_text = _generate_completion_code(patchplan, catalog_subset)
        out_path = str(Path(working_dir) / f"com_completion_human_revise_round{round_no}.py")
        write_com_completion_script(code_text, out_path)
        ok_exec = run_exec_script(out_path, stage=f"human_feedback_repair_{round_no}")
        # Local now (no longer `global`) -- default carries forward whatever state
        # already had so a save_aspen failure below doesn't silently regress it.
        com_completion_saved = bool(state.get("com_completion_saved", False))
        try:
            save_aspen(doc, out_com_completion_bkp_path)
            com_completion_saved = Path(out_com_completion_bkp_path).exists()
            print(f"[GRAPH][human_feedback_repair] com_completion saved={com_completion_saved} | path={out_com_completion_bkp_path}")
        except Exception as exc:
            _record_error(f"human_feedback_repair_{round_no}_save", exc)
        result = {
            "phase": "human_feedback",
            "ok": bool(ok_exec),
            "history": [{"attempt": round_no, "patchplan": patchplan, "completion_path": out_path, "exec_ok": bool(ok_exec)}],
            "last": state.get("final_validation", {}) or {},
            "schema_repair": {"ok": bool(ok_exec), "actions": patchplan.get("patches", []), "errors": [] if ok_exec else ["human feedback execution failed"]},
        }
        phase_results = list(state.get("phase_results", []) or []) + [result]
        part2_loop_status["human_repair_rounds"] = round_no
        return {
            **state,
            "ok": bool(state.get("ok", True)) and bool(ok_exec),
            "stage": "human_feedback_repair_done" if ok_exec else "human_feedback_repair_failed",
            "phase_results": phase_results,
            "human_repair_requested": False,
            "human_repair_rounds": round_no,
            "com_completion_saved": com_completion_saved,
            "errors": list(part2_state.get("errors", [])),
        }
    except Exception as exc:
        _record_error(f"human_feedback_repair_{round_no}", exc)
        return {**state, "ok": False, "stage": "human_feedback_repair_failed", "human_repair_requested": False, "human_repair_rounds": round_no, "errors": list(part2_state.get("errors", []))}

def write_result_node(state: Part2GraphState) -> Part2GraphState:
    validation_ok = bool((state.get("final_validation") or {}).get("ok", False))
    result = {
        "ok": bool(part2_state.get("ok", True)) and (not bool(state.get("human_stop_requested", False))) and validation_ok and Path((state.get("part2_loop_status") or {}).get("final_bkp_path") or out_com_repaired_bkp_path).exists(),
        "stage": state.get("stage"),
        "schema_input_path": out_validated_schema_path,
        "out_inp_path": out_inp_path,
        "out_bkp_path": out_bkp_path,
        "out_com_skeleton_path": out_com_path,
        "out_com_initial_bkp_path": out_com_initial_bkp_path,
        "out_com_repaired_bkp_path": out_com_repaired_bkp_path,
        "out_com_completion_bkp_path": out_com_completion_bkp_path,
        "out_com_proto_bkp_path": (state.get("part2_loop_status") or {}).get("final_bkp_path") or out_com_repaired_bkp_path,
        "final_output_kind": (state.get("part2_loop_status") or {}).get("final_output_kind", "repaired"),
        "final_bkp_path": (state.get("part2_loop_status") or {}).get("final_bkp_path") or out_com_repaired_bkp_path,
        "repair_applied": bool((state.get("part2_loop_status") or {}).get("repair_applied", True)),
        "com_initial_saved": bool(state.get("com_initial_saved", False)),
        "com_completion_saved": bool(state.get("com_completion_saved", False)),
        "com_repaired_saved": Path(out_com_repaired_bkp_path).exists(),
        # Diagnostic only (mcp_client verification): why any real MCP call fell back
        # to local_adapter this run. Read directly from globals() deliberately -- this
        # is a debug-only field, not one of the risk fields Phase 4 moved to state.
        "mcp_fallback_log": list(globals().get("mcp_fallback_log", [])),
        "out_com_completion_path": out_com_compl_path,
        "validation_ok": validation_ok,
        "validation_summary": (state.get("final_validation") or {}).get("summary", {}),
        "issues_count": int((state.get("final_validation") or {}).get("n_issues", 0)),
        "run_executed": bool((state.get("part2_loop_status") or {}).get("run_executed", False)),
        "his_path": (state.get("part2_loop_status") or {}).get("his_path", ""),
        "his_exists": bool((state.get("part2_loop_status") or {}).get("his_exists", False)),
        "his_nonempty": bool((state.get("part2_loop_status") or {}).get("his_nonempty", False)),
        "counts_reliable": bool((state.get("part2_loop_status") or {}).get("counts_reliable", False)),
        "his_error_count": (state.get("part2_loop_status") or {}).get("error_count", None),
        "his_warning_count": (state.get("part2_loop_status") or {}).get("warning_count", None),
        "severe_blocks": (state.get("part2_loop_status") or {}).get("severe_blocks", None),
        "error_blocks": (state.get("part2_loop_status") or {}).get("error_blocks", None),
        "warning_blocks": (state.get("part2_loop_status") or {}).get("warning_blocks", None),
        "repair_attempts": int((state.get("part2_loop_status") or {}).get("attempts", 0)),
        "phase_results": [_compact_phase_result(r) for r in (state.get("phase_results", []) or [])],
        "assessment": state.get("assessment", {}),
        "human_review_required": state.get("human_review_required", False),
        "human_answer": state.get("human_answer"),
        "human_review_decision": state.get("human_review_decision", ""),
        "human_repair_rounds": int(state.get("human_repair_rounds", 0) or 0),
        "human_review_bypassed": bool(state.get("human_review_bypassed", False)),
        "human_stop_requested": bool(state.get("human_stop_requested", False)),
        "mcp_trace": state.get("mcp_trace", []),
        "errors": part2_state.get("errors", []),
    }
    Path(part2_result_path).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[GRAPH][write_result] result written: {part2_result_path}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "doc" in globals() and doc is not None:
        try:
            close_aspen(doc)
        except Exception as exc:
            _record_error("close_after_write_result", exc)
    return {**state, "stage": "result_written", "part2_result_path": part2_result_path, "final_output_ready": bool(result.get("ok"))}


def build_session(runner_input_path: str = "./runner_input.json", record_event: Any = None) -> Dict[str, Any]:
    """Equivalent of part2_v1.ipynb cell 4 (path/config resolution) plus the
    deterministic bootstrap cells that always ran once, unconditionally,
    before the agent loop started (cells 6/8/10/12/14: INP skeleton
    generation -> INP->BKP conversion -> COM action plan -> COM skeleton
    codegen -> skeleton execution/doc creation) and cell 18's schema/aspen_code
    reload. These were never `@tool`-selectable in the original notebook
    either -- the model never chose to skip or reorder them -- so they belong
    in build_session(), not as agent-visible tools, exactly like Part3's
    build_session absorbing its own config bootstrap."""
    global PART2_STANDALONE_TEST_MODE, part2_hitl_mode
    global db_inp_path, db_com_path, component_db_path, out_validated_schema_path
    global working_dir, rules_base_dir, out_inp_path, out_bkp_path, out_com_path
    global out_com_proto_bkp_path, out_com_initial_bkp_path, out_com_repaired_bkp_path
    global out_com_completion_bkp_path, out_com_compl_path, aspen_setup_bat
    global part2_run_mode, part2_result_path, part2_input
    global llm_model, llm_temperature
    global part2_component_resolver_model, part2_inp_codegen_model, part2_plan_model
    global part2_code_model, part2_repair_plan_model, part2_repair_codegen_model
    global part2_token_usage_path, part2_agent_events_path
    global PHASE_MAX_ITER

    _reset_module_state()

    PART2_STANDALONE_TEST_MODE = os.environ.get("PART2_STANDALONE_TEST_MODE", "1").strip().lower() not in {"0", "false", "no"}
    part2_hitl_mode = os.environ.get("PART2_HITL_MODE", "bypass").strip().lower()
    print(f"[PART2] hitl_mode: {part2_hitl_mode}")

    part2_run_base_dir = _HERE / "run"
    part2_attempt_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    part2_attempt_dir = part2_run_base_dir / f"attempt_part2_{part2_attempt_stamp}"

    default_part2_input = {
        "db_inp_path": str(shared_file("inp_v1.yaml")),
        "db_com_path": str(shared_file("aspen_code_v3.yaml")),
        "component_db_path": str(_PROJECT_ROOT / "aspen_components.yaml"),
        "schema_input_path": str(part2_run_base_dir / "Schema_proto.yaml"),
        "working_dir": str(part2_attempt_dir),
        "rules_base_dir": str(shared_file("valid_block_rules.yaml").parent),
        "out_inp_path": str(part2_attempt_dir / "inp_proto.inp"),
        "out_bkp_path": str(part2_attempt_dir / "inp_proto.bkp"),
        "out_com_skeleton_path": str(part2_attempt_dir / "com_skeleton.py"),
        "out_com_proto_bkp_path": str(part2_attempt_dir / "com_proto_repaired.bkp"),
        "out_com_initial_bkp_path": str(part2_attempt_dir / "com_proto_initial.bkp"),
        "out_com_repaired_bkp_path": str(part2_attempt_dir / "com_proto_repaired.bkp"),
        "out_com_completion_bkp_path": str(part2_attempt_dir / "com_completion.bkp"),
        "out_com_completion_path": str(part2_attempt_dir / "com_completion_iter1.py"),
        "aspen_setup_bat": r"C:\Program Files\AspenTech\Aspen Plus V14.0\Engine\Xeq\aspsetup.bat",
        "run_mode": "full",
        "part2_result_path": str(part2_attempt_dir / "part2_result_iter1.json"),
        "repair_max_iter": 3,
        "human_repair_max_rounds": 1,
        "llm_config": {"model": "gpt-5.5", "temperature": 1},
    }

    runner_path = Path(runner_input_path)
    if PART2_STANDALONE_TEST_MODE:
        payload: Dict[str, Any] = {}
        part2_input = dict(default_part2_input)
    else:
        if not runner_path.exists():
            raise FileNotFoundError(f"runner_input.json not found: {runner_path}")
        payload = json.loads(runner_path.read_text(encoding="utf-8"))
        if not isinstance(payload.get("part2_input"), dict):
            raise KeyError("runner_input.json does not contain part2_input")
        part2_input = dict(payload["part2_input"])
        _runner_out_dir = Path(part2_input.get("out_com_proto_bkp_path", part2_input["working_dir"])).parent
        part2_input.setdefault("out_com_initial_bkp_path", str(_runner_out_dir / "com_proto_initial.bkp"))
        part2_input.setdefault("out_com_repaired_bkp_path", str(_runner_out_dir / "com_proto_repaired.bkp"))
        part2_input.setdefault("out_com_completion_bkp_path", str(_runner_out_dir / "com_completion.bkp"))

    required_part2_paths = {
        "db_inp_path": Path(part2_input["db_inp_path"]),
        "db_com_path": Path(part2_input["db_com_path"]),
        "component_db_path": Path(part2_input["component_db_path"]),
        "schema_input_path": Path(part2_input["schema_input_path"]),
        "rules_base_dir": Path(part2_input["rules_base_dir"]),
    }
    missing = {name: str(path) for name, path in required_part2_paths.items() if not path.exists()}
    if missing:
        raise FileNotFoundError(f"Missing Part2 input paths: {missing}")

    Path(part2_input["working_dir"]).mkdir(parents=True, exist_ok=True)

    db_inp_path = part2_input["db_inp_path"]
    db_com_path = part2_input["db_com_path"]
    component_db_path = part2_input["component_db_path"]
    out_validated_schema_path = part2_input["schema_input_path"]
    working_dir = part2_input["working_dir"]
    rules_base_dir = part2_input["rules_base_dir"]
    out_inp_path = part2_input["out_inp_path"]
    out_bkp_path = part2_input["out_bkp_path"]
    out_com_path = part2_input["out_com_skeleton_path"]
    out_com_proto_bkp_path = part2_input["out_com_proto_bkp_path"]
    out_com_initial_bkp_path = part2_input["out_com_initial_bkp_path"]
    out_com_repaired_bkp_path = part2_input["out_com_repaired_bkp_path"]
    out_com_completion_bkp_path = part2_input["out_com_completion_bkp_path"]
    out_com_compl_path = part2_input["out_com_completion_path"]
    aspen_setup_bat = part2_input["aspen_setup_bat"]
    part2_run_mode = part2_input.get("run_mode", "full")
    part2_result_path = part2_input["part2_result_path"]

    llm_config = dict(payload.get("llm_config", {}) or {})
    llm_config.update(part2_input.get("llm_config", {}) or {})
    llm_model = llm_config.get("model") or "gpt-5.2"
    llm_temperature = llm_config.get("temperature", 1)
    part2_component_resolver_model = llm_config.get("part2_component_resolver_model") or llm_model
    part2_inp_codegen_model = llm_config.get("part2_inp_codegen_model") or llm_model
    part2_plan_model = llm_config.get("part2_plan_model") or llm_model
    part2_code_model = llm_config.get("part2_code_model") or llm_model
    part2_repair_plan_model = llm_config.get("part2_repair_plan_model") or llm_model
    part2_repair_codegen_model = llm_config.get("part2_repair_codegen_model") or llm_model

    print(f"[PART2] PART2_STANDALONE_TEST_MODE: {PART2_STANDALONE_TEST_MODE}")
    print(f"[PART2] PROJECT_ROOT: {_PROJECT_ROOT}")
    print(f"[PART2] schema_input_path: {out_validated_schema_path}")
    print(f"[PART2] working_dir: {working_dir}")
    print(f"[PART2] out_inp_path: {out_inp_path}")
    print(f"[PART2] out_bkp_path: {out_bkp_path}")
    print(f"[PART2] out_com_skeleton_path: {out_com_path}")
    print(f"[PART2] out_com_initial_bkp_path: {out_com_initial_bkp_path}")
    print(f"[PART2] out_com_repaired_bkp_path: {out_com_repaired_bkp_path}")
    print(f"[PART2] out_com_completion_bkp_path: {out_com_completion_bkp_path}")
    print(f"[PART2] out_com_completion_path: {out_com_compl_path}")
    print(f"[PART2] rules_base_dir: {rules_base_dir}")
    print(f"[PART2] run_mode: {part2_run_mode}")
    print(f"[PART2] part2_result_path: {part2_result_path}")

    part2_token_usage_path = str(Path(working_dir) / "token_usage.jsonl")
    part2_agent_events_path = str(Path(working_dir) / "agent_events.jsonl")
    if record_event is not None:
        record_event(part2_agent_events_path, "part2_started", "part2", {
            "working_dir": working_dir,
            "model": llm_model,
            "repair_max_iter": part2_input.get("repair_max_iter", 3),
        })
    print(f"[PART2] token_usage_path: {part2_token_usage_path}")
    print(f"[PART2] agent_events_path: {part2_agent_events_path}")
    print(f"[PART2] human_repair_max_rounds: {part2_input.get('human_repair_max_rounds', 1)}")
    print(f"[PART2] llm_model: {llm_model}")

    PHASE_MAX_ITER = int((part2_input or {}).get("repair_max_iter", 3) or 3)

    # Deterministic bootstrap -- same order as the notebook's cells 6/8/10/12/14/18.
    _generate_inp_skeleton()
    _convert_inp_to_bkp()
    _build_com_action_plan()
    _generate_com_skeleton_code()
    _execute_com_skeleton()
    _load_phase_context_data()

    state = initial_part2_state()

    config = {
        "db_inp_path": db_inp_path,
        "db_com_path": db_com_path,
        "component_db_path": component_db_path,
        "out_validated_schema_path": out_validated_schema_path,
        "working_dir": working_dir,
        "rules_base_dir": rules_base_dir,
        "out_inp_path": out_inp_path,
        "out_bkp_path": out_bkp_path,
        "out_com_path": out_com_path,
        "out_com_proto_bkp_path": out_com_proto_bkp_path,
        "out_com_initial_bkp_path": out_com_initial_bkp_path,
        "out_com_repaired_bkp_path": out_com_repaired_bkp_path,
        "out_com_completion_bkp_path": out_com_completion_bkp_path,
        "out_com_compl_path": out_com_compl_path,
        "aspen_setup_bat": aspen_setup_bat,
        "part2_run_mode": part2_run_mode,
        "part2_result_path": part2_result_path,
        "part2_hitl_mode": part2_hitl_mode,
        "part2_input": part2_input,
    }

    return {
        "config": config,
        "state": state,
        "llm_model": llm_model,
        "llm_temperature": llm_temperature,
        "part2_component_resolver_model": part2_component_resolver_model,
        "part2_inp_codegen_model": part2_inp_codegen_model,
        "part2_plan_model": part2_plan_model,
        "part2_code_model": part2_code_model,
        "part2_repair_plan_model": part2_repair_plan_model,
        "part2_repair_codegen_model": part2_repair_codegen_model,
        "token_usage_path": part2_token_usage_path,
        "agent_events_path": part2_agent_events_path,
    }
