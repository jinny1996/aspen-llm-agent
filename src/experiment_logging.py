from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from langchain_core.callbacks import UsageMetadataCallbackHandler
except Exception:  # pragma: no cover - keeps notebooks runnable if callback API changes.
    UsageMetadataCallbackHandler = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_json(path: Any, default: Any = None) -> Any:
    if not path:
        return default
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Any, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def append_jsonl(path: Any, item: Dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Any) -> List[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except Exception:
            rows.append({"raw": line, "parse_error": True})
    return rows


def normalize_usage_metadata(usage_metadata: Any) -> Dict[str, Any]:
    if not isinstance(usage_metadata, dict):
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "raw": usage_metadata,
        }

    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    saw_any = False

    def add_from_dict(d: Dict[str, Any]) -> None:
        nonlocal saw_any
        aliases = {
            "input_tokens": ["input_tokens", "prompt_tokens"],
            "output_tokens": ["output_tokens", "completion_tokens"],
            "total_tokens": ["total_tokens"],
        }
        for key, names in aliases.items():
            for name in names:
                val = d.get(name)
                if isinstance(val, (int, float)):
                    totals[key] += int(val)
                    saw_any = True
                    break

    for value in usage_metadata.values():
        if isinstance(value, dict) and any(k in value for k in ("input_tokens", "output_tokens", "total_tokens", "prompt_tokens", "completion_tokens")):
            add_from_dict(value)
        elif isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, dict):
                    add_from_dict(nested)

    if not saw_any:
        add_from_dict(usage_metadata)

    return {
        "input_tokens": totals["input_tokens"] if saw_any else None,
        "output_tokens": totals["output_tokens"] if saw_any else None,
        "total_tokens": totals["total_tokens"] if saw_any else None,
        "raw": usage_metadata,
    }


@contextmanager
def token_usage(section: str, model: str = "", log_path: Any = "", extra: Optional[Dict[str, Any]] = None):
    callback = UsageMetadataCallbackHandler() if UsageMetadataCallbackHandler is not None else None
    callbacks = [callback] if callback is not None else []
    started_at = _now()
    try:
        yield callbacks
    finally:
        usage = normalize_usage_metadata(getattr(callback, "usage_metadata", None) if callback is not None else None)
        row = {
            "section": section,
            "model": model,
            "started_at": started_at,
            "finished_at": _now(),
            **usage,
        }
        if extra:
            row.update(extra)
        if log_path:
            append_jsonl(log_path, row)


def record_event(log_path: Any, event: str, stage: str = "", detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = {
        "event": event,
        "stage": stage,
        "created_at": _now(),
        "detail": detail or {},
    }
    append_jsonl(log_path, row)
    return row


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def collect_repair_iterations(part1_report: Dict[str, Any], part2_result: Dict[str, Any], part3_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    for item in (part1_report or {}).get("iterations", []) or []:
        rows.append({
            "part": "part1",
            "iteration": item.get("iter"),
            "stage": "schema_completion",
            "issue_count": item.get("n_missing"),
            "ok": item.get("n_missing") == 0,
            "patch_ops": item.get("n_patch_ops", 0),
            "source": "Schema_validation_completion_report_v2.json",
        })

    for idx, item in enumerate((part2_result or {}).get("phase_results", []) or [], start=1):
        validation = item.get("validation") or item.get("last") or {}
        rows.append({
            "part": "part2",
            "iteration": idx,
            "stage": item.get("phase"),
            "issue_count": validation.get("n_issues"),
            "ok": bool(item.get("ok", validation.get("ok", False))),
            "schema_repair_actions": item.get("schema_repair_actions"),
            "schema_repair_errors": item.get("schema_repair_errors"),
            "source": "part2_result_iter1.json",
        })

    for item in (part3_result or {}).get("iterations", []) or []:
        counts = item.get("counts") or {}
        issue_count = int(counts.get("severe") or 0) + int(counts.get("errors") or 0)
        rows.append({
            "part": "part3",
            "iteration": item.get("iter"),
            "stage": "simulation_repair",
            "issue_count": issue_count,
            "ok": item.get("status") == "NO_ISSUE" or issue_count == 0,
            "counts": counts,
            "source": "part3_result.json",
        })

    return rows


def infer_path_events(metadata: Dict[str, Any], part1_status: Dict[str, Any], part2_result: Dict[str, Any], part3_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    def add(source: str, target: str, reason: str, part: str, value: int = 1) -> None:
        events.append({
            "source": source,
            "target": target,
            "value": value,
            "reason": reason,
            "part": part,
            "case_id": metadata.get("case_id", ""),
            "model": metadata.get("model", ""),
        })

    add("User input", "Planning", "runner_started", "runner")

    part1_mcp = part1_status.get("mcp_trace") or []
    if part1_mcp:
        add("Planning", "MCP retrieval", f"part1_mcp_calls={len(part1_mcp)}", "part1")
        add("MCP retrieval", "Draft generation", "part1_context_ready", "part1")
    else:
        add("Planning", "Draft generation", "no_part1_mcp_trace", "part1")

    if part1_status.get("human_review_required") or "human" in str(part1_status.get("stage", "")).lower():
        add("Draft generation", "HITL", part1_status.get("human_intervention_type") or "part1_human_review", "part1")
        add("HITL", "Validation", "human_review_resumed_or_pending", "part1")
    else:
        add("Draft generation", "Validation", part1_status.get("stage", "part1_validated"), "part1")

    part1_missing = int(part1_status.get("final_missing_count") or 0)
    if part1_missing:
        add("Validation", "Failure", f"part1_missing={part1_missing}", "part1")
    else:
        add("Validation", "Part2 generation", "schema_ready", "part1")

    part2_mcp = part2_result.get("mcp_trace") or []
    if part2_mcp:
        add("Part2 generation", "MCP retrieval", f"part2_mcp_calls={len(part2_mcp)}", "part2")
        add("MCP retrieval", "Validation", "part2_validation_tools", "part2")
    else:
        add("Part2 generation", "Validation", "no_part2_mcp_trace", "part2")

    if part2_result.get("repair_applied"):
        add("Validation", "Repair", f"repair_attempts={part2_result.get('repair_attempts')}", "part2")
        add("Repair", "Final Aspen file", part2_result.get("final_output_kind", "repaired"), "part2")
    elif part2_result:
        add("Validation", "Final Aspen file", part2_result.get("final_output_kind", "initial"), "part2")

    if part2_result.get("human_review_required") or part2_result.get("human_review_decision"):
        add("Repair", "HITL", part2_result.get("human_review_decision") or "part2_human_review", "part2")

    if part3_result:
        add("Final Aspen file", "Simulation diagnosis", "part3_enabled", "part3")
        if part3_result.get("iterations"):
            add("Simulation diagnosis", "Repair", f"part3_iterations={len(part3_result.get('iterations') or [])}", "part3")
            add("Repair", "Final Aspen file", "part3_repaired", "part3")
        elif part3_result.get("ok"):
            add("Simulation diagnosis", "Final Aspen file", "part3_no_repair_needed", "part3")
        else:
            add("Simulation diagnosis", "Failure", part3_result.get("stage", "part3_failed"), "part3")

    final_ok = bool(part3_result.get("ok")) if part3_result else bool(part2_result.get("ok"))
    add("Final Aspen file", "Success" if final_ok else "Failure", "final_status", "runner")
    return events


def build_experiment_summary(runner_status: Dict[str, Any], runner_payload: Dict[str, Any]) -> Dict[str, Any]:
    metadata = dict((runner_payload or {}).get("experiment_metadata") or {})
    runner_dir = Path(runner_status.get("runner_dir") or metadata.get("run_dir") or ".")
    metadata.setdefault("run_dir", str(runner_dir))
    metadata.setdefault("created_at", _safe_get(runner_status, "created_at", default=""))

    part1_status = runner_status.get("part1_status") or _read_json(runner_status.get("part1_status_path"), {}) or {}
    part2_result = runner_status.get("part2_result") or _read_json(runner_status.get("part2_result_path"), {}) or {}
    part3_result = runner_status.get("part3_result") or _read_json(runner_status.get("part3_result_path"), {}) or {}
    part1_report = _read_json(part1_status.get("validation_report_path"), {}) or {}

    token_logs: List[Dict[str, Any]] = []
    for rel in ["part1/token_usage.jsonl", "part2/token_usage.jsonl", "part3/token_usage.jsonl", "token_usage.jsonl"]:
        token_logs.extend(read_jsonl(runner_dir / rel))

    total_tokens = sum(int(row.get("total_tokens") or 0) for row in token_logs)
    input_tokens = sum(int(row.get("input_tokens") or 0) for row in token_logs)
    output_tokens = sum(int(row.get("output_tokens") or 0) for row in token_logs)

    repair_iterations = collect_repair_iterations(part1_report, part2_result, part3_result)
    path_events = infer_path_events(metadata, part1_status, part2_result, part3_result)
    part1_schema_review_approved = bool(runner_status.get("part1_schema_review_approved"))
    part1_schema_path = (
        runner_status.get("part1_final_schema_path")
        or runner_status.get("part1_schema_input_path")
        or part1_status.get("final_output_schema_path")
        or part1_status.get("schema_output_path")
        or part1_status.get("final_schema_path")
    )
    schema_file_exists = bool(part1_schema_path) and Path(str(part1_schema_path)).exists()
    schema_valid = bool(part1_status.get("ok", False)) or (part1_schema_review_approved and schema_file_exists)

    summary = {
        "metadata": metadata,
        "success_flags": {
            "part1_ok": schema_valid,
            "part2_ok": bool(part2_result.get("ok", False)),
            "part3_ok": bool(part3_result.get("ok", False)) if part3_result else None,
            "schema_valid": schema_valid,
            "bkp_created": bool(part2_result.get("final_bkp_path")) and Path(str(part2_result.get("final_bkp_path"))).exists(),
            "aspen_open_or_run_executed": bool(part2_result.get("run_executed", False)),
            "simulation_success": bool(part3_result.get("ok", False)) if part3_result else None,
            "overall_success": bool(part3_result.get("ok")) if part3_result else bool(part2_result.get("ok")),
        },
        "repair_iterations": repair_iterations,
        "path_events": path_events,
        "token_usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "sections": token_logs,
        },
        "source_paths": {
            "runner_status_path": str(runner_dir / "runner_status.json"),
            "part1_status_path": runner_status.get("part1_status_path", ""),
            "part2_result_path": runner_status.get("part2_result_path", ""),
            "part3_result_path": runner_status.get("part3_result_path", ""),
        },
    }
    return summary
