from __future__ import annotations

from typing import Any, Dict, List, Optional

from aspen_code_v1 import (
    flash_set_pressure,
    flash_set_spec,
    flash_set_temperature,
    heater_set_pressure,
    heater_set_spec,
    heater_set_temperature,
    mheatx_set_outlet_stream,
    mheatx_set_spec,
    mheatx_set_value,
    rgibbs_set_duty,
    rgibbs_set_opt_chemeq,
    rgibbs_set_pressure,
    rgibbs_set_spec,
    rgibbs_set_temperature,
)


def _flowsheet(schema: Dict[str, Any]) -> Dict[str, Any]:
    return (schema or {}).get("flowsheet_schema") or (schema or {}).get("flowsheet") or {}


def _schema_blocks(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [b for b in (_flowsheet(schema).get("blocks") or []) if isinstance(b, dict) and b.get("id")]


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


def _str(x: Any, default: str = "") -> str:
    v = _value(x, default)
    return str(v).strip() if v is not None else default


def _specs(block: Dict[str, Any]) -> Dict[str, Any]:
    specs = block.get("specs")
    return specs if isinstance(specs, dict) else {}


def _block_id(block: Dict[str, Any]) -> str:
    return str(block.get("id") or "").strip()


def _block_type(block: Dict[str, Any]) -> str:
    return str(block.get("type") or "").strip().upper()


def _record(actions: List[Dict[str, Any]], action: str, block: str, **kwargs) -> None:
    actions.append({"action": action, "block": block, **kwargs})


def _run_action(errors: List[Dict[str, Any]], action: str, block: str, fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
        return True
    except Exception as e:
        errors.append({"action": action, "block": block, "error": repr(e)})
        return False


def _repair_heater(doc, block: Dict[str, Any], actions: List[Dict[str, Any]], errors: List[Dict[str, Any]], schema_missing: List[Dict[str, Any]]) -> None:
    b = _block_id(block)
    s = _specs(block)
    t = _num(s.get("T_out_C") if "T_out_C" in s else s.get("T_C"))
    p = _num(s.get("P_bar") if "P_bar" in s else s.get("P_out_bar"))
    mode = _str(s.get("spec_mode"), "").upper()

    missing = []
    if t is None:
        missing.append("T_out_C")
    if p is None:
        missing.append("P_bar")
    if missing:
        schema_missing.append({"block": b, "type": _block_type(block), "missing_schema_fields": missing})

    # For executable Part2 models, outlet temperature + pressure maps to Aspen TP.
    if t is not None or p is not None or mode in {"OUTLET_T", "TP"}:
        if _run_action(errors, "heater_set_spec", b, heater_set_spec, doc, b, "TP"):
            _record(actions, "heater_set_spec", b, spec_opt="TP")
    if t is not None:
        if _run_action(errors, "heater_set_temperature", b, heater_set_temperature, doc, b, t):
            _record(actions, "heater_set_temperature", b, T_C=t)
    if p is not None:
        if _run_action(errors, "heater_set_pressure", b, heater_set_pressure, doc, b, p):
            _record(actions, "heater_set_pressure", b, P_bar=p)


def _repair_rgibbs(doc, block: Dict[str, Any], actions: List[Dict[str, Any]], errors: List[Dict[str, Any]], schema_missing: List[Dict[str, Any]]) -> None:
    b = _block_id(block)
    s = _specs(block)
    t = _num(s.get("T_C") if "T_C" in s else s.get("T_out_C"))
    p = _num(s.get("P_bar") if "P_bar" in s else s.get("P_out_bar"))
    duty = _num(s.get("duty") if "duty" in s else s.get("Q"))

    missing = []
    if p is None:
        missing.append("P_bar")
    if t is None and duty is None:
        missing.append("T_C_or_duty")
    if missing:
        schema_missing.append({"block": b, "type": _block_type(block), "missing_schema_fields": missing})

    if _run_action(errors, "rgibbs_set_opt_chemeq", b, rgibbs_set_opt_chemeq, doc, b, "PHASE-CHEM"):
        _record(actions, "rgibbs_set_opt_chemeq", b, mode="PHASE-CHEM")

    if t is not None:
        if _run_action(errors, "rgibbs_set_spec", b, rgibbs_set_spec, doc, b, "TEMP"):
            _record(actions, "rgibbs_set_spec", b, spec_opt="TEMP")
        if _run_action(errors, "rgibbs_set_temperature", b, rgibbs_set_temperature, doc, b, t):
            _record(actions, "rgibbs_set_temperature", b, T_C=t)
    elif duty is not None:
        if _run_action(errors, "rgibbs_set_spec", b, rgibbs_set_spec, doc, b, "DUTY"):
            _record(actions, "rgibbs_set_spec", b, spec_opt="DUTY")
        if _run_action(errors, "rgibbs_set_duty", b, rgibbs_set_duty, doc, b, duty):
            _record(actions, "rgibbs_set_duty", b, duty=duty)

    if p is not None:
        if _run_action(errors, "rgibbs_set_pressure", b, rgibbs_set_pressure, doc, b, p):
            _record(actions, "rgibbs_set_pressure", b, P_bar=p)


def _repair_flash2(doc, block: Dict[str, Any], actions: List[Dict[str, Any]], errors: List[Dict[str, Any]], schema_missing: List[Dict[str, Any]]) -> None:
    b = _block_id(block)
    s = _specs(block)
    spec_mode = _str(s.get("spec_mode"), "TP").upper() or "TP"
    t = _num(s.get("T_out_C") if "T_out_C" in s else s.get("T_C"))
    p = _num(s.get("P_out_bar") if "P_out_bar" in s else s.get("P_bar"))

    missing = []
    if spec_mode == "TP" and t is None:
        missing.append("T_out_C")
    if spec_mode == "TP" and p is None:
        missing.append("P_out_bar")
    if missing:
        schema_missing.append({"block": b, "type": _block_type(block), "missing_schema_fields": missing})

    if _run_action(errors, "flash_set_spec", b, flash_set_spec, doc, b, spec_mode):
        _record(actions, "flash_set_spec", b, spec_opt=spec_mode)
    if t is not None:
        if _run_action(errors, "flash_set_temperature", b, flash_set_temperature, doc, b, t):
            _record(actions, "flash_set_temperature", b, T_C=t)
    if p is not None:
        if _run_action(errors, "flash_set_pressure", b, flash_set_pressure, doc, b, p):
            _record(actions, "flash_set_pressure", b, P_bar=p)


def _repair_mheatx(doc, block: Dict[str, Any], actions: List[Dict[str, Any]], errors: List[Dict[str, Any]], schema_missing: List[Dict[str, Any]]) -> None:
    b = _block_id(block)
    s = _specs(block)
    outlet_streams = s.get("outlet_streams")
    outlet_to_in: Dict[str, str] = {}
    if not isinstance(outlet_streams, dict) or not outlet_streams:
        schema_missing.append({"block": b, "type": _block_type(block), "missing_schema_fields": ["outlet_streams"]})
    else:
        outlet_to_in = {str(v): str(k) for k, v in outlet_streams.items()}
        for in_stream, out_stream in outlet_streams.items():
            if _run_action(errors, "mheatx_set_outlet_stream", b, mheatx_set_outlet_stream, doc, b, str(in_stream), str(out_stream)):
                _record(actions, "mheatx_set_outlet_stream", b, in_stream=str(in_stream), out_stream=str(out_stream))

    temp_specs = s.get("temp_specs")
    if isinstance(temp_specs, list):
        for item in temp_specs:
            if not isinstance(item, dict):
                continue
            out_stream = item.get("out_stream")
            t = _num(item.get("T_out_C"))
            if not out_stream or t is None:
                schema_missing.append({"block": b, "type": _block_type(block), "missing_schema_fields": ["temp_specs.out_stream_or_T_out_C"]})
                continue
            spec_stream = outlet_to_in.get(str(out_stream), str(out_stream))
            if _run_action(errors, "mheatx_set_spec", b, mheatx_set_spec, doc, b, spec_stream, "TEMP"):
                _record(actions, "mheatx_set_spec", b, in_stream=spec_stream, out_stream=str(out_stream), spec="TEMP")
            if _run_action(errors, "mheatx_set_value", b, mheatx_set_value, doc, b, spec_stream, t):
                _record(actions, "mheatx_set_value", b, in_stream=spec_stream, out_stream=str(out_stream), value=t)


def repair_block_specs_from_schema(
    doc,
    schema: Dict[str, Any],
    apply: bool = True,
    print_report: bool = True,
    target_blocks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    actions: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    schema_missing: List[Dict[str, Any]] = []
    target_set = {str(b).strip().upper() for b in (target_blocks or []) if str(b).strip()}

    for block in _schema_blocks(schema):
        b = _block_id(block)
        bt = _block_type(block)
        if not b:
            continue
        if target_set and b.upper() not in target_set:
            skipped.append({"block": b, "type": bt, "reason": "not_targeted_by_validation"})
            continue

        if not apply:
            skipped.append({"block": b, "type": bt, "reason": "apply_false"})
            continue

        if bt in {"MIXER"}:
            skipped.append({"block": b, "type": bt, "reason": "no_required_block_specs"})
        elif bt in {"HEATER"}:
            _repair_heater(doc, block, actions, errors, schema_missing)
        elif bt in {"RGIBBS"}:
            _repair_rgibbs(doc, block, actions, errors, schema_missing)
        elif bt in {"FLASH2"}:
            _repair_flash2(doc, block, actions, errors, schema_missing)
        elif bt in {"MHEATX"}:
            _repair_mheatx(doc, block, actions, errors, schema_missing)
        else:
            skipped.append({"block": b, "type": bt, "reason": "unsupported_block_type_for_schema_repair"})

    repair_summary: List[str] = []
    for act in actions:
        details = ", ".join(f"{k}={v}" for k, v in act.items() if k not in {"action", "block"})
        repair_summary.append(f"Applied {act['action']}: {act['block']}" + (f" ({details})" if details else ""))

    report = {
        "ok": not errors and not schema_missing,
        "applied": apply,
        "policy": {
            "schema_block_specs": "repair_from_schema_when_supported",
            "targeting": "validation_issue_blocks_only" if target_set else "all_schema_blocks",
            "schema_missing": "defer_to_llm_executable_fallback",
            "unsupported_block_types": "report_only",
        },
        "target_blocks": sorted(target_set),
        "actions": actions,
        "skipped": skipped,
        "schema_missing": schema_missing,
        "errors": errors,
        "repair_summary": repair_summary,
    }

    if print_report:
        print("[schema_block_spec_repair]")
        print(f"  actions={len(actions)}")
        print(f"  skipped={len(skipped)}")
        print(f"  schema_missing={len(schema_missing)}")
        if actions:
            print("  applied:")
            for line in repair_summary:
                print(f"    - {line}")
        if schema_missing:
            print("  schema_missing:")
            for item in schema_missing:
                print(f"    - block={item.get('block')} fields={item.get('missing_schema_fields')}")
        if skipped:
            print("  skipped:")
            for item in skipped:
                print(f"    - block={item.get('block')} type={item.get('type')} reason={item.get('reason')}")
        if errors:
            print(f"  errors={errors}")

    return report
