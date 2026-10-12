"""EDC-2e step 2: the prompt addendum.  Flag off = prompts untouched; flag on = rules inserted in a
template that still formats.  No LLM calls: the template is captured, then the call is aborted."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import edc_prompt as epr
import part1_stage_functions as p1

HERE = Path(__file__).resolve().parent
SCHEMA_LAYER = HERE.parent.parent / "2608 수정" / "Schema_v5.yaml"


class _Stop(Exception):
    pass


def captured_draft_template(flag: bool) -> str:
    seen = {}

    def fake_from_template(t, *a, **k):
        seen["t"] = t
        raise _Stop()

    with tempfile.TemporaryDirectory() as d:
        paper = Path(d) / "paper.md"
        paper.write_text("The reactor operates at 300 °C.", encoding="utf-8")
        env = {epr.ENV_FLAG: "1"} if flag else {}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(p1.PromptTemplate, "from_template", side_effect=fake_from_template):
            if not flag:
                os.environ.pop(epr.ENV_FLAG, None)
            try:
                p1.build_schema_draft_langchain(
                    user_input="x", paper_text_path=str(paper), paper_image_dir="", paper_image_glob="",
                    paper_image_top_k=1, schema_layer_path=str(SCHEMA_LAYER), out_schema_path=str(Path(d) / "s.yaml"),
                    use_image=False)
            except _Stop:
                pass
    return seen["t"]


class Switch(unittest.TestCase):
    def test_flag_values(self):
        for v, want in (("1", True), ("true", True), ("ON", True), ("0", False), ("", False), ("no", False)):
            with mock.patch.dict(os.environ, {epr.ENV_FLAG: v}):
                self.assertEqual(epr.enabled(), want, v)
        with mock.patch.dict(os.environ):
            os.environ.pop(epr.ENV_FLAG, None)
            self.assertFalse(epr.enabled())

    def test_off_returns_the_base_template_object_unchanged(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(epr.ENV_FLAG, None)
            base = "abc\n[User Input]\n{user_input}\n"
            self.assertIs(epr.draft_template(base), base)
            self.assertIs(epr.completion_template(base), base)


class Draft(unittest.TestCase):
    @unittest.skipUnless(SCHEMA_LAYER.exists(), "schema layer not found")
    def test_off_vs_on(self):
        off, on = captured_draft_template(False), captured_draft_template(True)
        self.assertNotIn("VERBATIM", off)
        self.assertIn("VERBATIM", on)
        # nothing of the original is lost or reordered: removing the addendum gives the original back
        self.assertEqual(on.replace(epr.DRAFT_RULES, ""), off)
        self.assertLess(on.index("VERBATIM"), on.index("[User Input]"))     # rules sit before the inputs

    @unittest.skipUnless(SCHEMA_LAYER.exists(), "schema layer not found")
    def test_template_still_formats_with_the_same_variables(self):
        from langchain_core.prompts import PromptTemplate
        off, on = PromptTemplate.from_template(captured_draft_template(False)), \
            PromptTemplate.from_template(captured_draft_template(True))
        self.assertEqual(sorted(on.input_variables), sorted(off.input_variables))
        text = on.format(**{v: "<%s>" % v for v in on.input_variables})
        self.assertIn('"quote_scope"', text)
        self.assertIn('{"formula": "f*0.5", "inputs": {"f": "S1.flow"}}', text)       # braces survived


class Completion(unittest.TestCase):
    def test_inserted_before_the_output_format_and_formats(self):
        from langchain_core.prompts import PromptTemplate
        base = p1.COMPLETION_PROMPT.template
        with mock.patch.dict(os.environ, {epr.ENV_FLAG: "1"}):
            new = epr.completion_template(base)
        self.assertEqual(new.replace(epr.COMPLETION_RULES, ""), base)
        self.assertLess(new.index("VERBATIM"), new.index("STRICT OUTPUT FORMAT"))
        a, b = PromptTemplate.from_template(base), PromptTemplate.from_template(new)
        self.assertEqual(sorted(a.input_variables), sorted(b.input_variables))
        b.format(**{v: "<%s>" % v for v in b.input_variables})

    def test_anchor_missing_appends_rather_than_dropping(self):
        with mock.patch.dict(os.environ, {epr.ENV_FLAG: "1"}):
            self.assertTrue(epr.completion_template("no anchor here").endswith(epr.COMPLETION_RULES))


class Passthrough(unittest.TestCase):
    """The extra keys the prompt asks for must survive the schema round trip untouched."""

    def test_extra_keys_reach_the_ledger(self):
        import dof_ledger as dl
        node = {"value": 330, "source": "paper", "confidence": 0.6,
                "evidence": {"doc_id": "paper_text", "locator": "s2", "quote": "heat the gas mixture to 330 °C"},
                "quote_scope": "other_config",
                "alternatives": [{"value": 300, "quote": "operates at 300 °C"}]}
        sch = {"flowsheet_schema": {"blocks": [{"id": "B1", "type": "RGIBBS",
                                                "specs": {"T_C": node, "P_bar": {"value": 30, "source": "paper"}}}],
                                    "streams": [], "connectivity": {"edges": []}}, "io": {"feeds": []}}
        e = {x["id"]: x for x in dl.build_ledger(sch)["entries"]}["B1.T_C"]
        self.assertEqual(e["provenance"]["quote_scope"], "other_config")
        self.assertEqual(e["provenance"]["alternatives"][0]["value"], 300)


if __name__ == "__main__":
    unittest.main()
