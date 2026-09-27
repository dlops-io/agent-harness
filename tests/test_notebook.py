"""Execute the saved Colab lesson cells offline; skip install, clone and credentials."""
import ast
import asyncio
from contextlib import ExitStack, chdir, redirect_stdout
import inspect
import io
import json
from unittest.mock import patch

from formaggio.config import ROOT
from formaggio.fixtures.harness_fixture import HarnessFixture
from tests.support import RecordingTest
from tests.test_context import ScriptedResponses
from tests.test_layers import local_api


NOTEBOOKS = ("act_1_2.ipynb", "act_3.ipynb", "act_4.ipynb", "act_5.ipynb", "act_6.ipynb")


class NotebookTests(RecordingTest):
    def notebook(self, name):
        return json.loads((ROOT / "notebooks" / name).read_text())

    def execute_notebook(self, name, *, decision="approve"):
        namespace, clients, backends = {}, [], []
        output = io.StringIO()
        original_client = HarnessFixture.client

        def model_client(*args, **kwargs):
            backend = ScriptedResponses()
            api = local_api(backend)
            backends.append(backend)
            clients.append(api)
            return api

        def fixture_client(fixture):
            api = original_client(fixture)
            clients.append(api)
            return api

        async def execute():
            for index, cell in enumerate(self.notebook(name)["cells"]):
                tags = cell["metadata"].get("tags", [])
                if cell["cell_type"] != "code" or not set(tags) & {"imports", "settings", "lesson"}:
                    continue
                compiled = compile("".join(cell["source"]), f"{name}:cell-{index}", "exec",
                                   flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                value = eval(compiled, namespace)
                if inspect.isawaitable(value):
                    await value
                if "imports" in tags:
                    # Replace only the notebook's client; do not patch openai globally,
                    # since the later acts' fixtures construct their own local clients.
                    namespace["AsyncOpenAI"] = model_client
                    namespace["MODEL"] = "fixture-model"
                if "settings" in tags:
                    namespace["USE_FIXTURES"] = True

        run_root = self.root / name / decision
        run_root.mkdir(parents=True)
        with ExitStack() as stack:
            stack.enter_context(chdir(run_root))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(patch("formaggio.agents.harness.AsyncOpenAI", side_effect=model_client))
            stack.enter_context(patch.object(HarnessFixture, "client", fixture_client))
            for module in ("workflow_runtime", "planner_runtime", "composition_runtime"):
                stack.enter_context(patch(f"formaggio.agents.{module}.AsyncOpenAI",
                                          side_effect=AssertionError("Notebook attempted a live API call")))
            # Exercise the real callback, including its invalid-answer retry.
            review = stack.enter_context(patch("builtins.input", side_effect=["invalid", decision]))
            asyncio.run(execute())
        self.assertTrue(all(api.is_closed() for api in clients))
        self.assertNotIn("unit-test-credential", output.getvalue())
        self.assertNotIn("local-fixture-key", output.getvalue())
        return namespace, backends, output.getvalue(), review

    def test_notebooks_keep_imports_at_top_and_share_settings_without_saved_outputs(self):
        shared = {}
        for name in NOTEBOOKS:
            with self.subTest(notebook=name):
                notebook = self.notebook(name)
                self.assertEqual(notebook["nbformat"], 4)
                lesson_seen = False
                for index, cell in enumerate(notebook["cells"]):
                    if cell["cell_type"] != "code":
                        continue
                    self.assertEqual(cell["outputs"], [])
                    self.assertIsNone(cell["execution_count"])
                    source = "".join(cell["source"])
                    compile(source, f"{name}:cell-{index}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                    tags = cell["metadata"]["tags"]
                    if "lesson" in tags:
                        lesson_seen = True
                    imports = [node for node in ast.walk(ast.parse(source))
                               if isinstance(node, (ast.Import, ast.ImportFrom))]
                    if imports:
                        self.assertFalse(lesson_seen)
                        self.assertTrue(set(tags) & {"setup", "imports"})
                    for tag in ("imports", "settings"):
                        if tag in tags:
                            self.assertEqual(source, shared.setdefault(tag, source))
                self.assertTrue(lesson_seen)

    def test_acts_1_2_preserve_agent_requests_and_compare_context(self):
        state, backends, output, review = self.execute_notebook("act_1_2.ipynb")
        self.assertEqual(len(backends), 4)
        for backend in backends[1:3]:
            self.assertEqual(backend.requests, backends[0].requests)
        self.assertNotEqual(backends[2].requests, backends[3].requests)
        self.assertEqual(state["act1_result"]["cart_check_status"], "passed")
        results = state["context_results"]
        self.assertEqual(results["basic"]["context"]["sources"], [])
        self.assertTrue(results["enriched"]["context"]["sources"])
        for result in results.values():
            self.assertEqual(result["cart_check_status"], "passed")
        self.assertIn("Customer ask", output)
        self.assertIn("Comparison", output)
        review.assert_not_called()

    def test_act3_requires_review_and_preserves_both_decisions(self):
        for decision, expected in (("approve", "placed"), ("decline", "declined")):
            with self.subTest(decision=decision):
                state, _, output, review = self.execute_notebook("act_3.ipynb", decision=decision)
                result = state["act3_result"]
                self.assertEqual(result["outcome"].status, expected)
                self.assertEqual(result["mode"], "fixture")
                self.assertEqual(result["model_calls"], 0)
                if decision == "approve":
                    self.assertIsNotNone(result["outcome"].receipt.approval_ticket_id)
                else:
                    self.assertIsNone(result["outcome"].receipt)
                self.assertEqual(review.call_count, 2)
                self.assertIn("Manager review", output)
                self.assertIn("Please enter approve or decline.", output)

    def test_act4_reviews_email_and_saves_only_when_approved(self):
        for decision, expected in (("approve", "saved"), ("decline", "declined")):
            with self.subTest(decision=decision):
                state, _, output, review = self.execute_notebook("act_4.ipynb", decision=decision)
                result = state["act4_result"]
                self.assertEqual(result["status"], expected)
                self.assertTrue(result["scripted"])
                self.assertEqual(bool(result["artifacts"]), decision == "approve")
                artifacts = list((self.root / "act_4.ipynb" / decision).rglob("*.html"))
                self.assertEqual(bool(artifacts), decision == "approve")
                self.assertTrue(result["preferences"])
                self.assertTrue(result["tasks"])
                self.assertFalse(result["order_placed"])
                self.assertFalse(result["email_transmitted"])
                self.assertEqual(review.call_count, 2)
                self.assertIn("Review this email draft", output)
                self.assertIn("Subject:", output)

    def test_act5_loads_skills_only_for_the_relevant_task(self):
        state, _, output, review = self.execute_notebook("act_5.ipynb")
        stock, tasting = (state["act5_results"][key] for key in ("stock-question", "tasting-plan"))
        self.assertEqual(stock["status"], "answered")
        self.assertEqual(stock["skills_loaded"], [])
        self.assertEqual(tasting["status"], "plan_proposed")
        self.assertEqual(tasting["skills_loaded"], ["tasting-planning"])
        self.assertTrue(tasting["skill_resources"])
        self.assertIn("Skills used", output)
        review.assert_not_called()

    def test_act6_checks_checkout_and_tasting_delivery_separately(self):
        state, _, output, review = self.execute_notebook("act_6.ipynb")
        result = state["act6_result"]
        self.assertEqual(result["orders"][0]["status"], "placed")
        self.assertIsNotNone(result["orders"][0]["receipt"])
        self.assertTrue(result["order_placed"])
        self.assertEqual(result["workflow_model_calls"], 0)
        self.assertTrue(state["delivery_checks"])
        self.assertTrue(state["delivery_checks"][-1]["ready"])
        self.assertIn("tasting-planning", result["skills_loaded"])
        self.assertIn("Ready: True", output)
        review.assert_not_called()
