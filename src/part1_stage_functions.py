"""Part1 stage logic, refactored out of part1_v1.ipynb's inline cells into
real functions (Phase C1, 2026-09-02 -- gradio-chatbot-architecture-plan item C),
mirroring part3_stage_functions.py's session-dict pattern established in
Phase 3.

Behavior is preserved from the original notebook cells -- this is a
restructuring (explicit session dict in/out instead of bare notebook
globals/AGENT_SESSION), not a rewrite of the underlying logic. Part1 has no
Aspen COM / thread-affine resource at all (confirmed by inspection -- the
notebook's `win32com.client` import was always dead), so unlike Part2/Part3
this session dict needs no "doc" field.

session shape:
    session = {
        "config": {...},   # resolved once by build_session, read-only after
        "state": {...},    # AGENT_SESSION's old flat body -- mutated per tool call
        "llm_model": "...", "llm_temperature": 1,
        "part1_schema_model": "...", "part1_completion_model": "...", "part1_image_model": "...",
        "token_usage_path": "...", "agent_events_path": "...",
    }

Part1 has no separate final-result dict the way Part2/Part3 do -- its "result"
is the status file (session["config"]["part1_status_path"], written via
_session_write_status/write_part1_status) plus the schema YAML artifacts
already on disk.
"""
from __future__ import annotations

import base64
import block_ids
import edc_prompt
import json
import os
import re
import subprocess
import tempfile
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from paths import data_dir, shared_file
from typing import Any, Dict, List, Optional, Tuple

import yaml
from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import JsonOutputParser, StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

from agent_loop_utils import HumanInputRequired
from experiment_logging import token_usage

# Set by build_session(); read by build_schema_draft_langchain/llm_complete_missing
# etc. below (ported as module-level global reads, same convention as
# MCP_ADAPTER_MODE -- avoids threading a new parameter through every pure
# helper function just for logging).
TOKEN_USAGE_PATH: Optional[str] = None


# ===========================================================================
# MCP gating (mirrors part3_stage_functions.py's item-2/3 pattern)
# ===========================================================================
MCP_ADAPTER_MODE = os.environ.get("MCP_ADAPTER_MODE", "local_adapter").strip().lower()
MCP_CLIENT_FALLBACK_TO_LOCAL = True
MCP_FALLBACK_LOG: List[Dict[str, Any]] = []

_HERE = Path(__file__).resolve().parent
_MCP_CALL_TOOL_PATH = _HERE / "mcp_call_tool.py"


def mcp_tool_result(
    tool_name: str,
    result: Any = None,
    ok: bool = True,
    error: str = "",
    mode: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "tool": tool_name,
        "mode": mode or MCP_ADAPTER_MODE,
        "ok": ok,
        "result": result,
        "error": error,
        "called_at": datetime.now().isoformat(timespec="seconds"),
    }


def mcp_client_tool_call(tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    helper_path = _MCP_CALL_TOOL_PATH
    if not helper_path.exists():
        return mcp_tool_result(
            tool_name, result=None, ok=False,
            error=f"MCP helper script not found: {helper_path}", mode="mcp_client",
        )

    args_file = None
    out_file = None
    # Same file-based child-trace fix as Part2/Part3:
    # subprocess.run(capture_output=True) intermittently loses piped stdout
    # under a busy nbconvert-launched Jupyter kernel; a plain file write has
    # no such race. Part1 has not itself observed the failure, but applies
    # the same fix for consistency.
    max_attempts = 2
    last_error = ""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(arguments or {}, f, ensure_ascii=False)
            args_file = f.name
        out_file = args_file + ".out.json"

        for attempt in range(1, max_attempts + 1):
            try:
                os.unlink(out_file)
            except OSError:
                pass
            completed = subprocess.run(
                [os.sys.executable, str(helper_path), tool_name, "--arguments-file", args_file, "--output-file", out_file],
                cwd=str(_HERE.parent),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=90,
            )
            if completed.returncode != 0:
                last_error = (completed.stderr or completed.stdout or "").strip()
                time.sleep(0.5)
                continue

            if Path(out_file).exists():
                raw = Path(out_file).read_text(encoding="utf-8")
            else:
                stdout_lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
                raw = stdout_lines[0] if stdout_lines else ""
            if not raw:
                last_error = f"no output from output-file or stdout (attempt {attempt}/{max_attempts})."
                time.sleep(0.5)
                continue
            try:
                payload = json.loads(raw)
            except Exception as parse_exc:
                last_error = f"{type(parse_exc).__name__}: {parse_exc} | raw={raw!r:.300}"
                time.sleep(0.5)
                continue
            payload["mode"] = "mcp_client"
            if attempt > 1:
                payload["retries"] = attempt - 1
            return payload
        return mcp_tool_result(tool_name, result=None, ok=False, error=f"gave up after {max_attempts} attempts: {last_error}", mode="mcp_client")
    except Exception as exc:
        return mcp_tool_result(tool_name, result=None, ok=False, error=f"{type(exc).__name__}: {exc}", mode="mcp_client")
    finally:
        for _p in (args_file, out_file):
            if _p:
                try:
                    os.unlink(_p)
                except OSError:
                    pass


def maybe_mcp_client_call(tool_name: str, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if MCP_ADAPTER_MODE != "mcp_client":
        return None
    call = mcp_client_tool_call(tool_name, arguments)
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


def mcp_get_paper_context(session: Dict[str, Any], max_chars: int = 12000) -> Dict[str, Any]:
    config = session["config"]
    client_call = maybe_mcp_client_call("paper_context_tool", {
        "paper_text_path": config["paper_text_path"],
        "max_chars": max_chars,
    })
    if client_call is not None:
        return client_call
    try:
        text = load_text(config["paper_text_path"], max_chars=max_chars)
        return mcp_tool_result("paper_context_tool", {
            "paper_text_path": config["paper_text_path"],
            "paper_text_exists": Path(config["paper_text_path"]).exists(),
            "max_chars": max_chars,
            "text": text,
        }, mode="local_adapter")
    except Exception as exc:
        return mcp_tool_result("paper_context_tool", ok=False, error=f"{type(exc).__name__}: {exc}", mode="local_adapter")


def mcp_get_image_hints(session: Dict[str, Any]) -> Dict[str, Any]:
    config = session["config"]
    arguments = {
        "paper_image_dir": config["paper_image_dir"],
        "paper_image_glob": config["paper_image_glob"],
        "paper_image_top_k": config["paper_image_top_k"],
    }
    client_call = maybe_mcp_client_call("image_context_tool", arguments)
    if client_call is not None:
        return client_call
    return mcp_tool_result("image_context_tool", dict(arguments), mode="local_adapter")


def mcp_get_schema_rules(session: Dict[str, Any]) -> Dict[str, Any]:
    config = session["config"]
    client_call = maybe_mcp_client_call("schema_rule_tool", {"schema_layer_path": config["schema_layer_path"]})
    if client_call is not None:
        return client_call
    try:
        schema_layer = load_yaml(config["schema_layer_path"])
        return mcp_tool_result("schema_rule_tool", {
            "schema_layer_path": config["schema_layer_path"],
            "top_keys": list(schema_layer.keys()) if isinstance(schema_layer, dict) else [],
            "schema_layer": schema_layer,
        }, mode="local_adapter")
    except Exception as exc:
        return mcp_tool_result("schema_rule_tool", ok=False, error=f"{type(exc).__name__}: {exc}", mode="local_adapter")


def mcp_schema_precheck(session: Dict[str, Any], schema_obj: Dict[str, Any], schema_layer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    config = session["config"]
    client_call = maybe_mcp_client_call("schema_validation_tool", {
        "schema_obj": schema_obj,
        "schema_layer": schema_layer,
        "schema_layer_path": config["schema_layer_path"],
    })
    if client_call is not None:
        return client_call
    try:
        schema_layer = schema_layer or load_yaml(config["schema_layer_path"])
        schema_quality = schema_quality_report(schema_obj)
        normalized_schema = deepcopy(schema_obj)
        normalize_specs(normalized_schema)
        normalized_schema = normalize_unique_stream_edges(normalized_schema)
        validate_unique_stream_edges(normalized_schema)
        missing = validate_schema_for_execution(normalized_schema, schema_layer=schema_layer)
        return mcp_tool_result("schema_validation_tool", {
            "schema_quality": schema_quality,
            "missing": missing,
            "n_missing": len(missing),
            "structural_error": "",
        }, mode="local_adapter")
    except Exception as exc:
        return mcp_tool_result("schema_validation_tool", {
            "schema_quality": schema_quality_report(schema_obj) if isinstance(schema_obj, dict) else {},
            "missing": [],
            "n_missing": 0,
            "structural_error": f"{type(exc).__name__}: {exc}",
        }, ok=False, error=f"{type(exc).__name__}: {exc}", mode="local_adapter")


# ===========================================================================
# Schema draft generation helpers (ported verbatim from part1_v1.ipynb cell 8
# -- pure functions, only change is TOKEN_USAGE_PATH module global instead of
# the notebook's bare `part1_token_usage_path` global)
# ===========================================================================
# 이부분에서 LLM이 user input, paper, schema layer를 바탕으로 "Schema_proto_build.yaml"파일을 생성


# =========================
# Helpers
# =========================
def load_yaml(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def dump_yaml(obj, path: str):
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, sort_keys=False, allow_unicode=True)

def load_text(path: str, max_chars: int = 40000) -> str:
    """Toy: 너무 길면 앞부분만 사용(안정성)"""
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        txt = f.read()
    return txt[:max_chars]


def _write_review_draft(parsed: dict, out_schema_path: str, review_path: str = None) -> str:
    if not review_path:
        base, ext = os.path.splitext(out_schema_path)
        review_path = f"{base}_draft{ext or '.yaml'}"
    dump_yaml(parsed, review_path)
    return review_path


# =========================
# Image (VLM) Helpers
# =========================
def _list_image_paths(paper_image_dir: str, paper_image_glob: str = "") -> List[str]:
    if not paper_image_dir:
        return []
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
    if os.path.isfile(paper_image_dir):
        return [paper_image_dir] if os.path.splitext(paper_image_dir)[1].lower() in exts else []
    if not os.path.isdir(paper_image_dir):
        return []

    paths = []
    if paper_image_glob:
        try:
            import glob
            for p in glob.glob(os.path.join(paper_image_dir, paper_image_glob)):
                if os.path.isfile(p) and os.path.splitext(p)[1].lower() in exts:
                    paths.append(os.path.abspath(p))
        except Exception:
            pass
    if not paths:
        for name in os.listdir(paper_image_dir):
            p = os.path.join(paper_image_dir, name)
            if os.path.isfile(p) and os.path.splitext(p)[1].lower() in exts:
                paths.append(os.path.abspath(p))
    return sorted(set(paths))


def select_image_paths_for_scope(
    user_input: str,
    paper_text: str,
    paper_image_dir: str,
    paper_image_glob: str = "",
    top_k: int = 4,
) -> List[str]:
    candidates = _list_image_paths(paper_image_dir, paper_image_glob=paper_image_glob)
    if not candidates:
        return []
    if len(candidates) <= top_k:
        return candidates

    # Lightweight filename heuristic before VLM call.
    q = (user_input or "").lower()
    t = (paper_text or "").lower()
    key_words = ["figure", "fig", "flowsheet", "process", "pfd", "schema", "reactor", "stream"]
    if "methanol" in q or "methanol" in t:
        key_words.append("methanol")
    if "aromat" in q or "aromat" in t:
        key_words.append("aromat")

    scored = []
    for p in candidates:
        name = os.path.basename(p).lower()
        s = 0
        for kw in key_words:
            if kw in name:
                s += 2
        if re.search(r"(fig|figure)[-_ ]?\d+", name):
            s += 1
        scored.append((s, p))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [p for _, p in scored[:max(1, int(top_k))]]


def _image_mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    if ext == ".bmp":
        return "image/bmp"
    if ext in (".tif", ".tiff"):
        return "image/tiff"
    return "image/png"


def _image_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def vlm_extract_image_info(image_path: str, model: str = "gpt-5.2", temperature: float = 1) -> Dict[str, Any]:
    if not image_path or not os.path.isfile(image_path):
        return {"image_path": image_path, "blocks": [], "streams": [], "connections": [], "conditions": [], "notes": "image missing"}

    prompt = (
        "You are an expert process engineer. Extract information from the process image. "
        "Return ONLY valid JSON. No markdown.\n"
        "Return JSON with keys: blocks, streams, connections, conditions, notes.\n"
        "- blocks: list of {id, name, type_visual, confidence, evidence} (if visible)\n"
        "  * type_visual must be one of [heat_exchanger, heater, cooler, reactor, separator, compressor, valve, pump, mixer, splitter, unknown]\n"
        "  * confidence in [0,1]\n"
        "  * evidence is a short visual cue (symbol/shape/label)\n"
        "- streams: list of {id, name, type, role} (if visible)\n"
        "- connections: list of {from_block, to_block, stream} (if visible)\n"
        "- conditions: list of {item, value, unit, context} (if visible)\n"
        "- notes: any other useful cues.\n"
        "If nothing is legible, return empty lists and a short note."
    )

    llm = ChatOpenAI(model=model, temperature=temperature)
    parser = JsonOutputParser()

    mime = _image_mime(image_path)
    b64 = _image_b64(image_path)
    msg = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
    )

    try:
        with token_usage("part1_image_vlm", model=model, log_path=TOKEN_USAGE_PATH, extra={"image_path": image_path}) as _callbacks:
            raw = llm.invoke([msg], config={"callbacks": _callbacks}).content
        parsed = parser.parse(raw) if isinstance(raw, str) else {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed["image_path"] = image_path
        return parsed
    except Exception:
        return {"image_path": image_path, "blocks": [], "streams": [], "connections": [], "conditions": [], "notes": "vlm parse failed"}


def vlm_extract_multi_image_info(image_paths: List[str], model: str = "gpt-5.2", temperature: float = 1) -> Dict[str, Any]:
    items = []
    for p in image_paths:
        items.append(vlm_extract_image_info(p, model=model, temperature=temperature))
    return {"selected_image_paths": image_paths, "items": items}

# =========================
# Part1 (LangChain)
# =========================
def build_schema_draft_langchain(
    user_input: str,
    paper_text_path: str,
    paper_image_dir: str,
    paper_image_glob: str,
    paper_image_top_k: int,
    schema_layer_path: str,
    out_schema_path: str,
    model: str = "gpt-5-mini",
    temperature: float = 1,
    review_path: str = None,
    use_image: bool = True,
    image_model: str = "gpt-5.2",
):
    # paper + schema 로드
    schema_layer = load_yaml(schema_layer_path)
    paper_text = load_text(paper_text_path)

    # VLM image evidence (optional)
    selected_image_paths = select_image_paths_for_scope(
        user_input=user_input,
        paper_text=paper_text,
        paper_image_dir=paper_image_dir,
        paper_image_glob=paper_image_glob,
        top_k=paper_image_top_k,
    )
    image_info = (
        vlm_extract_multi_image_info(selected_image_paths, model=image_model, temperature=temperature)
        if use_image else {}
    )
    image_evidence = json.dumps(image_info, ensure_ascii=False)

    meta = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "user_input": user_input,
        "paper_text_path": paper_text_path,
        "paper_image_dir": paper_image_dir,
        "paper_image_glob": paper_image_glob,
        "paper_image_top_k": paper_image_top_k,
        "selected_image_paths": selected_image_paths,
        "schema_layer_path": schema_layer_path,
        "paper_text_exists": bool(paper_text_path) and os.path.isfile(paper_text_path),
        "paper_image_dir_exists": bool(paper_image_dir) and os.path.isdir(paper_image_dir),
        "stage": "part1_schema_build",
    }
    if edc_prompt.enabled():
        meta["edc_prompt"] = edc_prompt.VERSION
        print("[PART1] EDC prompt addendum ON (PART1_EDC_PROMPT)")

    prompt = PromptTemplate.from_template(edc_prompt.draft_template("""
You are a process schema generator.
You MUST produce exactly one JSON object that follows the provided schema-layer rules.

Canonical downstream contract (critical):
- The final schema MUST expose these top-level canonical keys whenever the information is available:
  - aspen_schema.property_method
  - aspen_schema.components
  - flowsheet_schema.blocks
  - flowsheet_schema.streams
  - flowsheet_schema.connectivity.edges
  - io.feeds
  - codegen.port_policy
- Do not place critical fields only under schema_proto, aspen_input, aspen, simulation, or value.
- schema_proto is optional. If you include schema_proto, the same critical information must still be represented in the canonical top-level keys above.
- If a field is uncertain but necessary for Aspen execution, include a conservative value with notes/source/confidence instead of omitting the canonical field.


Critical scope rule:
- The paper may include multiple processes/flowsheets/operating modes. You MUST output a schema for ONLY ONE unit process requested by the user_input.
- First, determine the SINGLE target unit requested by the user_input and declare it in a top-level field "scope" as:
  {{
    "target_unit": "...",
    "included_units": ["..."],
    "excluded_units": ["..."],
    "rationale": "brief reason referencing user_input keywords"
  }}
- Then generate blocks/streams/specs ONLY for that target scope.
- The target scope must be internally complete: every block you include must participate in the flowsheet topology, not exist as a disconnected placeholder.

Inclusion criteria:
- Build the canonical top-level schema by jointly using both paper text and paper image evidence.
- For topology (blocks/streams/connectivity), cross-check text and image together before deciding.
- For operating specs/conditions, prefer explicit values from text/table and use image as auxiliary evidence.
- If uncertain, keep the block with lower confidence note rather than dropping it immediately.
- Do not include a unit operation in flowsheet_schema.blocks unless you can also place it into a consistent stream path in flowsheet_schema.connectivity.edges.
- If you include N blocks in flowsheet_schema.blocks, each of those block IDs must appear at least once in flowsheet_schema.connectivity.edges as either edge.from.block or edge.to.block.
- Do not create orphan blocks. If evidence is too weak to connect a candidate unit operation to the rest of the target process, exclude it from flowsheet_schema.blocks instead of leaving it disconnected.
- flowsheet_schema.streams and flowsheet_schema.connectivity.edges must describe the same topology: every process stream ID should correspond to exactly one connectivity edge, and every connectivity edge should reference a stream declared in flowsheet_schema.streams.
- When a feed enters the target process boundary, represent that with from=null and a valid downstream block in connectivity; when a product leaves the target process boundary, represent that with to.block=null.


Equipment type decision policy (text + image priority):
- Use BOTH paper text and paper image evidence.
- If text names an equipment type explicitly (e.g., heat exchanger, reactor, separator), prioritize text.
- If text is functional/ambiguous (e.g., preheater, cooler duty only) and image provides a clear symbol/type_visual with confidence >= 0.7, prioritize image type.
- If text and image conflict and both are strong, keep the more explicit source and record a short reason in block notes.
- Do NOT force equipment type from image only; text evidence can override when explicit.

Output format constraints:
- Return ONLY valid JSON. No markdown/code fences.
- The top-level JSON value MUST be an object starting with {{ and ending with }}, not an array.
- Follow schema-layer keys and types as strictly as possible.
- Keep unknown values as null or omit only if allowed by schema-layer intent.
- Do not drop unit operations unless both text+image evidence indicate they are out of scope.
- Before finalizing, check topology consistency again: the set of block IDs in flowsheet_schema.blocks must match the set of block IDs referenced by connectivity, except for explicit null process-boundary endpoints.
- For every FEED stream in flowsheet.streams, you MUST create a matching io.feeds entry with specs: flow, basis, z, T_C, P_bar.

[User Input]
{user_input}

[Paper Text]
{paper_text}

[Paper Image Evidence (JSON)]
{paper_image_evidence}

[Schema Layer Rules (JSON)]
{schema_layer_json}
"""))

    llm = ChatOpenAI(model=model, temperature=temperature)
    parser = JsonOutputParser()
    chain = prompt | llm

    with token_usage("part1_schema_generation", model=model, log_path=TOKEN_USAGE_PATH) as _callbacks:
        raw_msg = chain.invoke({
            "user_input": user_input,
            "paper_text": paper_text,
            "paper_image_evidence": image_evidence,
            "paper_image_paths": selected_image_paths,
            "schema_layer_json": json.dumps(schema_layer, ensure_ascii=False),
        }, config={"callbacks": _callbacks})
    raw = raw_msg.content if hasattr(raw_msg, "content") else str(raw_msg)
    raw_debug_path = f"{os.path.splitext(out_schema_path)[0]}_raw_response.txt"

    def _extract_json_object_text(text: str) -> str:
        text = (text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start:end + 1]
        return text

    def _parse_schema_json(raw_value):
        if isinstance(raw_value, dict):
            return raw_value
        if isinstance(raw_value, list) and len(raw_value) == 1 and isinstance(raw_value[0], dict):
            print("[PART1][WARN] Schema draft output was a single-item list; unwrapped to JSON object.")
            return raw_value[0]
        if not isinstance(raw_value, str):
            return raw_value

        text = _extract_json_object_text(raw_value)
        try:
            parsed_obj = json.loads(text)
        except Exception:
            parsed_obj = None
        if isinstance(parsed_obj, dict):
            return parsed_obj
        if isinstance(parsed_obj, list) and len(parsed_obj) == 1 and isinstance(parsed_obj[0], dict):
            print("[PART1][WARN] Schema draft output was a single-item list; unwrapped to JSON object.")
            return parsed_obj[0]

        try:
            parsed_obj = parser.parse(raw_value)
        except Exception:
            parsed_obj = None
        if isinstance(parsed_obj, dict):
            return parsed_obj
        if isinstance(parsed_obj, list) and len(parsed_obj) == 1 and isinstance(parsed_obj[0], dict):
            print("[PART1][WARN] Schema draft output was a single-item list; unwrapped to JSON object.")
            return parsed_obj[0]
        return parsed_obj

    parsed = _parse_schema_json(raw)

    if not isinstance(parsed, dict):
        with open(raw_debug_path, "w", encoding="utf-8") as f:
            f.write(raw if isinstance(raw, str) else repr(raw))

        repair_prompt = PromptTemplate.from_template("""
You repair malformed JSON.
Return ONLY one valid JSON object. No markdown. No explanation.
Do not add, remove, or reinterpret data unless required to make JSON syntax valid.
If the input is truncated, close only the minimum open objects/arrays needed.

[MALFORMED JSON]
{raw_json}
""".strip())
        repair_llm = ChatOpenAI(model=model, temperature=temperature)
        repair_msg = (repair_prompt | repair_llm).invoke({"raw_json": raw if isinstance(raw, str) else repr(raw)})
        repaired_raw = repair_msg.content if hasattr(repair_msg, "content") else str(repair_msg)
        repaired_debug_path = f"{os.path.splitext(out_schema_path)[0]}_raw_response_repaired.txt"
        with open(repaired_debug_path, "w", encoding="utf-8") as f:
            f.write(repaired_raw)

        parsed = _parse_schema_json(repaired_raw)
        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"Schema draft output parsed as {type(parsed).__name__}, expected JSON object (dict). "
                f"Raw response saved: {raw_debug_path}; repair response saved: {repaired_debug_path}"
            )
        print(f"[PART1][WARN] Repaired malformed schema JSON: {repaired_debug_path}")

    parsed.setdefault("meta", {})
    if not isinstance(parsed["meta"], dict):
        parsed["meta"] = {}
    parsed["meta"].update(meta)

    review_path = _write_review_draft(parsed, out_schema_path, review_path)
    print(f"[REVIEW] Draft schema written: {review_path}")

    return {
        "draft_path": review_path,
        "draft_schema": parsed,
        "meta": meta,
    }



# =========================
# Schema Quality Check
# =========================
CANONICAL_SCHEMA_REQUIREMENTS = {
    "property_method": {
        "kind": "scalar",
        "canonical": ("aspen_schema", "property_method"),
        "alternatives": [
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
        ],
    },
    "components": {
        "kind": "list",
        "canonical": ("aspen_schema", "components"),
        "alternatives": [
            ("schema_proto", "aspen", "components"),
            ("schema_proto", "aspen_input", "components"),
            ("schema_proto", "aspen_schema", "components"),
            ("schema_proto", "components"),
            ("aspen", "components"),
            ("simulation", "aspen", "components"),
            ("value", "components"),
            ("components",),
        ],
    },
    "blocks": {
        "kind": "list",
        "canonical": ("flowsheet_schema", "blocks"),
        "alternatives": [
            ("flowsheet", "blocks"),
            ("schema_proto", "flowsheet_schema", "blocks"),
            ("schema_proto", "flowsheet", "blocks"),
            ("schema_proto", "blocks"),
            ("blocks",),
            ("value", "flowsheet_schema", "blocks"),
        ],
    },
    "streams": {
        "kind": "list",
        "canonical": ("flowsheet_schema", "streams"),
        "alternatives": [
            ("flowsheet", "streams"),
            ("schema_proto", "flowsheet_schema", "streams"),
            ("schema_proto", "flowsheet", "streams"),
            ("schema_proto", "streams"),
            ("streams",),
            ("value", "flowsheet_schema", "streams"),
        ],
    },
    "connectivity_edges": {
        "kind": "list",
        "canonical": ("flowsheet_schema", "connectivity", "edges"),
        "alternatives": [
            ("flowsheet_schema", "connectivity"),
            ("flowsheet", "connectivity", "edges"),
            ("flowsheet", "connectivity"),
            ("schema_proto", "flowsheet_schema", "connectivity", "edges"),
            ("schema_proto", "flowsheet_schema", "connectivity"),
            ("schema_proto", "flowsheet", "connectivity", "edges"),
            ("schema_proto", "connectivity", "edges"),
            ("schema_proto", "connectivity"),
            ("connectivity", "edges"),
            ("connectivity",),
        ],
    },
    "feeds": {
        "kind": "list",
        "canonical": ("io", "feeds"),
        "alternatives": [
            ("io_schema", "feeds"),
            ("schema_proto", "io", "feeds"),
            ("schema_proto", "io_schema", "feeds"),
            ("schema_proto", "aspen_input", "feeds"),
            ("value", "io", "feeds"),
        ],
    },
    "port_policy": {
        "kind": "dict",
        "canonical": ("codegen", "port_policy"),
        "alternatives": [
            ("codegen_schema", "port_policy"),
            ("schema_proto", "codegen", "port_policy"),
            ("schema_proto", "codegen_schema", "port_policy"),
            ("value", "codegen", "port_policy"),
        ],
    },
}


def _schema_path_str(path) -> str:
    return ".".join(path) if path else ""


def _schema_get(obj, path):
    cur = obj
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return None
    return cur


def _schema_value_ok(value, kind: str) -> bool:
    if kind == "list":
        return isinstance(value, list) and len(value) > 0
    if kind == "dict":
        return isinstance(value, dict) and len(value) > 0
    if kind == "scalar":
        return value is not None and str(value).strip() != ""
    return value is not None


def _schema_value_count(value, kind: str):
    if kind in {"list", "dict"} and isinstance(value, (list, dict)):
        return len(value)
    return None


def schema_quality_report(schema_obj: dict) -> Dict[str, Any]:
    report = {}
    if not isinstance(schema_obj, dict):
        return {"_error": "schema is not a dict"}

    for name, spec in CANONICAL_SCHEMA_REQUIREMENTS.items():
        kind = spec["kind"]
        canonical = spec["canonical"]
        canonical_value = _schema_get(schema_obj, canonical)
        canonical_ok = _schema_value_ok(canonical_value, kind)

        found_path = canonical if canonical_ok else None
        found_value = canonical_value if canonical_ok else None
        if not canonical_ok:
            for path in spec.get("alternatives", []):
                value = _schema_get(schema_obj, path)
                if _schema_value_ok(value, kind):
                    found_path = path
                    found_value = value
                    break

        item = {
            "ok": bool(canonical_ok),
            "canonical_path": _schema_path_str(canonical),
            "canonical_present": canonical_value is not None,
            "found": bool(found_path),
            "found_path": _schema_path_str(found_path) if found_path else None,
        }
        count = _schema_value_count(found_value, kind)
        if count is not None:
            item["count"] = count
        if kind == "scalar" and found_value is not None:
            item["value"] = str(found_value)
        report[name] = item

    report["ok"] = all(v.get("ok") for k, v in report.items() if isinstance(v, dict))
    report["canonical_missing"] = [
        k for k, v in report.items()
        if isinstance(v, dict) and not v.get("ok")
    ]
    report["found_elsewhere"] = [
        k for k, v in report.items()
        if isinstance(v, dict) and (not v.get("ok")) and v.get("found")
    ]
    return report


def print_schema_quality(report: Dict[str, Any], prefix: str = "[PART1][SCHEMA CHECK]"):
    if not isinstance(report, dict):
        return
    for name in ["property_method", "components", "blocks", "streams", "connectivity_edges", "feeds", "port_policy"]:
        item = report.get(name, {})
        if not isinstance(item, dict):
            continue
        if item.get("ok"):
            extra = f" count={item['count']}" if "count" in item else ""
            if "value" in item:
                extra += f" value={item['value']}"
            print(f"{prefix} {name}: OK {item.get('canonical_path')}{extra}")
        elif item.get("found"):
            extra = f" count={item['count']}" if "count" in item else ""
            if "value" in item:
                extra += f" value={item['value']}"
            print(f"{prefix}[WARN] {name}: missing canonical {item.get('canonical_path')}; found at {item.get('found_path')}{extra}")
        else:
            print(f"{prefix}[WARN] {name}: missing canonical {item.get('canonical_path')}; not found in known fallback paths")

def finalize_schema_from_draft(
    draft_path: str,
    out_schema_path: str,
    meta: Dict[str, Any] = None,
):
    parsed = load_yaml(draft_path)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Draft schema is invalid: {draft_path}")

    # Normalize common alias keys emitted by LLM/schema layers.
    # io_schema -> io, codegen_schema -> codegen
    if isinstance(parsed.get("io_schema"), dict):
        parsed.setdefault("io", {})
        if isinstance(parsed.get("io"), dict):
            if not isinstance(parsed["io"].get("feeds"), list) or len(parsed["io"].get("feeds") or []) == 0:
                if isinstance(parsed["io_schema"].get("feeds"), list):
                    parsed["io"]["feeds"] = parsed["io_schema"]["feeds"]
    if isinstance(parsed.get("codegen_schema"), dict):
        parsed.setdefault("codegen", {})
        if isinstance(parsed.get("codegen"), dict):
            if not isinstance(parsed["codegen"].get("port_policy"), dict):
                if isinstance(parsed["codegen_schema"].get("port_policy"), dict):
                    parsed["codegen"]["port_policy"] = parsed["codegen_schema"]["port_policy"]

    if meta:
        parsed.setdefault("meta", {})
        if not isinstance(parsed["meta"], dict):
            parsed["meta"] = {}
        parsed["meta"].update(meta)

    renamed = block_ids.shorten_block_ids(parsed)        # MHeatX helper blocks need short block IDs
    if renamed:
        parsed.setdefault("meta", {})
        if not isinstance(parsed["meta"], dict):
            parsed["meta"] = {}
        parsed["meta"]["block_ids_shortened"] = renamed
        print(f"[PART1] block IDs shortened for Aspen: {block_ids.describe(renamed)}")

    schema_quality = schema_quality_report(parsed)
    parsed.setdefault("meta", {})
    if not isinstance(parsed["meta"], dict):
        parsed["meta"] = {}
    parsed["meta"]["schema_quality"] = schema_quality
    print_schema_quality(schema_quality)

    dump_yaml(parsed, out_schema_path)
    print(f"[OK] Part1 schema written: {out_schema_path}")
    return parsed

# Execution moved to the LangGraph cells below.


# ===========================================================================
# Schema access/normalize/validate/complete pipeline (ported verbatim from
# part1_v1.ipynb cell 12 -- pure functions, only change is TOKEN_USAGE_PATH)
# ===========================================================================
# Dedup: reuse core helpers already defined in previous cell
# Existing reused helpers: load_yaml, dump_yaml, load_text,
# select_image_path, _image_mime, _image_b64, vlm_extract_image_info

def dump_json(obj, path: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def num_value(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, dict):
        v = x.get("value")
        if isinstance(v, (int, float)):
            return float(v)
    return None

def has_num(x) -> bool:
    return num_value(x) is not None

def spec_has_value(x) -> bool:
    if x is None:
        return False
    if isinstance(x, dict):
        v = x.get("value")
        if v is None and "value" not in x:
            # a MAPPING spec (SPLITTER split_fractions = {out_stream: fraction}, Schema_v5) has no "value" key; it is filled
            # when at least one entry is a number or a provenance dict with a value.  Before this, every block with such a
            # field failed Part1's required-field check however it was filled.
            return any((isinstance(e, (int, float)) and not isinstance(e, bool)) or (isinstance(e, dict) and e.get("value") is not None)
                       for e in x.values())
        return v is not None and v != ""
    if isinstance(x, (int, float)):
        return True
    if isinstance(x, str):
        return x.strip() != ""
    return False
# =========================
# JSON Pointer / Patch (RFC6902 subset) with "-" append support
# =========================
def _jsonptr_unescape(tok: str) -> str:
    return tok.replace("~1", "/").replace("~0", "~")

def _get_parent_and_key(doc: Any, pointer: str) -> Tuple[Any, Any]:
    if pointer == "" or pointer == "/":
        raise ValueError("Pointer points to root; not supported for parent/key.")
    if not pointer.startswith("/"):
        raise ValueError(f"Invalid JSON pointer: {pointer}")

    parts = [_jsonptr_unescape(p) for p in pointer.lstrip("/").split("/")]
    cur = doc
    for p in parts[:-1]:
        if isinstance(cur, dict):
            if p not in cur:
                raise KeyError(f"Path not found while traversing: {p}")
            cur = cur[p]
        elif isinstance(cur, list):
            cur = cur[int(p)]
        else:
            raise TypeError(f"Cannot traverse into type: {type(cur)}")

    last = parts[-1]
    if isinstance(cur, list):
        key = "-" if last == "-" else int(last)
    else:
        key = last
    return cur, key

def apply_json_patch(doc: Any, patch_ops: List[Dict[str, Any]]) -> Any:
    doc = json.loads(json.dumps(doc))  # deep copy
    for op in patch_ops:
        o = op.get("op")
        path = op.get("path")
        if isinstance(path, str) and path.startswith("/value/") and "value" not in doc:
            path = path[len("/value"):]
        if path == "/value" and "value" not in doc:
            path = "/"

        # Compatibility for top-level schema variants (both directions).
        if isinstance(path, str):
            if path.startswith("/flowsheet_schema/") and ("flowsheet_schema" not in doc) and isinstance(doc.get("flowsheet"), dict):
                path = "/flowsheet/" + path[len("/flowsheet_schema/"):]
            elif path.startswith("/flowsheet/") and ("flowsheet" not in doc) and isinstance(doc.get("flowsheet_schema"), dict):
                path = "/flowsheet_schema/" + path[len("/flowsheet/"):]

            if path.startswith("/aspen_schema/") and ("aspen_schema" not in doc) and isinstance(doc.get("aspen"), dict):
                path = "/aspen/" + path[len("/aspen_schema/"):]
            elif path.startswith("/aspen/") and ("aspen" not in doc) and isinstance(doc.get("aspen_schema"), dict):
                path = "/aspen_schema/" + path[len("/aspen/"):]

            if path.startswith("/io_schema/") and ("io_schema" not in doc) and isinstance(doc.get("io"), dict):
                path = "/io/" + path[len("/io_schema/"):]
            elif path.startswith("/io/") and ("io" not in doc) and isinstance(doc.get("io_schema"), dict):
                path = "/io_schema/" + path[len("/io/"):]
        if o not in ("add", "remove", "replace"):
            raise ValueError(f"Unsupported op: {o}")
        if not isinstance(path, str):
            raise ValueError("Patch 'path' must be a string.")

        if o == "remove":
            parent, key = _get_parent_and_key(doc, path)
            if isinstance(parent, dict):
                parent.pop(key, None)
            elif isinstance(parent, list):
                if key == "-":
                    raise ValueError("remove with '-' is invalid")
                parent.pop(key)
            else:
                raise TypeError("Remove parent must be dict/list.")
        else:
            value = op.get("value")
            parent, key = _get_parent_and_key(doc, path)
            if isinstance(parent, dict):
                parent[key] = value
            elif isinstance(parent, list):
                if o == "add":
                    if key == "-":
                        parent.append(value)
                    else:
                        if key < 0 or key > len(parent):
                            raise IndexError(f"List index out of range: {key}")
                        if key == len(parent):
                            parent.append(value)
                        else:
                            parent.insert(key, value)
                else:  # replace
                    if key == "-" or key < 0 or key >= len(parent):
                        raise IndexError(f"Replace index out of range: {key}")
                    parent[key] = value
            else:
                raise TypeError("Add/replace parent must be dict/list.")
    return doc


# =========================
# Schema access helpers (supports both v4 structure and legacy)
# =========================
def _schema_mode(schema: Dict[str, Any]) -> str:
    if isinstance(schema.get("value"), dict):
        return "value"
    if isinstance(schema.get("flowsheet"), dict) or isinstance(schema.get("flowsheet_schema"), dict):
        return "top"
    return "value"

def value_root(schema: Dict[str, Any]) -> Dict[str, Any]:
    v = schema.get("value")
    return v if isinstance(v, dict) else schema

def _get(schema: Dict[str, Any], *path, default=None):
    cur = schema
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur

def get_blocks_ref(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = _schema_mode(schema)
    if mode == "value":
        v = value_root(schema)
        if not isinstance(v.get("blocks"), list):
            v["blocks"] = []
        return v["blocks"]

    fs = None
    if isinstance(schema.get("flowsheet"), dict):
        fs = schema.setdefault("flowsheet", {})
    else:
        fs = schema.setdefault("flowsheet_schema", {})

    if not isinstance(fs.get("blocks"), list):
        fs["blocks"] = []
    return fs["blocks"]

def get_feeds_ref(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = _schema_mode(schema)
    if mode == "value":
        v = value_root(schema)
        io = v.setdefault("io", {})
        if not isinstance(io.get("feeds"), list):
            io["feeds"] = []
        return io["feeds"]
    io = schema.setdefault("io", {})
    if not isinstance(io.get("feeds"), list):
        io["feeds"] = []
    feeds = io["feeds"]
    # Fallback for alias key shape: io_schema.feeds
    if len(feeds) == 0 and isinstance(schema.get("io_schema"), dict):
        alias_feeds = schema["io_schema"].get("feeds")
        if isinstance(alias_feeds, list):
            io["feeds"] = alias_feeds
            feeds = io["feeds"]
    return feeds

def get_blocks(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    return get_blocks_ref(schema)


def get_streams(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = _schema_mode(schema)
    if mode == "value":
        v = value_root(schema)
        streams = v.get("streams")
    else:
        fs = schema.get("flowsheet") if isinstance(schema.get("flowsheet"), dict) else schema.get("flowsheet_schema")
        streams = (fs or {}).get("streams")
    return streams if isinstance(streams, list) else []


def get_edges(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = _schema_mode(schema)
    if mode == "value":
        v = value_root(schema)
        edges = _get(v, "connectivity", "edges", default=None)
    else:
        fs = schema.get("flowsheet") if isinstance(schema.get("flowsheet"), dict) else schema.get("flowsheet_schema")
        edges = _get(fs or {}, "connectivity", "edges", default=None)
    return edges if isinstance(edges, list) else []


def get_feeds(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    return get_feeds_ref(schema)


def get_components_v(s: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = _schema_mode(s)
    if mode == "value":
        v = value_root(s)
        comps = v.get("components")
    else:
        comps = s.get("components")
    return comps if isinstance(comps, list) else []


def normalize_specs(schema: Dict[str, Any]) -> None:
    # Normalize common alias keys emitted by LLM/schema layers.
    if isinstance(schema.get("io_schema"), dict):
        schema.setdefault("io", {})
        if isinstance(schema.get("io"), dict):
            if not isinstance(schema["io"].get("feeds"), list) or len(schema["io"].get("feeds") or []) == 0:
                if isinstance(schema["io_schema"].get("feeds"), list):
                    schema["io"]["feeds"] = schema["io_schema"]["feeds"]
    if isinstance(schema.get("codegen_schema"), dict):
        schema.setdefault("codegen", {})
        if isinstance(schema.get("codegen"), dict):
            if not isinstance(schema["codegen"].get("port_policy"), dict):
                if isinstance(schema["codegen_schema"].get("port_policy"), dict):
                    schema["codegen"]["port_policy"] = schema["codegen_schema"]["port_policy"]

    # ensure block specs dicts exist so patch paths are traversable
    for b in get_blocks_ref(schema):
        if isinstance(b, dict) and b.get("specs") is None:
            b["specs"] = {}

    # ensure feed specs dicts exist
    for f in get_feeds_ref(schema):
        if isinstance(f, dict):
            specs = f.get("specs")
            if specs is None:
                f["specs"] = {}



# =========================
# Connectivity guard: one stream -> one edge
# =========================


def normalize_unique_stream_edges(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Ensure each stream ID appears in connectivity.edges only once by renaming
    later duplicates to fresh IDs and syncing flowsheet.streams list.
    """
    fs = schema.get("flowsheet") if isinstance(schema.get("flowsheet"), dict) else schema.get("flowsheet_schema")
    if not isinstance(fs, dict):
        return schema

    conn = fs.get("connectivity") if isinstance(fs.get("connectivity"), dict) else None
    edges = (conn or {}).get("edges") if isinstance((conn or {}).get("edges"), list) else None
    streams = fs.get("streams") if isinstance(fs.get("streams"), list) else None
    if edges is None or streams is None:
        return schema

    used = set()
    existing_ids = set()
    for st in streams:
        if isinstance(st, dict):
            sid = st.get("id")
            if isinstance(sid, str) and sid.strip():
                existing_ids.add(sid.strip())

    def _fresh_id() -> str:
        i = 1
        while True:
            cand = f"S_AUTO_{i}"
            if cand not in existing_ids:
                existing_ids.add(cand)
                return cand
            i += 1

    changed = []
    for idx, e in enumerate(edges):
        if not isinstance(e, dict):
            continue
        sid = e.get("stream")
        if not isinstance(sid, str) or not sid.strip():
            continue
        sid = sid.strip()
        if sid not in used:
            used.add(sid)
            continue

        new_sid = _fresh_id()
        e["stream"] = new_sid
        streams.append({"id": new_sid, "type": "MATERIAL", "role": "INTERNAL"})
        changed.append((idx, sid, new_sid))

    if changed:
        print("[PART1] normalized duplicate stream ids:", changed)

    return schema

def validate_unique_stream_edges(schema: Dict[str, Any]) -> None:
    edges = get_edges(schema)
    if not isinstance(edges, list) or len(edges) == 0:
        return

    seen: Dict[str, int] = {}
    dup_rows: List[str] = []

    for i, e in enumerate(edges):
        if not isinstance(e, dict):
            continue

        s = e.get("stream")
        if not isinstance(s, str) or not s.strip():
            continue

        key = s.strip().upper()
        prev = seen.get(key)
        if prev is None:
            seen[key] = i
            continue

        prev_e = edges[prev] if isinstance(edges[prev], dict) else {}
        cur_e = e

        p_from = ((prev_e.get("from") or {}).get("block"))
        p_to = ((prev_e.get("to") or {}).get("block"))
        c_from = ((cur_e.get("from") or {}).get("block"))
        c_to = ((cur_e.get("to") or {}).get("block"))

        dup_rows.append(
            f"stream={key} | edge#{prev}: {p_from}->{p_to} | edge#{i}: {c_from}->{c_to}"
        )

    if dup_rows:
        joined = "\\n - " + "\\n - ".join(dup_rows)
        raise ValueError(
            "Invalid connectivity: duplicate stream edges detected (one stream must have one source and one sink)."
            + joined
        )


MULTI_OUTLET_REQUIRED_ROLES = {
    "FLASH2": ("V", "L"),
}


def _norm_id(x) -> str:
    return str(x or "").strip().upper()


def _norm_port_role(x) -> str:
    role = str(x or "").strip().upper()
    aliases = {
        "VAP": "V",
        "VAPOR": "V",
        "VAP_OUT": "V",
        "V(OUT)": "V",
        "LIQ": "L",
        "LIQUID": "L",
        "LIQ_OUT": "L",
        "L(OUT)": "L",
    }
    return aliases.get(role, role)


def _fresh_stream_id(streams: List[Dict[str, Any]]) -> str:
    existing = set()
    nums = []
    for st in streams:
        if not isinstance(st, dict):
            continue
        sid = str(st.get("id") or "").strip().upper()
        if not sid:
            continue
        existing.add(sid)
        if sid.startswith("S") and sid[1:].isdigit():
            nums.append(int(sid[1:]))
    n = max(nums, default=0) + 1
    while f"S{n}" in existing:
        n += 1
    return f"S{n}"


def validate_multi_outlet_product_coverage(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Create schema repair tasks for blocks whose required product outlet roles are missing."""
    mode = _schema_mode(schema)
    if mode == "value":
        v = value_root(schema)
        fs = v
        stream_base = "/value/streams"
        edge_base = "/value/connectivity/edges"
    else:
        fs = schema.get("flowsheet") if isinstance(schema.get("flowsheet"), dict) else schema.get("flowsheet_schema")
        stream_base = "/flowsheet/streams" if isinstance(schema.get("flowsheet"), dict) else "/flowsheet_schema/streams"
        edge_base = "/flowsheet/connectivity/edges" if isinstance(schema.get("flowsheet"), dict) else "/flowsheet_schema/connectivity/edges"
    if not isinstance(fs, dict):
        return []

    blocks = fs.get("blocks") if isinstance(fs.get("blocks"), list) else []
    streams = fs.get("streams") if isinstance(fs.get("streams"), list) else []
    conn = fs.get("connectivity") if isinstance(fs.get("connectivity"), dict) else {}
    edges = conn.get("edges") if isinstance(conn.get("edges"), list) else []

    block_types = {
        _norm_id(b.get("id")): _norm_id(b.get("type"))
        for b in blocks
        if isinstance(b, dict) and str(b.get("id") or "").strip()
    }

    outlet_stream_by_block_role: Dict[Tuple[str, str], str] = {}
    for e in edges:
        if not isinstance(e, dict):
            continue
        src = e.get("from") or {}
        dst = e.get("to") or {}
        if not isinstance(src, dict):
            continue
        block_id = _norm_id(src.get("block"))
        role = _norm_port_role(src.get("port_role"))
        stream_id = str(e.get("stream") or "").strip()
        # an outlet port that feeds a DOWNSTREAM BLOCK is as present as one that leaves the flowsheet. Counting only
        # terminal ("product") edges made every intermediate FLASH2 get a duplicate placeholder product stream on the same
        # port, which Aspen rejects ("unavailable action") -- found with case F (SEP-2 -> SPL-1 / V-1).
        if block_id and role and stream_id:
            outlet_stream_by_block_role[(block_id, role)] = stream_id

    tasks: List[Dict[str, Any]] = []
    proposed_streams: List[Dict[str, Any]] = []
    for block_id, btype in block_types.items():
        required_roles = MULTI_OUTLET_REQUIRED_ROLES.get(btype)
        if not required_roles:
            continue
        present_roles = {role for (bid, role), _sid in outlet_stream_by_block_role.items() if bid == block_id}
        for role in required_roles:
            if role in present_roles:
                continue
            new_stream = _fresh_stream_id(streams + proposed_streams)
            proposed_streams.append({"id": new_stream, "type": "MATERIAL", "role": "PRODUCT"})
            tasks.append({
                "id": f"connectivity_{block_id}_{role}_product_stream_decl",
                "path": f"{stream_base}/{len(streams) + len(proposed_streams) - 1}",
                "kind": "connectivity",
                "required": True,
                "hint_keywords": [block_id, btype, role, new_stream, "product stream", "multi outlet"],
                "message": f"{btype} block {block_id} requires a distinct {role} product outlet stream. Add stream {new_stream} as MATERIAL PRODUCT."
            })
            tasks.append({
                "id": f"connectivity_{block_id}_{role}_product_edge",
                "path": f"{edge_base}/{len(edges) + (len(proposed_streams) - 1)}",
                "kind": "connectivity",
                "required": True,
                "hint_keywords": [block_id, btype, role, new_stream, "product edge", "connectivity"],
                "message": f"{btype} block {block_id} is missing product outlet role {role}. Add connectivity edge stream={new_stream}, from.block={block_id}, from.port_role={role}, to.block=null, to.port_role={role}."
            })
    return tasks
# =========================
# Validator: produce missing_tasks
# =========================
def validate_schema_for_execution(
    schema: Dict[str, Any],
    schema_layer: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Produces missing_tasks for execution.
    IMPORTANT:
      - Numeric fields may be raw number OR provenance dict {value, source, ...}.
    """

    tasks: List[Dict[str, Any]] = []
    mode = _schema_mode(schema)
    if mode == "value":
        comp_base = "/value/components"
        block_base = "/value/blocks"
        feed_base = "/value/io/feeds"
    else:
        comp_base = "/aspen/components" if isinstance(schema.get("aspen"), dict) else "/aspen_schema/components"
        block_base = "/flowsheet/blocks" if isinstance(schema.get("flowsheet"), dict) else "/flowsheet_schema/blocks"
        feed_base = "/io/feeds" if isinstance(schema.get("io"), dict) else "/io_schema/feeds"

    # --- connectivity graph: required product outlet coverage for multi-outlet blocks
    tasks.extend(validate_multi_outlet_product_coverage(schema))

    # --- components: aspen_id required (for execution)
    comps = get_components_v(schema)
    for i, c in enumerate(comps):
        aspen_id = (c or {}).get("aspen_id")
        if not isinstance(aspen_id, str) or not aspen_id.strip():
            formula = (c or {}).get("formula")
            name = (c or {}).get("name")
            tasks.append({
                "id": f"component_aspen_id_{i}",
                "path": f"{comp_base}/{i}/aspen_id",
                "kind": "component",
                "required": True,
                "hint_keywords": [k for k in [formula, name, "component", "Aspen ID", "databank"] if k],
                "message": "Missing aspen_id required for INP generation."
            })


    # --- feed specs: flow + basis + z + T_C + P_bar required (for execution)
    feeds = get_feeds(schema)
    for i, f in enumerate(feeds):
        sid = (f or {}).get("stream")
        specs = (f or {}).get("specs") or {}
        # fields
        flow = (specs.get("flow") or {})
        flow_value = flow.get("value")
        flow_basis = flow.get("basis")
        basis = specs.get("basis")
        z = specs.get("z")
        T_C = specs.get("T_C")
        P_bar = specs.get("P_bar")

        # require them
        checks = [
            ("flow", "value", flow_value, ["flow", "kg/h", "kmol", "ton/day", "feed flow"]),
            ("flow", "basis", flow_basis, ["MASS", "MOLE", "basis"]),
            ("basis", None, basis, ["MOLE-FRAC", "MASS-FRAC", "composition basis"]),
            ("z", None, z, ["composition", "mole fraction", "mass fraction"]),
            ("T_C", None, T_C, ["temperature", "°C", "K"]),
            ("P_bar", None, P_bar, ["pressure", "bar", "kPa", "MPa"]),
        ]
        for key1, key2, val, kw in checks:
            missing = False
            if val is None:
                missing = True
            elif key1 == "z" and (not isinstance(val, dict) or len(val) == 0):
                missing = True
            elif key1 in ("T_C", "P_bar") and not has_num(val):
                missing = True
            elif key1 == "flow" and key2 == "value" and not has_num(val):
                missing = True
            elif key1 == "flow" and key2 == "basis" and (not isinstance(val, str) or not val.strip()):
                missing = True
            elif key1 == "basis" and (not isinstance(val, str) or not val.strip()):
                missing = True

            if missing:
                sub = f"{key1}/{key2}" if key2 else key1
                path = f"{feed_base}/{i}/specs/{key1}"
                if key2:
                    path += f"/{key2}"

                tasks.append({
                    "id": f"feed_{sid}_{i}_{sub.replace('/','_')}",
                    "path": path,
                    "kind": "feed_spec",
                    "required": True,
                    "hint_keywords": [sid, "feed", *kw, "operating conditions"],
                    "message": f"Missing required feed spec field: {sub} for stream {sid}"
                })


    # --- block specs: schema-driven (general)
    blocks = get_blocks(schema)
    block_specs_schema = (schema_layer or {}).get("block_specs_schema") if isinstance(schema_layer, dict) else None
    if isinstance(block_specs_schema, dict):
        for i, b in enumerate(blocks):
            btype = ((b or {}).get("type") or "").upper()
            if btype == "MHEATX":
                continue  # handled separately

            bid = (b or {}).get("id")
            spec_schema = block_specs_schema.get(btype)
            if not isinstance(spec_schema, dict):
                continue

            specs = (b or {}).get("specs") or {}
            fields = spec_schema.get("fields") or {}
            required_one_of = spec_schema.get("required_one_of") or []

            # HEATER special rule:
            # Must satisfy a 2-variable pair depending on spec_mode.
            if btype == "HEATER":
                spec_mode_raw = specs.get("spec_mode")
                spec_mode = str(spec_mode_raw).upper().strip() if isinstance(spec_mode_raw, str) else ""

                if not spec_mode:
                    tasks.append({
                        "id": f"block_{bid}_spec_mode",
                        "path": f"{block_base}/{i}/specs/spec_mode",
                        "kind": "block_spec",
                        "required": True,
                        "hint_keywords": [bid, "HEATER", "spec_mode", "TP", "TD", "PD", "OUTLET_T"],
                        "message": f"Missing HEATER spec field: spec_mode"
                    })
                    continue

                if spec_mode in {"OUTLET_T", "TP"}:
                    needed = ["T_out_C", "P_bar"]
                elif spec_mode in {"TD", "DUTY"}:
                    needed = ["T_out_C", "duty"]
                elif spec_mode == "PD":
                    needed = ["P_bar", "duty"]
                else:
                    tasks.append({
                        "id": f"block_{bid}_spec_mode_invalid",
                        "path": f"{block_base}/{i}/specs/spec_mode",
                        "kind": "block_spec",
                        "required": True,
                        "hint_keywords": [bid, "HEATER", "spec_mode", "TP", "TD", "PD", "OUTLET_T", "DUTY"],
                        "message": f"Invalid HEATER spec_mode: {spec_mode}. Allowed: TP, TD, PD, OUTLET_T, DUTY"
                    })
                    continue

                for fname in needed:
                    if not spec_has_value(specs.get(fname)):
                        tasks.append({
                            "id": f"block_{bid}_{fname}",
                            "path": f"{block_base}/{i}/specs/{fname}",
                            "kind": "block_spec",
                            "required": True,
                            "hint_keywords": [bid, "HEATER", spec_mode, fname, "spec"],
                            "message": f"Missing HEATER paired spec field: {fname} (spec_mode={spec_mode})"
                        })
                continue


            # FLASH2 special rule:
            # Use two-spec modes only: TP, TD, or PD. Fraction-based separator specs are
            # outside the Part1 execution contract for FLASH2.
            if btype == "FLASH2":
                spec_mode_raw = specs.get("spec_mode")
                spec_mode = str(spec_mode_raw).upper().strip() if isinstance(spec_mode_raw, str) else ""

                if not spec_mode:
                    tasks.append({
                        "id": f"block_{bid}_spec_mode",
                        "path": f"{block_base}/{i}/specs/spec_mode",
                        "kind": "block_spec",
                        "required": True,
                        "hint_keywords": [bid, "FLASH2", "spec_mode", "TP", "TD", "PD"],
                        "message": "Missing FLASH2 spec field: spec_mode"
                    })
                    continue

                if spec_mode == "TP":
                    needed = ["T_out_C", "P_out_bar"]
                elif spec_mode == "TD":
                    needed = ["T_out_C", "duty"]
                elif spec_mode == "PD":
                    needed = ["P_out_bar", "duty"]
                else:
                    tasks.append({
                        "id": f"block_{bid}_spec_mode_invalid",
                        "path": f"{block_base}/{i}/specs/spec_mode",
                        "kind": "block_spec",
                        "required": True,
                        "hint_keywords": [bid, "FLASH2", "spec_mode", "TP", "TD", "PD"],
                        "message": f"Invalid FLASH2 spec_mode: {spec_mode}. Allowed: TP, TD, PD"
                    })
                    continue

                for fname in needed:
                    if not spec_has_value(specs.get(fname)):
                        tasks.append({
                            "id": f"block_{bid}_{fname}",
                            "path": f"{block_base}/{i}/specs/{fname}",
                            "kind": "block_spec",
                            "required": True,
                            "hint_keywords": [bid, "FLASH2", spec_mode, fname, "spec"],
                            "message": f"Missing FLASH2 paired spec field: {fname} (spec_mode={spec_mode})"
                        })
                continue

            # required_one_of: ensure at least one present
            if isinstance(required_one_of, list) and len(required_one_of) > 0:
                any_ok = any(spec_has_value(specs.get(f)) for f in required_one_of if isinstance(f, str))
                if not any_ok:
                    first = next((f for f in required_one_of if isinstance(f, str)), None)
                    if first:
                        tasks.append({
                            "id": f"block_{bid}_{first}",
                            "path": f"{block_base}/{i}/specs/{first}",
                            "kind": "block_spec",
                            "required": True,
                            "hint_keywords": [bid, btype, first, "spec"],
                            "message": f"Missing {btype} spec: one of {required_one_of} is required"
                        })

            # fields: require value for all fields not in required_one_of
            if isinstance(fields, dict):
                for fname in fields.keys():
                    if fname in required_one_of:
                        continue
                    if not spec_has_value(specs.get(fname)):
                        tasks.append({
                            "id": f"block_{bid}_{fname}",
                            "path": f"{block_base}/{i}/specs/{fname}",
                            "kind": "block_spec",
                            "required": True,
                            "hint_keywords": [bid, btype, fname, "spec"],
                            "message": f"Missing {btype} spec field: {fname}"
                        })

# --- connectivity: ensure MHeatX roles are present if MHeatX exists
    # Policy:
    # 1) specs.outlet_streams must be dict with >=2 pairs {in_stream: out_stream}
    # 2) specs.temp_specs must be list
    # 3) len(temp_specs) must be len(outlet_streams)-1  (N-1 rule)
    # 4) each temp_specs item: out_stream must be in outlet_streams.values()
    # 5) each temp_specs item: T_out_C must be numeric (raw or {value: ...})
    for i, b in enumerate(blocks):
        btype = ((b or {}).get("type") or "").upper()
        if btype != "MHEATX":
            continue

        bid = (b or {}).get("id")
        specs = (b or {}).get("specs") or {}

        outlet_streams = specs.get("outlet_streams")
        temp_specs = specs.get("temp_specs")

        # 1) outlet_streams required
        if not isinstance(outlet_streams, dict) or len(outlet_streams) < 2:
            tasks.append({
                "id": f"mheatx_{bid}_outlet_streams",
                "path": f"{block_base}/{i}/specs/outlet_streams",
                "kind": "block_spec",
                "required": True,
                "hint_keywords": [bid, "MHeatX", "outlet_streams", "inlet", "outlet", "FEED", "COOL"],
                "message": f"MHeatX {bid} requires specs.outlet_streams mapping with >=2 in->out pairs."
            })
            continue

        N = len(outlet_streams)
        out_values = [v for v in outlet_streams.values() if isinstance(v, str) and v.strip()]
        out_set = set(out_values)

        # 2) temp_specs required
        if not isinstance(temp_specs, list):
            tasks.append({
                "id": f"mheatx_{bid}_temp_specs",
                "path": f"{block_base}/{i}/specs/temp_specs",
                "kind": "block_spec",
                "required": True,
                "hint_keywords": [bid, "MHeatX", "TEMP", "outlet temperature", "N-1"],
                "message": f"MHeatX {bid} requires specs.temp_specs list (TEMP-only policy)."
            })
            continue

        # 3) N-1 rule
        required_n = max(N - 1, 1)
        if len(temp_specs) != required_n:
            tasks.append({
                "id": f"mheatx_{bid}_temp_specs_count",
                "path": f"{block_base}/{i}/specs/temp_specs",
                "kind": "block_spec",
                "required": True,
                "hint_keywords": [bid, "MHeatX", "TEMP", "N-1", f"pairs={N}"],
                "message": f"MHeatX {bid} TEMP specs should be N-1 (pairs={N} -> required={required_n}). Currently={len(temp_specs)}."
            })

        # 4-5) each item validation
        for j, item in enumerate(temp_specs):
            os_ = (item or {}).get("out_stream")
            T_ = (item or {}).get("T_out_C")

            if not isinstance(os_, str) or not os_.strip():
                tasks.append({
                    "id": f"mheatx_{bid}_temp_specs_{j}_out_stream",
                    "path": f"{block_base}/{i}/specs/temp_specs/{j}/out_stream",
                    "kind": "block_spec",
                    "required": True,
                    "hint_keywords": [bid, "MHeatX", "out_stream", *out_values],
                    "message": f"MHeatX {bid} temp_specs[{j}] missing out_stream"
                })
            elif os_.strip() not in out_set:
                tasks.append({
                    "id": f"mheatx_{bid}_temp_specs_{j}_out_stream_invalid",
                    "path": f"{block_base}/{i}/specs/temp_specs/{j}/out_stream",
                    "kind": "block_spec",
                    "required": True,
                    "hint_keywords": [bid, "MHeatX", "out_stream", "must match outlet_streams values", *out_values],
                    "message": f"MHeatX {bid} temp_specs[{j}].out_stream='{os_}' not in outlet_streams values={sorted(out_set)}"
                })

            if not has_num(T_):
                tasks.append({
                    "id": f"mheatx_{bid}_temp_specs_{j}_T_out_C",
                    "path": f"{block_base}/{i}/specs/temp_specs/{j}/T_out_C",
                    "kind": "block_spec",
                    "required": True,
                    "hint_keywords": [bid, "MHeatX", "TEMP", "outlet temperature", "°C", "K"],
                    "message": f"MHeatX {bid} temp_specs[{j}] missing numeric T_out_C"
                })

    return tasks


# =========================
# Evidence retriever: keyword-based snippet extraction from paper.md
# =========================
def retrieve_evidence(paper_text: str, tasks: List[Dict[str, Any]], window: int = 250) -> Dict[str, Any]:
    """
    For each task:
      1) keyword-based snippet search (existing)
      2) if insufficient, add regex-based numeric/unit snippets (FLOW/T/P/composition hints)
    """
    if not paper_text:
        return {"paper_available": False, "items": []}

    # Common numeric/unit patterns (cheap but high recall)
    # - FLOW: kg/h, kmol/h, ton/day, tonne/day, t/d
    # - TEMP: °C, C, K
    # - PRESS: bar, kPa, MPa
    COMMON_PATTERNS = [
        (r"(-?\d+(?:\.\d+)?)\s*(kg/h|kg\/h|kmol/h|kmol\/h|ton/day|tonne/day|t/d)\b", "FLOW"),
        (r"(-?\d+(?:\.\d+)?)\s*(°C|C)\b", "TEMP_C"),
        (r"(-?\d+(?:\.\d+)?)\s*K\b", "TEMP_K"),
        (r"(-?\d+(?:\.\d+)?)\s*(bar|kPa|MPa)\b", "PRESS"),
        # composition-ish patterns (very loose; helps LLM find mole fraction tables)
        (r"(mole fraction|mol%|molar fraction|mass fraction|wt%)", "COMP_BASIS"),
    ]

    def _collect_keyword_snips(kws2: List[str], max_snips: int = 6) -> List[Dict[str, Any]]:
        snips = []
        for kw in kws2[:8]:
            for m in re.finditer(re.escape(kw), paper_text, flags=re.IGNORECASE):
                s = max(0, m.start() - window)
                e = min(len(paper_text), m.end() + window)
                ctx = paper_text[s:e].replace("\n", " ")
                snips.append({"keyword": kw, "span": [s, e], "text": ctx})
                if len(snips) >= max_snips:
                    return snips
        return snips

    def _collect_pattern_snips(patterns: List[Tuple[str, str]], max_snips: int = 6) -> List[Dict[str, Any]]:
        snips = []
        for pat, tag in patterns:
            for m in re.finditer(pat, paper_text, flags=re.IGNORECASE):
                s = max(0, m.start() - window)
                e = min(len(paper_text), m.end() + window)
                ctx = paper_text[s:e].replace("\n", " ")
                snips.append({"keyword": tag, "span": [s, e], "text": ctx})
                if len(snips) >= max_snips:
                    return snips
        return snips

    items = []
    for t in tasks:
        kws = [k for k in (t.get("hint_keywords") or []) if isinstance(k, str) and k.strip()]

        # de-duplicate keywords while preserving order
        seen = set()
        kws2 = []
        for k in kws:
            kk = k.strip()
            if kk.lower() not in seen:
                seen.add(kk.lower())
                kws2.append(kk)

        # 1) keyword snippets
        snippets = _collect_keyword_snips(kws2, max_snips=6)

        # 2) if too few snippets, add numeric/unit patterns (boost recall)
        #    especially for feed_spec/block_spec tasks
        if len(snippets) < 2:
            kind = (t.get("kind") or "").lower()

            # You can specialize patterns per kind if needed
            patterns = COMMON_PATTERNS

            # For component mapping tasks, also look for Aspen-like IDs such as C3H6-1
            if "component" in kind:
                patterns = COMMON_PATTERNS + [
                    (r"\b[A-Z][A-Z0-9]{1,10}-\d+\b", "ASPEN_ID_CAND"),
                    (r"\bC\d+H\d+(?:-\d+)?\b", "FORMULA_CAND"),
                ]

            pat_snips = _collect_pattern_snips(patterns, max_snips=6 - len(snippets))
            snippets.extend(pat_snips)

        items.append({
            "task_id": t["id"],
            "path": t.get("path"),
            "message": t.get("message"),
            "snippets": snippets
        })
    return {"paper_available": True, "items": items}


# =========================
# LLM completion prompt (JSON Patch only)
# =========================
COMPLETION_PROMPT = PromptTemplate.from_template(
"""
You are an expert process engineer assisting Aspen Plus automation.
Task: fill missing REQUIRED fields in a schema using paper snippets first, then reasonable assumptions.

CANONICAL SCHEMA CONTRACT FOR FINAL OUTPUT:
- Preserve or create patches toward these canonical locations when relevant: aspen_schema.property_method, aspen_schema.components, flowsheet_schema.blocks, flowsheet_schema.streams, flowsheet_schema.connectivity.edges, io.feeds, codegen.port_policy.
- Do not place critical Aspen execution fields only under schema_proto, aspen_input, aspen, simulation, or value.
- Do not invent new values only to satisfy the contract; use existing schema information, paper evidence, or explicit reasonable assumptions following the fill policy.

FILL POLICY:
1) If supported by paper snippets, fill with:
   - source: "paper"
   - include evidence (doc_id/locator/quote) if possible.
2) If paper evidence is insufficient, you MUST still fill REQUIRED fields using a reasonable engineering assumption:
   - source: "llm"
   - confidence <= 0.7
   - add a short rationale.
3) HEATER rule (mandatory):
   - spec_mode OUTLET_T or TP => fill BOTH T_out_C and P_bar.
   - spec_mode TD or DUTY => fill BOTH T_out_C and duty.
   - spec_mode PD => fill BOTH P_bar and duty.
4) Only leave still_missing if it is truly impossible.
STRICT OUTPUT FORMAT:
- For numeric fields (flow.value, T_C, P_bar, T_out_C, P_out_bar, dP_bar), ALWAYS set value as an object:
  {{"value": <number>, "source": "paper"|"llm", "confidence": <0..1>, "evidence": "<short locator or quote or blank>", "rationale": "<short, only when source=llm>"}}
- For non-numeric strings (e.g., flow.basis, basis, spec_mode), set raw string values.
- "filled" must include ONLY task_ids for which you actually emitted a patch op.
- If you patch, use exactly the "path" provided in missing_tasks.

Return ONLY valid JSON with keys: patch, filled, still_missing, rationale.
Use RFC6902 JSON Patch ops. Prefer "replace".
Do NOT use '/-' append. Use explicit indexes.

CONTEXT:
[User Input]
{user_input}

[Schema Layer Rules (YAML as JSON)]
{schema_layer_json}

[Current Schema (proto/partially filled)]
{schema_json}

[Missing Tasks]
{missing_tasks_json}

[Evidence Pack from paper.md (keyword snippets)]
{evidence_json}

[Evidence from image (VLM extract)]
{image_evidence_json}

[Selected image paths]
{image_paths_json}

Return the JSON object now.
""".strip()
)


def llm_complete_missing(
    user_input: str,
    schema_layer: Dict[str, Any],
    schema: Dict[str, Any],
    missing_tasks: List[Dict[str, Any]],
    evidence_pack: Dict[str, Any],
    image_evidence: Dict[str, Any],
    image_paths: List[str],
    model: str = "gpt-5-mini",
    temperature: float = 1,
) -> Dict[str, Any]:
    llm = ChatOpenAI(model=model, temperature=temperature)
    parser = JsonOutputParser()
    completion_prompt = (PromptTemplate.from_template(edc_prompt.completion_template(COMPLETION_PROMPT.template))
                         if edc_prompt.enabled() else COMPLETION_PROMPT)
    chain = completion_prompt | llm | parser

    with token_usage("part1_schema_completion", model=model, log_path=TOKEN_USAGE_PATH, extra={"missing_count": len(missing_tasks or [])}) as _callbacks:
        out = chain.invoke({
            "user_input": user_input,
            "schema_layer_json": json.dumps(schema_layer, ensure_ascii=False),
            "schema_json": json.dumps(schema, ensure_ascii=False),
            "missing_tasks_json": json.dumps(missing_tasks, ensure_ascii=False),
            "evidence_json": json.dumps(evidence_pack, ensure_ascii=False),
            "image_evidence_json": json.dumps(image_evidence, ensure_ascii=False),
            "image_paths_json": json.dumps(image_paths, ensure_ascii=False),
        }, config={"callbacks": _callbacks})
    if not isinstance(out, dict):
        raise RuntimeError("Completion output is not a JSON object.")
    if not isinstance(out.get("patch", []), list):
        raise RuntimeError("Completion 'patch' must be a list.")
    return out


# =========================
# Orchestrator: validate -> evidence -> LLM patch -> apply -> loop
# =========================
def part1_validate_and_complete(
    user_input: str,
    paper_text_path: str,
    schema_layer_path: str,
    proto_schema_path: str,
    out_validated_schema_path: str,
    out_report_json_path: Optional[str] = None,
    model: str = "gpt-5-mini",
    temperature: float = 1,
    max_iters: int = 3,
    use_image: bool = True,
    image_model: str = "gpt-5.2",
    paper_image_dir: str = "",
    paper_image_glob: str = "",
    paper_image_top_k: int = 4,
):
    schema_layer = load_yaml(schema_layer_path)
    paper_text = load_text(paper_text_path, max_chars=200000)
    schema = load_yaml(proto_schema_path)

    # ensure specs dicts exist so patch paths resolve
    normalize_specs(schema)

    # Normalize duplicate stream IDs first, then validate strictly.
    schema = normalize_unique_stream_edges(schema)
    validate_unique_stream_edges(schema)

    # VLM image evidence (optional)
    selected_image_paths = select_image_paths_for_scope(
        user_input=user_input,
        paper_text=paper_text,
        paper_image_dir=paper_image_dir,
        paper_image_glob=paper_image_glob,
        top_k=paper_image_top_k,
    )
    image_info = (
        vlm_extract_multi_image_info(selected_image_paths, model=image_model, temperature=temperature)
        if use_image else {}
    )

    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "temperature": temperature,
        "iterations": []
    }

    for it in range(1, max_iters + 1):
        missing = validate_schema_for_execution(schema, schema_layer=schema_layer)
        report["iterations"].append({
            "iter": it,
            "n_missing": len(missing),
            "missing": missing
        })

        if not missing:
            break

        evidence = retrieve_evidence(paper_text, missing)
        completion = llm_complete_missing(
            user_input=user_input,
            schema_layer=schema_layer,
            schema=schema,
            missing_tasks=missing,
            evidence_pack=evidence,
            image_evidence=image_info,
            image_paths=selected_image_paths,
            model=model,
            temperature=temperature,
        )

        patch_ops = completion.get("patch", [])
        # Apply patch
        if patch_ops:
            schema = apply_json_patch(schema, patch_ops)

        report["iterations"][-1]["completion"] = completion
        report["iterations"][-1]["n_patch_ops"] = len(patch_ops)

    # final validation
    final_missing = validate_schema_for_execution(schema, schema_layer=schema_layer)
    report["final_missing"] = final_missing
    report["ok"] = (len(final_missing) == 0)

    # stamp meta
    schema.setdefault("meta", {})
    if not isinstance(schema["meta"], dict):
        schema["meta"] = {}
    schema_quality = schema_quality_report(schema)
    schema["meta"].update({
        "validated_at": datetime.now().isoformat(timespec="seconds"),
        "validation_completion": {
            "ok": report["ok"],
            "max_iters": max_iters,
            "model": model,
            "temperature": temperature,
            "final_missing_count": len(final_missing),
        },
        "schema_quality": schema_quality,
    })
    report["schema_quality"] = schema_quality
    print_schema_quality(schema_quality)

    dump_yaml(schema, out_validated_schema_path)
    if out_report_json_path:
        dump_json(report, out_report_json_path)

    print(f"[OK] Validated schema written: {out_validated_schema_path}")
    if out_report_json_path:
        print(f"[OK] Validation/completion report written: {out_report_json_path}")

    if not report["ok"]:
        print("[WARN] Some required fields are still missing. See report['final_missing'].")

    return schema, report

# Execution moved to the LangGraph cells below.


# ===========================================================================
# build_session
# ===========================================================================
def build_session(runner_input_path: str = "./runner_input.json", record_event: Any = None) -> Dict[str, Any]:
    """Equivalent of part1_v1.ipynb cell 6 (config bootstrap) + cell 14's
    AGENT_SESSION initial structure. `record_event` is the same
    experiment_logging.record_event function the notebook already imports --
    passed in rather than imported here to avoid this module depending on the
    notebook's own sys.path setup timing."""
    runner_path = Path(runner_input_path)
    if runner_path.exists():
        payload = json.loads(runner_path.read_text(encoding="utf-8"))
    else:
        payload = {}
    runner_part1_input = payload.get("part1_input", {}) or {}

    part1_run_root = _HERE / "run"
    part1_working_dir = runner_part1_input.get("working_dir") or runner_part1_input.get("output_dir") or ""
    if part1_working_dir:
        part1_run_dir = Path(part1_working_dir)
        part1_run_dir.mkdir(parents=True, exist_ok=True)
        part1_run_root = part1_run_dir.parent
        part1_attempt_id = part1_run_dir.name
    else:
        part1_attempt_id = datetime.now().strftime("attempt_%Y%m%d_%H%M%S")
        part1_run_dir = part1_run_root / part1_attempt_id
        suffix = 1
        while part1_run_dir.exists():
            part1_run_dir = part1_run_root / f"{part1_attempt_id}_{suffix:02d}"
            suffix += 1
        part1_run_dir.mkdir(parents=True, exist_ok=True)
        part1_attempt_id = part1_run_dir.name

    def _run_path(filename: str) -> str:
        return str(part1_run_dir / filename)

    def _keep_run_dir(path_value: Optional[str], default_filename: str) -> str:
        filename = Path(path_value or default_filename).name
        return _run_path(filename)

    default_part1_input = {
        "user_input_text": "ATR 공정을 설계해줘",
        "paper_text_path": str(data_dir() / "jinny_DB" / "parsed" / "paper2" / "paper_h2.md"),
        "paper_image_path": str(data_dir() / "jinny_DB" / "parsed" / "paper2" / "Fig. 2.jpg"),
        "paper_image_dir": r"",
        "paper_image_glob": str(data_dir() / "jinny_DB" / "parsed" / "paper2" / "*.jpg"),
        "paper_image_top_k": 4,
        "schema_layer_path": str(shared_file("Schema_v5.yaml")),
        "schema_output_path": _run_path("Schema_proto.yaml"),
        "final_schema_path": _run_path("Schema_validated.yaml"),
        "validation_report_path": _run_path("Schema_validation_completion_report_v2.json"),
        "run_mode": "auto",
        "part1_status_path": _run_path("part1_status.json"),
        "llm_config": {
            "model": "gpt-5.4-mini",
            "temperature": 1,
            "part1_schema_model": "gpt-5.5",
            "part1_completion_model": "gpt-5.5",
            "part1_image_model": "gpt-5.5",
        },
    }

    part1_input = dict(default_part1_input)
    part1_input.update(runner_part1_input)

    user_input = part1_input.get("user_input_text", default_part1_input["user_input_text"])
    paper_text_path = part1_input.get("paper_text_path", default_part1_input["paper_text_path"])
    paper_image_dir = (
        part1_input.get("paper_image_dir")
        or part1_input.get("paper_image_path")
        or default_part1_input["paper_image_path"]
    )
    paper_image_glob = part1_input.get("paper_image_glob", default_part1_input["paper_image_glob"])
    paper_image_top_k = int(part1_input.get("paper_image_top_k", default_part1_input["paper_image_top_k"]) or 4)
    schema_layer_path = part1_input.get("schema_layer_path", default_part1_input["schema_layer_path"])

    out_schema_path = _keep_run_dir(part1_input.get("schema_output_path"), "Schema_proto.yaml")
    final_schema_path = _keep_run_dir(part1_input.get("final_schema_path"), "Schema_validated.yaml")
    validation_report_path = _keep_run_dir(part1_input.get("validation_report_path"), "Schema_validation_completion_report_v2.json")
    part1_status_path = _keep_run_dir(part1_input.get("part1_status_path"), "part1_status.json")

    part1_run_mode = part1_input.get("run_mode", default_part1_input["run_mode"])

    llm_config = dict(payload.get("llm_config", {}) or {})
    llm_config.update(part1_input.get("llm_config", {}) or {})
    llm_model = llm_config.get("model") or "gpt-5.4"
    llm_temperature = llm_config.get("temperature", 1)
    part1_schema_model = llm_config.get("part1_schema_model") or llm_model
    part1_completion_model = llm_config.get("part1_completion_model") or llm_model
    part1_image_model = llm_config.get("part1_image_model") or llm_model

    global TOKEN_USAGE_PATH
    token_usage_path = str(part1_run_dir / "token_usage.jsonl")
    agent_events_path = str(part1_run_dir / "agent_events.jsonl")
    TOKEN_USAGE_PATH = token_usage_path

    part1_hitl_mode = os.environ.get("PART1_HITL_MODE", "bypass").strip().lower()

    print(f"[PART1] user_input: {user_input}")
    print(f"[PART1] run_mode: {part1_run_mode}")
    print(f"[PART1] fallback_model: {llm_model}")
    print(f"[PART1] schema_model: {part1_schema_model}")
    print(f"[PART1] completion_model: {part1_completion_model}")
    print(f"[PART1] image_model: {part1_image_model}")
    print(f"[PART1] run_root: {part1_run_root}")
    print(f"[PART1] attempt_id: {part1_attempt_id}")
    print(f"[PART1] run_dir: {part1_run_dir}")
    print(f"[PART1] schema_output_path: {out_schema_path}")
    print(f"[PART1] final_schema_path: {final_schema_path}")
    print(f"[PART1] validation_report_path: {validation_report_path}")
    print(f"[PART1] part1_status_path: {part1_status_path}")
    print(f"[PART1] token_usage_path: {token_usage_path}")
    print(f"[PART1] agent_events_path: {agent_events_path}")
    print(f"[PART1] hitl_mode: {part1_hitl_mode}")

    if record_event is not None:
        record_event(agent_events_path, "part1_started", "part1", {
            "attempt_id": part1_attempt_id,
            "run_dir": str(part1_run_dir),
            "run_mode": part1_run_mode,
            "schema_model": part1_schema_model,
            "completion_model": part1_completion_model,
            "image_model": part1_image_model,
        })

    config = {
        "user_input": user_input,
        "paper_text_path": paper_text_path,
        "paper_image_dir": paper_image_dir,
        "paper_image_glob": paper_image_glob,
        "paper_image_top_k": paper_image_top_k,
        "schema_layer_path": schema_layer_path,
        "out_schema_path": out_schema_path,
        "final_schema_path": final_schema_path,
        "validation_report_path": validation_report_path,
        "part1_status_path": part1_status_path,
        "part1_run_dir": str(part1_run_dir),
        "part1_run_root": str(part1_run_root),
        "part1_attempt_id": part1_attempt_id,
        "part1_run_mode": part1_run_mode,
        "part1_hitl_mode": part1_hitl_mode,
        # off by default. On = the review question is chosen by edc_policy and the reply is applied.
        "edc_policy": os.environ.get("PART1_EDC_POLICY", "0").strip().lower() in ("1", "true", "on", "yes"),
    }

    state: Dict[str, Any] = {
        "user_input": user_input,
        "effective_user_input": user_input,
        "paper_context": "",
        "image_hints": {},
        "schema_layer_obj": {},
        "mcp_trace": [],
        "input_assessment": {},
        "draft_bundle": {},
        "schema_obj": {},
        "schema_assessment": {},
        "human_review_decision": "",
        "status": {},
    }

    return {
        "config": config,
        "state": state,
        "llm_model": llm_model,
        "llm_temperature": llm_temperature,
        "part1_schema_model": part1_schema_model,
        "part1_completion_model": part1_completion_model,
        "part1_image_model": part1_image_model,
        "token_usage_path": token_usage_path,
        "agent_events_path": agent_events_path,
    }


def write_part1_status(session: Dict[str, Any], data: Dict[str, Any]) -> None:
    path = Path(session["config"]["part1_status_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _session_write_status(session: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    state = session["state"]
    status = dict(state.get("status", {}) or {})
    status.update(updates)
    status["mcp_trace"] = list(state.get("mcp_trace", []) or [])
    status["mcp_fallback_log"] = list(MCP_FALLBACK_LOG)
    state["status"] = status
    write_part1_status(session, status)
    return status


# ===========================================================================
# Input assessment (ported from cell 14)
# ===========================================================================
INPUT_ASSESS_PROMPT = PromptTemplate.from_template("""
You are the human-in-the-loop gatekeeper for Part1 schema drafting.
Decide whether the user's input is sufficient to start schema drafting, or whether a human clarification is needed first.

Return ONLY valid JSON. No markdown.

Core rule:
- Pass if the user_input provides either:
  1) a concrete process / technology / unit operation / section name, OR
  2) a clear boundary such as a figure, table, section, subsystem, or explicit scope.
- Ask for human clarification only if the target is missing or too broad to choose one schema-drafting target from the paper context.

What is NOT required at this stage:
- Detailed operating conditions
- Feed ratios or composition
- Property method
- Equipment specifications
- Whether to model a reactor, Aspen flowsheet, or full process, when the named process itself is already clear

Examples of ready_for_draft reasoning:
- "Design the ATR process" is enough if ATR is a concrete target in the paper.
- "Model the process in Figure 2" is enough if Figure 2 identifies one flowsheet.
- "Build the separator section" is enough if the paper context has a clear separator section.

Examples of needs_human_input reasoning:
- "Design a process" with no process name or boundary.
- "Design a hydrogen process" when the paper contains multiple hydrogen-production alternatives and the user did not choose one.
- "Use this paper" when the paper contains multiple candidate processes/sections and no target is selected.

When asking a question:
- Ask only for the missing target or boundary.
- Include candidate targets from the paper context when available.
- Keep it short and actionable.

Return JSON with keys:
- decision: "ready_for_draft" or "needs_human_input"
- target_process: string or null
- target_boundary: string or null
- confidence: number between 0 and 1
- candidate_targets: list of strings
- reason: string
- question: string, empty when decision is ready_for_draft
- missing_items: list of strings

[User Input]
{user_input}

[Paper Context Excerpt]
{paper_context}

[Image Path Hints]
{image_hints_json}
""")

_CLARIFY_FALLBACK_QUESTION = "무슨 공정 또는 범위 스키마를 원하시나요? 프로세스명이나 Figure/Table/Section 정보를 알려주세요."


def assess_user_input(
    session: Dict[str, Any],
    user_input_text: str,
    paper_context_override: Optional[str] = None,
    image_hints_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    config = session["config"]
    text = (user_input_text or "").strip()
    if not text:
        return {
            "decision": "needs_human_input",
            "target_process": None,
            "target_boundary": None,
            "confidence": 1.0,
            "candidate_targets": [],
            "reason": "No user input was provided.",
            "question": _CLARIFY_FALLBACK_QUESTION,
            "missing_items": ["target_process_or_boundary"],
        }

    paper_context = paper_context_override
    if paper_context is None:
        paper_context = load_text(config["paper_text_path"], max_chars=12000)
    image_hints = image_hints_override or {
        "paper_image_dir": config["paper_image_dir"],
        "paper_image_glob": config["paper_image_glob"],
        "paper_image_top_k": config["paper_image_top_k"],
    }

    llm = ChatOpenAI(model=session["llm_model"], temperature=session["llm_temperature"])
    parser = JsonOutputParser()
    chain = INPUT_ASSESS_PROMPT | llm | parser

    try:
        with token_usage("part1_input_assessment", model=session["llm_model"], log_path=TOKEN_USAGE_PATH) as _callbacks:
            out = chain.invoke({
                "user_input": text,
                "paper_context": paper_context,
                "image_hints_json": json.dumps(image_hints, ensure_ascii=False),
            }, config={"callbacks": _callbacks})
        if not isinstance(out, dict):
            raise RuntimeError("Input assessment output is not a JSON object.")

        decision = out.get("decision")
        if decision not in {"ready_for_draft", "needs_human_input"}:
            out["decision"] = "needs_human_input"
            out["reason"] = f"Invalid or missing decision from input assessor: {decision}"

        if out["decision"] == "needs_human_input" and not out.get("question"):
            out["question"] = _CLARIFY_FALLBACK_QUESTION
        if out["decision"] == "ready_for_draft":
            out["question"] = ""
            out["missing_items"] = []

        out.setdefault("target_process", None)
        out.setdefault("target_boundary", None)
        out.setdefault("confidence", None)
        out.setdefault("candidate_targets", [])
        out.setdefault("reason", "")
        out.setdefault("missing_items", [])
        return out
    except Exception as exc:
        return {
            "decision": "needs_human_input",
            "target_process": None,
            "target_boundary": None,
            "confidence": None,
            "candidate_targets": [],
            "reason": f"Input assessment failed: {type(exc).__name__}: {exc}",
            "question": _CLARIFY_FALLBACK_QUESTION,
            "missing_items": ["target_process_or_boundary"],
        }


def _walk_dict_items(obj: Any, path: str = ""):
    if isinstance(obj, dict):
        yield path, obj
        for key, value in obj.items():
            next_path = f"{path}.{key}" if path else str(key)
            yield from _walk_dict_items(value, next_path)
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            yield from _walk_dict_items(value, f"{path}[{idx}]")


def collect_schema_human_review_items(schema_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    critical_markers = [
        "property_method", "components", "feeds", "flow", "z", "T_C", "P_bar",
        "T_out_C", "P_out_bar", "duty", "blocks", "connectivity", "specs",
    ]
    for path, value in _walk_dict_items(schema_obj):
        if not isinstance(value, dict):
            continue
        source = str(value.get("source") or "").lower()
        confidence = value.get("confidence")
        try:
            confidence_value = float(confidence) if confidence is not None else None
        except Exception:
            confidence_value = None
        is_low_conf_assumption = source in {"llm", "default", "inferred"} and (
            confidence_value is None or confidence_value <= 0.45
        )
        is_critical = any(marker in path for marker in critical_markers)
        if is_low_conf_assumption and is_critical:
            items.append({
                "kind": "low_confidence_assumption",
                "field_path": path,
                "source": source,
                "confidence": confidence_value,
                "value": value.get("value"),
                "reason": "Critical execution field uses a low-confidence inferred/default/LLM value.",
                "question": f"Review whether {path}={value.get('value')} is acceptable for this process.",
            })
    return items[:8]


# ===========================================================================
# Tool-facing stage functions (session-ified versions of cell 14's @tool bodies)
# ===========================================================================
def fetch_paper_context(session: Dict[str, Any]) -> Dict[str, Any]:
    state = session["state"]
    paper_call = mcp_get_paper_context(session, max_chars=12000)
    image_call = mcp_get_image_hints(session)
    rules_call = mcp_get_schema_rules(session)

    paper_context = (paper_call.get("result") or {}).get("text") or ""
    image_hints = image_call.get("result") or {}
    schema_layer_obj = (rules_call.get("result") or {}).get("schema_layer") or {}

    state["paper_context"] = paper_context
    state["image_hints"] = image_hints
    state["schema_layer_obj"] = schema_layer_obj
    state["mcp_trace"] = mcp_trace_append(state, paper_call, image_call, rules_call)

    return {
        "paper_ok": paper_call.get("ok"),
        "rules_ok": rules_call.get("ok"),
        "paper_chars": len(paper_context),
    }


def assess_input_readiness(session: Dict[str, Any]) -> Dict[str, Any]:
    state = session["state"]
    assessment = assess_user_input(
        session,
        state.get("effective_user_input", state.get("user_input")),
        paper_context_override=state.get("paper_context") or None,
        image_hints_override=state.get("image_hints") or None,
    )
    state["input_assessment"] = assessment
    _session_write_status(session, {
        "ok": assessment.get("decision") == "ready_for_draft",
        "stage": "input_assessed",
        "input_decision": assessment.get("decision"),
        "input_assessment": assessment,
        "human_review_required": assessment.get("decision") == "needs_human_input",
        "human_intervention_type": "input_clarification" if assessment.get("decision") == "needs_human_input" else None,
        "human_question": assessment.get("question") or "",
        "final_output_schema_path": None,
        "final_output_kind": None,
        "final_output_ready": False,
    })
    return assessment


def request_human_clarification(session: Dict[str, Any], question: str, human_answer: Optional[str] = None) -> Dict[str, Any]:
    state = session["state"]
    hitl_mode = session["config"]["part1_hitl_mode"]
    assessment = state.get("input_assessment", {}) or {}

    if hitl_mode == "bypass":
        answer = (assessment.get("candidate_targets") or [None])[0] or state.get("user_input")
        resumed_assessment = {
            "decision": "ready_for_draft_after_human",
            "target_process": answer,
            "target_boundary": None,
            "confidence": assessment.get("confidence"),
            "candidate_targets": assessment.get("candidate_targets", []),
            "reason": "Auto-selected because PART1_HITL_MODE=bypass (human review disabled).",
            "question": "",
            "missing_items": [],
            "previous_assessment": assessment,
        }
        state["input_assessment"] = resumed_assessment
        state["effective_user_input"] = answer
        _session_write_status(session, {
            "ok": True,
            "stage": "human_input_bypassed",
            "input_decision": "ready_for_draft_after_human",
            "input_assessment": resumed_assessment,
            "human_review_required": False,
            "human_review_bypassed": True,
            "human_intervention_type": None,
            "human_question": "",
            "human_answer": answer,
            "effective_user_input": answer,
        })
        return {"answer": answer, "bypassed": True}

    if hitl_mode == "chat":
        if human_answer is None:
            raise HumanInputRequired({
                "type": "clarification",
                "question": question,
                "context": {
                    "candidate_targets": assessment.get("candidate_targets", []),
                    "confidence": assessment.get("confidence"),
                },
            })
        answer = str(human_answer)
        resumed_assessment = {
            "decision": "ready_for_draft_after_human",
            "target_process": answer,
            "target_boundary": None,
            "confidence": assessment.get("confidence"),
            "candidate_targets": assessment.get("candidate_targets", []),
            "reason": "Human-provided answer (PART1_HITL_MODE=chat).",
            "question": question,
            "missing_items": [],
            "previous_assessment": assessment,
        }
        state["input_assessment"] = resumed_assessment
        state["effective_user_input"] = answer
        _session_write_status(session, {
            "ok": True,
            "stage": "human_input_answered_chat",
            "input_decision": "ready_for_draft_after_human",
            "input_assessment": resumed_assessment,
            "human_review_required": False,
            "human_review_bypassed": False,
            "human_intervention_type": None,
            "human_question": question,
            "human_answer": answer,
            "effective_user_input": answer,
        })
        return {"answer": answer, "bypassed": False}

    answer = state.get("user_input")
    state["effective_user_input"] = answer
    _session_write_status(session, {"stage": "human_input_answered_fallback", "human_answer": answer})
    return {"answer": answer, "bypassed": False, "note": f"unrecognized PART1_HITL_MODE={hitl_mode!r}"}


def draft_schema(session: Dict[str, Any]) -> Dict[str, Any]:
    state = session["state"]
    config = session["config"]
    draft_bundle = build_schema_draft_langchain(
        user_input=state.get("effective_user_input") or state.get("user_input"),
        paper_text_path=config["paper_text_path"],
        paper_image_dir=config["paper_image_dir"],
        paper_image_glob=config["paper_image_glob"],
        paper_image_top_k=config["paper_image_top_k"],
        schema_layer_path=config["schema_layer_path"],
        out_schema_path=config["out_schema_path"],
        model=session["part1_schema_model"],
        temperature=session["llm_temperature"],
        review_path=None,
        use_image=True,
        image_model=session["part1_image_model"],
    )
    schema_obj = finalize_schema_from_draft(
        draft_path=draft_bundle["draft_path"],
        out_schema_path=config["out_schema_path"],
        meta=draft_bundle["meta"],
    )
    state["draft_bundle"] = draft_bundle
    state["schema_obj"] = schema_obj
    _session_write_status(session, {
        "ok": True,
        "stage": "draft_ready",
        "input_decision": (state.get("input_assessment") or {}).get("decision"),
        "input_assessment": state.get("input_assessment"),
        "effective_user_input": state.get("effective_user_input") or state.get("user_input"),
        "human_answer": (state.get("status") or {}).get("human_answer"),
        "draft_path": draft_bundle["draft_path"],
        "schema_output_path": config["out_schema_path"],
        "final_schema_path": config["final_schema_path"],
        "final_output_schema_path": config["out_schema_path"],
        "final_output_kind": "proto",
        "final_output_ready": False,
        "top_keys": list(schema_obj.keys()),
        "schema_quality": (schema_obj.get("meta", {}) or {}).get("schema_quality"),
    })
    return {"top_keys": list(schema_obj.keys()), "schema_output_path": config["out_schema_path"]}


def assess_schema_quality(session: Dict[str, Any]) -> Dict[str, Any]:
    state = session["state"]
    config = session["config"]
    schema_obj = state.get("schema_obj") or load_yaml(config["out_schema_path"])
    schema_layer = state.get("schema_layer_obj") or load_yaml(config["schema_layer_path"])

    precheck_call = mcp_schema_precheck(session, schema_obj, schema_layer=schema_layer)
    precheck = precheck_call.get("result") or {}
    schema_quality = precheck.get("schema_quality") or schema_quality_report(schema_obj)
    missing = precheck.get("missing") or []
    structural_error = precheck.get("structural_error") or ""
    state["mcp_trace"] = mcp_trace_append(state, precheck_call)

    human_review_items = _review_items_for_session(session, schema_obj)

    if structural_error:
        decision = "needs_completion"
        reason = "Draft schema has structural issues that should be repaired before downstream execution."
    elif missing:
        decision = "needs_completion"
        reason = f"Draft schema has {len(missing)} execution-critical missing item(s)."
    elif human_review_items:
        decision = "needs_human_review"
        if _edc_enabled(session):
            reason = f"Draft schema has {len(human_review_items)} value(s) the paper does not settle (EDC policy)."
        else:
            reason = f"Draft schema has {len(human_review_items)} high-impact low-confidence item(s) requiring human review."
    elif not schema_quality.get("ok"):
        decision = "needs_completion"
        reason = "Draft schema is missing one or more canonical schema sections."
    else:
        decision = "enough"
        reason = "Draft schema satisfies canonical sections and execution precheck found no missing items."

    assessment = {
        "decision": decision,
        "reason": reason,
        "schema_quality": schema_quality,
        "n_missing": len(missing),
        "missing": missing,
        "human_review_items": human_review_items,
        "structural_error": structural_error,
    }
    state["schema_assessment"] = assessment

    status_updates = {
        "stage": "schema_assessed",
        "agent_decision": decision,
        "assessment_reason": reason,
        "assessment_n_missing": len(missing),
        "schema_assessment": assessment,
        "human_review_required": decision == "needs_human_review",
        "human_intervention_type": "schema_review" if decision == "needs_human_review" else None,
        "human_review_items": human_review_items,
    }
    if decision == "enough":
        status_updates.update({
            "ok": True,
            "final_output_schema_path": config["out_schema_path"],
            "final_output_kind": "proto",
            "final_output_ready": True,
            "validation_report_path": None,
            "final_missing_count": 0,
        })
    elif decision == "needs_human_review":
        status_updates.update({
            "ok": False,
            "final_output_schema_path": config["out_schema_path"],
            "final_output_kind": "proto_review_pending",
            "final_output_ready": False,
            "validation_report_path": None,
            "final_missing_count": 0,
        })
    else:
        status_updates.update({
            "final_output_schema_path": None,
            "final_output_kind": None,
            "final_output_ready": False,
        })
    _session_write_status(session, status_updates)
    return {"decision": decision, "reason": reason, "n_review_items": len(human_review_items)}


_LEGACY_REVIEW_QUESTION = "{n} low-confidence schema item(s) need your review. Approve, or add notes?"


# ---------------------------------------------------------------------------
# (PART1_EDC_POLICY): policy-chosen questions and replies that really change the schema.
# Everything below is inert unless config["edc_policy"] is true.
# ---------------------------------------------------------------------------
def _edc_enabled(session: Dict[str, Any]) -> bool:
    return bool((session.get("config") or {}).get("edc_policy"))


def _write_edc_artifacts(session: Dict[str, Any], review: Optional[Dict[str, Any]]) -> None:
    """edc_assumption_ledger.json next to the schema: every value the paper does not state,
    whether or not the user was asked, plus what the user changed.  Never breaks the flow."""
    try:
        import edc_policy as ep
        edc = session["state"].get("edc") or {}
        if not edc:
            return
        out = Path(session["config"]["part1_run_dir"]) / "edc_assumption_ledger.json"
        out.write_text(json.dumps(ep.assumption_ledger(edc["classified"], edc["selected"], review),
                                  ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as exc:                                  # pragma: no cover - diagnostics only
        print(f"[PART1] EDC ledger not written: {exc!r}")


def _review_items_for_session(session: Dict[str, Any], schema_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flag off: the old confidence-threshold selector, unchanged.
    Flag on: the EDC policy; the classification/selection is kept in state for the review step."""
    if not _edc_enabled(session):
        return collect_schema_human_review_items(schema_obj)
    import edc_policy as ep
    res = ep.policy_review_items(schema_obj, load_text(session["config"]["paper_text_path"], max_chars=2_000_000))
    session["state"]["edc"] = {"classified": res["classified"], "selected": res["selected"]}
    _write_edc_artifacts(session, review=None)
    return res["items"]


def ep_parse(text: str, state: Dict[str, Any]) -> Dict[str, Any]:
    import edc_policy as ep
    asked = [r["id"] for r in (((state.get("edc") or {}).get("selected") or {}).get("asked") or [])]
    return ep.parse_review_answer(text, known_ids=asked)


def _asked_edc_ids(session: Dict[str, Any]) -> List[str]:
    edc = session["state"].get("edc") or {}
    return [r["id"] for r in ((edc.get("selected") or {}).get("asked") or [])]


def _edc_unrecognised_message(parsed: Dict[str, Any], ids: List[str]) -> str:
    settable = [i for i in ids if not i.endswith(".z")]            # a composition cannot be set by one number
    example = f" (예: `{settable[0]}=350`)" if settable else ""
    lines = ["답변을 값으로 인식하지 못했습니다. 이대로 진행하면 메모로만 기록되고 스키마 값은 바뀌지 않습니다.",
             f"인식하지 못한 입력: {parsed['leftover']!r}"]
    if parsed.get("unknown_ids"):
        lines.append("이번 질문에 없는 ID입니다: " + ", ".join(parsed["unknown_ids"])
                     + "  (ID는 실행마다 달라질 수 있습니다. 위 질문에 나온 ID를 그대로 써 주세요.)")
    if ids:
        lines.append("이번 질문의 ID: " + ", ".join(ids))
    lines.append(f"값을 바꾸려면 `ID=숫자` 형식으로 다시 입력해 주세요{example}. 그대로 진행하려면 `ok`를 입력해 주세요.")
    return "\n".join(lines)


def _apply_edc_answer(session: Dict[str, Any], answer: str, schema_path: Optional[str] = None) -> Dict[str, Any]:
    """Parse the reply (`ok` or `ID=value; ...`), write the changed schema back (a copy of the
    original is kept as *.pre_review.yaml), log everything.  Out-of-range or impossible values only
    produce warnings -- the user's number is used."""
    import edc_policy as ep
    state, config = session["state"], session["config"]
    parsed = ep.parse_review_answer(answer, known_ids=_asked_edc_ids(session))
    result: Dict[str, Any] = {"reply_was_ok": parsed["ok"], "unparsed_text": parsed["leftover"],
                              "applied": [], "ignored": [], "warnings": []}
    for uid in parsed.get("unknown_ids") or []:        # typed ids that are not among the asked items
        result["ignored"].append({"id": uid, "reason": "이번 질문에 없는 ID"})
    if parsed["overrides"]:
        out_path = Path(schema_path or config["out_schema_path"])
        schema_obj = state.get("schema_obj") or load_yaml(str(out_path))
        dump_yaml(schema_obj, str(out_path.with_name(out_path.stem + ".pre_review.yaml")))
        result.update(ep.apply_overrides(schema_obj, parsed["overrides"], state["edc"]["classified"]))
        dump_yaml(schema_obj, str(out_path))
        state["schema_obj"] = schema_obj
    _write_edc_artifacts(session, review=result)
    return result


def edc_review_after_completion(session: Dict[str, Any], human_answer: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """PART1_EDC_POLICY on: the usual review is skipped when the draft was incomplete (assess ->
    needs_completion -> complete_and_validate), yet the completion step is exactly where assumptions
    get filled in.  So the same policy question is asked on the COMPLETED schema (Schema_validated.yaml,
    the file Part2 reads) and the reply is written into that file.  None = nothing to ask / already
    reviewed.  Raises HumanInputRequired like review_schema_with_human."""
    if not _edc_enabled(session):
        return None
    state, config = session["state"], session["config"]
    if config.get("part1_hitl_mode") != "chat" or state.get("human_review_decision"):
        return None
    path = Path(config["final_schema_path"])
    if not path.exists():
        return None
    import edc_policy as ep
    schema_obj = load_yaml(str(path))
    res = ep.policy_review_items(schema_obj, load_text(config["paper_text_path"], max_chars=2_000_000))
    state["edc"] = {"classified": res["classified"], "selected": res["selected"]}
    state["schema_obj"] = schema_obj
    _write_edc_artifacts(session, review=None)
    asked_ids = [r["id"] for r in res["selected"]["asked"]]
    if not asked_ids:
        return None
    if human_answer is None:
        raise HumanInputRequired({
            "type": "schema_review",
            "question": ep.format_question(res["selected"], will_apply=True),
            "context": {"human_review_items": res["items"], "edc_policy": True, "after_completion": True},
        })
    notes = str(human_answer)
    if not state.get("edc_reask_done"):
        probe = ep_parse(notes, state)
        if probe["leftover"] and not probe["ok"] and not probe["overrides"]:
            state["edc_reask_done"] = True
            raise HumanInputRequired({
                "type": "schema_review",
                "question": _edc_unrecognised_message(probe, asked_ids),
                "context": {"edc_policy": True, "retry": True, "after_completion": True},
            })
    review = _apply_edc_answer(session, notes, schema_path=str(path))
    state["human_review_decision"] = "approve"
    _session_write_status(session, {
        "human_review_required": False,
        "human_review_decision": "approve",
        "human_review_notes": notes,
        "edc_review": review,
        "edc_review_after_completion": True,
        "final_output_schema_path": config["final_schema_path"],
    })
    return review


def _schema_node_by_path(schema_obj: Any, path: str) -> Any:
    """Follow 'flowsheet_schema.blocks[1].specs.T_C'-style paths; None if anything is missing."""
    cur = schema_obj
    for part in path.split("."):
        m = re.match(r"^([^\[\]]+)((?:\[\d+\])*)$", part)
        if not m:
            return None
        key, idxs = m.group(1), re.findall(r"\[(\d+)\]", m.group(2))
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
        for i in idxs:
            if not isinstance(cur, list) or int(i) >= len(cur):
                return None
            cur = cur[int(i)]
    return cur


def _describe_review_item(schema_obj: Any, item: Dict[str, Any]) -> str:
    """One readable line (plus the model's own rationale / evidence when it left one)."""
    path = str(item.get("field_path") or "")
    label = path
    m = re.match(r"^flowsheet_schema\.blocks\[(\d+)\]\.specs\.(.+)$", path)
    if m:
        blk = _schema_node_by_path(schema_obj, f"flowsheet_schema.blocks[{m.group(1)}]")
        if isinstance(blk, dict):
            kind = f"{blk.get('type')}" + (f", {blk.get('name')}" if blk.get("name") else "")
            label = f"{blk.get('id')} ({kind}) · {m.group(2)}"
    else:
        m = re.match(r"^io\.feeds\[(\d+)\]\.specs\.(.+)$", path)
        if m:
            feed = _schema_node_by_path(schema_obj, f"io.feeds[{m.group(1)}]")
            if isinstance(feed, dict):
                name = f", {feed.get('name')}" if feed.get("name") else ""
                label = f"피드 {feed.get('stream')}{name} · {m.group(2)}"
    conf = item.get("confidence")
    line = f"{label} = {item.get('value')}  [{item.get('source')}, 신뢰도 {conf if conf is not None else '없음'}]"
    node = _schema_node_by_path(schema_obj, path)
    if isinstance(node, dict):
        why = str(node.get("rationale") or "").strip()
        ev = node.get("evidence")
        quote = str(ev.get("quote") if isinstance(ev, dict) else (ev or "")).strip()
        if why:
            line += "\n     모델의 설명: " + why[:200] + ("…" if len(why) > 200 else "")
        if quote:
            line += '\n     인용: "' + quote[:140] + ("…" if len(quote) > 140 else "") + '"'
    return line


def format_schema_review_question(schema_obj: Any, items: List[Dict[str, Any]]) -> str:
    """Chat question for the schema review.  The selection of items is NOT changed here; this only
    makes the question say what is being approved.  Falls back to the legacy text on any error."""
    try:
        lines = [f"스키마에서 신뢰도가 낮은 값 {len(items)}개를 확인해 주세요."]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {_describe_review_item(schema_obj, it)}")
        lines.append("이 버전에서는 답변이 값을 바꾸지 않고 메모로만 기록됩니다. "
                     "그대로 진행하려면 `approve`를, 메모를 남기려면 내용을 입력해 주세요.")
        return "\n".join(lines)
    except Exception:
        return _LEGACY_REVIEW_QUESTION.format(n=len(items))


def review_schema_with_human(session: Dict[str, Any], human_answer: Optional[str] = None) -> Dict[str, Any]:
    state = session["state"]
    config = session["config"]
    hitl_mode = config["part1_hitl_mode"]
    items = (state.get("schema_assessment") or {}).get("human_review_items", [])

    if hitl_mode == "bypass":
        action = "approve"
        notes = "Auto-approved because PART1_HITL_MODE=bypass (human review disabled)."
        state["human_review_decision"] = action
        _session_write_status(session, {
            "ok": True,
            "stage": "human_schema_review_bypassed",
            "human_review_required": False,
            "human_review_bypassed": True,
            "human_review_decision": action,
            "human_review_notes": notes,
            "final_output_schema_path": config["out_schema_path"],
            "final_output_kind": "proto_human_approved",
            "final_output_ready": True,
            "final_missing_count": 0,
        })
        return {"decision": action, "bypassed": True, "n_items": len(items)}

    if hitl_mode == "chat":
        edc_on = _edc_enabled(session) and bool((state.get("edc") or {}).get("selected"))
        if human_answer is None:
            if edc_on:
                import edc_policy as ep
                raise HumanInputRequired({
                    "type": "schema_review",
                    "question": ep.format_question(state["edc"]["selected"], will_apply=True),
                    "context": {"human_review_items": items, "edc_policy": True},
                })
            try:
                schema_for_msg = state.get("schema_obj") or load_yaml(config["out_schema_path"])
            except Exception:
                schema_for_msg = {}
            raise HumanInputRequired({
                "type": "schema_review",
                "question": format_schema_review_question(schema_for_msg, items),
                "context": {"human_review_items": items},
            })
        notes = str(human_answer)
        if edc_on and not state.get("edc_reask_done"):
            probe = ep_parse(notes, state)
            if probe["leftover"] and not probe["ok"] and not probe["overrides"]:
                # one chance to retype: the reply looked like a value change but was not understood
                state["edc_reask_done"] = True
                raise HumanInputRequired({
                    "type": "schema_review",
                    "question": _edc_unrecognised_message(probe, [r["id"] for r in state["edc"]["selected"]["asked"]]),
                    "context": {"edc_policy": True, "retry": True},
                })
        edc_review = _apply_edc_answer(session, notes) if edc_on else None
        action = "approve"  # the schema is now final either way; with the EDC policy the reply may have changed values first
        state["human_review_decision"] = action
        _session_write_status(session, {
            "ok": True,
            "stage": "human_schema_review_answered_chat",
            "human_review_required": False,
            "human_review_bypassed": False,
            "human_review_decision": action,
            "human_review_notes": notes,
            "final_output_schema_path": config["out_schema_path"],
            "final_output_kind": "proto_human_approved",
            "final_output_ready": True,
            "final_missing_count": 0,
            **({"edc_review": edc_review} if edc_review is not None else {}),
        })
        out = {"decision": action, "bypassed": False, "human_notes": notes, "n_items": len(items)}
        if edc_review is not None:
            out["edc_review"] = edc_review
        return out

    state["human_review_decision"] = "approve"
    _session_write_status(session, {
        "stage": "human_schema_review_fallback",
        "human_review_decision": "approve",
        "final_output_schema_path": config["out_schema_path"],
        "final_output_kind": "proto_human_approved",
        "final_output_ready": True,
    })
    return {"decision": "approve", "bypassed": False, "note": f"unrecognized PART1_HITL_MODE={hitl_mode!r}"}


def complete_and_validate(session: Dict[str, Any]) -> Dict[str, Any]:
    state = session["state"]
    config = session["config"]
    validated_obj, report_obj = part1_validate_and_complete(
        user_input=state.get("effective_user_input") or state.get("user_input"),
        paper_text_path=config["paper_text_path"],
        schema_layer_path=config["schema_layer_path"],
        proto_schema_path=config["out_schema_path"],
        out_validated_schema_path=config["final_schema_path"],
        out_report_json_path=config["validation_report_path"],
        model=session["part1_completion_model"],
        temperature=session["llm_temperature"],
        max_iters=3,
        use_image=True,
        image_model=session["part1_image_model"],
        paper_image_dir=config["paper_image_dir"],
        paper_image_glob=config["paper_image_glob"],
        paper_image_top_k=config["paper_image_top_k"],
    )
    ok = bool((report_obj or {}).get("ok", False))
    _session_write_status(session, {
        "ok": ok,
        "stage": "completion_done",
        "agent_decision": (state.get("schema_assessment") or {}).get("decision"),
        "schema_output_path": config["out_schema_path"],
        "final_schema_path": config["final_schema_path"],
        "final_output_schema_path": config["final_schema_path"],
        "final_output_kind": "validated",
        "final_output_ready": ok,
        "validation_report_path": config["validation_report_path"],
        "final_missing_count": len((report_obj or {}).get("final_missing", [])),
        "schema_quality": (report_obj or {}).get("schema_quality"),
        "schema_assessment": state.get("schema_assessment"),
    })
    return {"ok": ok, "final_schema_path": config["final_schema_path"]}
