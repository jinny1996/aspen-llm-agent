"""Patch-plan safety helpers for Part3, ported out of part3_v1.ipynb's stage-code
exec-string cell (previously "cell 6" in the notebook) so they are real,
importable functions instead of code executed via exec(code, globals(), globals()).

Behavior is unchanged from the original notebook cell -- this is a pure
extraction, not a rewrite of the logic.
"""
from __future__ import annotations

import ast as _ast
from typing import Any, Dict, Set


def _walk_catalog_items(node):
    if isinstance(node, dict):
        if isinstance(node.get("name"), str):
            yield node
        for v in node.values():
            yield from _walk_catalog_items(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_catalog_items(v)


def filter_catalog_by_functions(catalog: Dict[str, Any], allowed: Set[str]) -> Dict[str, Any]:
    out = {"categories": {}}
    categories = (catalog or {}).get("categories", {}) or {}
    for cat, items in categories.items():
        kept = [it for it in (items or []) if isinstance(it, dict) and it.get("name") in allowed]
        if kept:
            out["categories"][cat] = kept
    return out


def build_fn_index(catalog: Dict[str, Any]) -> Dict[str, Set[str]]:
    fn_index: Dict[str, Set[str]] = {}
    for it in _walk_catalog_items(catalog or {}):
        fn = it.get("name")
        allowed: Set[str] = set()
        if isinstance(it.get("args"), dict):
            allowed = {k for k in it["args"].keys() if k != "doc"}
        fn_index[fn] = allowed
    return fn_index


def safety_check_patchplan(plan: Dict[str, Any], fn_index: Dict[str, Set[str]]) -> None:
    if not isinstance(plan, dict) or not isinstance(plan.get("patches"), list):
        raise ValueError("invalid plan shape")
    for pch in plan["patches"]:
        if pch.get("action") != "call":
            raise ValueError("only action=call allowed")
        fn = pch.get("fn")
        if fn not in fn_index:
            raise ValueError(f"fn not allowed / not in catalog: {fn}")
        args = pch.get("args", {})
        units = pch.get("units", {})
        if not isinstance(args, dict) or not isinstance(units, dict):
            raise ValueError("args/units must be dict")
        args.pop("doc", None)
        units.pop("doc", None)
        pch["args"] = args
        pch["units"] = units
        allowed = fn_index.get(fn, set())
        if allowed:
            extra = set(args.keys()) - allowed
            if extra:
                raise ValueError(f"extra args not allowed: {fn} -> {sorted(extra)}")
        for k, val in args.items():
            if isinstance(val, (int, float)) and (k not in units or not str(units[k]).strip()):
                raise ValueError(f"missing unit for numeric arg: {fn}.{k}")


# Union of Part2's per-phase allowlists. Part3 patches the same Aspen model but isn't
# scoped to one fixed phase (it acts on whatever the diagnosis points at), so it reuses
# the combined, already-vetted set instead of inventing a new risk assessment from scratch.
PART3_FUNCTION_ALLOWLIST = (
    {"set_property_method"}
    | {"connect_stream", "disconnect_stream", "stream_add", "block_add"}
    | {
        "stream_set_temperature", "stream_set_pressure", "stream_set_flow_base",
        "stream_set_total_flow", "stream_set_basis", "stream_set_component_value",
        "stream_set_components", "stream_define",
    }
    | {
        "heater_set_spec", "heater_set_temperature", "heater_set_pressure", "heater_set_duty", "heater_set_vapor_frac", "heater_config",
        "pump_set_type", "pump_set_spec", "pump_set_eff", "pump_set_nphase", "pump_configure",
        "rgibbs_set_opt_chemeq", "rgibbs_set_spec", "rgibbs_set_temperature", "rgibbs_set_pressure", "rgibbs_set_duty", "rgibbs_config",
        "rplug_set_type", "rplug_set_opt_tspec", "rplug_set_reactor_temp", "rplug_set_temp_profile", "rplug_set_multitube", "rplug_set_ntube", "rplug_set_length", "rplug_set_diameter", "rplug_set_phase", "rplug_set_pressure", "rplug_set_pressure_drop", "rplug_set_catalyst_presence", "rplug_set_catalyst_props",
        "mheatx_set_outlet_stream", "mheatx_set_spec", "mheatx_set_value", "mheatx_configure",
        "flash_set_spec", "flash_set_temperature", "flash_set_pressure", "flash_set_duty", "flash_set_vapor_frac", "flash_config",
        "splt_set_frac",
        "comp_set_model_type", "comp_set_type", "comp_set_spec", "comp_set_eff", "comp_set_nphase", "comp_configure",
        "radfrac_set_calc_mode", "radfrac_set_nstage", "radfrac_set_condenser", "radfrac_set_reboiler", "radfrac_set_no_phase", "radfrac_set_spec", "radfrac_set_feed_stage", "radfrac_set_stage_pressure", "radfrac_configure",
    }
)


def run_patch_script_continue(code_text: str, exec_globals: Dict[str, Any], stage: str = "patch_exec") -> Dict[str, Any]:
    """AST-parse + execute statement by statement into the given exec_globals dict,
    matching Part2's run_exec_script fail-soft behavior: one bad statement doesn't
    abort the rest of the patch. Executes into exec_globals (not caller globals()) to
    preserve Part3's existing sandboxed-execution boundary."""
    try:
        mod = _ast.parse(code_text, filename=f"<{stage}>")
    except Exception as e:
        return {"ok": False, "n_ok": 0, "n_fail": 0, "n_total": 0, "errors": [f"AST parse failed: {e}"]}

    n_total = len(mod.body)
    n_ok = 0
    errors = []
    for i, node in enumerate(mod.body, start=1):
        stmt_mod = _ast.Module(body=[node], type_ignores=[])
        _ast.fix_missing_locations(stmt_mod)
        snippet = (_ast.get_source_segment(code_text, node) or f"<stmt {i}>").strip().replace("\n", " ")
        try:
            exec(compile(stmt_mod, f"<{stage}>", "exec"), exec_globals, exec_globals)
            n_ok += 1
        except Exception as e:
            errors.append(f"stmt {i}/{n_total} failed: {e} | code: {snippet[:220]}")
            print(f"[PART3][{stage}] stmt {i}/{n_total} failed: {e} | code: {snippet[:220]}")
            continue
    print(f"[PART3][{stage}] statements ok={n_ok}, failed={n_total - n_ok}, total={n_total}")
    return {"ok": n_ok == n_total, "n_ok": n_ok, "n_fail": n_total - n_ok, "n_total": n_total, "errors": errors}
