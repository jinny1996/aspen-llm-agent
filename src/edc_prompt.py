"""edc_prompt.py -- EDC-2e: prompt addenda that make Part1 emit structured evidence.

Switched on by the environment variable PART1_EDC_PROMPT=1.  Off (default): the Part1 prompts are
byte-for-byte what they were, nothing in this module is used.

The addenda are appended to the existing prompts; the existing text is never edited.  The classifier
(edc_policy) re-checks every claim against the paper, so the fields only ever add questions or
confirm a value that can be recomputed / found in the paper -- they cannot make a value look better
than the paper supports.

The strings go through LangChain's PromptTemplate, so literal braces are doubled ({{ }}).
"""
from __future__ import annotations

import os

ENV_FLAG = "PART1_EDC_PROMPT"
VERSION = "v2"      # v1 asked for "one table row including its column headings"; that produced
                    # non-contiguous quotes that fail verification. v2 forbids it.
_ANCHOR_DRAFT = "\n[User Input]\n{user_input}"
_ANCHOR_COMPLETION = "\nSTRICT OUTPUT FORMAT:"


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "0").strip().lower() in ("1", "true", "on", "yes")


DRAFT_RULES = """
Evidence rules for numeric specs (feed flow/T_C/P_bar, block T_C/P_bar/T_out_C/P_out_bar/dP_bar/duty,
split fractions, efficiencies). They add information to each value node; they do NOT change the
topology, scope or output-format rules above, and every value node keeps value/source/confidence.
1. evidence.quote must be copied VERBATIM from [Paper Text]: one contiguous span of at most two
   sentences, or ONE table row copied from its row label to the end of the row. Never put the column
   headings in front of a row and never join pieces from different places: the quote must appear in the
   paper exactly as written. No ellipses, no paraphrase, no added commentary. Put any explanation
   (for example which column the number belongs to) in "rationale". If no such span supports the value, do not invent
   one: use source "inferred" or "llm" and leave the quote empty.
2. For every value that has a quote add "quote_scope": "this_unit" if the quoted text states this
   value for the unit/stream you are modelling; "other_config" if it describes a different
   configuration, unit, case or section than the one in the user request; "sweep_range" if the paper
   varies this quantity over a range and does not fix it; otherwise "general".
3. If the paper gives more than one candidate value for this quantity (cases, scenarios, sweep
   points, different sections), add "alternatives": [{{"value": <number>, "quote": "<verbatim span
   containing that value>"}}, ...] listing EVERY candidate you can quote, including the one you chose.
   Never choose between candidates silently, and never list a value you cannot quote.
4. If a value is not written in the paper but calculated by you from other values of this schema,
   add "derivation": {{"formula": "<plain arithmetic using the input names>", "inputs": {{"<name>":
   "<stream or block id>.<variable>"}}}}, for example {{"formula": "f*0.5", "inputs": {{"f": "S1.flow"}}}}.
   Do not give source "paper" to a calculated value.
5. Feed composition: keep io.feeds[].specs.z a plain mapping component -> fraction, and add a sibling
   io.feeds[].specs.z_provenance = {{"source": ..., "confidence": ..., "evidence": {{"doc_id": ...,
   "locator": ..., "quote": ...}}, "rationale": ...}} that follows rule 1.
6. If aspen_schema.property_method is not the method the paper names, add
   aspen_schema.property_method_note = {{"paper_method": "...", "used_method": "...", "reason": "..."}}.
"""

COMPLETION_RULES = """
EDC evidence rules (apply to every patched numeric value object):
- If you give a quote in "evidence", copy it VERBATIM from the paper snippets (at most two sentences or
  one table row from its row label to the end of the row, without the column headings); it must appear in
  the paper exactly as written. No ellipses, joined pieces, paraphrase or commentary. Explanations go in "rationale".
- A value object may also carry "quote_scope" ("this_unit" | "other_config" | "sweep_range" | "general"),
  "alternatives" ([{{"value": <number>, "quote": "<verbatim span>"}}, ...] for EVERY candidate the paper
  gives for this quantity, including your choice) and "derivation" ({{"formula": "...", "inputs":
  {{"<name>": "<id>.<variable>"}}}} when you calculated the value). Never pick one of several paper
  values silently, and never use source "paper" for a calculated value.
"""


def _insert_before(template: str, anchor: str, addendum: str) -> str:
    i = template.rfind(anchor)
    if i < 0:                                   # anchor not found: append, never drop the rules
        return template + addendum
    return template[:i] + addendum + template[i:]


def draft_template(base: str) -> str:
    """Draft prompt template; unchanged unless PART1_EDC_PROMPT is on."""
    return _insert_before(base, _ANCHOR_DRAFT, DRAFT_RULES) if enabled() else base


def completion_template(base: str) -> str:
    return _insert_before(base, _ANCHOR_COMPLETION, COMPLETION_RULES) if enabled() else base
