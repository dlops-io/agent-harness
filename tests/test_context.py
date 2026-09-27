import asyncio
import copy
import json
import io
import sqlite3
from contextlib import redirect_stdout
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from acts.act1_agent import run_agent
from acts.act2_context import preview_context
from formaggio.config import load_json
from formaggio.agents.runtime import ConsoleProgress
from formaggio.agents.context import build_context, instructions
from formaggio.shop.store import Store
from tests.support import RecordingTest, fixture


class ScriptedResponses:
    """Real SDK/HTTP serialization with a local, explicitly scripted Responses endpoint."""
    def __init__(self, *, error=False, endless=False):
        self.requests = []
        self.error, self.endless = error, endless
        _, self.items = fixture()

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        if self.error:
            return httpx.Response(500, json={"error": {"message": "Fixture service failure", "type": "server_error"}})
        count = len(self.requests)
        if count == 1 or self.endless:
            output = [{"type": "function_call", "id": f"fc_{count}", "call_id": f"call_{count}",
                       "name": "get_catalog", "arguments": "{}", "status": "completed"}]
        elif count == 2:
            output = [{"type": "function_call", "id": "fc_2", "call_id": "call_2", "name": "preview_order",
                       "arguments": json.dumps({"items": [i.model_dump() for i in self.items]}), "status": "completed"}]
        else:
            reply = {"message": "Scripted proposal only. No order placed.", "items": [i.model_dump() for i in self.items]}
            output = [{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": json.dumps(reply), "annotations": []}]}]
        return httpx.Response(200, json={"id": f"resp_{count}", "object": "response", "created_at": 1,
            "model": "fixture-model", "status": "completed", "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}})


class ContextTests(RecordingTest):
    def test_preview_has_same_request_and_explained_selection(self):
        packet = preview_context()
        self.assertEqual(packet["basic"]["sources"], [])
        rich = packet["enriched"]
        candidates = rich["sources"][1]["content"]["candidates"]
        self.assertEqual({p["product_id"] for p in candidates},
                         {"epoisses", "bucheron", "mimolette", "taleggio", "manchego", "gouda", "halloumi"})
        exclusions = {p["product_id"]: p["reasons"] for p in rich["excluded_products"]}
        self.assertIn("allergen_conflict", exclusions["pistachio_manchego"])
        self.assertIn("fictional_shipping_policy", exclusions["comte"])
        self.assertIn("insufficient_stock_for_minimum", exclusions["munster"])
        self.assertIn("null means", packet["same_customer_message"])

    def test_customer_isolation_and_current_request_precedence(self):
        request, _ = fixture(preferences=["Prefer Italian cheese today"])
        customers = copy.deepcopy(load_json("customers.json"))
        customers[1]["preferences"] = ["OTHER_CUSTOMER_PRIVATE_MARKER"]
        packet = build_context(request, Store(), "enriched", customers=customers)
        self.assertNotIn("OTHER_CUSTOMER_PRIVATE_MARKER", json.dumps(packet))
        self.assertNotIn("shivas", json.dumps(packet))
        self.assertIn("Prefer Italian cheese today", json.dumps(packet))
        self.assertIn("take precedence", instructions(Store()))
        with self.assertRaises(ValueError):
            build_context(request, Store(), "enriched", customers=[customers[0], customers[0]])

    def test_missing_allergies_are_explicit_not_assumed_safe(self):
        request, _ = fixture("missing-details")
        packet = build_context(request, Store(), "enriched")
        catalog = packet["sources"][1]["content"]
        self.assertTrue(catalog["unresolved_allergy_information"])
        self.assertTrue(catalog["selection_is_not_final_validation"])
        self.assertIn("walnut_chevre", {p["product_id"] for p in catalog["candidates"]})

    def run_scripted(self, mode, backend=None, *, progress=None):
        backend = backend or ScriptedResponses()
        async def execute():
            async with AsyncOpenAI(api_key="unit-test-credential", base_url="https://fixture.invalid/v1",
                                   max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(backend))) as api:
                return await run_agent(self.recorder, mode=mode, model="fixture-model", api_client=api,
                                       execution_mode="fixture", act=2, progress=progress)
        return asyncio.run(execute()), backend

    def test_real_sdk_calls_trace_tools_and_each_model_input(self):
        result, backend = self.run_scripted("enriched")
        self.assertEqual(len(backend.requests), 3)
        self.assertEqual(result["model_calls"], 3)
        self.assertTrue(result["report"].ok)
        events = self.recorder.timeline(result["run_id"])
        requests = [e for e in events if e["event_type"] == "model.request"]
        self.assertEqual(len(requests), 3)
        self.assertIn("Retrieved context", json.dumps(requests[0]["payload"]))
        self.assertIn("function_result", json.dumps(requests[1]["payload"]))
        self.assertEqual([e["payload"]["name"] for e in events if e["event_type"] == "tool.completed"],
                         ["get_catalog", "preview_order"])
        self.assertTrue(any(e["event_type"] == "proposal.submitted" for e in events))
        spans = self.recorder.query("SELECT * FROM spans WHERE run_id=?", (result["run_id"],))
        self.assertTrue(any("get_catalog" in s["name"] for s in spans))
        self.assertTrue(any("FormaggioAssistant" in s["name"] for s in spans))
        self.assertEqual(len({s["trace_id"] for s in spans}), 1)
        self.assertTrue(any("gen_ai.usage" in s["attributes_json"] for s in spans))
        self.assertNotIn("unit-test-credential", json.dumps(events) + json.dumps(spans))
        self.assertFalse(any(e["event_type"] == "order.placed" for e in events))
        self.assertEqual(self.recorder.query("SELECT count(*) AS n FROM evaluations")[0]["n"], 0)

    def test_progress_explains_real_calls_without_changing_the_agent_input(self):
        from cli import print_agent_result
        from formaggio.agents.context import customer_message, load_scenario
        lines = []
        result, backend = self.run_scripted("basic", progress=ConsoleProgress(lines.append, show_json=True))
        output = "\n".join(lines)
        question = customer_message(load_scenario("standard"))
        self.assertIn(question, output)
        self.assertIn(question, json.dumps(backend.requests[0], ensure_ascii=False).replace('\\n', '\n').replace('\\"', '"'))
        self.assertIn("🤖 Model call 1 · Shop assistant · fixture-model", output)
        self.assertIn("🔧 Tool call 1: get_catalog", output)
        self.assertIn("🔧 Tool call 2: preview_order", output)
        self.assertIn('"grams": 300', output)
        self.assertIn("Latest tool results in context: get_catalog", output)
        self.assertIn("100 input tokens", output)
        self.assertIn("preview truncated", output)
        self.assertNotIn("unit-test-credential", output)
        self.assertEqual((result["model_calls"], result["tool_calls"]), (3, 2))
        final = io.StringIO()
        with redirect_stdout(final):
            print_agent_result(result)
        self.assertIn("✅ Cart satisfies the shop rules.", final.getvalue())
        self.assertIn("Tool calls: 2", final.getvalue())
        self.assertIn("No order was placed", final.getvalue())

    def test_cli_json_flag_changes_display_but_not_model_inputs_or_trace(self):
        import cli
        outputs, requests, traces = [], [], []
        for show_json in (False, True):
            backend = ScriptedResponses()
            async def local_agent(recorder, **kwargs):
                async with AsyncOpenAI(api_key="unit-test-credential", base_url="https://fixture.invalid/v1",
                                       max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(backend))) as api:
                    result = await run_agent(recorder, api_client=api, execution_mode="fixture", **kwargs)
                    traces.append(recorder.timeline(result["run_id"]))
                    return result
            output = io.StringIO()
            args = ["cli.py", "--act", "1", "--scenario", "standard", "--model", "fixture-model",
                    "--db", str(self.root / f"cli-{show_json}.sqlite")]
            if show_json:
                args.append("--show-json")
            with patch("sys.argv", args), patch("acts.act1_agent.run_act1", side_effect=local_agent), redirect_stdout(output):
                cli.main()
            outputs.append(output.getvalue())
            requests.append(backend.requests)
        for output in outputs:
            lines = output.strip().splitlines()
            self.assertTrue(lines[-2].startswith("🔎 Run:"))
            self.assertTrue(lines[-1].startswith("💾 Recorded in"))
            self.assertEqual(output.count("🔎 Run:"), 1)
        compact, detailed = outputs
        self.assertIn("👤 Customer ask:\nCould you help me order cheese", compact)
        self.assertNotIn("🛡️ Proposals only; no orders are placed.", compact)
        self.assertNotIn("💡 Use --show-json", compact)
        for phrase in ["12 guests", "$150.00", "PA", "avoid nuts", "France", "funk rating of 4", "suggest some pairings"]:
            self.assertIn(phrase, compact)
        self.assertIn("product: epoisses; grams: 300", compact)
        self.assertIn("products:", compact)
        self.assertNotIn('"customer_id":', compact)
        self.assertNotIn('"products":', compact)
        self.assertNotIn('"items":', compact)
        self.assertIn('"customer_id": "pavlos"', detailed)
        self.assertIn('"products":', detailed)
        self.assertIn("Exact customer message sent to the model", detailed)
        self.assertLess(len(compact), len(detailed))
        self.assertEqual(requests[0], requests[1])
        for events in traces:
            catalog = next(e["payload"] for e in events if e["event_type"] == "tool.completed")
            self.assertIn('halloumi', json.dumps(catalog))  # Full catalog still recorded in compact mode.

    def test_readable_ask_preserves_missing_and_unconfirmed_details(self):
        from formaggio.agents.context import customer_ask
        request, _ = fixture("missing-details")
        text = customer_ask(request)
        self.assertIn("need to confirm the destination", text)
        self.assertIn("need to check everyone's allergies", text)
        request, _ = fixture("standard", allergies=[], allergies_confirmed=False,
                             order_authorized=False, required_min_funk=0,
                             preferences=["Prefer Italian cheese today"])
        text = customer_ask(request)
        for phrase in ["haven't heard of any allergies", "need to confirm", "don't place an order yet", "funk rating of 0", "Prefer Italian cheese today"]:
            self.assertIn(phrase, text)

    def test_only_context_changes_between_modes(self):
        basic, a = self.run_scripted("basic")
        rich, b = self.run_scripted("enriched")
        first_a, first_b = a.requests[0], b.requests[0]
        for field in ["model", "tools", "text", "max_output_tokens", "store", "instructions"]:
            self.assertEqual(first_a.get(field), first_b.get(field), field)
        self.assertFalse(first_a["store"])
        self.assertNotIn("Retrieved context", json.dumps(first_a))
        self.assertIn("Retrieved context", json.dumps(first_b))
        self.assertEqual(basic["reply"].items, rich["reply"].items)  # fixture outputs, not quality claim
        self.assertNotEqual(basic["run_id"], rich["run_id"])

    def test_service_failure_marks_run_error_without_retry(self):
        backend = ScriptedResponses(error=True)
        with self.assertRaises(Exception):
            self.run_scripted("basic", backend)
        self.assertEqual(len(backend.requests), 1)
        runs = self.recorder.query("SELECT status FROM runs WHERE model='fixture-model'")
        self.assertEqual(runs, [{"status": "error"}])

    def test_repeated_tool_calls_are_bounded(self):
        backend = ScriptedResponses(endless=True)
        with self.assertRaises(Exception):
            self.run_scripted("basic", backend)
        self.assertLessEqual(len(backend.requests), 8)

    def test_offline_preview_does_not_open_database_or_client(self):
        import cli
        output = io.StringIO()
        with patch("sys.argv", ["cli.py", "--preview-context"]), redirect_stdout(output), \
             patch.object(cli, "Recorder", side_effect=AssertionError("Preview opened a database")), \
             patch("formaggio.agents.runtime.AsyncOpenAI", side_effect=AssertionError("Preview opened a client")):
            cli.main()
        packet = json.loads(output.getvalue())
        self.assertEqual(len(packet["same_tools"]), 5)
        self.assertEqual(packet["basic"]["sources"], [])

    def test_sdk_audit_failure_aborts_before_tool_and_next_model_call(self):
        backend = ScriptedResponses()
        original = self.recorder.event
        def record(run_id, event, payload=None, **kwargs):
            if event == "tool.requested":
                raise sqlite3.OperationalError("Fixture audit unavailable")
            return original(run_id, event, payload, **kwargs)
        with patch.object(self.recorder, "event", side_effect=record), self.assertRaises(Exception):
            self.run_scripted("basic", backend)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(self.recorder.query("SELECT count(*) AS n FROM events WHERE event_type='tool.completed'")[0]["n"], 0)
