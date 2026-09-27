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
