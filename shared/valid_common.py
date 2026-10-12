# validator/common.py
from __future__ import annotations

from typing import Any, Dict, List, Optional


def make_issue(
    category: str,
    target_type: str,
    target: str,
    rule_id: str,
    message: str = "",
    path: Optional[str] = None,
    current: Any = None,
    required: Any = None,
    detail: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "category": category,
        "target_type": target_type,  # "BLOCK" | "STREAM"
        "target": target,            # e.g., "B1" | "S1"
        "rule_id": rule_id,
        "message": message or rule_id,
        "path": path,
        "current": current,
        "required": required,
        "detail": detail or {},
    }


def wrap_result(
    validator: str,
    issues: List[Dict[str, Any]],
    targets: Optional[Dict[str, List[str]]] = None,
    raw: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    targets = targets or {"blocks": [], "feeds": [], "products": []}
    raw = raw or {}
    n_targets = (
        len(targets.get("blocks", []))
        or len(targets.get("feeds", []))
        or 0
    )

    return {
        "validator": validator,                      # "connectivity" | "feeds" | "block_specs"
        "ok": len(issues) == 0,
        "summary": {"n_targets": n_targets, "n_issues": len(issues)},
        "targets": targets,
        "issues": issues,
        "raw": raw,
    }


def print_report(res: Dict[str, Any], max_issues: int = 50) -> None:
    print(
        f"[{res['validator']}] ok={res['ok']} | "
        f"targets={res['summary']['n_targets']} | issues={res['summary']['n_issues']}"
    )
    for it in res["issues"][:max_issues]:
        tgt = f"{it['target_type']}:{it['target']}"
        msg = it.get("message") or it["rule_id"]
        print(f" - ({it['category']}) {tgt} | {it['rule_id']} | {msg}")
