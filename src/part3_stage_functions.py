"""Part3 stage logic, refactored out of part3_v1.ipynb's PART3_STAGE_CODE
exec-string dict into real functions.

Behavior is preserved from the original notebook cells -- this is a
restructuring (explicit args/return instead of `exec(code, globals(), globals())`
reading/writing notebook globals), not a rewrite of the underlying logic.

All functions operate on a single `session: dict` with this shape (mirrors
Part2's PART2_SESSION pattern in part2_v1.ipynb, cell 31):

    session = {
        "config": {...},          # PART3_CONFIG
        "state": {...},           # PART3_STATE
        "result": {...},          # PART3_RESULT
        "doc": None,               # open Aspen COM document, thread-affine;
                                    # set by run_patch_apply, consumed+closed by
                                    # run_rerun_and_parse. Never passed through the
                                    # LLM/tool-call boundary -- only ever read/written
                                    # here, in this thread.
        "project_root": Path(...),
        "llm_model": "...", "llm_temperature": 1,
        "part3_diagnosis_model": "...",
        "part3_patch_plan_model": "...",
        "part3_patch_codegen_model": "...",
        "token_usage_path": "...",
        "agent_events_path": "...",
        "catalog_subset": {...},   # built once by run_patch_plan, reused by codegen
        "fn_index": {...},
        "repair_cycle_count": 0,   # bumped by begin_repair_attempt()
        "repair_cycle_cap": 3,
    }

Each `run_*` function mutates `session["state"]` / `session["result"]` in place
and also returns the same session for convenience, matching the "operate on a
plain dict state, return the updated dict" convention already used by Part2's
node functions.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import win32com.client
import yaml
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

from agent_loop_utils import HumanInputRequired
from experiment_logging import token_usage
from part3_patch_safety import (
    PART3_FUNCTION_ALLOWLIST,
    build_fn_index,
    filter_catalog_by_functions,
    run_patch_script_continue,
    safety_check_patchplan,
)

ISSUE_START_RE = re.compile(
    r"^\s*\*\*\s*(SEVERE\s+ERRORS?|ERRORS?|WARNINGS?)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# MCP gating (item 2, 2026-09-01): part3_assessment_tool is the only Part3
# tool that actually gets called (assess_final, below). This mirrors the
# local_adapter/mcp_client gating that part2_v1.ipynb cell22 does inline in
# the notebook, but lives here at module level instead -- part3_stage_functions.py
# already knows its own path (Path(__file__)) and receives working_dir explicitly
# through `session["config"]`, so there is no need to reach into notebook
# globals() the way Part2's inline version does.
MCP_ADAPTER_MODE = os.environ.get("MCP_ADAPTER_MODE", "local_adapter").strip().lower()
MCP_CLIENT_FALLBACK_TO_LOCAL = True
# Diagnostic-only: records why a real MCP call fell back to local_adapter.
MCP_FALLBACK_LOG: List[Dict[str, Any]] = []

_MCP_HERE = Path(__file__).resolve().parent
_MCP_CALL_TOOL_PATH = _MCP_HERE / "mcp_call_tool.py"


def mcp_tool_result(tool_name: str, result: Any = None, ok: bool = True, error: str = "", mode: Optional[str] = None) -> Dict[str, Any]:
    return {
        "tool": tool_name,
        "mode": mode or MCP_ADAPTER_MODE,
        "ok": bool(ok),
        "result": result or {},
        "error": error or "",
        "called_at": datetime.now().isoformat(timespec="seconds"),
    }


def mcp_client_tool_call(tool_name: str, arguments: Dict[str, Any], working_dir: str) -> Dict[str, Any]:
    if not _MCP_CALL_TOOL_PATH.exists():
        return mcp_tool_result(tool_name, ok=False, error=f"MCP helper script not found: {_MCP_CALL_TOOL_PATH}", mode="mcp_client")
    work_dir = Path(working_dir or Path.cwd())
    work_dir.mkdir(parents=True, exist_ok=True)
    args_path = work_dir / f"part3_mcp_args_{tool_name}.json"
    out_path = work_dir / f"part3_mcp_out_{tool_name}.json"
    # Same fix as Part2's mcp_client_tool_call (item 1, 2026-09-01, file-based
    # child-side trace instrumentation): the child always completes and flushes
    # its stdout print successfully, but the parent's
    # subprocess.run(capture_output=True) intermittently lost the piped stdout
    # anyway for some tools when spawned from inside a busy nbconvert-launched
    # Jupyter kernel. Fix: have the child write its result to a plain file and
    # read that instead of proc.stdout, which has no such pipe race.
    # proc.stdout is kept only as a last-resort fallback.
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
                [sys.executable, str(_MCP_CALL_TOOL_PATH), tool_name, "--arguments-file", str(args_path), "--output-file", str(out_path)],
                cwd=str(_MCP_HERE.parent),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                text=True,
                encoding="utf-8",  # 2026-09-01 (item 3 merge verification): without this,
                # subprocess.run() decodes the child's stdout/stderr with
                # locale.getpreferredencoding() (cp949 on this Korean Windows
                # setup), which raises UnicodeDecodeError on non-ASCII bytes
                # (e.g. the Korean path segments already in this repo) and
                # silently forces a fallback to local_adapter every time in
                # some shells. Part1's mcp_client_tool_call already had this
                # fixed; Part2/Part3 did not until now.
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


def maybe_mcp_client_call(tool_name: str, arguments: Dict[str, Any], working_dir: str) -> Optional[Dict[str, Any]]:
    if MCP_ADAPTER_MODE != "mcp_client":
        return None
    call = mcp_client_tool_call(tool_name, arguments, working_dir)
    if call.get("ok") or not MCP_CLIENT_FALLBACK_TO_LOCAL:
        return call
    print(f"[MCP][WARN] {tool_name}: {call.get('error')} Falling back to local_adapter.")
    MCP_FALLBACK_LOG.append({"tool": tool_name, "error": call.get("error", ""), "at": call.get("called_at")})
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


def mcp_part3_assessment(part3_state: Dict[str, Any], part3_result: Dict[str, Any], working_dir: str) -> Dict[str, Any]:
    arguments = {"part3_state": part3_state, "part3_result": part3_result}
    client_call = maybe_mcp_client_call("part3_assessment_tool", arguments, working_dir)
    if client_call:
        return client_call
    try:
        from mcp_server import part3_assessment_tool
        payload = part3_assessment_tool(part3_state, part3_result)
        payload["mode"] = "local_adapter"
        return payload
    except Exception as exc:
        return mcp_tool_result("part3_assessment_tool", ok=False, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Shared low-level helpers (used by more than one stage)
# ---------------------------------------------------------------------------

def _read_his_lines(his_path: str) -> List[str]:
    with open(his_path, "r", encoding="utf-8", errors="ignore") as f:
        return [ln.rstrip("\n") for ln in f.readlines()]


def _extract_issue_blocks(lines: List[str]) -> List[List[str]]:
    issues: List[List[str]] = []
    current: List[str] = []
    capture = False
    for line in lines:
        if ISSUE_START_RE.search(line):
            if current:
                issues.append(current)
                current = []
            capture = True
            current.append(line)
            continue
        if capture:
            if line.strip() == "":
                issues.append(current)
                current = []
                capture = False
            else:
                current.append(line)
    if current:
        issues.append(current)
    return issues


def _count_issue_levels(blocks: List[List[str]]) -> Dict[str, int]:
    counts = {"severe": 0, "errors": 0, "warnings": 0}
    for b in blocks:
        head = b[0] if b else ""
        m = ISSUE_START_RE.search(head)
        if not m:
            continue
        label = m.group(1).upper()
        if "SEVERE" in label:
            counts["severe"] += 1
        elif "WARNING" in label:
            counts["warnings"] += 1
        elif "ERROR" in label:
            counts["errors"] += 1
    return counts


def _export_inp_from_bkp_once(bkp_path: str, output_stem: str) -> None:
    doc = win32com.client.Dispatch("Apwn.Document")
    try:
        doc.InitFromFile2(bkp_path)
        export_stem = str(Path(bkp_path).parent / output_stem)
        doc.Export(4, export_stem)
    finally:
        try:
            doc.Close(False)
        except Exception:
            pass


def _load_aspen_helper_module(project_root: Path, module_name: str):
    helper_path = project_root / "aspen_code_v1.py"
    if not helper_path.exists():
        raise FileNotFoundError(f"Aspen helper module not found: {helper_path}")
    spec = importlib.util.spec_from_file_location(module_name, str(helper_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _classify_aspen_error_for_rag(error_lines: List[str]) -> Dict[str, Any]:
    text = " ".join(error_lines)
    block = None
    model = None

    m = re.search(r'BLOCK:\s*"([A-Za-z0-9_\-]+)"', text)
    if m:
        block = m.group(1)
    m = re.search(r'MODEL:\s*"([A-Za-z0-9_\-]+)"', text)
    if m:
        model = m.group(1)

    tags = []
    if "VALID PHASES" in text:
        tags.append("VALID PHASES")
    if "LIQUID PHASE" in text:
        tags.append("LIQUID PHASE")
    if "ISENTROPIC" in text or "ISENTR" in text:
        tags.append("ISENTROPIC")
    if "DEW POINT" in text:
        tags.append("BELOW DEW POINT")
    if "OUTLET PRESSURE" in text and "HIGHER THAN INLET" in text:
        tags.append("OUTLET_PRESSURE_HIGHER_THAN_INLET")
    if "TURBINE" in text:
        tags.append("TURBINE")

    return {"block": block, "model": model, "root_cause_tags": tags, "raw_text": text}


def _build_search_queries(error_lines: List[str]) -> List[str]:
    info = _classify_aspen_error_for_rag(error_lines)
    block, model, tags, text = info["block"], info["model"], info["root_cause_tags"], info["raw_text"]
    q = []
    if model:
        q.append(f"Aspen Plus {model} error troubleshooting")
    if "OUTLET_PRESSURE_HIGHER_THAN_INLET" in tags:
        if model:
            q.append(f"Aspen Plus {model} turbine outlet pressure higher than inlet")
            q.append(f"Aspen Plus {model} pressure specification turbine")
        else:
            q.append("Aspen Plus turbine outlet pressure higher than inlet")
    if "VALID PHASES" in tags:
        q.append(f"Aspen Plus {model or ''} VALID PHASES liquid phase".strip())
    if "LIQUID PHASE" in tags and model:
        q.append(f"Aspen Plus {model} liquid phase compressor error")
    if block and model:
        q.append(f"Aspen Plus block {block} model {model} error")
    m = re.search(r"THE\s+CALCULATED\s+OUTLET\s+PRESSURE[^.]*", text, re.IGNORECASE)
    if m:
        phrase = m.group(0).strip()
        q.append(f"Aspen Plus {model or ''} {phrase}".strip())
    return list(dict.fromkeys([x for x in q if x]))


def _extract_error_keywords(error_lines: List[str]) -> List[str]:
    text = " ".join(error_lines)
    tokens = re.findall(r"[A-Za-z0-9\-_]+", text)
    stopwords = {
        "error", "warning", "severe", "unable", "calculation",
        "block", "stream", "component", "system", "while", "executing",
    }
    kws = [t.lower() for t in tokens if len(t) >= 4 and t.lower() not in stopwords]
    return list(dict.fromkeys(kws))[:20]


def _compute_relevance_score(text: str, keywords: List[str]) -> int:
    txt = (text or "").lower()
    return sum(1 for kw in keywords if kw in txt)


def _select_top_k_docs(search_results: Dict[str, List[Dict[str, Any]]], error_lines: List[str], top_k: int = 5) -> List[Dict[str, Any]]:
    keywords = _extract_error_keywords(error_lines)
    scored = []
    for hits in search_results.values():
        for h in hits:
            content = h.get("content", "") or ""
            score = _compute_relevance_score(content, keywords)
            if score > 0:
                scored.append({"title": h.get("title", ""), "url": h.get("url", ""), "content": content, "score": score})
    scored.sort(key=lambda x: x["score"], reverse=True)
    seen = set()
    top_docs = []
    for d in scored:
        url = d.get("url")
        if not url or url in seen:
            continue
        seen.add(url)
        top_docs.append(d)
        if len(top_docs) >= top_k:
            break
    return top_docs


def _derive_symptom_tags(issue_lines: List[str]) -> List[str]:
    text = "\n".join(issue_lines).upper()
    tags = []
    if "OUTLET PRESSURE" in text and "HIGHER THAN INLET" in text:
        tags.append("OUTLET_PRESSURE_HIGHER_THAN_INLET")
    if "TURBINE" in text:
        tags.append("TURBINE")
    if "VALID PHASES" in text:
        tags.append("VALID_PHASES_NOT_SET")
    if "LIQUID PHASE" in text:
        tags.append("LIQUID_PHASE_PRESENT")
    if "MORE THAN ONE SINK" in text:
        tags.append("STREAM_MULTI_SINK")
    if "MORE THAN ONE SOURCE" in text:
        tags.append("STREAM_MULTI_SOURCE")
    if "INLET AND OUTLET OF THE BLOCK" in text:
        tags.append("STREAM_INLET_OUTLET_SAME_BLOCK")
    if "CANNOT FIND TEMP FOR ISENTROPIC" in text:
        tags.append("ISENTROPIC_TEMP_FAILURE")
    return list(dict.fromkeys(tags))


def _safe_json_parse_diag(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"findings": [], "summary": "", "next_actions": [], "symptom_tags": []}
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {
        "findings": [],
        "summary": text[:1000],
        "next_actions": ["LLM output was not valid JSON. Review raw output."],
        "symptom_tags": [],
    }


def _ensure_diag_schema(diag: Any) -> Dict[str, Any]:
    if not isinstance(diag, dict):
        diag = {}
    if "findings" not in diag or not isinstance(diag["findings"], list):
        diag["findings"] = []
    if "summary" not in diag or not isinstance(diag["summary"], str):
        diag["summary"] = ""
    if "next_actions" not in diag or not isinstance(diag["next_actions"], list):
        diag["next_actions"] = []
    if "symptom_tags" not in diag or not isinstance(diag["symptom_tags"], list):
        diag["symptom_tags"] = []
    for f in diag["findings"]:
        if not isinstance(f, dict):
            continue
        f.setdefault("type", "error")
        f.setdefault("code", None)
        f.setdefault("message", "")
        f.setdefault("line_no", [])
        f.setdefault("excerpt", "")
        f.setdefault("probable_cause", "")
        f.setdefault("fix_action", "")
        f.setdefault("fix_snippet", {"kind": "COM", "content": ""})
        f.setdefault("where", "GLOBAL")
        f.setdefault("severity", "medium")
        f.setdefault("confidence", 0.0)
        f.setdefault("evidence_docs", [])
    return diag


def _safe_json_parse_patch_plan(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {"intent": "UNSPECIFIED", "summary": text[:1000], "patches": []}


def _ensure_patch_plan_schema(plan: Any) -> Dict[str, Any]:
    if not isinstance(plan, dict):
        plan = {}
    plan.setdefault("intent", "UNSPECIFIED")
    plan.setdefault("summary", "")
    if not isinstance(plan.get("patches"), list):
        plan["patches"] = []
    return plan


def _docs_text(top_docs: List[Dict[str, Any]]) -> str:
    return "\n\n".join([
        f"[doc{i+1}] title={d.get('title','')}\nurl={d.get('url','')}\nsnippet={str(d.get('content',''))[:500]}"
        for i, d in enumerate(top_docs)
    ])


# ---------------------------------------------------------------------------
# Session bootstrap
# ---------------------------------------------------------------------------

def build_session(runner_input_path: str = "./runner_input.json", record_event=None, token_usage_ctx=None) -> Dict[str, Any]:
    """Equivalent of the old config_contract + config_print stage code."""
    default_part3 = {
        "working_dir": r"C:\Users\USER\Desktop\3. LLM (2601)\2608 수정",
        "aspen_setup_bat": r"C:\Program Files\AspenTech\Aspen Plus V14.0\Engine\Xeq\aspsetup.bat",
        "bkp_path": r"C:\Users\USER\Desktop\3. LLM (2601)\2608 수정\Aspen_error handling (bkp, his)\Simulation_comp.bkp",
        "run_stem": "part3_iter0",
        "max_iter": 3,
        "llm_config": {"model": "gpt-5.2", "temperature": 1},
    }

    runner_path = Path(runner_input_path)
    if runner_path.exists():
        payload = json.loads(runner_path.read_text(encoding="utf-8"))
        p2 = payload.get("part2_input", {}) or {}
        p3 = payload.get("part3_input", {}) or {}
    else:
        payload, p2, p3 = {}, {}, {}

    working_dir = p3.get("working_dir") or p2.get("working_dir", default_part3["working_dir"])
    aspen_setup_bat = p3.get("aspen_setup_bat") or p2.get("aspen_setup_bat", default_part3["aspen_setup_bat"])
    bkp_path = p3.get("bkp_path") or p2.get("out_com_proto_bkp_path", default_part3["bkp_path"])
    run_stem = p3.get("run_stem", default_part3["run_stem"])
    max_iter = p3.get("max_iter", default_part3["max_iter"])

    llm_config = dict(payload.get("llm_config", {}) or {})
    llm_config.update(p2.get("llm_config", {}) or {})
    llm_config.update(p3.get("llm_config", {}) or {})
    llm_model = llm_config.get("model") or "gpt-5.2"
    llm_temperature = llm_config.get("temperature", 1)
    part3_diagnosis_model = llm_config.get("part3_diagnosis_model") or llm_model
    part3_patch_plan_model = llm_config.get("part3_patch_plan_model") or llm_model
    part3_patch_codegen_model = llm_config.get("part3_patch_codegen_model") or llm_model
    print(f"[PART3] llm_model: {llm_model}")

    token_usage_path = str(Path(working_dir) / "token_usage.jsonl")
    agent_events_path = str(Path(working_dir) / "agent_events.jsonl")
    if record_event is not None:
        record_event(agent_events_path, "part3_started", "part3", {"working_dir": working_dir, "model": llm_model, "max_iter": max_iter})
    print(f"[PART3] token_usage_path: {token_usage_path}")
    print(f"[PART3] agent_events_path: {agent_events_path}")

    part2_result_path = p2.get("part2_result_path", "")
    part3_result_path = p3.get("part3_result_path") or str(Path(working_dir) / "part3_result.json")
    part2_result: Dict[str, Any] = {}
    if part2_result_path and Path(part2_result_path).exists():
        try:
            part2_result = json.loads(Path(part2_result_path).read_text(encoding="utf-8"))
        except Exception:
            part2_result = {}

    if (part2_result or {}).get("final_bkp_path"):
        bkp_path = part2_result["final_bkp_path"]

    part2_run_executed = bool((part2_result or {}).get("run_executed", False))
    part2_severe_blocks = int((part2_result or {}).get("severe_blocks") or 0)
    part2_error_blocks = int((part2_result or {}).get("error_blocks") or 0)
    part2_warning_blocks = int((part2_result or {}).get("warning_blocks") or 0)
    skip_by_part2_result = part2_run_executed and (part2_severe_blocks == 0 and part2_error_blocks == 0)
    his_path_from_runner = (part2_result or {}).get("his_path")

    config = {
        "working_dir": working_dir,
        "aspen_setup_bat": aspen_setup_bat,
        "bkp_path": bkp_path,
        "run_stem": run_stem,
        "max_iter": max_iter,
        "his_path": his_path_from_runner,
        "part2_result_path": part2_result_path,
        "part3_result_path": part3_result_path,
        "part2_result_counts": {
            "severe": part2_severe_blocks,
            "errors": part2_error_blocks,
            "warnings": part2_warning_blocks,
        },
        "skip_by_part2_result": skip_by_part2_result,
    }

    state: Dict[str, Any] = {
        "inp_path": None,
        "his_path": None,
        "issues": [],
        "queries": [],
        "top_docs": [],
        "diagnosis": None,
    }

    result: Dict[str, Any] = {
        "success": False,
        "ok": False,
        "part3_result_path": part3_result_path,
        "iterations": [],
        "last_issues": [],
    }

    sim_dir = str(Path(bkp_path).parent)
    Path(sim_dir).mkdir(parents=True, exist_ok=True)
    print(f"[CONFIG] working_dir={working_dir}")
    print(f"[CONFIG] sim_dir(from bkp_path)={sim_dir}")
    print(f"[CONFIG] bkp_path={bkp_path}")
    print(f"[CONFIG] his_path(from runner)={his_path_from_runner}")
    print(f"[CONFIG] part2_result_path={part2_result_path}")
    print(f"[CONFIG] part2_counts={config['part2_result_counts']}")
    print(f"[CONFIG] skip_by_part2_result={skip_by_part2_result}")

    return {
        "config": config,
        "state": state,
        "result": result,
        "doc": None,
        "llm_model": llm_model,
        "llm_temperature": llm_temperature,
        "part3_diagnosis_model": part3_diagnosis_model,
        "part3_patch_plan_model": part3_patch_plan_model,
        "part3_patch_codegen_model": part3_patch_codegen_model,
        "token_usage_path": token_usage_path,
        "agent_events_path": agent_events_path,
        "project_root": Path(working_dir),
        "catalog_subset": None,
        "fn_index": None,
        "repair_cycle_count": 0,
        "repair_cycle_cap": int(max_iter or 3),
    }


# ---------------------------------------------------------------------------
# Stage functions (one per former PART3_STAGE_CODE entry)
# ---------------------------------------------------------------------------

def run_initial_aspen(session: Dict[str, Any]) -> Dict[str, Any]:
    config, state = session["config"], session["state"]
    bkp_path = config["bkp_path"]
    sim_dir = str(Path(bkp_path).parent)
    run_stem = config["run_stem"]
    his_path = config.get("his_path")

    doc = win32com.client.Dispatch("Apwn.Document")
    try:
        doc.InitFromFile2(bkp_path)
        print(f"[RUN] Aspen reopen check OK: {bkp_path}")
    finally:
        try:
            doc.Close(False)
        except Exception:
            pass

    his_candidates = []
    if his_path and str(his_path).strip():
        his_candidates.append(str(his_path))
    his_candidates.append(str(Path(bkp_path).with_suffix(".his")))
    his_candidates.append(str(Path(sim_dir) / f"{run_stem}.his"))

    his_path_resolved = None
    for cand in his_candidates:
        p = Path(cand)
        if p.exists() and p.stat().st_size > 0:
            his_path_resolved = str(p)
            break

    if not his_path_resolved:
        raise FileNotFoundError("No usable .his found. Expected one of: " + " | ".join(his_candidates))

    inp_guess = str(Path(his_path_resolved).with_suffix(".inp"))
    inp_path = inp_guess if Path(inp_guess).exists() else None

    state["inp_path"] = inp_path
    state["his_path"] = his_path_resolved
    print(f"[RUN] INP: {inp_path}")
    print(f"[RUN] HIS: {his_path_resolved}")
    return session


def parse_his(session: Dict[str, Any]) -> Dict[str, Any]:
    config, state, result = session["config"], session["state"], session["result"]

    if config.get("skip_by_part2_result", False):
        counts = dict(config.get("part2_result_counts", {}) or {})
        counts = {
            "severe": int(counts.get("severe") or 0),
            "errors": int(counts.get("errors") or 0),
            "warnings": int(counts.get("warnings") or 0),
        }
        state["issues"] = []
        state["parse_counts"] = counts
        state["early_exit_after_parse"] = True
        result["success"] = True
        result["iterations"] = []
        result["last_issues"] = []
        result["final_bkp_path"] = config["bkp_path"]
        print("[PARSE] skipped by part2_result: severe/errors already zero.")
        print(f"[PARSE] counts(from part2_result): severe={counts['severe']} errors={counts['errors']} warnings={counts['warnings']}")
        return session

    lines = _read_his_lines(state["his_path"])
    issue_blocks = _extract_issue_blocks(lines)
    counts = _count_issue_levels(issue_blocks)
    state["issues"] = issue_blocks
    state["parse_counts"] = counts

    early_exit = counts.get("severe", 0) == 0 and counts.get("errors", 0) == 0
    state["early_exit_after_parse"] = early_exit
    if early_exit:
        result["success"] = True
        result["iterations"] = []
        result["last_issues"] = []
        result["final_bkp_path"] = config["bkp_path"]

    print(f"[PARSE] issue_blocks: {len(issue_blocks)}")
    print(f"[PARSE] counts: severe={counts['severe']} errors={counts['errors']} warnings={counts['warnings']}")
    if issue_blocks:
        print("[PARSE] first issue (head):")
        print("\n".join(issue_blocks[0])[:800])
    else:
        print("[PARSE] no '** SEVERE ERROR/ERROR/WARNING' header found")
    if early_exit:
        print("[PARSE] early-exit ON: severe/errors are zero -> skip remaining patch pipeline.")
    return session


def begin_repair_attempt(session: Dict[str, Any]) -> Dict[str, Any]:
    """Gate for one full rag->diagnose->patch_plan->patch_codegen->patch_apply->
    rerun_and_parse cycle. Call this once at the start of every cycle (including
    the first). Enforces a hard cap (session['repair_cycle_cap'], default 3) so
    the agent loop cannot repair-cycle forever even if it keeps deciding to
    retry -- this is a code-level guard, not just a prompt instruction."""
    session["repair_cycle_count"] = int(session.get("repair_cycle_count", 0)) + 1
    count = session["repair_cycle_count"]
    cap = int(session.get("repair_cycle_cap", 3))
    allowed = count <= cap
    if not allowed:
        print(f"[CYCLE] budget exhausted: attempt {count} > cap {cap}. Must stop repairing now.")
    else:
        print(f"[CYCLE] starting repair attempt {count}/{cap}")
    return {"allowed": allowed, "attempt": count, "cap": cap}


def run_rag(session: Dict[str, Any]) -> Dict[str, Any]:
    state = session["state"]
    issue_blocks = state.get("issues", []) or []
    error_lines = issue_blocks[0] if issue_blocks else []
    queries = _build_search_queries(error_lines) if error_lines else []

    print(f"[RAG] queries: {len(queries)}")
    for i, q in enumerate(queries, 1):
        print(f"  {i}. {q}")

    search_results: Dict[str, List[Dict[str, Any]]] = {}
    top_docs: List[Dict[str, Any]] = []
    tavily_api_key = os.getenv("TAVILY_API_KEY")

    if queries and tavily_api_key:
        from tavily import TavilyClient

        client = TavilyClient(api_key=tavily_api_key)
        for q in queries:
            res = client.search(query=q, max_results=5)
            hits = [
                {"title": r.get("title", ""), "url": r.get("url", ""), "content": r.get("content", "")}
                for r in res.get("results", [])
            ]
            search_results[q] = hits
        top_docs = _select_top_k_docs(search_results, error_lines, top_k=5)
    else:
        print("[RAG] Tavily key not set or no queries; skipping web search")

    state["queries"] = queries
    state["top_docs"] = top_docs
    print(f"[RAG] top_docs: {len(top_docs)}")
    for d in top_docs:
        print(f"  [score={d['score']}] {d['title']}")
        print(f"    {d['url']}")
    return session


def run_diagnose(session: Dict[str, Any]) -> Dict[str, Any]:
    state = session["state"]
    issue_blocks = state.get("issues", []) or []
    top_docs = state.get("top_docs", []) or []
    issue_lines = issue_blocks[0] if issue_blocks else []
    symptom_tags_seed = _derive_symptom_tags(issue_lines)

    if not issue_lines:
        diagnosis = {
            "findings": [],
            "summary": "No issue block found from HIS parse.",
            "next_actions": ["Run Aspen and parse HIS first."],
            "symptom_tags": [],
        }
    else:
        docs_text = _docs_text(top_docs)
        issue_text = "\n".join(issue_lines)
        prompt = PromptTemplate(
            template=(
                "You are an Aspen Plus simulation debugging expert.\n"
                "Return STRICT JSON only (no markdown fences).\n"
                "Be generic: errors may come from BLOCK / STREAM / CONNECTION / GLOBAL settings.\n\n"
                "Schema:\n"
                "{{\n"
                "  \"symptom_tags\": [\"UPPER_SNAKE_CASE\"],\n"
                "  \"findings\": [\n"
                "    {{\n"
                "      \"type\": \"error|warning\",\n"
                "      \"code\": \"string|null\",\n"
                "      \"message\": \"short paraphrase\",\n"
                "      \"line_no\": [0],\n"
                "      \"excerpt\": \"<=120 chars\",\n"
                "      \"probable_cause\": \"one-line cause\",\n"
                "      \"fix_action\": \"concrete action\",\n"
                "      \"fix_snippet\": {{\"kind\": \"COM|INP\", \"content\": \"minimal snippet\"}},\n"
                "      \"where\": \"GLOBAL|COMPONENTS|STREAM:<id>|BLOCK:<id>|CONNECTION\",\n"
                "      \"severity\": \"high|medium|low\",\n"
                "      \"confidence\": 0.0,\n"
                "      \"evidence_docs\": [1]\n"
                "    }}\n"
                "  ],\n"
                "  \"summary\": \"2-3 lines\",\n"
                "  \"next_actions\": [\"ordered next step\"]\n"
                "}}\n\n"
                "Known symptom tag seeds: {symptom_tags_seed}\n\n"
                "Issue lines:\n{issue_text}\n\n"
                "Retrieved docs:\n{docs_text}"
            )
        )
        llm = ChatOpenAI(model=session["part3_diagnosis_model"], temperature=session["llm_temperature"])
        chain = prompt | llm | StrOutputParser()
        with token_usage("part3_diagnosis", model=session["part3_diagnosis_model"], log_path=session["token_usage_path"], extra={"cycle": session.get("repair_cycle_count")}) as _callbacks:
            raw_output = chain.invoke({
                "issue_text": issue_text,
                "docs_text": docs_text,
                "symptom_tags_seed": json.dumps(symptom_tags_seed, ensure_ascii=False),
            }, config={"callbacks": _callbacks})
        diagnosis = _ensure_diag_schema(_safe_json_parse_diag(raw_output))
        diagnosis["symptom_tags"] = list(dict.fromkeys(symptom_tags_seed + diagnosis.get("symptom_tags", [])))
        diagnosis["_raw"] = raw_output

    state["diagnosis"] = diagnosis
    print(f"[DIAG] findings: {len(diagnosis.get('findings', []))}")
    print(f"[DIAG] symptom_tags: {diagnosis.get('symptom_tags', [])}")
    print(f"[DIAG] summary: {diagnosis.get('summary', '')[:200]}")
    if diagnosis.get("next_actions"):
        print("[DIAG] next_actions[0]:", diagnosis["next_actions"][0])
    return session


def run_patch_plan(session: Dict[str, Any], human_feedback: str = "") -> Dict[str, Any]:
    state = session["state"]
    diagnosis = state.get("diagnosis", {}) or {}
    issue_blocks = state.get("issues", []) or []
    top_docs = state.get("top_docs", []) or []
    issue_lines = issue_blocks[0] if issue_blocks else []
    issue_text = "\n".join(issue_lines)
    docs_text = _docs_text(top_docs)

    if session.get("catalog_subset") is None:
        function_db_path = str(session["project_root"] / "aspen_code_v3.yaml")
        if not Path(function_db_path).exists():
            raise FileNotFoundError(f"Function DB not found: {function_db_path}")
        with open(function_db_path, "r", encoding="utf-8") as f:
            function_db = yaml.safe_load(f)
        session["catalog_subset"] = filter_catalog_by_functions(function_db, PART3_FUNCTION_ALLOWLIST)
        session["fn_index"] = build_fn_index(session["catalog_subset"])
    catalog_subset = session["catalog_subset"]

    if not issue_lines:
        patch_plan = {"intent": "NO_ISSUE", "summary": "No issue lines available.", "patches": []}
    else:
        prompt = PromptTemplate.from_template("""
You are an Aspen Plus COM patch planner.

Return ONLY valid JSON. No markdown. No explanation.

Output schema:
{{"intent":"short upper snake case","summary":"2-3 lines","patches":[{{"id":str,"action":"call","fn":str,"args":object,"units":object,"reason":str}}]}}

Rules:
- action must be "call".
- Use ONLY function names appearing in aspen_code_catalog_subset.
- Args must EXACTLY match the args defined in catalog except "doc". Never include "doc" in args.
- Every numeric argument must have a unit in "units".
- Do not invent block/stream IDs not present in diagnosis or issue text.
- If human feedback is provided, prioritize addressing it over the default diagnosis-only fix.
- If no actionable fix can be derived, return {{"intent":"NO_ACTIONABLE_PATCH","summary":"...","patches":[]}}.

HUMAN FEEDBACK (from revise request, empty if none):
{human_feedback}

DIAGNOSIS JSON:
{diagnosis_json}

ISSUE TEXT:
{issue_text}

RETRIEVED DOCS:
{docs_text}

ASPEN CODE CATALOG SUBSET:
{catalog_yaml}

Return JSON now.
""".strip())
        llm = ChatOpenAI(model=session["part3_patch_plan_model"], temperature=session["llm_temperature"])
        chain = prompt | llm | StrOutputParser()
        with token_usage("part3_patch_plan", model=session["part3_patch_plan_model"], log_path=session["token_usage_path"], extra={"cycle": session.get("repair_cycle_count")}) as _callbacks:
            raw_plan = chain.invoke({
                "human_feedback": human_feedback or "(none)",
                "diagnosis_json": json.dumps(diagnosis, ensure_ascii=False, indent=2),
                "issue_text": issue_text,
                "docs_text": docs_text,
                "catalog_yaml": yaml.safe_dump(catalog_subset, sort_keys=False, allow_unicode=True),
            }, config={"callbacks": _callbacks})
        patch_plan = _ensure_patch_plan_schema(_safe_json_parse_patch_plan(raw_plan))
        patch_plan["_raw"] = raw_plan

    state["patch_plan"] = patch_plan
    print("[PATCH_PLAN] intent:", patch_plan.get("intent"))
    print("[PATCH_PLAN] patches:", len(patch_plan.get("patches", [])))
    return session


def run_patch_codegen(session: Dict[str, Any]) -> Dict[str, Any]:
    config, state = session["config"], session["state"]
    bkp_path = config["bkp_path"]
    run_stem = config["run_stem"]
    sim_dir = str(Path(bkp_path).parent)

    patch_plan = state.get("patch_plan") or {}
    catalog_subset = session.get("catalog_subset") or {}
    fn_index = session.get("fn_index") or build_fn_index(catalog_subset)

    if not patch_plan.get("patches"):
        patch_code = "print('[NO_ACTIONABLE_PATCH] patch plan has no patches')"
    else:
        safety_check_patchplan(patch_plan, fn_index)
        prompt = PromptTemplate.from_template("""
You are generating a Python script for an Aspen Plus COM patch.

Output ONLY raw Python code. No markdown. No explanation.

Requirements:
- Do NOT define functions.
- Do NOT create/open/save/close Aspen document.
- Assume `doc` already exists in globals().
- Output ONLY patch call lines corresponding to the plan below.
- Use ONLY helper functions in the catalog.

patch plan JSON:
{patchplan_json}

aspen_code catalog subset YAML:
{catalog_yaml}

Output Python code now.
""".strip())
        llm = ChatOpenAI(model=session["part3_patch_codegen_model"])
        chain = prompt | llm | StrOutputParser()
        with token_usage("part3_patch_codegen", model=session["part3_patch_codegen_model"], log_path=session["token_usage_path"], extra={"cycle": session.get("repair_cycle_count")}) as _callbacks:
            patch_code = chain.invoke({
                "patchplan_json": json.dumps(patch_plan, ensure_ascii=False, indent=2),
                "catalog_yaml": yaml.safe_dump(catalog_subset, sort_keys=False, allow_unicode=True),
            }, config={"callbacks": _callbacks}).strip()

    patch_code_path = str(Path(sim_dir) / f"{run_stem}_patch_codegen.py")
    with open(patch_code_path, "w", encoding="utf-8") as f:
        f.write(patch_code)

    state["patch_code"] = patch_code
    state["patch_code_path"] = patch_code_path
    state["patch_codegen_meta"] = {
        "ready": True,
        "generator": f"llm_{session['part3_patch_codegen_model']}",
        "function_db_source": "patch_plan_catalog_subset",
        "patch_code_path": patch_code_path,
        "n_patches": len(patch_plan.get("patches", [])),
    }
    print("[CODEGEN] ready")
    print("[CODEGEN] patch_code_path:", patch_code_path)
    print("[CODEGEN] chars:", len(patch_code))
    return session


def run_patch_apply(session: Dict[str, Any]) -> Dict[str, Any]:
    config, state = session["config"], session["state"]
    bkp_path = config["bkp_path"]
    run_stem = config["run_stem"]
    sim_dir = str(Path(bkp_path).parent)

    patch_code = (state.get("patch_code") or "").strip()
    patch_code_path = state.get("patch_code_path")
    if (not patch_code) and patch_code_path and Path(patch_code_path).exists():
        patch_code = Path(patch_code_path).read_text(encoding="utf-8")
    if not patch_code:
        raise RuntimeError("patch_code is empty. Run patch codegen first.")
    if "```" in patch_code:
        lines = patch_code.splitlines()
        kept = [ln for ln in lines if not ln.strip().startswith("```")]
        patch_code = "\n".join(kept).strip()

    updated_bkp_path = str(Path(sim_dir) / f"{run_stem}_updated.bkp")
    aspen_mod = _load_aspen_helper_module(session["project_root"], "aspen_code_v1_apply")

    exec_globals: Dict[str, Any] = {"__builtins__": __builtins__}
    for name, obj in vars(aspen_mod).items():
        if not name.startswith("__"):
            exec_globals[name] = obj

    forbidden_tokens = ["start_aspen_doc(", "open_aspen(", "save_aspen(", "close_aspen(", "run_aspen("]
    violations = [tok for tok in forbidden_tokens if tok in patch_code]
    if violations:
        raise RuntimeError(f"patch_code violates v2 contract (lifecycle calls): {violations}")

    doc = None
    try:
        doc = exec_globals["start_aspen_doc"]()
        exec_globals["open_aspen"](doc, bkp_path, visible=False)
        exec_globals["doc"] = doc

        exec_report = run_patch_script_continue(patch_code, exec_globals, stage="patch_apply")
        patch_apply_result = {
            "ok": exec_report["ok"],
            "status": "PATCHED" if exec_report["ok"] else "PARTIAL_EXEC_ERROR",
            "message": "Patch statements executed. Judgement happens in rerun_and_parse.",
            "executed_lines": exec_report["n_ok"],
            "errors": exec_report["errors"],
        }
        session["doc"] = doc
    except Exception as e:
        patch_apply_result = {
            "ok": False,
            "status": "EXEC_ERROR",
            "message": str(e),
            "executed_lines": 0,
            "errors": [traceback.format_exc()],
        }
        if doc is not None:
            try:
                exec_globals["close_aspen"](doc)
            except Exception:
                pass
        session["doc"] = None

    state["patch_apply_result"] = patch_apply_result
    state["updated_bkp_path"] = updated_bkp_path
    print("[APPLY] status:", patch_apply_result["status"])
    print("[APPLY] ok:", patch_apply_result["ok"])
    print("[APPLY] updated_bkp_path:", updated_bkp_path)
    if patch_apply_result["errors"]:
        print("[APPLY] error(head):", patch_apply_result["errors"][0].splitlines()[-1])
    return session


def run_rerun_and_parse(session: Dict[str, Any]) -> Dict[str, Any]:
    config, state, result = session["config"], session["state"], session["result"]
    run_stem = config["run_stem"]
    max_iter = int(config.get("max_iter", 1))
    sim_dir = str(Path(Path(config["bkp_path"]).parent))
    updated_bkp_path = state.get("updated_bkp_path") or str(Path(sim_dir) / f"{run_stem}_updated.bkp")
    post_his_path = str(Path(updated_bkp_path).with_suffix(".his"))

    aspen_mod = _load_aspen_helper_module(session["project_root"], "aspen_code_v1_rerun")
    run_aspen, save_aspen, close_aspen = aspen_mod.run_aspen, aspen_mod.save_aspen, aspen_mod.close_aspen

    doc = session.get("doc")
    if doc is None:
        raise RuntimeError("No open Aspen document found. Run patch_apply first.")

    rerun_summary = {
        "status": "UNKNOWN",
        "updated_bkp_path": updated_bkp_path,
        "post_his_path": post_his_path,
        "counts": {},
        "loop_history": [],
        "error": None,
    }

    try:
        for loop_idx in range(1, max_iter + 1):
            run_aspen(doc)
            save_aspen(doc, updated_bkp_path)

            his_file = Path(post_his_path)
            found = False
            for _ in range(30):
                if his_file.exists() and his_file.stat().st_size > 0:
                    found = True
                    break
                time.sleep(0.5)
            if not found:
                raise FileNotFoundError(f"HIS not found or empty after run/save: {post_his_path}")

            lines = _read_his_lines(post_his_path)
            issue_blocks = _extract_issue_blocks(lines)
            counts = _count_issue_levels(issue_blocks)
            severe_total, errors_total, warnings_total = counts.get("severe", 0), counts.get("errors", 0), counts.get("warnings", 0)
            has_issue = severe_total > 0 or errors_total > 0

            rerun_summary["counts"] = counts
            rerun_summary["loop_history"].append({
                "loop_idx": loop_idx, "severe": severe_total, "errors": errors_total,
                "warnings": warnings_total, "n_issue_blocks": len(issue_blocks),
            })

            state["his_path"] = post_his_path
            state["issues"] = issue_blocks

            if not has_issue:
                rerun_summary["status"] = "NO_ISSUE"
                result["success"] = True
                print("[RERUN] no severe/errors in post-patch .his (warnings allowed) -> success.")
                break

            rerun_summary["status"] = "ISSUE_DETECTED"
            result["success"] = False
            print("[RERUN] issues detected in post-patch .his.")
            print(f"[RERUN] totals -> severe={severe_total}, errors={errors_total}, warnings={warnings_total}")
            break
    except Exception as e:
        rerun_summary["status"] = "RERUN_ERROR"
        rerun_summary["error"] = str(e)
        result["success"] = False
        print("[RERUN] error:", e)
        print(traceback.format_exc().splitlines()[-1])
    finally:
        try:
            close_aspen(doc)
        except Exception:
            pass
        session["doc"] = None

    state["rerun_summary"] = rerun_summary
    state["updated_bkp_path"] = updated_bkp_path
    result["last_issues"] = state.get("issues", [])
    if rerun_summary.get("status") == "NO_ISSUE":
        result["final_bkp_path"] = updated_bkp_path

    print("[RERUN] status:", rerun_summary["status"])
    print("[RERUN] updated_bkp_path:", updated_bkp_path)
    print("[RERUN] post_his_path:", post_his_path)
    if rerun_summary.get("counts"):
        c = rerun_summary["counts"]
        print("[RERUN] Severe:", c.get("severe", 0), "Errors:", c.get("errors", 0), "Warnings:", c.get("warnings", 0))
    return session


def assess_final(session: Dict[str, Any]) -> Dict[str, Any]:
    """Delegates to mcp_server.part3_assessment_tool so the assessment
    rule stays identical to the fixed-graph version. As of item 2
    (2026-09-01), this goes through the same local_adapter/mcp_client gating
    as Part1/Part2 (see mcp_part3_assessment above) instead of a bare direct
    import, and the call is recorded into state["mcp_trace"]."""
    config, state, result = session["config"], session["state"], session["result"]
    mcp_call = mcp_part3_assessment(state, result, working_dir=config.get("working_dir", ""))
    state["mcp_trace"] = mcp_trace_append(state, mcp_call)
    if not mcp_call.get("ok", True):
        print(f"[PART3][MCP][ERROR] part3_assessment_tool failed: {mcp_call.get('error')}")
    assessment = mcp_call.get("result") or {}
    return assessment


def review_with_human(
    session: Dict[str, Any],
    assessment: Dict[str, Any],
    hitl_mode: str = "bypass",
    human_answer: Optional[Any] = None,
) -> Dict[str, Any]:
    """Ported from part3_v1.ipynb's human_review_node (previously defined inline
    in the graph-building cell, not in PART3_STAGE_CODE). PART3_HITL_MODE=bypass
    (the runner default, since nbconvert can't answer input()) auto-approves.
    PART3_HITL_MODE=chat (Phase B, 2026-09-02) raises HumanInputRequired for a
    real human to answer -- replacing the LangGraph interrupt() this used to
    call, which stopped working once Phase 3 removed the compiled StateGraph
    there was nothing left for interrupt() to suspend."""
    if hitl_mode == "bypass":
        print("[REVIEW] bypassed (PART3_HITL_MODE=bypass): auto-approving")
        return {
            "stage": "human_review_bypassed",
            "human_answer": {"decision": "approve", "notes": "Auto-approved because PART3_HITL_MODE=bypass (human review disabled)."},
            "human_review_decision": "approve",
            "human_review_required": False,
            "human_review_bypassed": True,
            "human_revise_requested": False,
            "human_stop_requested": False,
        }

    payload = {
        "type": "part3_final_review",
        "question": "Part3 still has issues or errors. Reply with approve, stop, or revise plus notes.",
        "assessment": assessment,
        "allowed_responses": ["approve", "complete", "revise", "stop", "revise_stop"],
        "response_schema": {"decision": "approve|complete|revise|stop|revise_stop", "notes": "optional"},
    }

    if hitl_mode == "chat":
        if human_answer is None:
            print("[REVIEW] pausing (PART3_HITL_MODE=chat) for final review")
            raise HumanInputRequired(payload)
        response = human_answer
    else:
        # Any other PART3_HITL_MODE value (legacy "interactive"): used to call
        # LangGraph's interrupt() here, but that only works inside a compiled
        # StateGraph -- this is called directly from a plain @tool in the
        # bind_tools()+while-loop agent (Phase 3) now, where interrupt() has
        # nothing to suspend and would just raise. Treat as bypass instead of
        # crashing.
        print(f"[REVIEW] unrecognized PART3_HITL_MODE={hitl_mode!r}, treating as bypass")
        response = {"decision": "approve", "notes": "Auto-approved: unrecognized PART3_HITL_MODE."}

    if isinstance(response, dict):
        obj = dict(response)
    elif isinstance(response, str):
        raw = response.strip()
        decision, notes = (raw.split(":", 1) + [""])[:2] if ":" in raw else (raw, "")
        obj = {"decision": decision.strip(), "notes": notes.strip(), "raw": response}
    else:
        obj = {"decision": "", "notes": "", "raw": response}
    obj["decision"] = str(obj.get("decision", "")).lower().strip()
    obj["notes"] = str(obj.get("notes") or obj.get("message") or obj.get("instruction") or "").strip()

    decision = obj.get("decision", "")
    if decision in {"approve", "complete"}:
        return {"stage": "human_review_approved", "human_answer": obj, "human_review_decision": decision, "human_revise_requested": False, "human_stop_requested": False}
    if decision == "revise":
        return {"stage": "human_review_revise_requested", "human_answer": obj, "human_review_decision": decision, "human_revise_requested": True, "human_stop_requested": False}
    return {"stage": "human_review_stopped", "human_answer": obj, "human_review_decision": decision or "stop", "human_revise_requested": False, "human_stop_requested": True}


def write_result(
    session: Dict[str, Any],
    assessment: Dict[str, Any],
    human_review_required: bool = False,
    human_answer: Optional[Dict[str, Any]] = None,
    human_review_decision: str = "",
    human_review_bypassed: bool = False,
    human_stop_requested: bool = False,
) -> str:
    config, state, result = session["config"], session["state"], session["result"]
    final_bkp = result.get("final_bkp_path") or assessment.get("final_bkp_path") or config.get("bkp_path", "")
    ok = bool(assessment.get("success", result.get("success", False))) and not bool(human_stop_requested)

    out = {
        **result,
        "ok": ok,
        "success": ok,
        "final_bkp_path": final_bkp,
        "part3_config": config,
        "parse_counts": state.get("parse_counts", {}),
        "last_issues": state.get("issues", []),
        "diagnosis": state.get("diagnosis"),
        "patch_plan": state.get("patch_plan"),
        "patch_apply_result": state.get("patch_apply_result"),
        "rerun_summary": state.get("rerun_summary"),
        "assessment": assessment,
        "human_review_required": bool(human_review_required),
        "human_answer": human_answer,
        "human_review_decision": human_review_decision,
        "human_review_bypassed": bool(human_review_bypassed),
        "human_stop_requested": bool(human_stop_requested),
        "repair_cycle_count": session.get("repair_cycle_count", 0),
        "errors": result.get("errors", []),
        "mcp_trace": state.get("mcp_trace", []),
        "mcp_fallback_log": list(MCP_FALLBACK_LOG),
    }
    path = Path(config.get("part3_result_path") or "part3_result.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[WRITE_RESULT] result written: {path}")
    print(json.dumps({"ok": out.get("ok"), "final_bkp_path": out.get("final_bkp_path"), "part3_result_path": str(path)}, ensure_ascii=False, indent=2))
    result["ok"] = ok
    return str(path)
