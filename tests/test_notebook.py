"""Execute the saved Colab lesson cells offline; skip install, clone and credentials."""
import ast
import asyncio
from contextlib import ExitStack, chdir, redirect_stdout
import inspect
import io
import itertools
import json
from unittest.mock import patch

from formaggio.config import ROOT
from formaggio.fixtures.harness_fixture import HarnessFixture
from tests.support import RecordingTest
from tests.test_context import ScriptedResponses
from tests.test_layers import local_api
from tests.test_shipping_live_lesson import ShippingResponses


NOTEBOOKS = ("act_1_2.ipynb", "act_3.ipynb", "act_4.ipynb", "act_5.ipynb", "act_6.ipynb")


class NotebookTests(RecordingTest):
    def notebook(self, name):
        return json.loads((ROOT / "notebooks" / name).read_text())

    def execute_notebook(self, name, *, decision="approve", repeats=0):
        namespace, clients, backends = {}, [], []
        shipping_backends = []
        rendered = []
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

        def shipping_client(*args, **kwargs):
            # Test the notebook's live intake/revision path with local HTTP responses.
            backend = ShippingResponses()
            api = local_api(backend)
            shipping_backends.append(backend)
            clients.append(api)
            return api

        async def execute():
            for index, cell in enumerate(self.notebook(name)["cells"]):
                tags = cell["metadata"].get("tags", [])
                if cell["cell_type"] != "code" or not set(tags) & {"imports", "settings", "lesson", "evaluation"}:
                    continue
                source = "".join(cell["source"])
                if name == "full_notebook.ipynb" and "imports" in tags:
                    # Colab-only introductory tables are outside the lesson replay.
                    tree = ast.parse(source)
                    tree.body = [node for node in tree.body if not (
                        isinstance(node, ast.Import) and any(alias.name == "pandas" for alias in node.names)
                    ) and not (isinstance(node, ast.ImportFrom) and node.module == "IPython.display")]
                    source = ast.unparse(tree)
                shipping_cell = name == "full_notebook.ipynb" and "act3-shipping" in tags
                if shipping_cell:
                    namespace["USE_FIXTURES"] = False
                compiled = compile(source, f"{name}:cell-{index}", "exec",
                                   flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
                value = eval(compiled, namespace)
                if inspect.isawaitable(value):
                    await value
                if shipping_cell:
                    namespace["USE_FIXTURES"] = True
                if "imports" in tags:
                    # Replace only the notebook's client; do not patch openai globally,
                    # since the later acts' fixtures construct their own local clients.
                    namespace["AsyncOpenAI"] = model_client
                    namespace["MODEL"] = "fixture-model"
                if "settings" in tags:
                    namespace["USE_FIXTURES"] = True
                    namespace["RUN_CONTEXT_EVALUATION"] = repeats > 0
                    namespace["CONTEXT_REPEATS"] = repeats

        run_root = self.root / name / decision
        run_root.mkdir(parents=True)
        with ExitStack() as stack:
            stack.enter_context(chdir(run_root))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(patch("formaggio.operations.chat_view._display_html", side_effect=rendered.append))
            stack.enter_context(patch("formaggio.agents.harness.AsyncOpenAI", side_effect=model_client))
            stack.enter_context(patch.object(HarnessFixture, "client", fixture_client))
            for module in ("workflow_runtime", "planner_runtime", "composition_runtime"):
                stack.enter_context(patch(f"formaggio.agents.{module}.AsyncOpenAI",
                                          side_effect=shipping_client if name == "full_notebook.ipynb" and module == "workflow_runtime"
                                          else AssertionError("Notebook attempted a live API call")))
            # Exercise the real callback, including its invalid-answer retry.
            review = stack.enter_context(patch("builtins.input", side_effect=itertools.cycle(["invalid", decision])))
            asyncio.run(execute())
        self.assertEqual(len(rendered), {"act_1_2.ipynb": 3, "act_5.ipynb": 2, "full_notebook.ipynb": 18}.get(name, 1))
        for html in rendered:
            self.assertIn("Request and response", html)
            self.assertIn("Steps and checks", html)
            self.assertNotIn("unit-test-credential", html)
        self.assertTrue(all(api.is_closed() for api in clients))
        self.assertNotIn("unit-test-credential", output.getvalue())
        self.assertNotIn("local-fixture-key", output.getvalue())
        if name == "full_notebook.ipynb":
            self.assertEqual([len(b.requests) for b in shipping_backends], [1, 2])
            self.assertEqual(shipping_backends[0].requests[0], shipping_backends[1].requests[0])
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
        self.assertEqual(len(backends), 5)
        for backend in backends[1:2]:
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

    def test_complete_notebook_runs_all_acts_and_optional_context_evaluation(self):
        state, backends, output, review = self.execute_notebook("full_notebook.ipynb", repeats=2)
        self.assertEqual(len(backends), 10)  # Six main runs plus two modes repeated twice.
        self.assertEqual(len(state["context_trials"]), 4)
        self.assertTrue(all(row["cart_check"] == "passed" for row in state["context_trials"]))
        self.assertEqual([row["mode"] for row in state["context_trials"]],
                         ["basic", "enriched", "basic", "enriched"])
        before, after = state["act3_before_result"], state["act3_after_result"]
        self.assertEqual(before["outcome"].status, "blocked")
        self.assertIsNone(before["outcome"].receipt)
        self.assertEqual(after["outcome"].status, "placed")
        self.assertEqual([v.product_id for v in before["outcome"].report.violations], ["comte"])
        self.assertNotIn("comte", [i.product for i in after["outcome"].report.items])
        self.assertEqual(state["act4_interactive_result"]["status"], "saved")
        self.assertEqual(state["act4_approve_result"]["status"], "saved")
        self.assertEqual(state["act4_decline_result"]["status"], "declined")
        self.assertTrue(state["act4_approve_result"]["artifacts"])
        self.assertEqual(state["act4_decline_result"]["artifacts"], [])
        self.assertEqual(state["act5_sourcing_result"]["status"], "saved")
        self.assertIn("vendor-outreach", state["act5_sourcing_result"]["skills_loaded"])
        self.assertEqual(state["act5_stock_result"]["skills_loaded"], [])
        self.assertIn("tasting-planning", state["act5_tasting_result"]["skills_loaded"])
        self.assertTrue(state["act6_interactive_result"]["order_placed"])
        self.assertTrue(state["act6_approve_result"]["order_placed"])
        declined = state["act6_decline_result"]
        self.assertFalse(declined["order_placed"])
        self.assertEqual(declined["orders"][0]["status"], "declined")
        self.assertIsNone(declined["orders"][0].get("receipt"))
        self.assertNotEqual(state["act2_basic_result"]["run_id"], state["context_results"]["basic"]["run_id"])
        self.assertNotEqual(state["act2_enriched_result"]["run_id"], state["context_results"]["enriched"]["run_id"])
        self.assertTrue(state["act6_result"]["order_placed"])
        self.assertTrue(state["delivery_checks"][-1]["ready"])
        self.assertEqual(review.call_count, 6)
        evaluation = state["evaluation_report"]
        self.assertEqual(evaluation["expected_runs"], 6)
        self.assertEqual(evaluation["passed"], 6)
        self.assertEqual(evaluation["mode"], "fixture")
        self.assertEqual(len(evaluation["case_summary"]), 3)
        saved_path = self.root / "full_notebook.ipynb" / "approve" / state["evaluation_json"]
        saved = json.loads(saved_path.read_text())
        self.assertEqual(saved["report"], evaluation)
        self.assertEqual(set(saved["traces"]), {run["run_id"] for run in evaluation["runs"]})
        self.assertTrue(all(events for events in saved["traces"].values()))
        self.assertIn("RAW MILK", output)
        self.assertIn("Removed: Comté", output)
        self.assertIn("Repeated comparison", output)

    def test_optional_evaluation_keeps_failed_trials_in_the_denominator(self):
        notebook = self.notebook("act_1_2.ipynb")
        source = next("".join(cell["source"]) for cell in notebook["cells"]
                      if cell["cell_type"] == "code" and "context_trials = []" in "".join(cell["source"]))
        async def fail(*args, **kwargs):
            raise RuntimeError("Injected evaluation failure")
        lesson = type("BrokenLesson", (), {"run": fail})()
        namespace = {"RUN_CONTEXT_EVALUATION": True, "CONTEXT_REPEATS": 2,
                     "CONTEXT_SCENARIO": "personalized", "MODEL": "fixture-model", "DB_PATH": self.root / "trials.sqlite",
                     "Recorder": type(self.recorder), "build_act2": lambda **kwargs: lesson}
        output = io.StringIO()
        with redirect_stdout(output):
            asyncio.run(eval(compile(source, "optional-evaluation", "exec",
                                    flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT), namespace))
        self.assertEqual(len(namespace["context_trials"]), 4)
        self.assertTrue(all(row["cart_check"] == "error" for row in namespace["context_trials"]))
        self.assertIn("0/2 carts passed; 2 run errors", output.getvalue())
        self.assertIn("unavailable (0/2 trials measured)", output.getvalue())

    def test_complete_notebook_has_shared_settings_and_matches_cli_tutorial(self):
        full = self.notebook("full_notebook.ipynb")
        all_code = ["".join(cell["source"]) for cell in full["cells"] if cell["cell_type"] == "code"]
        assignments = [node for text in all_code for node in ast.walk(ast.parse(text))
                       if isinstance(node, ast.Assign) for target in node.targets
                       if isinstance(target, ast.Name) and target.id == "DB_PATH"]
        self.assertEqual(len(assignments), 1)
        self.assertIn('request = load_scenario(SCENARIO)',
                      next(text for text in all_code if "# @title The Customer Request" in text))
        expected = [
            ("python cli.py --act 1 --scenario standard", "Harness", "SCENARIO", None, None),
            ("python cli.py --act 2 --scenario personalized --context basic", "build_act2", "CONTEXT_SCENARIO", "basic", None),
            ("python cli.py --act 2 --scenario personalized --context enriched", "build_act2", "CONTEXT_SCENARIO", "enriched", None),
            ("python cli.py --act 2 --compare", "run_act2", "CONTEXT_SCENARIO", "both", None),
            ("python cli.py --act 3", "build_act3", "pa-shipping-blocked", None, "review_order"),
            ("python cli.py --act 3 --scenario pa-shipping", "build_act3", "pa-shipping", None, "review_order"),
            ("python cli.py --act 4", "build_act4", "event-shortage", None, "review_email"),
            ("python cli.py --act 4 --email-decision approve", "build_act4", "event-shortage", None, "approve_review"),
            ("python cli.py --act 4 --email-decision decline", "build_act4", "event-shortage", None, "decline_review"),
            ("python cli.py --act 5", "build_act5", "event-shortage", None, "review_email"),
            ("python cli.py --act 5 --scenario stock-question", "build_act5", "stock-question", None, "review_email"),
            ("python cli.py --act 5 --scenario tasting-plan", "build_act5", "tasting-plan", None, "review_email"),
            ("python cli.py --act 6", "build_act6", "standard", None, "review_order"),
            ("python cli.py --act 6 --scenario manager-approval", "build_act6", "manager-approval", None, "review_order"),
            ("python cli.py --act 6 --scenario manager-approval --manager-decision approve", "build_act6", "manager-approval", None, "approve_review"),
            ("python cli.py --act 6 --scenario manager-approval --manager-decision decline", "build_act6", "manager-approval", None, "decline_review"),
        ]
        tutorial = [c for c in full["cells"] if "cli_command" in c["metadata"]]
        self.assertEqual([c["metadata"]["cli_command"] for c in tutorial], [row[0] for row in expected])
        def value(node):
            return node.id if isinstance(node, ast.Name) else ast.literal_eval(node)
        for cell, (command, factory, scenario, mode, reviewer) in zip(tutorial, expected):
            with self.subTest(command=command):
                tree = ast.parse("".join(cell["source"]))
                call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                            and isinstance(n.func, ast.Name) and n.func.id == factory)
                options = {k.arg: value(k.value) for k in call.keywords}
                self.assertEqual(options["scenario"], scenario)
                self.assertEqual(options["model"], "MODEL")
                if factory in {"build_act3", "build_act4", "build_act5", "build_act6"}:
                    self.assertEqual(options["fixture"], "USE_FIXTURES")
                if mode:
                    self.assertEqual(options["mode"], mode)
                if reviewer:
                    run = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                               and isinstance(n.func, ast.Attribute) and n.func.attr == "run")
                    review = next(k.value for k in run.keywords if k.arg in {"manager", "reviewer"})
                    self.assertEqual(value(review), reviewer)
                    if reviewer in {"approve_review", "decline_review"}:
                        self.assertEqual(options["decision_source"], "test_option")
        setting_source = next("".join(c["source"]) for c in full["cells"] if "settings" in c["metadata"].get("tags", []))
        settings = {n.targets[0].id: value(n.value) for n in ast.parse(setting_source).body
                    if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                    and isinstance(n.value, ast.Constant)}
        self.assertEqual(settings["SCENARIO"], "standard")
        self.assertEqual(settings["CONTEXT_SCENARIO"], "personalized")
        self.assertFalse(settings["USE_FIXTURES"])
        lesson_seen = False
        for cell in full["cells"]:
            if cell["cell_type"] == "code":
                if "lesson" in cell["metadata"].get("tags", []):
                    lesson_seen = True
                tree = ast.parse("".join(cell["source"]))
                if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree)):
                    self.assertFalse(lesson_seen)
                self.assertEqual(cell["outputs"], [])
                self.assertIsNone(cell["execution_count"])
                compile("".join(cell["source"]), "complete-notebook", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)

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
