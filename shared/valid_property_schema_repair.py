from __future__ import annotations

from typing import Any, Dict, Optional

from aspen_code_v1 import set_property_method


PROPERTY_METHOD_NODE = r"\Data\Properties\Specifications\Input\GOPSETNAME"
PROPERTY_METHOD_BASE_NODE = r"\Data\Properties\Specifications\Input\GBASEOPSET"


def _norm_method(value: Any) -> str:
    return str(value or "").strip().upper()


def _schema_property_method(schema: Dict[str, Any]) -> Optional[str]:
    method = ((schema or {}).get("aspen_schema") or {}).get("property_method")
    method = str(method or "").strip()
    return method or None


def _find_node(doc, path: str):
    try:
        return doc.Tree.FindNode(path)
    except Exception:
        return None


def get_property_method(doc) -> Optional[str]:
    node = _find_node(doc, PROPERTY_METHOD_NODE)
    try:
        value = node.Value if node is not None else None
    except Exception:
        value = None
    value = str(value or "").strip()
    if value:
        return value

    base_node = _find_node(doc, PROPERTY_METHOD_BASE_NODE)
    try:
        value = base_node.Value if base_node is not None else None
    except Exception:
        value = None
    value = str(value or "").strip()
    return value or None


def _set_optional_base_method(doc, method: str) -> bool:
    node = _find_node(doc, PROPERTY_METHOD_BASE_NODE)
    if node is None:
        return False
    try:
        node.Value = method
        return True
    except Exception:
        return False


def validate_property_method_from_schema(
    doc,
    schema: Dict[str, Any],
    print_report: bool = True,
) -> Dict[str, Any]:
    expected = _schema_property_method(schema)
    current = get_property_method(doc)
    issues = []

    if not expected:
        issues.append({
            "category": "PROPERTY_METHOD",
            "target_type": "SYSTEM",
            "target": "property_method",
            "rule_id": "schema_property_method_missing",
            "message": "schema missing aspen_schema.property_method",
            "path": "aspen_schema.property_method",
            "current": current,
            "required": "non-empty property method",
            "detail": {},
        })
    elif _norm_method(current) != _norm_method(expected):
        issues.append({
            "category": "PROPERTY_METHOD",
            "target_type": "SYSTEM",
            "target": "property_method",
            "rule_id": "property_method_mismatch",
            "message": f"property method mismatch: current={current!r}, expected={expected!r}",
            "path": PROPERTY_METHOD_NODE,
            "current": current,
            "required": expected,
            "detail": {"schema_path": "aspen_schema.property_method"},
        })

    ok = not issues
    result = {
        "validator": "property_method",
        "ok": ok,
        "summary": {"n_targets": 1, "n_issues": len(issues)},
        "targets": {"property_method": expected},
        "issues": issues,
        "raw": {"expected": expected, "current": current},
    }
    if print_report:
        print(f"[property_method] ok={ok} | current={current} | expected={expected} | issues={len(issues)}")
        for issue in issues:
            print(f" - {issue['rule_id']} | {issue['message']}")
    return result


def repair_property_method_from_schema(
    doc,
    schema: Dict[str, Any],
    apply: bool = True,
    print_report: bool = True,
) -> Dict[str, Any]:
    expected = _schema_property_method(schema)
    current_before = get_property_method(doc)
    actions = []
    errors = []
    schema_missing = []

    if not expected:
        schema_missing.append({
            "field": "property_method",
            "schema_path": "aspen_schema.property_method",
            "reason": "missing required property method in schema",
        })
    elif _norm_method(current_before) != _norm_method(expected):
        if apply:
            try:
                set_property_method(doc, expected)
                _set_optional_base_method(doc, expected)
                actions.append({
                    "action": "set_property_method",
                    "method": expected,
                    "previous": current_before,
                })
            except Exception as exc:
                errors.append({
                    "action": "set_property_method",
                    "method": expected,
                    "error": repr(exc),
                })
        else:
            actions.append({
                "action": "set_property_method",
                "method": expected,
                "previous": current_before,
                "dry_run": True,
            })

    current_after = get_property_method(doc)
    ok = bool(expected) and _norm_method(current_after) == _norm_method(expected) and not errors
    report = {
        "ok": ok,
        "applied": apply,
        "policy": {
            "schema_property_method": "repair_from_schema",
            "schema_missing": "defer_to_llm_executable_fallback",
        },
        "expected": expected,
        "current_before": current_before,
        "current_after": current_after,
        "actions": actions,
        "schema_missing": schema_missing,
        "errors": errors,
        "repair_summary": [
            f"Applied set_property_method: {a['method']} (previous={a.get('previous')})"
            for a in actions
            if a.get("action") == "set_property_method"
        ],
    }
    if print_report:
        print("[schema_property_method_repair]")
        print(f"  expected={expected}")
        print(f"  current_before={current_before}")
        print(f"  current_after={current_after}")
        print(f"  actions={len(actions)}")
        if schema_missing:
            print(f"  schema_missing={schema_missing}")
        if errors:
            print(f"  errors={errors}")
    return report
