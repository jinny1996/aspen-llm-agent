# LLM Agent for Literature-to-Aspen Process Modeling

An LLM tool-calling agent system that reads a paper (text + process diagram), builds an Aspen Plus flowsheet model, validates and repairs it, and audits **which parameter values are actually grounded in the paper** ("Converged != Faithful").

> Research code (work in progress). Papers, API keys, and Aspen model files are **not** included. Aspen Plus (COM) license is required to run Part 2/3.

## Pipeline
| Part | Role |
|---|---|
| Part 1 (`src/part1_stage_functions.py`) | paper + figure -> process schema (YAML), each value with a quoted source |
| Part 2 (`src/part2_stage_functions.py`) | schema -> Aspen model via COM; 4-stage deterministic validators + repair loop (`shared/validators/`) |
| Part 3 (`src/part3_stage_functions.py`) | run log (`.his`) parsing, RAG diagnosis, safety-checked patch, rerun (max 3 repair cycles enforced in code) |

## Key components
- `src/agent_loop_utils.py` - hand-written tool-calling loop (`bind_tools` + while) with pause/resume for human-in-the-loop (`HumanInputRequired`). Chosen over LangGraph ToolNode because Aspen COM objects are thread-affine.
- `src/mcp_server.py`, `src/mcp_call_tool.py` - a real MCP (stdio) server exposing 10 context/validation/assessment tools (paper & figure context, schema rules/validation, Aspen function catalog, repair and result assessment). The agents' own 27 `@tool` functions (Part1: 7, Part2: 8, Part3: 12) are LangChain tools; they reach the MCP tools through an adapter. `MCP_ADAPTER_MODE=mcp_client` routes those calls through the stdio server; the default `local_adapter` calls the same functions in-process.
- `src/chat_orchestrator.py`, `src/app.py` - turn-based orchestrator and Gradio chatbot with live narration.
- **EDC (Evidence-grounded DoF Closure)**: `src/dof_ledger.py`, `edc_policy*.py`, `edc_semantic_validator.py` - deterministic (no LLM call) evidence classification, citation checks, impact-ranked questions (cap 5), assumption ledger.
- `tests/` - unit tests.

## Docs
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (Korean) - detailed code structure: agent loop, Part1-3 flow, EDC, MCP, data files.

## Caveats
Research code under development. Evaluation scripts, gold labels and run outputs are not part of this repository, and no accuracy or generalization claim is made.

## Setup
```bash
pip install -r requirements.txt
```
Create a `.env` with `OPENAI_API_KEY=...` (optional: `TAVILY_API_KEY`). Behavior switches are environment variables (all default off):

| Variable | Effect |
|---|---|
| `PART1_EDC_POLICY=1` | evidence-based question selection, answer application, assumption summary |
| `PART1_EDC_PROMPT=1` | prompt additions (verbatim quotes, candidate values, composition basis) |
| `PART1_EDC_POLICY_V2=1` | question policy v2 (uses the semantic validator) |
| `MCP_ADAPTER_MODE=mcp_client` | call tools through the MCP stdio server (default: local adapter) |
| `ASPEN_AGENT_DATA_DIR=<dir>` | shared data directory (papers, `Schema_v5.yaml`, `aspen_code_v3.yaml`, validators); default: `shared/` in this repo (see `src/paths.py`) |

## What can and cannot be run from this repository
- **Unit tests** run without an API key or Aspen. 309 tests pass; 15 are skipped because they look for unpublished run outputs or for the original directory layout (e.g. `../../2608 수정/`), which this repository does not reproduce.
- **The full pipeline** (`src/app.py`, `src/chat_orchestrator.py`) additionally needs a licensed Aspen Plus (COM), an OpenAI API key, and the original data directory (parsed papers and figures, `jinny_DB/`), which is not published here because of paper copyrights. The code is published for review, not as a ready-to-run package.

## Tests
```bash
python -m unittest discover -s tests -t .
```

## License
All rights reserved. Please do not copy, modify or redistribute this code without permission.
