"""Phase D orchestrator (gradio-chatbot-architecture-plan item D).

Replaces runner_v1.ipynb's nbconvert-subprocess batching (Part1/2/3 each run
as a separate `ExecutePreprocessor` pass, with human interrupts requiring a
separate manual re-run) with one long-lived `PipelineSession` object that
runs all three parts *in-process*, turn by turn, through a single
`handle_user_message(text)` entry point. This is what makes a real chat UI
(Phase F's Gradio app) possible: a human answer is just the next chat
message, handled by resuming the paused `run_tool_calling_agent` loop
(agent_loop_utils.HumanInputRequired / resume_tool_calling_agent) instead of
re-invoking a whole notebook.

Threading contract (documented in part2_stage_functions.py's own module
docstring, repeated here because it applies just as much at this layer):
every Aspen COM call must happen on the same OS thread that first created
`doc`. This module does not hop threads itself -- whatever drives a given
PipelineSession (a REPL, a test, Gradio's callback) must always call
`handle_user_message()` for that session from the same thread, for the
session's whole lifetime. This is a solo local tool (see
gradio-chatbot-architecture-plan's confirmed architecture decision #1), so a
single calling thread is sufficient -- no thread pool, no session-affinity
routing needed.

HITL mode: this orchestrator always runs each part with `*_HITL_MODE=chat`
(never the notebooks' batch-mode "bypass" default), since the whole point of
Phase D is to let real human turns answer these questions through the chat
loop instead of auto-approving them.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
for _path in [_HERE, _HERE.parent]:
    _p = str(_path)
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The notebooks never load .env themselves -- they worked because VS Code's
# Jupyter extension auto-injects the workspace .env into the kernel's
# environment. A plain terminal/script run gets no such help and instead
# inherits whatever OPENAI_API_KEY happens to be set at the OS level, which
# can be a stale leftover from long ago (bit this module's own first manual
# test, 2026-09-03: a valid-looking but out-of-credit key silently shadowed
# the current one in .env). load_dotenv(..., override=True) makes this
# module's behavior the same everywhere it runs (Jupyter, a terminal, a
# future Gradio process) instead of depending on which launcher happened to
# populate the environment correctly.
from dotenv import load_dotenv

load_dotenv(_HERE.parent.parent / ".env", override=True)

from langchain_openai import ChatOpenAI
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from agent_loop_utils import run_tool_calling_agent, resume_tool_calling_agent
from experiment_logging import record_event
import part1_stage_functions as p1
import part2_stage_functions as p2
import part3_stage_functions as p3

# 2608 수정 holds the shared data (jinny_DB, Schema_v5.yaml, aspen_code_v3.yaml,
# ...) that every phaseN folder under 2609 수정 reads from without copying --
# see [[project-folder-layout]]. Same BASE_DIR runner_v1.ipynb uses.
BASE_DIR = Path(r"C:\Users\USER\Desktop\3. LLM (2601)\2608 수정")


# ===========================================================================
# System prompts (verbatim from part{1,2,3}_v1.ipynb's agent-run cells)
# ===========================================================================
PART1_SYSTEM_PROMPT = """
You are the Part1 orchestrator for an Aspen Plus schema-drafting pipeline. You
have tools for each step; call them in a sensible order and stop once the
schema is drafted and either judged "enough" or completed/reviewed.

Typical flow:
1. fetch_paper_context - get paper text/image hints/schema rules.
2. assess_input_readiness - check if the user's request is specific enough.
   If it returns needs_human_input, call request_human_clarification next.
3. draft_schema - generate Schema_proto.yaml.
4. assess_schema_quality - check the draft. If "enough", you are done.
   If "needs_human_review", call review_schema_with_human next.
   If "needs_completion" (or review says complete), call complete_and_validate.
5. Stop once assess_schema_quality says "enough", or after
   complete_and_validate / review_schema_with_human has produced a final
   result. Do not call the same tool more than once unless a prior step
   changed its input (e.g. re-running assess_schema_quality after drafting
   again is fine; re-running draft_schema without new information is not).

Once done, reply with a short final summary of what was produced (schema
path, decision) and do not call any more tools.
""".strip()

PART2_SYSTEM_PROMPT = """
You are the Part2 orchestrator for an Aspen Plus COM model repair pipeline.
You have tools for each step; call them in a sensible order and stop once
write_result has run.

Typical flow:
1. prepare_part2, then fetch_part2_context.
2. run_repair_phase for "property_method", then "connectivity", then "feeds",
   then "block_specs" -- in that order, each once.
3. final_validate, then assess_final.
4. If assess_final returned human_review_required=true, call
   review_with_human. If that returns human_repair_requested=true, call
   repair_with_human_feedback then final_validate again, then assess_final
   again.
5. Call write_result once assess_final (or review_with_human) has settled the
   outcome. Do not call write_result more than once, and do not repeat a
   repair phase that already ran successfully in this pass.

Once done, reply with a short final summary (ok, final output kind/path) and
do not call any more tools.
""".strip()

PART3_SYSTEM_PROMPT = """
You are the Part3 orchestrator for an Aspen Plus HIS diagnosis/patch pipeline.
You have tools for each step; call them in a sensible order and stop once
write_result has run.

Typical flow:
1. run_aspen_check (skip only if skip_by_part2_result is already true), then
   parse_his.
2. If parse_his's early_exit_after_parse is true, skip straight to step 4 --
   do not start a repair cycle.
3. Otherwise, run repair cycles: call begin_repair_attempt, check "allowed".
   - If allowed=true: search_error_docs, diagnose_issue, plan_patch,
     generate_patch_code, apply_patch, rerun_and_check, in that order.
     If rerun_and_check comes back clean (no severe/errors), stop repairing
     and go to step 4. If issues remain, call begin_repair_attempt again to
     open another cycle (still bounded by the same 3-cycle budget).
   - If allowed=false: the repair budget (3 cycles) is exhausted. Stop
     repairing immediately -- do not call search_error_docs/diagnose_issue/
     etc. again -- and go to step 4 with whatever the last rerun_and_check
     reported.
4. assess_result. If needs_human_review=true, call review_with_human.
5. Call write_result once assess_result (or review_with_human) has settled
   the outcome. Do not call write_result more than once.

Never call search_error_docs, diagnose_issue, plan_patch, generate_patch_code,
apply_patch, or rerun_and_check without an immediately preceding
begin_repair_attempt call in the same cycle that returned allowed=true.

Once done, reply with a short final summary (ok, final bkp path, repair
cycles used) and do not call any more tools.
""".strip()


# ===========================================================================
# Tool factories -- one call per part per PipelineSession, closing over that
# part's session dict. Bodies copied verbatim from the notebooks' tool cells
# (part1_v1.ipynb cell6, part2_v1.ipynb cell4, part3_v1.ipynb cell7); only the
# module-global PART{1,2,3}_SESSION references became closure params.
# ===========================================================================

def _build_part1_tools(session: Dict[str, Any]) -> List[Any]:
    @tool
    def fetch_paper_context() -> str:
        """Fetch the paper text excerpt, image hints, and schema rules via the MCP
        adapters and cache them for later steps. Call this first, before assessing
        the user's input or drafting a schema."""
        return json.dumps(p1.fetch_paper_context(session), ensure_ascii=False)

    @tool
    def assess_input_readiness() -> str:
        """Check whether the current user input is specific enough to start schema
        drafting. Returns a JSON object with "decision" ("ready_for_draft" or
        "needs_human_input"), "question" (if clarification is needed), and
        "candidate_targets". Call fetch_paper_context first so this has paper
        context to reason over. If decision is needs_human_input, call
        request_human_clarification next before drafting."""
        return json.dumps(p1.assess_input_readiness(session), ensure_ascii=False)

    @tool
    def request_human_clarification(question: str, human_answer: Optional[str] = None) -> str:
        """Ask a human to clarify the schema-drafting target when
        assess_input_readiness returned needs_human_input. Under
        PART1_HITL_MODE=chat this pauses and asks a live human `question`.
        Returns the answer that will be used. Never pass human_answer
        yourself -- leave it unset; it is filled in automatically once a real
        human has answered."""
        return json.dumps(
            p1.request_human_clarification(session, question, human_answer=human_answer),
            ensure_ascii=False,
        )

    @tool
    def draft_schema() -> str:
        """Generate the Part1 schema draft (Schema_proto.yaml) from the current
        effective user input and paper context. Requires assess_input_readiness to
        have run (and request_human_clarification if it asked for one) so
        effective_user_input is set."""
        return json.dumps(p1.draft_schema(session), ensure_ascii=False)

    @tool
    def assess_schema_quality() -> str:
        """Check the drafted schema for structural issues, missing execution-
        critical items, and low-confidence assumptions that need human review.
        Returns a JSON object with "decision" ("enough", "needs_completion", or
        "needs_human_review") and "reason". Call draft_schema first."""
        return json.dumps(p1.assess_schema_quality(session), ensure_ascii=False)

    @tool
    def review_schema_with_human(human_answer: Optional[str] = None) -> str:
        """Route the schema for human review when assess_schema_quality returned
        needs_human_review. Under PART1_HITL_MODE=chat this pauses and shows a
        live human the low-confidence items, then records their notes -- note
        that by default Part1 does not support a schema "revise" outcome, so
        the draft is still used as the final schema either way; the human's
        notes are only recorded for the record. When PART1_EDC_POLICY=1 the
        human may instead reply with `ID=value` pairs (e.g. B2.T_C=350); those
        values are written into the schema (out-of-range values only produce
        warnings) and the result is returned under "edc_review". Only call this when the last
        assess_schema_quality decision was needs_human_review. Never pass
        human_answer yourself -- leave it unset; it is filled in
        automatically once a real human has answered."""
        return json.dumps(
            p1.review_schema_with_human(session, human_answer=human_answer),
            ensure_ascii=False,
        )

    @tool
    def complete_and_validate(human_answer: Optional[str] = None) -> str:
        """Run the final schema completion/validation pass and write
        Schema_validated.yaml + the validation report. Call this when
        assess_schema_quality decided needs_completion, or after
        review_schema_with_human decided "complete". This is normally the last
        step -- once it returns, Part1's work is done. When PART1_EDC_POLICY=1
        and the schema has values the paper does not settle, this tool may pause
        to ask the human about them after the completion pass (the reply is
        written into Schema_validated.yaml). Never pass human_answer yourself --
        leave it unset; it is filled in automatically once a real human has
        answered."""
        state = session["state"]
        if "completion_result" not in state:          # a resume after a question must not redo the (paid) pass
            state["completion_result"] = p1.complete_and_validate(session)
        result = dict(state["completion_result"])
        review = p1.edc_review_after_completion(session, human_answer=human_answer)
        if review is not None:
            result["edc_review"] = review
        return json.dumps(result, ensure_ascii=False)

    return [
        fetch_paper_context,
        assess_input_readiness,
        request_human_clarification,
        draft_schema,
        assess_schema_quality,
        review_schema_with_human,
        complete_and_validate,
    ]


def _build_part2_tools(session: Dict[str, Any]) -> List[Any]:
    def _merge(result: Dict[str, Any]) -> Dict[str, Any]:
        session.update(result)
        return result

    def _ensure_com_initialized() -> None:
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass

    @tool
    def prepare_part2() -> str:
        """Mark Part2 paths/globals as ready. Call this first."""
        result = p2.prepare_node(session)
        _merge(result)
        return json.dumps({"stage": result.get("stage")}, ensure_ascii=False)

    @tool
    def fetch_part2_context() -> str:
        """Fetch Part2's run phases, schema phase contexts, and COM catalog via the
        MCP adapters. Call this after prepare_part2 and before running repair
        phases."""
        result = p2.mcp_context_node(session)
        _merge(result)
        return json.dumps({"stage": result.get("stage"), "run_phases": result.get("run_phases")}, ensure_ascii=False)

    @tool
    def run_repair_phase(phase: str) -> str:
        """Run schema-driven repair + validation for one Aspen model phase and
        return {"ok", "stage"}. Valid phase values: "property_method",
        "connectivity", "feeds", "block_specs". Run them in that order (each phase
        builds on the previous one -- e.g. connectivity assumes the property
        method is already set), and don't call it twice for the same phase in one
        pass. If a phase reports ok=False, later phases will be skipped
        automatically until final_validate/assess_final decide what to do next."""
        _ensure_com_initialized()
        phase = (phase or "").strip()
        if phase not in p2.RUN_PHASES:
            return json.dumps({"ok": False, "error": f"unknown phase: {phase!r}, expected one of {p2.RUN_PHASES}"}, ensure_ascii=False)
        result = p2._run_single_phase_repair_node(session, phase)
        _merge(result)
        return json.dumps({"ok": result.get("ok"), "stage": result.get("stage")}, ensure_ascii=False)

    @tool
    def final_validate() -> str:
        """Run final full-model validation (and, if any phase was repaired, save +
        reopen the Aspen document to produce a .his file Part3 can consume). Call
        this after running the repair phases you intend to run."""
        _ensure_com_initialized()
        result = p2.final_validate_node(session)
        _merge(result)
        return json.dumps({
            "stage": result.get("stage"),
            "final_output_kind": (result.get("part2_loop_status") or {}).get("final_output_kind"),
        }, ensure_ascii=False)

    @tool
    def assess_final() -> str:
        """Assess the final validation result and decide whether human review is
        needed. Call this right after final_validate."""
        result = p2.assess_final_node(session)
        _merge(result)
        return json.dumps({
            "stage": result.get("stage"),
            "human_review_required": result.get("human_review_required"),
        }, ensure_ascii=False)

    @tool
    def review_with_human(human_answer: Optional[Any] = None) -> str:
        """Route the final result for human review when assess_final said
        human_review_required=true. Under PART2_HITL_MODE=chat this pauses
        and asks a live human to approve/complete/revise/stop. If
        human_repair_requested comes back true, just call
        repair_with_human_feedback next -- it already reads the human's notes
        straight from session state, no need to pass them yourself. Only
        call this when the last assess_final result had
        human_review_required=true. Never pass human_answer yourself -- leave
        it unset; it is filled in automatically once a real human has
        answered."""
        result = p2.human_review_node(session, human_answer=human_answer)
        _merge(result)
        return json.dumps({
            "stage": result.get("stage"),
            "human_review_decision": result.get("human_review_decision"),
            "human_repair_requested": result.get("human_repair_requested"),
            "human_notes": (result.get("human_answer") or {}).get("notes", ""),
        }, ensure_ascii=False)

    @tool
    def repair_with_human_feedback() -> str:
        """Apply a repair based on the human reviewer's revise notes, then you
        should call final_validate again. Only call this when review_with_human
        returned human_repair_requested=true."""
        _ensure_com_initialized()
        result = p2.human_feedback_repair_node(session)
        _merge(result)
        return json.dumps({"stage": result.get("stage"), "ok": result.get("ok")}, ensure_ascii=False)

    @tool
    def write_result() -> str:
        """Write the final part2_result.json and close the Aspen document. This is
        always the last step -- call it once assess_final said
        human_review_required=false, or after review_with_human /
        repair_with_human_feedback settled the outcome."""
        _ensure_com_initialized()
        result = p2.write_result_node(session)
        _merge(result)
        return json.dumps({
            "stage": result.get("stage"),
            "final_output_ready": result.get("final_output_ready"),
            "part2_result_path": result.get("part2_result_path"),
        }, ensure_ascii=False)

    return [
        prepare_part2,
        fetch_part2_context,
        run_repair_phase,
        final_validate,
        assess_final,
        review_with_human,
        repair_with_human_feedback,
        write_result,
    ]


def _build_part3_tools(
    session: Dict[str, Any],
    last_assessment: Dict[str, Any],
    human_review: Dict[str, Any],
    hitl_mode: str,
) -> List[Any]:
    @tool
    def run_aspen_check() -> str:
        """Reopen the Part2 .bkp once as a sanity check and resolve the .his path to
        analyze. Call this first, UNLESS skip_by_part2_result is already true (in
        which case skip straight to parse_his)."""
        p3.run_initial_aspen(session)
        return json.dumps({"his_path": session["state"].get("his_path")}, ensure_ascii=False)

    @tool
    def parse_his() -> str:
        """Parse the .his file (or reuse Part2's already-known counts when
        skip_by_part2_result is true) into severe/error/warning counts. Call this
        right after run_aspen_check (or first, if skip_by_part2_result is
        already true). If early_exit_after_parse is true (severe=0 and
        errors=0), the model is already clean: skip straight to assess_result,
        do not start any repair cycle."""
        p3.parse_his(session)
        state = session["state"]
        return json.dumps({
            "parse_counts": state.get("parse_counts"),
            "early_exit_after_parse": state.get("early_exit_after_parse"),
        }, ensure_ascii=False)

    @tool
    def begin_repair_attempt() -> str:
        """Open one repair cycle: search_error_docs -> diagnose_issue -> plan_patch
        -> generate_patch_code -> apply_patch -> rerun_and_check, in that order.
        You may run AT MOST 3 repair cycles total in one Part3 pass. Call this
        tool at the start of EVERY cycle (including the first) and check
        "allowed" in the result before doing anything else in that cycle. If
        allowed=false, the repair budget is exhausted -- stop repairing
        immediately and call assess_result / write_result with whatever the last
        rerun_and_check reported, even if issues remain."""
        return json.dumps(p3.begin_repair_attempt(session), ensure_ascii=False)

    @tool
    def search_error_docs() -> str:
        """Web-search (Tavily, if configured) for context on the current top issue
        block. First step inside a repair cycle, right after begin_repair_attempt
        allowed it."""
        p3.run_rag(session)
        state = session["state"]
        return json.dumps({"n_queries": len(state.get("queries", [])), "n_top_docs": len(state.get("top_docs", []))}, ensure_ascii=False)

    @tool
    def diagnose_issue() -> str:
        """LLM diagnosis of the current top issue block, using the parsed issues and
        search_error_docs results. Call after search_error_docs."""
        p3.run_diagnose(session)
        diag = session["state"].get("diagnosis") or {}
        return json.dumps({"symptom_tags": diag.get("symptom_tags"), "summary": diag.get("summary")}, ensure_ascii=False)

    @tool
    def plan_patch(human_feedback: str = "") -> str:
        """LLM patch plan (whitelisted function+args only) built from the current
        diagnosis. Pass human_feedback only when this cycle was triggered by a
        human "revise" request; leave it empty otherwise. Call after
        diagnose_issue."""
        p3.run_patch_plan(session, human_feedback=human_feedback)
        plan = session["state"].get("patch_plan") or {}
        return json.dumps({"intent": plan.get("intent"), "n_patches": len(plan.get("patches", []))}, ensure_ascii=False)

    @tool
    def generate_patch_code() -> str:
        """Turn the current patch_plan into Python code that calls whitelisted Aspen
        COM helper functions. Call after plan_patch."""
        p3.run_patch_codegen(session)
        return json.dumps({"patch_code_path": session["state"].get("patch_code_path")}, ensure_ascii=False)

    @tool
    def apply_patch() -> str:
        """Open the current .bkp and execute the generated patch code against it,
        statement by statement (fail-soft: one bad statement doesn't abort the
        rest). Keeps the Aspen document open in this thread for rerun_and_check.
        Call after generate_patch_code."""
        p3.run_patch_apply(session)
        r = session["state"].get("patch_apply_result") or {}
        return json.dumps({"status": r.get("status"), "ok": r.get("ok")}, ensure_ascii=False)

    @tool
    def rerun_and_check() -> str:
        """Run the patched Aspen document, save it, and parse the resulting .his for
        severe/error/warning counts. Closes the document. Always the last step in
        a repair cycle, right after apply_patch. If the result shows severe=0 and
        errors=0, the cycle succeeded: stop repairing and go to assess_result.
        Otherwise, either start another begin_repair_attempt cycle (if the budget
        allows) or, once the budget is exhausted, go straight to assess_result."""
        p3.run_rerun_and_parse(session)
        summary = session["state"].get("rerun_summary") or {}
        return json.dumps({"status": summary.get("status"), "counts": summary.get("counts")}, ensure_ascii=False)

    @tool
    def assess_result() -> str:
        """Assess whether Part3's current state needs human review. Call this once
        issues are resolved (parse_his showed early_exit, or a repair cycle's
        rerun_and_check came back clean) OR the repair budget is exhausted."""
        assessment = p3.assess_final(session)
        last_assessment.clear()
        last_assessment.update(assessment)
        return json.dumps({"success": assessment.get("success"), "needs_human_review": assessment.get("needs_human_review")}, ensure_ascii=False)

    @tool
    def review_with_human(human_answer: Any = None) -> str:
        """Route the final result for human review when assess_result said
        needs_human_review=true. Under PART3_HITL_MODE=chat this pauses and
        asks a live human to approve/complete/revise/stop. If
        revise_requested comes back true, pass this call's own "human_notes"
        into plan_patch(human_feedback=...) on the next repair cycle. Only
        call this when the last assess_result had needs_human_review=true.
        Never pass human_answer yourself -- leave it unset; it is filled in
        automatically once a real human has answered."""
        review = p3.review_with_human(session, last_assessment, hitl_mode=hitl_mode, human_answer=human_answer)
        human_review.clear()
        human_review.update(review)
        return json.dumps({
            "decision": review.get("human_review_decision"),
            "revise_requested": review.get("human_revise_requested"),
            "human_notes": (review.get("human_answer") or {}).get("notes", ""),
        }, ensure_ascii=False)

    @tool
    def write_result() -> str:
        """Write the final part3_result.json. Always the last step -- call it once
        assess_result said needs_human_review=false, or after review_with_human
        settled the outcome."""
        path = p3.write_result(
            session,
            last_assessment or {},
            human_review_required=bool(last_assessment.get("needs_human_review")) if last_assessment else False,
            human_answer=human_review.get("human_answer"),
            human_review_decision=human_review.get("human_review_decision", ""),
            human_review_bypassed=human_review.get("human_review_bypassed", False),
            human_stop_requested=human_review.get("human_stop_requested", False),
        )
        return json.dumps({"part3_result_path": path, "ok": session["result"].get("ok")}, ensure_ascii=False)

    return [
        run_aspen_check,
        parse_his,
        begin_repair_attempt,
        search_error_docs,
        diagnose_issue,
        plan_patch,
        generate_patch_code,
        apply_patch,
        rerun_and_check,
        assess_result,
        review_with_human,
        write_result,
    ]


def _load_json(path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


# ===========================================================================
# Phase E: narration layer.
#
# experiment_logging.record_event() turned out to be too sparse to build a
# narration off of -- across all of part1/2/3_stage_functions.py it is only
# ever called once per part (build_session()'s own "part{1,2,3}_started"
# event). The far richer, already-available signal is the tool-call
# sequence itself: this orchestrator already knows exactly which of the
# fixed ~27 tool names ran, in order, every time run_tool_calling_agent /
# resume_tool_calling_agent returns. So narration here works directly off
# that message list instead of any logging system -- one static
# (stage, tool_name) -> friendly-Korean-line lookup table, no new logging.
#
# This does not touch the raw print() statements already in
# part{1,2,3}_stage_functions.py / agent_loop_utils.py -- those keep printing
# to the terminal exactly as before (useful for debugging), this layer only
# controls what PipelineSession.handle_user_message() puts in its "message"
# field for a chat UI to display.
# ===========================================================================

_TOOL_NARRATION: Dict[tuple, str] = {
    # Part1
    ("part1", "fetch_paper_context"): "논문 내용과 그림을 확인하고 있습니다.",
    ("part1", "assess_input_readiness"): "요청이 충분히 구체적인지 검토하고 있습니다.",
    ("part1", "request_human_clarification"): "목표 공정을 명확히 하기 위해 질문을 준비하고 있습니다.",
    ("part1", "draft_schema"): "Aspen 스키마 초안을 작성하고 있습니다.",
    ("part1", "assess_schema_quality"): "작성된 스키마의 품질을 점검하고 있습니다.",
    ("part1", "review_schema_with_human"): "스키마를 사람 검토용으로 넘기고 있습니다.",
    ("part1", "complete_and_validate"): "스키마를 최종 보완하고 검증하고 있습니다.",
    # Part2
    ("part2", "prepare_part2"): "Part2 작업 환경을 준비하고 있습니다.",
    ("part2", "fetch_part2_context"): "Aspen COM 함수 목록과 검증 규칙을 불러오고 있습니다.",
    ("part2", "final_validate"): "전체 모델을 최종 검증하고 있습니다.",
    ("part2", "assess_final"): "검증 결과를 평가해서 다음 단계를 결정하고 있습니다.",
    ("part2", "review_with_human"): "결과를 사람 검토용으로 넘기고 있습니다.",
    ("part2", "repair_with_human_feedback"): "사람 피드백을 반영해 모델을 다시 손보고 있습니다.",
    ("part2", "write_result"): "최종 결과를 저장하고 있습니다.",
    # Part3
    ("part3", "run_aspen_check"): "Aspen 모델을 다시 열어 상태를 확인하고 있습니다.",
    ("part3", "parse_his"): "시뮬레이션 로그를 분석해 에러/경고를 집계하고 있습니다.",
    ("part3", "begin_repair_attempt"): "새 수정 사이클을 시작하고 있습니다.",
    ("part3", "search_error_docs"): "에러 원인을 웹에서 검색하고 있습니다.",
    ("part3", "diagnose_issue"): "문제 원인을 진단하고 있습니다.",
    ("part3", "plan_patch"): "수정 계획을 세우고 있습니다.",
    ("part3", "generate_patch_code"): "수정 코드를 생성하고 있습니다.",
    ("part3", "apply_patch"): "생성된 코드를 Aspen 모델에 적용하고 있습니다.",
    ("part3", "rerun_and_check"): "모델을 재실행해서 결과를 확인하고 있습니다.",
    ("part3", "assess_result"): "최종 결과를 평가하고 있습니다.",
    ("part3", "review_with_human"): "결과를 사람 검토용으로 넘기고 있습니다.",
    ("part3", "write_result"): "최종 결과를 저장하고 있습니다.",
}


def _narrate_tool_call(stage: str, name: str, args: Dict[str, Any]) -> str:
    if stage == "part2" and name == "run_repair_phase":
        phase = args.get("phase") or "?"
        return f"'{phase}' 단계를 구축/보정하고 있습니다."
    return _TOOL_NARRATION.get((stage, name), f"{name} 실행 중입니다.")


def _extract_narration_lines(stage: str, new_messages: List[Any]) -> List[str]:
    """Walk only the messages appended during ONE handle_user_message() call
    (the caller slices `new_messages` to that range -- see call sites) and
    turn each tool call the model made into one friendly line, in order.
    Ignores the final plain AIMessage with no tool_calls (the model's closing
    summary) -- that becomes the caller's own "message" text instead."""
    lines = []
    for m in new_messages:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for call in m.tool_calls:
                name = call.get("name") or ""
                args = call.get("args") or {}
                lines.append(_narrate_tool_call(stage, name, args))
    return lines


def _format_narration_block(lines: List[str]) -> str:
    if not lines:
        return ""
    return "\n".join(f"· {line}" for line in lines) + "\n\n"


# A callback a live UI (Phase F's app.py) passes into
# PipelineSession.handle_user_message() to receive one friendly narration
# line at a time, as it happens, instead of only all-at-once when the whole
# call returns. Called synchronously on whatever thread is running the
# pipeline call -- must be fast and non-raising (e.g. a queue.put()).
OnNarration = Callable[[str], None]


def _make_on_tool_call(stage: str, on_narration: Optional[OnNarration]) -> Optional[Callable[[str, Dict[str, Any]], None]]:
    """Adapts an OnNarration callback into the (name, args) shape
    agent_loop_utils.run_tool_calling_agent's on_tool_call expects, applying
    this module's own (stage, tool_name) -> friendly-line lookup."""
    if on_narration is None:
        return None

    def _on_tool_call(name: str, args: Dict[str, Any]) -> None:
        on_narration(_narrate_tool_call(stage, name, args))

    return _on_tool_call


class PipelineSession:
    """One conversation's worth of Part1->Part2->Part3 state. Call
    `handle_user_message(text)` once per chat turn: the very first call kicks
    off Part1 using `text` as the process request; every call after that
    either answers a pending HITL question (if `self.pending` is set) or is
    ignored with a friendly "nothing pending" reply.

    Not thread-safe across sessions with shared globals -- but see this
    module's docstring: Part2/Part3 touch a thread-affine Aspen COM `doc`,
    so a single session must anyway only ever be driven from one thread.
    """

    def __init__(
        self,
        *,
        base_dir: Path = BASE_DIR,
        paper_id: str = "paper2",
        process_type: str = "ATR",
        mcp_adapter_mode: str = "local_adapter",
        model: str = "gpt-5.5",
        paper_text_path: Optional[Path] = None,
        paper_image_path: Optional[Path] = None,
        paper_image_glob: Optional[str] = None,
        paper_image_top_k: int = 4,
        part1_only: bool = False,
    ) -> None:
        # The four paper_* overrides select a different paper / restrict which figures
        # Part1 sees (e.g. a single flowsheet figure).  All default to the old
        # paper_id-based behaviour, so existing callers are unchanged.
        self.base_dir = Path(base_dir)
        self.part1_only = bool(part1_only)      # experiments: stop once Part1 has written its schema
        self.stage = "part1"
        self.pending: Optional[Dict[str, Any]] = None
        self.part1_session: Optional[Dict[str, Any]] = None
        self.part2_session: Optional[Dict[str, Any]] = None
        self.part3_session: Optional[Dict[str, Any]] = None
        self.part1_status: Dict[str, Any] = {}
        self.part2_result: Dict[str, Any] = {}
        self.part3_result: Dict[str, Any] = {}
        self._part3_last_assessment: Dict[str, Any] = {}
        self._part3_human_review: Dict[str, Any] = {}

        os.environ["MCP_ADAPTER_MODE"] = mcp_adapter_mode

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.runner_dir = _HERE / "run" / f"attempt_chat_{stamp}"
        self.part1_dir = self.runner_dir / "part1"
        self.part2_dir = self.runner_dir / "part2"
        self.part3_dir = self.runner_dir / "part3"
        for d in (self.part1_dir, self.part2_dir, self.part3_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.runner_input_path = self.runner_dir / "runner_input.json"

        llm_config = {"model": model, "temperature": 1}
        self.llm_config = llm_config

        self.part1_input: Dict[str, Any] = {
            "run_mode": "auto",
            "user_input_text": "",  # filled in on the first handle_user_message call
            "paper_text_path": str(paper_text_path) if paper_text_path
            else str(self.base_dir / "jinny_DB" / "parsed" / paper_id / "paper_h2.md"),
            "paper_image_path": str(paper_image_path) if paper_image_path
            else str(self.base_dir / "jinny_DB" / "parsed" / paper_id / "Fig. 2.jpg"),
            "paper_image_dir": "",
            "paper_image_glob": paper_image_glob if paper_image_glob
            else str(self.base_dir / "jinny_DB" / "parsed" / paper_id / "*.jpg"),
            "paper_image_top_k": paper_image_top_k,
            "schema_layer_path": str(self.base_dir / "Schema_v5.yaml"),
            "working_dir": str(self.part1_dir),
            "schema_output_path": str(self.part1_dir / "Schema_proto.yaml"),
            "final_schema_path": str(self.part1_dir / "Schema_validated.yaml"),
            "validation_report_path": str(self.part1_dir / "Schema_validation_completion_report_v2.json"),
            "clarification_answer": "",
            "part1_status_path": str(self.part1_dir / "part1_status.json"),
            "llm_config": llm_config,
        }
        self.part2_input: Dict[str, Any] = {
            "run_mode": "full",
            "db_inp_path": str(self.base_dir / "inp_v1.yaml"),
            "db_com_path": str(self.base_dir / "aspen_code_v3.yaml"),
            "component_db_path": str(self.base_dir / "aspen_components.yaml"),
            "schema_input_path": "",  # filled in from part1's output
            "working_dir": str(self.part2_dir),
            "rules_base_dir": str(self.base_dir),
            "out_inp_path": str(self.part2_dir / "inp_proto.inp"),
            "out_bkp_path": str(self.part2_dir / "inp_proto.bkp"),
            "out_com_skeleton_path": str(self.part2_dir / "com_skeleton.py"),
            "out_com_proto_bkp_path": str(self.part2_dir / "com_proto_repaired.bkp"),
            "out_com_initial_bkp_path": str(self.part2_dir / "com_proto_initial.bkp"),
            "out_com_repaired_bkp_path": str(self.part2_dir / "com_proto_repaired.bkp"),
            "out_com_completion_bkp_path": str(self.part2_dir / "com_completion.bkp"),
            "out_com_completion_path": str(self.part2_dir / "com_completion_iter1.py"),
            "aspen_setup_bat": r"C:\Program Files\AspenTech\Aspen Plus V14.0\Engine\Xeq\aspsetup.bat",
            "part2_result_path": str(self.part2_dir / "part2_result_iter1.json"),
            "repair_max_iter": 3,
            "human_repair_max_rounds": 1,
            "llm_config": llm_config,
        }
        self.part3_input: Dict[str, Any] = {
            "working_dir": str(self.part3_dir),
            "aspen_setup_bat": self.part2_input["aspen_setup_bat"],
            "bkp_path": "",  # filled in from part2's output
            "run_stem": "part3_iter0",
            "max_iter": 3,
            "part3_result_path": str(self.part3_dir / "part3_result.json"),
            "llm_config": llm_config,
        }
        self._write_runner_input()

    # -- shared plumbing ----------------------------------------------------

    def _write_runner_input(self) -> None:
        payload = {
            "llm_config": self.llm_config,
            "part1_input": self.part1_input,
            "part2_input": self.part2_input,
            "part3_input": self.part3_input,
        }
        self.runner_input_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _run_agent(
        self, stage: str, llm, tools, system_prompt, user_prompt, max_steps,
        on_narration: Optional["OnNarration"] = None,
    ) -> Dict[str, Any]:
        on_tool_call = _make_on_tool_call(stage, on_narration)
        return run_tool_calling_agent(
            llm, tools, system_prompt, user_prompt, max_steps=max_steps, on_tool_call=on_tool_call
        )

    # -- Part1 ----------------------------------------------------------

    def _start_part1(self, user_text: str, on_narration: Optional["OnNarration"] = None) -> Dict[str, Any]:
        self.part1_input["user_input_text"] = user_text
        self._write_runner_input()
        os.environ["PART1_HITL_MODE"] = "chat"

        self.part1_session = p1.build_session(
            runner_input_path=str(self.runner_input_path), record_event=record_event
        )
        tools = _build_part1_tools(self.part1_session)
        llm = ChatOpenAI(
            model=self.part1_session["llm_model"],
            temperature=self.part1_session["llm_temperature"],
        )
        result = self._run_agent(
            "part1", llm, tools, PART1_SYSTEM_PROMPT,
            f"Draft and validate the Part1 schema for this request: {user_text}",
            max_steps=25, on_narration=on_narration,
        )
        return self._handle_agent_result("part1", llm, tools, result, new_messages_start=2, on_narration=on_narration)

    def _finish_part1(self, messages, on_narration: Optional["OnNarration"] = None) -> Dict[str, Any]:
        state = self.part1_session["state"]
        status = state.get("status") or {}
        self.part1_status = status
        schema_path = status.get("final_output_schema_path") or status.get("schema_output_path")
        note = (
            f"[Part1 완료] 스키마 생성됨: {schema_path}\n"
            f"(decision={status.get('agent_decision')}, ok={status.get('ok')})"
        )
        if not schema_path or not Path(schema_path).exists() or not status.get("ok", True):
            self.stage = "done"
            return {
                "status": "error",
                "message": note + "\n[중단] Part1이 유효한 스키마를 만들지 못해 Part2로 진행할 수 없습니다.",
            }
        if self.part1_only:
            self.stage = "done"
            return {"status": "done", "message": note + "\n[part1-only] Part2/3은 실행하지 않고 여기서 종료합니다."
                    + self._assumption_summary_block()}
        self.part2_input["schema_input_path"] = str(schema_path)
        self._write_runner_input()
        self.stage = "part2"
        start_note = "[Part2 시작] Aspen 모델 구축 중..."
        if on_narration is not None:
            on_narration(note)
            on_narration(start_note)
        part2_result = self._start_part2(on_narration=on_narration)
        return self._prefix_message(part2_result, note + "\n\n" + start_note + "\n")

    # -- Part2 ----------------------------------------------------------

    def _start_part2(self, on_narration: Optional["OnNarration"] = None) -> Dict[str, Any]:
        os.environ["PART2_STANDALONE_TEST_MODE"] = "0"
        os.environ["PART2_HITL_MODE"] = "chat"

        full_session = p2.build_session(
            runner_input_path=str(self.runner_input_path), record_event=record_event
        )
        self.part2_session = full_session["state"]
        tools = _build_part2_tools(self.part2_session)
        llm = ChatOpenAI(
            model=full_session["llm_model"],
            temperature=full_session["llm_temperature"],
        )
        result = self._run_agent(
            "part2", llm, tools, PART2_SYSTEM_PROMPT,
            "Repair and validate the Part2 Aspen COM model, then write the result.",
            max_steps=40, on_narration=on_narration,
        )
        return self._handle_agent_result("part2", llm, tools, result, new_messages_start=2, on_narration=on_narration)

    def _finish_part2(self, messages, on_narration: Optional["OnNarration"] = None) -> Dict[str, Any]:
        result_path = Path(self.part2_input["part2_result_path"])
        if not result_path.exists():
            self.stage = "done"
            return {
                "status": "error",
                "message": f"[중단] Part2 결과 파일을 찾지 못했습니다: {result_path}",
            }
        part2_result = _load_json(result_path)
        self.part2_result = part2_result
        note = (
            f"[Part2 완료] final_output_kind={part2_result.get('final_output_kind')}, "
            f"ok={part2_result.get('ok')}, issues={part2_result.get('issues_count')}"
        )
        bkp_path = (
            part2_result.get("final_bkp_path")
            or part2_result.get("out_com_proto_bkp_path")
            or self.part2_input["out_com_proto_bkp_path"]
        )
        if not part2_result.get("ok") or not bkp_path or not Path(bkp_path).exists():
            self.stage = "done"
            return {
                "status": "error",
                "message": note + "\n[중단] Part2가 유효한 Aspen 모델 파일을 만들지 못해 Part3로 진행할 수 없습니다.",
            }
        self.part3_input["bkp_path"] = str(bkp_path)
        self._write_runner_input()
        self.stage = "part3"
        start_note = "[Part3 시작] 시뮬레이션 진단/패치 중..."
        if on_narration is not None:
            on_narration(note)
            on_narration(start_note)
        part3_result = self._start_part3(on_narration=on_narration)
        return self._prefix_message(part3_result, note + "\n\n" + start_note + "\n")

    # -- Part3 ----------------------------------------------------------

    def _start_part3(self, on_narration: Optional["OnNarration"] = None) -> Dict[str, Any]:
        os.environ["PART3_HITL_MODE"] = "chat"
        part3_hitl_mode = os.environ["PART3_HITL_MODE"]

        self.part3_session = p3.build_session(
            runner_input_path=str(self.runner_input_path), record_event=record_event
        )
        self._part3_last_assessment = {}
        self._part3_human_review = {}
        tools = _build_part3_tools(
            self.part3_session, self._part3_last_assessment, self._part3_human_review, part3_hitl_mode
        )
        llm = ChatOpenAI(
            model=self.part3_session["llm_model"],
            temperature=self.part3_session["llm_temperature"],
        )
        result = self._run_agent(
            "part3", llm, tools, PART3_SYSTEM_PROMPT,
            "Diagnose and patch the Part3 Aspen model based on its .his file, then write the result.",
            max_steps=40, on_narration=on_narration,
        )
        return self._handle_agent_result("part3", llm, tools, result, new_messages_start=2, on_narration=on_narration)

    def _assumption_summary_block(self) -> str:
        """PART1_EDC_POLICY on: the values the paper did not settle, in plain language, after the final
        message.  Empty (and silent) when the flag is off, nothing was recorded, or anything goes wrong."""
        try:
            from edc_policy import summary_from_file
            text = summary_from_file(Path(self.part1_dir) / "edc_assumption_ledger.json")
        except Exception:
            return ""
        return ("\n\n" + text) if text else ""

    def _finish_part3(self, messages, on_narration: Optional["OnNarration"] = None) -> Dict[str, Any]:
        result_path = Path(self.part3_input["part3_result_path"])
        if not result_path.exists():
            self.stage = "done"
            return {
                "status": "error",
                "message": f"[중단] Part3 결과 파일을 찾지 못했습니다: {result_path}",
            }
        part3_result = _load_json(result_path)
        self.part3_result = part3_result
        self.stage = "done"
        final_bkp = part3_result.get("final_bkp_path")
        return {
            "status": "done",
            "message": (
                f"[Part3 완료] ok={part3_result.get('ok')}\n"
                f"[전체 완료] 최종 모델 파일: {final_bkp}"
                + self._assumption_summary_block()
            ),
            "final_bkp_path": final_bkp,
        }

    # -- generic agent-result plumbing (paused/done/max_steps) -----------

    def _handle_agent_result(
        self, stage: str, llm, tools, result: Dict[str, Any], new_messages_start: int = 0,
        on_narration: Optional["OnNarration"] = None,
    ) -> Dict[str, Any]:
        narration_lines = _extract_narration_lines(stage, result["messages"][new_messages_start:])
        narration_block = _format_narration_block(narration_lines)

        status = result.get("status")
        if status == "paused":
            self.pending = {
                "stage": stage,
                "llm": llm,
                "tools": tools,
                "messages": result["messages"],
                "pending_tool_call": result["pending_tool_call"],
            }
            payload = result.get("payload") or {}
            question = payload.get("question") or "사람의 확인이 필요합니다."
            return {
                "status": "paused",
                "message": narration_block + question,
                "payload": payload,
                "narration": narration_lines,
            }

        self.pending = None
        if status == "max_steps":
            self.stage = "done"
            return {
                "status": "error",
                "message": narration_block + f"[중단] {stage}이(가) 최대 스텝 수에 도달했지만 끝나지 않았습니다.",
                "narration": narration_lines,
            }

        # status == "done"
        finisher = {
            "part1": self._finish_part1,
            "part2": self._finish_part2,
            "part3": self._finish_part3,
        }[stage]
        finished = finisher(result["messages"], on_narration=on_narration)
        if narration_block:
            finished = self._prefix_message(finished, narration_block)
        # finisher() may recurse into the next part (e.g. _finish_part1 ->
        # _start_part2 -> another _handle_agent_result call), which already
        # set "narration" to *that* part's lines -- combine rather than
        # overwrite so an auto-transition's narration reads start to finish
        # (this part's lines first, then whatever the next part already
        # produced), not just the innermost part's.
        finished["narration"] = narration_lines + finished.get("narration", [])
        return finished

    @staticmethod
    def _prefix_message(result: Dict[str, Any], prefix: str) -> Dict[str, Any]:
        result = dict(result)
        result["message"] = prefix + result.get("message", "")
        return result

    # -- public entry point ----------------------------------------------

    def handle_user_message(
        self, text: str, on_narration: Optional["OnNarration"] = None
    ) -> Dict[str, Any]:
        """Advance the pipeline by one chat turn. Returns a dict with at
        least {"status", "message"}: status is "paused" (waiting on this
        reply's answer -- pass the human's next message back in here),
        "done" (the whole pipeline finished; see "final_bkp_path"), or
        "error" (a part failed or ran out of steps; pipeline stopped).

        `on_narration(line: str)`, if given, is called synchronously -- on
        whatever thread is running this call -- the moment each friendly
        narration line becomes available (i.e. as each tool call starts),
        rather than only once this whole call returns. This is what lets a
        live UI (Phase F's Gradio app) show progress in real time instead of
        going silent until a part finishes or pauses; it changes nothing
        about this method's own return value, which still carries the full
        "message"/"narration" exactly as before."""
        if self.pending is not None:
            pending = self.pending
            self.pending = None
            prev_len = len(pending["messages"])
            on_tool_call = _make_on_tool_call(pending["stage"], on_narration)
            result = resume_tool_calling_agent(
                pending["llm"],
                pending["tools"],
                pending["messages"],
                pending["pending_tool_call"],
                human_answer=text,
                max_steps=40,
                on_tool_call=on_tool_call,
            )
            return self._handle_agent_result(
                pending["stage"], pending["llm"], pending["tools"], result,
                new_messages_start=prev_len, on_narration=on_narration,
            )

        if self.stage == "part1" and self.part1_session is None:
            return self._start_part1(text, on_narration=on_narration)

        return {
            "status": "error",
            "message": "지금은 답변을 기다리는 질문이 없습니다. 이미 파이프라인이 끝났거나 진행 중일 수 있습니다.",
        }


def _run_cli() -> None:
    """Minimal terminal REPL for manually exercising a PipelineSession before
    Phase F's Gradio app exists. Run this file directly:
        python chat_orchestrator.py
    Real OpenAI API calls (and, once Part2 starts, Aspen COM/license usage)
    happen here -- this is not a free/local smoke test."""
    print("=== Phase D chat_orchestrator manual test ===")
    print("Part1의 첫 사용자 요청을 입력하세요 (예: 'ATR 공정을 설계해줘').")
    print("Ctrl+C로 언제든 중단할 수 있습니다.\n")

    session = PipelineSession()
    first = input("You: ").strip()
    result = session.handle_user_message(first)

    while True:
        print(f"\n[{result.get('status')}] {result.get('message')}\n")
        if result.get("status") in {"done", "error"}:
            break
        answer = input("You: ").strip()
        result = session.handle_user_message(answer)


if __name__ == "__main__":
    _run_cli()
