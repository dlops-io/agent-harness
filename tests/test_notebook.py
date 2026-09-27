"""Run the actual lesson cells with local model responses, without installing or cloning."""
import ast
import asyncio
from contextlib import redirect_stdout
import inspect
import io
import json
from pathlib import Path
from unittest.mock import patch

from formaggio.config import ROOT
from tests.support import RecordingTest
from tests.test_context import ScriptedResponses
from tests.test_layers import local_api


class NotebookTests(RecordingTest):
    def test_lesson_cells_execute_with_local_responses_and_keep_agent_definition_equivalent(self):
        notebook = json.loads((ROOT / "notebooks/acts_1_2.ipynb").read_text())
        namespace, clients, backends = {"Path": Path}, [], []
        def client_factory(*args, **kwargs):
            backend = ScriptedResponses()
            api = local_api(backend)
            clients.append(api)
            backends.append(backend)
            return api
        async def execute():
            for index, cell in enumerate(notebook["cells"]):
                if cell["cell_type"] != "code" or "lesson" not in cell["metadata"].get("tags", []):
                    continue
                self.assertEqual(cell["outputs"], [])
                source = "".join(cell["source"])
                compiled = compile(source, f"notebook-cell-{index}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                value = eval(compiled, namespace)
                if inspect.isawaitable(value):
                    await value
                # Keep notebook DB writes temporary and the model explicitly labeled.
                namespace["DB_PATH"] = self.root / "notebook.sqlite"
                namespace["MODEL"] = "fixture-model"
        output = io.StringIO()
        with patch("openai.AsyncOpenAI", side_effect=client_factory), \
             patch("formaggio.agents.harness.AsyncOpenAI", side_effect=client_factory), redirect_stdout(output):
            asyncio.run(execute())
        self.assertEqual(len(backends), 5)
        self.assertTrue(all(api.is_closed() for api in clients))
        for backend in backends[1:4]:
            self.assertEqual(backend.requests, backends[0].requests)
        self.assertEqual(namespace["result_without_check"]["cart_check_status"], "not_run")
        comparison = namespace["context_results"]
        self.assertEqual(comparison["basic"]["context"]["sources"], [])
        self.assertTrue(comparison["enriched"]["context"]["sources"])
        self.assertNotIn("unit-test-credential", output.getvalue())

    def test_act3_lesson_cells_execute_offline_with_required_checks(self):
        notebook = json.loads((ROOT / "notebooks/act_3.ipynb").read_text())
        namespace = {}
        async def execute():
            for index, cell in enumerate(notebook["cells"]):
                if cell["cell_type"] != "code":
                    continue
                self.assertEqual(cell["outputs"], [])
                source = "".join(cell["source"])
                compiled = compile(source, f"act3-cell-{index}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                if "lesson" not in cell["metadata"].get("tags", []):
                    continue
                value = eval(compiled, namespace)
                if inspect.isawaitable(value):
                    await value
                namespace["DB_PATH"] = self.root / "act3_notebook.sqlite"
        output = io.StringIO()
        with patch("formaggio.agents.workflow_runtime.AsyncOpenAI", side_effect=AssertionError("Offline lesson called API")), \
             redirect_stdout(output):
            asyncio.run(execute())
        for name in ("result", "result_without_trace", "approval_result"):
            result = namespace[name]
            self.assertEqual(result["outcome"].status, "placed")
            self.assertEqual(result["mode"], "fixture")
            self.assertEqual(result["model_calls"], 0)
        self.assertEqual(namespace["result"]["outcome"].attempts, 2)
        self.assertEqual(namespace["result_without_trace"]["outcome"].attempts, 2)
        self.assertIsNotNone(namespace["approval_result"]["outcome"].receipt.approval_ticket_id)
        self.assertIn("validate_and_price", output.getvalue())

    def test_act4_lesson_cells_execute_offline_and_preserve_decline_without_layers(self):
        from formaggio.fixtures.harness_fixture import HarnessFixture
        notebook = json.loads((ROOT / "notebooks/act_4.ipynb").read_text())
        namespace, clients = {}, []
        original = HarnessFixture.client
        def client(fixture):
            value = original(fixture)
            clients.append(value)
            return value
        async def execute():
            for index, cell in enumerate(notebook["cells"]):
                if cell["cell_type"] != "code":
                    continue
                self.assertEqual(cell["outputs"], [])
                compiled = compile("".join(cell["source"]), f"act4-cell-{index}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                if "lesson" not in cell["metadata"].get("tags", []):
                    continue
                value = eval(compiled, namespace)
                if inspect.isawaitable(value):
                    await value
                namespace["DB_PATH"] = self.root / "act4_notebook.sqlite"
                namespace["OUTPUT_ROOT"] = self.root / "artifacts"
        with patch.object(HarnessFixture, "client", client), \
             patch("formaggio.agents.planner_runtime.AsyncOpenAI", side_effect=AssertionError("Offline lesson called live API")), \
             redirect_stdout(io.StringIO()):
            asyncio.run(execute())
        for name in ("result", "minimal_result", "uncompacted_result", "compacted_result"):
            result = namespace[name]
            self.assertEqual(result["status"], "declined")
            self.assertEqual(result["artifacts"], [])
            self.assertTrue(result["scripted"])
        self.assertEqual(namespace["minimal_result"]["tasks"], [])
        self.assertEqual(namespace["minimal_result"]["preferences"], [])
        self.assertEqual(namespace["uncompacted_result"]["compactions"], 0)
        self.assertGreater(namespace["compacted_result"]["compactions"], 0)
        self.assertIn("decline", namespace["states"][-1]["email_decisions"].values())
        self.assertEqual(len(clients), 4)
        self.assertTrue(all(api.is_closed() for api in clients))

    def test_act5_lesson_cells_execute_offline_with_progressive_loading_and_template_gate(self):
        from formaggio.fixtures.skills_fixture import SkillsFixture
        notebook = json.loads((ROOT / "notebooks/act_5.ipynb").read_text())
        namespace, clients = {}, []
        original = SkillsFixture.client
        def client(fixture):
            value = original(fixture)
            clients.append(value)
            return value
        async def execute():
            for index, cell in enumerate(notebook["cells"]):
                if cell["cell_type"] != "code":
                    continue
                self.assertEqual(cell["outputs"], [])
                compiled = compile("".join(cell["source"]), f"act5-cell-{index}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                if "lesson" not in cell["metadata"].get("tags", []):
                    continue
                value = eval(compiled, namespace)
                if inspect.isawaitable(value):
                    await value
                namespace["DB_PATH"] = self.root / "act5_notebook.sqlite"
                namespace["OUTPUT_ROOT"] = self.root / "artifacts"
        with patch.object(SkillsFixture, "client", client), \
             patch("formaggio.agents.planner_runtime.AsyncOpenAI", side_effect=AssertionError("Offline lesson called live API")), \
             redirect_stdout(io.StringIO()):
            asyncio.run(execute())
        self.assertEqual(namespace["stock_result"]["status"], "answered")
        self.assertEqual(namespace["stock_result"]["skills_loaded"], [])
        self.assertEqual(namespace["tasting_result"]["status"], "plan_proposed")
        self.assertEqual(namespace["tasting_result"]["skills_loaded"], ["tasting-planning"])
        self.assertEqual(namespace["outreach_result"]["status"], "declined")
        self.assertEqual(len(namespace["outreach_result"]["skill_resources"]), 4)
        self.assertEqual(namespace["without_skills_result"]["status"], "blocked")
        self.assertEqual(namespace["without_skills_result"]["skills_loaded"], [])
        self.assertFalse(list(self.root.rglob("*.html")))
        self.assertEqual(len(clients), 4)
        self.assertTrue(all(api.is_closed() for api in clients))
