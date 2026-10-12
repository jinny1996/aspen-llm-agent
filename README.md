# LLM Agent for Literature-to-Aspen Process Modeling

An LLM tool-calling agent system that reads a paper (text + process diagram), builds an Aspen Plus flowsheet model, validates and repairs it, and audits **which parameter values are actually grounded in the paper** ("Converged != Faithful").

> Research code (work in progress). Papers, API keys, and Aspen model files are **not** included. Aspen Plus (COM) license is required to run Part 2/3.

## Pipeline
| Part | Role |
|---|---|
| Part 1 (`src/part1_stage_functions.py`) | paper + figure -> process schema (YAML), each value with a quoted source |
| Part 2 (`src/part2_stage_functions.py`) | schema -> Aspen model via COM; 4-stage deterministic validators + repair loop (`shared/valid_*.py`) |
| Part 3 (`src/part3_stage_functions.py`) | run log (`.his`) parsing, RAG diagnosis, safety-checked patch, rerun (max 3 repair cycles enforced in code) |

## Key components
- `src/agent_loop_utils.py` - hand-written tool-calling loop (`bind_tools` + while) with pause/resume for human-in-the-loop (`HumanInputRequired`). Chosen over LangGraph ToolNode because Aspen COM objects are thread-affine.
- `src/mcp_server.py`, `src/mcp_call_tool.py` - 10 tools exposed through a real MCP (stdio) server.
- `src/chat_orchestrator.py`, `src/app.py` - turn-based orchestrator and Gradio chatbot with live narration.
- **EDC (Evidence-grounded DoF Closure)**: `src/dof_ledger.py`, `edc_policy*.py`, `edc_semantic_validator.py` - deterministic (no LLM call) evidence classification, citation checks, impact-ranked questions (cap 5), assumption ledger.
- Evaluation: `src/score_*.py`, `eval_*.py`, `gold_labels/` (labels are mostly drafts; see caveats).

## Docs
`docs/` - system overview, code-flow, EDC research design, evaluation summaries (`EVAL_SUMMARY.md`, `HOLDOUT_*_RESULT.md`).

## Caveats
Pilot-scale evaluation: 6 papers, 3-5 repeats, gold labels mostly drafted by an AI assistant. Development cases were used to design rules (in-sample); hold-out results are reported separately in `docs/HOLDOUT_*`. Hard-coded local paths remain in a few scripts.

## Tests
`python -m pytest src` (unit tests need no API key or Aspen).
