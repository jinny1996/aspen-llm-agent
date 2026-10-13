"""Tool-calling agent loop shared by the Part1/Part2/Part3 agents.

A minimal ReAct-style loop (`llm.bind_tools` + while loop) written by hand instead of
using LangGraph's prebuilt agent: tools must run in the calling thread because the Aspen
COM document object used by Part2/Part3 is thread-affine.

Features:
  * pause/resume for human-in-the-loop: a tool raises `HumanInputRequired`, the loop
    returns `status="paused"`, and `resume_tool_calling_agent` re-invokes the same tool
    with the human's answer (convention: such tools accept `human_answer=None`).
  * `on_tool_call(name, args)` callback fired right before every tool invocation, used
    by the chat UI to stream progress.
  * a hard `max_steps` limit.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage


class HumanInputRequired(Exception):
    """Raise this from inside a @tool function to pause the agent loop and
    hand control back to whoever is driving it (a live chat turn, a REPL,
    etc.) instead of resolving the tool call yourself.

    `payload` is whatever the caller needs to present the question to a human
    -- e.g. {"question": "...", "context": {...}}. It is surfaced verbatim in
    the paused result's "payload" field.

    Convention (Phase B, 2026-09-02): any tool that can raise this must
    accept a keyword argument `human_answer: Optional[str] = None` (or
    Optional[Any] if it needs a structured answer). Raise when it's None and
    a real answer is needed; when resume_tool_calling_agent re-invokes the
    tool with `human_answer` filled in, do the same bookkeeping the
    "bypass"-mode branch already does, just using the real answer instead of
    an auto-picked one. Docstrings should tell the model never to pass
    human_answer itself -- it's filled in by the resume mechanism, not chosen
    by the model.

    Known limitation: if the model's turn contains more than one tool call
    and a later one raises this, any tool calls after the paused one in that
    same batch are left unresolved when the loop pauses -- resuming only
    answers the one that paused. In practice every current system prompt
    directs the model to call exactly one tool per turn, so this hasn't
    needed a general fix; revisit if a system prompt ever asks for
    multi-tool turns."""

    def __init__(self, payload: Dict[str, Any]):
        super().__init__(payload.get("question") or "human input required")
        self.payload = payload


def run_tool_calling_agent(
    llm,
    tools,
    system_prompt: str,
    user_prompt: Optional[str] = None,
    messages: Optional[List[Any]] = None,
    max_steps: int = 40,
    on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Minimal ReAct-style loop: ask the model what to do next, run whichever
    tool(s) it picked directly in this thread, feed the result back, repeat
    until the model stops calling tools, a tool raises HumanInputRequired, or
    max_steps is hit.

    Tools run directly in this thread (via tool.invoke(args), no ToolNode, no
    ThreadPoolExecutor) so thread-affine resources -- notably Part2/Part3's
    Aspen COM `doc` object -- never cross a thread boundary. This is why this
    hand-rolled loop is used instead of LangGraph's create_react_agent.

    Pass `messages` (from a previous paused/resumed call's result, with the
    human's answer already appended -- see `resume_tool_calling_agent`) to
    continue an existing conversation instead of starting fresh from
    `system_prompt`/`user_prompt`.

    `on_tool_call(name, args)`, if given, is called synchronously right
    before EVERY tool invocation (including one that goes on to raise
    HumanInputRequired) -- this is the hook a live UI uses to show progress
    as it happens instead of only after this function returns. Exceptions
    raised by the callback itself are not caught; keep it fast and
    non-raising (e.g. a queue.put()), not something that can fail.

    Returns a dict, one of:
      {"status": "done", "messages": [...]}
          The model produced a final answer (no more tool calls).
      {"status": "max_steps", "messages": [...]}
          Hit max_steps without a final answer.
      {"status": "paused", "messages": [...], "pending_tool_call": {...}, "payload": {...}}
          A tool raised HumanInputRequired. `pending_tool_call` is the raw
          LangChain tool_call dict (has "id", "name", "args") -- pass it to
          `resume_tool_calling_agent` along with the human's answer once you
          have one.
    """
    tools_by_name = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)
    if messages is None:
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt or "")]
    else:
        messages = list(messages)

    for step in range(1, max_steps + 1):
        ai_msg = llm_with_tools.invoke(messages)
        messages.append(ai_msg)
        tool_calls = getattr(ai_msg, "tool_calls", None) or []
        if not tool_calls:
            print(f"[AGENT] step {step}: final answer, no more tool calls")
            return {"status": "done", "messages": messages}
        for call in tool_calls:
            name = call.get("name")
            args = call.get("args") or {}
            print(f"[AGENT] step {step}: calling {name}({args})")
            if on_tool_call is not None:
                on_tool_call(name, args)
            tool_fn = tools_by_name.get(name)
            if tool_fn is None:
                result_text = f"error: unknown tool {name!r}"
            else:
                try:
                    result_text = str(tool_fn.invoke(args))
                except HumanInputRequired as pause:
                    print(f"[AGENT] step {step}: {name} requires human input -- pausing")
                    return {
                        "status": "paused",
                        "messages": messages,
                        "pending_tool_call": call,
                        "payload": pause.payload,
                    }
                except Exception as exc:
                    result_text = f"error: {type(exc).__name__}: {exc}"
            messages.append(ToolMessage(content=result_text, tool_call_id=call.get("id")))
    else:
        print(f"[AGENT] reached max_steps={max_steps} without a final answer")
        return {"status": "max_steps", "messages": messages}


def resume_tool_calling_agent(
    llm,
    tools,
    messages: List[Any],
    pending_tool_call: Dict[str, Any],
    human_answer: Any,
    max_steps: int = 40,
    on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Resume a paused run_tool_calling_agent() call by re-invoking the SAME
    tool that raised HumanInputRequired, with `human_answer` merged into its
    original arguments -- not by fabricating a ToolMessage. This lets the
    tool run its normal bookkeeping (session-state updates, status-file
    writes, etc.) using the real answer, exactly like the "bypass" branch
    already does with an auto-computed one -- see the `human_answer`
    convention documented on HumanInputRequired above.

    `messages` should be the paused result's "messages" list, and
    `pending_tool_call` its "pending_tool_call", both unchanged. `on_tool_call`
    is the same live-progress hook run_tool_calling_agent takes -- fired here
    for the resumed call itself, and threaded through to the continued loop
    below so the rest of this turn keeps streaming too."""
    tools_by_name = {t.name: t for t in tools}
    name = pending_tool_call.get("name")
    args = dict(pending_tool_call.get("args") or {})
    args["human_answer"] = human_answer
    print(f"[AGENT] resuming: calling {name}(...) with the human's answer")
    if on_tool_call is not None:
        on_tool_call(name, args)
    tool_fn = tools_by_name.get(name)
    if tool_fn is None:
        result_text = f"error: unknown tool {name!r}"
    else:
        try:
            result_text = str(tool_fn.invoke(args))
        except HumanInputRequired as pause:
            # The tool asked a further/different question instead of
            # resolving -- stay paused with that new question rather than
            # erroring out.
            print(f"[AGENT] resume: {name} requires further human input -- staying paused")
            return {
                "status": "paused",
                "messages": list(messages),
                "pending_tool_call": pending_tool_call,
                "payload": pause.payload,
            }
        except Exception as exc:
            result_text = f"error: {type(exc).__name__}: {exc}"
    resumed_messages = list(messages)
    resumed_messages.append(ToolMessage(content=result_text, tool_call_id=pending_tool_call.get("id")))
    return run_tool_calling_agent(
        llm, tools, system_prompt="", messages=resumed_messages, max_steps=max_steps, on_tool_call=on_tool_call
    )
