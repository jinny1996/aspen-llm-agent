"""Phase F Gradio app (gradio-chatbot-architecture-plan item F).

A thin UI over chat_orchestrator.PipelineSession: the user talks to it like
Claude/ChatGPT, and the final deliverable (the Aspen .bkp file) shows up as a
downloadable file once the pipeline finishes.

Threading: chat_orchestrator.py's own docstring documents that every Aspen
COM call must happen on the same OS thread that first created `doc`. Gradio
itself gives no such guarantee -- its queue dispatches each event through a
thread pool, so two consecutive chat turns can easily land on different
worker threads. `_PipelineWorker` below exists solely to close that gap: it
owns exactly one dedicated background thread for the process's lifetime, and
every `PipelineSession.handle_user_message()` call -- across every turn, for
every session -- is marshaled onto that one thread via a queue. This is the
same "one worker thread is enough, no thread pool needed" decision already
confirmed in gradio-chatbot-architecture-plan (solo local tool, no
concurrent-user story), just made explicit here since Gradio is the first
layer in this project that could otherwise violate it silently.

Single global session (not one per gr.State): part2/3_stage_functions.py
keep Aspen COM state (`doc`, `schema`, ...) in *module-level* globals, not
per-session objects, so two PipelineSessions running Part2/3 at the same
time in one process would clobber each other regardless of how the UI
layer scopes them. Matches the confirmed architecture decision (solo local
tool, single Aspen Plus license/COM instance) -- one browser tab, one
session, for the life of this process.
"""
from __future__ import annotations

import queue
import threading
import traceback
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import gradio as gr

from chat_orchestrator import PipelineSession, _format_narration_block


class _PipelineWorker:
    """One dedicated background thread. Call `.run(fn, *args)` to execute
    `fn` on that thread and block the caller until it returns (or raises).

    COM note: a background thread is not automatically COM-ready just
    because it's the only thread ever touching `doc` -- win32com/pythoncom
    additionally requires CoInitialize() to have been called ON THAT THREAD
    before any COM object is created there. The CLI entry point
    (chat_orchestrator.py's _run_cli()) never hit this because it runs
    everything on the interpreter's main thread, which effectively already
    has an implicit apartment; this worker thread is a genuinely new OS
    thread and has none, so the very first COM call from
    part2_stage_functions.build_session() (creating `doc` via
    start_aspen_doc()) failed with "CoInitialize가 호출되지 않았습니다."
    until this thread called CoInitialize() once for itself, up front."""

    def __init__(self) -> None:
        self._tasks: "queue.Queue[Tuple[Callable, tuple, Future]]" = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="pipeline-worker")
        self._thread.start()

    def _loop(self) -> None:
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        while True:
            fn, args, fut = self._tasks.get()
            try:
                fut.set_result(fn(*args))
            except BaseException as exc:  # noqa: BLE001 -- surface it to the caller, don't kill the worker
                fut.set_exception(exc)

    def run(self, fn: Callable, *args: Any) -> Any:
        """Blocking: submit `fn` and wait for its result on the caller's
        own thread. Fine for short/simple calls (e.g. _reset)."""
        return self.submit(fn, *args).result()

    def submit(self, fn: Callable, *args: Any) -> Future:
        """Non-blocking: submit `fn` and return immediately with a Future.
        Use this when the caller needs to do something else (e.g. poll a
        progress queue) while `fn` runs on the worker thread."""
        fut: Future = Future()
        self._tasks.put((fn, args, fut))
        return fut


_worker = _PipelineWorker()
_session: Optional[PipelineSession] = None


def _get_or_create_session() -> PipelineSession:
    global _session
    if _session is None:
        _session = PipelineSession()
    return _session


def _advance(text: str, progress_q: "queue.Queue[str]") -> Dict[str, Any]:
    """Runs on the dedicated worker thread (see _PipelineWorker). Forwards
    each live narration line into `progress_q` as it happens -- the Gradio
    handler (_submit, on a different thread) drains that queue while this
    call is still in flight to stream progress to the chat UI in real time."""
    session = _get_or_create_session()
    return session.handle_user_message(text, on_narration=progress_q.put)


def _reset() -> Tuple[List[Dict[str, str]], gr.update]:
    def _do() -> None:
        global _session
        _session = None

    _worker.run(_do)
    return [], gr.update(value=None, visible=False)


def _with_assistant_content(history: List[Dict[str, str]], content: str) -> List[Dict[str, str]]:
    """Returns a NEW list/dict with the last (assistant) message's content
    replaced -- never mutates `history` or its last element in place. Each
    yield in _respond_stream must hand Gradio an independent snapshot; a
    shared, repeatedly-mutated dict would make every already-yielded value
    silently show whatever the *latest* content is by the time anything
    downstream (Gradio's own diffing, a test, ...) actually looks at it."""
    return history[:-1] + [{**history[-1], "content": content}]


def _respond_stream(message: str, history: List[Dict[str, str]]):
    """Generator so Gradio can render progress as it happens instead of only
    once the whole pipeline call returns: each yield updates the chatbot
    with whatever narration lines have arrived so far, in the same live
    assistant bubble, which then gets replaced by the polished final message
    once the call actually finishes (identical end state to the old
    non-streaming version -- only how it gets there is different)."""
    if not (message or "").strip():
        yield history, "", gr.update()
        return

    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": ""},
    ]
    yield history, "", gr.update()

    progress_q: "queue.Queue[str]" = queue.Queue()
    fut = _worker.submit(_advance, message, progress_q)

    lines: List[str] = []
    while not fut.done():
        try:
            lines.append(progress_q.get(timeout=0.2))
        except queue.Empty:
            continue
        history = _with_assistant_content(history, _format_narration_block(lines))
        yield history, "", gr.update()

    # Drain anything that landed between the last poll and the future
    # completing -- queue.put()/fut.set_result() aren't atomic together, so
    # a few lines can still be sitting in the queue right after fut.done().
    while True:
        try:
            lines.append(progress_q.get_nowait())
        except queue.Empty:
            break

    try:
        result = fut.result()
    except Exception as exc:  # pipeline raised something _handle_agent_result didn't catch
        error_text = f"[예외 발생] {type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=3)}"
        history = _with_assistant_content(history, error_text)
        yield history, "", gr.update(value=None, visible=False)
        return

    history = _with_assistant_content(history, result.get("message", ""))

    final_bkp_path = result.get("final_bkp_path")
    if result.get("status") == "done" and final_bkp_path and Path(final_bkp_path).exists():
        file_update = gr.update(value=final_bkp_path, visible=True)
    else:
        file_update = gr.update()

    yield history, "", file_update


with gr.Blocks(title="Aspen 모델링 챗봇 (Phase F)") as demo:
    gr.Markdown(
        "# Aspen 모델링 챗봇\n"
        "논문을 근거로 Aspen Plus 모델을 대화형으로 만듭니다. "
        "먼저 만들고 싶은 공정을 말씀해 주세요 (예: *ATR 공정을 설계해줘*)."
    )
    chatbot = gr.Chatbot(height=520, label=None)
    msg = gr.Textbox(
        placeholder="메시지를 입력하세요...",
        label="메시지",
        show_label=False,
    )
    with gr.Row():
        send_btn = gr.Button("전송", variant="primary")
        reset_btn = gr.Button("새 세션 시작")
    file_out = gr.File(label="최종 모델 파일", visible=False)

    msg.submit(_respond_stream, [msg, chatbot], [chatbot, msg, file_out])
    send_btn.click(_respond_stream, [msg, chatbot], [chatbot, msg, file_out])
    reset_btn.click(_reset, None, [chatbot, file_out])


if __name__ == "__main__":
    # gr.Blocks.__init__ already enables queueing by default in this Gradio
    # version -- no need to call .queue() again here.
    demo.launch()
