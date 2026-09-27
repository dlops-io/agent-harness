"""Exercise layer combinations against the real SDK with local HTTP responses."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError, replace
import io
import itertools
import json
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from acts.act1_agent import build_act1, build_agent
from cli import print_agent_result
from formaggio.agents.context import ShopContextProvider, build_context, customer_message, load_scenario
from formaggio.agents.layers import Budget, CartCheck, Context, Layer, Trace
from formaggio.agents.runtime import ConsoleProgress, make_client
from formaggio.shop.data_models import LineItem
from formaggio.shop.store import Store
from tests.support import RecordingTest
from tests.test_context import ScriptedResponses


def local_api(backend):
    return AsyncOpenAI(api_key="unit-test-credential", base_url="https://fixture.invalid/v1", max_retries=0,
                       http_client=httpx.AsyncClient(transport=httpx.MockTransport(backend)))


def configured(**kwargs):
    return build_act1(model="fixture-model", execution_mode="fixture", **kwargs)


class FinalOnly(ScriptedResponses):
    def __call__(self, request):
        response = super().__call__(request).json()
        reply = {"message": "Fixture final reply", "items": [item.model_dump() for item in self.items]}
        response["output"] = [{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": json.dumps(reply), "annotations": []}]}]
        return httpx.Response(200, json=response)


class LayerTests(RecordingTest):
    async def execute(self, harness, backend=None, **kwargs):
        backend = backend or ScriptedResponses()
        async with local_api(backend) as api:
            result = await harness.run(self.recorder, api_client=api, **kwargs)
            self.assertFalse(api.is_closed())  # Borrowed clients belong to the caller.
        return result, backend

    def latest(self):
        return self.recorder.query("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1")[0]

    async def native(self, mode="basic"):
        """Run the SDK agent directly, without Harness or any execution middleware."""
        backend, store, request = ScriptedResponses(), Store(), load_scenario("standard")
        async with local_api(backend) as api:
            client, _, _ = make_client("fixture-model", [], api)
            providers = [] if mode == "basic" else [ShopContextProvider(
                build_context(request, store, mode), self.recorder, self.run_id)]
            agent = build_agent(client, store, request, context_providers=providers)
            response = await agent.run(customer_message(request), session=agent.create_session())
        self.assertTrue(response.value.items)
        return backend.requests

    def test_complete_model_request_sequences_match_native_agent_in_both_modes(self):
        async def check():
            for mode in ("basic", "enriched"):
                expected = await self.native(mode)
                result, actual = await self.execute(configured(mode=mode))
                self.assertEqual(expected, actual.requests)
                self.assertEqual(len(actual.requests), 3)
                self.assertTrue(result["report"].ok)
                events = self.recorder.timeline(result["run_id"])
                final = next(e for e in events if e["event_type"] == "agent.result")
                self.assertIsNotNone(final["span_id"])
                self.assertEqual(final["payload"]["cart_check_status"], "passed")
        asyncio.run(check())

    def test_all_sixteen_removal_combinations_preserve_inputs_and_printable_contract(self):
        async def check():
            expected = await self.native()
            for enabled in itertools.product((False, True), repeat=4):
                with self.subTest(enabled=enabled):
                    harness = configured()
                    names = ["trace", "budget", "context", "cart_check"]
                    for name, keep in zip(names, enabled):
                        if not keep:
                            harness = harness.without(name)
                    result, backend = await self.execute(harness)
                    self.assertEqual(expected, backend.requests)
                    self.assertEqual((result["model_calls"], result["tool_calls"]), (3, 2))
                    self.assertEqual(result["cart_check_status"], "passed" if enabled[3] else "not_run")
                    self.assertEqual(result["mode"], "basic" if enabled[2] else "none")
                    output = io.StringIO()
                    with redirect_stdout(output):
                        print_agent_result(result)
                    if not enabled[3]:
                        self.assertIsNone(result["report"])
                        self.assertIn("not run (CartCheck layer disabled)", output.getvalue())
                    events = self.recorder.timeline(result["run_id"])
                    self.assertEqual(any(e["event_type"] == "model.request" for e in events), enabled[0])
                    audit = [e for e in events if e["event_type"] == "tool.requested"]
                    self.assertEqual(len(audit), 2)  # Required audit remains in every configuration.
                    self.assertEqual("arguments" in audit[0]["payload"], enabled[0])
                    stored = self.recorder.query("SELECT snapshot_json FROM versions WHERE version_id=(SELECT version_id FROM runs WHERE run_id=?)", (result["run_id"],))
                    snapshot = json.loads(stored[0]["snapshot_json"])
                    self.assertEqual(snapshot["function_limits"]["max_iterations"], 8 if enabled[1] else 40)
                    self.assertEqual(snapshot["max_model_calls"], 8 if enabled[1] else None)
                    self.assertFalse(snapshot["function_limits"]["allow_concurrent_invocation"])
        asyncio.run(check())

    def test_exact_model_and_tool_budgets_are_independent_of_trace_and_layer_order(self):
        async def check():
            for trace in (False, True):
                for kind in ("model", "tool"):
                    with self.subTest(trace=trace, kind=kind):
                        harness = configured().without("budget")
                        harness = harness.add(Budget(model_calls=1 if kind == "model" else 8,
                                                     tool_calls=1 if kind == "tool" else 20))
                        if not trace:
                            harness = harness.without("trace")
                        backend = ScriptedResponses(endless=True)
                        with self.assertRaises(Exception):
                            await self.execute(harness, backend)
                        self.assertEqual(len(backend.requests), 1 if kind == "model" else 2)
                        self.assertEqual(self.latest()["status"], "error")
                        events = self.recorder.timeline(self.latest()["run_id"])
                        self.assertEqual(sum(e["event_type"] == "tool.started" for e in events), 1)
        asyncio.run(check())

    def test_initialization_and_finish_failures_close_run_and_owned_client(self):
        class FailingLayer(Layer):
            name = "injected_failure"
            def __init__(self, phase):
                self.phase = phase
            def attach(self, run):
                if self.phase == "attach":
                    raise RuntimeError("injected attach failure")
            def finish(self, run, reply):
                if self.phase == "finish":
                    raise RuntimeError("injected finish failure")
        async def check():
            for phase in ("attach", "finish", "client", "agent"):
                with self.subTest(phase=phase):
                    backend = ScriptedResponses()
                    api = local_api(backend)
                    harness = build_act1(model="fixture-model").add(FailingLayer(phase))
                    if phase == "agent":
                        def broken(*args, **kwargs):
                            raise RuntimeError("injected agent binding failure")
                        harness = replace(harness, agent_factory=broken)
                    try:
                        with patch("formaggio.agents.harness.AsyncOpenAI", return_value=api):
                            if phase == "client":
                                with patch("formaggio.agents.harness.make_client", side_effect=RuntimeError("injected client failure")):
                                    with self.assertRaisesRegex(RuntimeError, "injected"):
                                        await harness.run(self.recorder)
                            else:
                                with self.assertRaisesRegex(RuntimeError, "injected"):
                                    await harness.run(self.recorder)
                        self.assertEqual(self.latest()["status"], "error")
                        self.assertIsNotNone(self.latest()["ended_at"])
                        if phase != "attach":
                            self.assertTrue(api.is_closed())
                        self.assertEqual(len(backend.requests), 3 if phase == "finish" else 0)
                    finally:
                        await api.close()
        asyncio.run(check())

    def test_configure_failure_creates_no_run(self):
        class Broken(Layer):
            def configure(self, run):
                raise ValueError("bad configuration")
        before = len(self.recorder.query("SELECT * FROM runs"))
        with self.assertRaisesRegex(ValueError, "bad configuration"):
            asyncio.run(self.execute(configured().add(Broken())))
        self.assertEqual(len(self.recorder.query("SELECT * FROM runs")), before)

    def test_timeout_and_cancellation_finish_runs_without_closing_borrowed_client(self):
        async def check():
            for cancelled in (False, True):
                started = asyncio.Event()
                async def blocked(request):
                    started.set()
                    await asyncio.Event().wait()
                harness = configured().without("budget").add(Budget(seconds=10 if cancelled else .03))
                async with local_api(blocked) as api:
                    task = asyncio.create_task(harness.run(self.recorder, api_client=api))
                    await started.wait()
                    if cancelled:
                        task.cancel()
                    with self.assertRaises(asyncio.CancelledError if cancelled else TimeoutError):
                        await task
                    self.assertFalse(api.is_closed())
                    self.assertEqual(self.latest()["status"], "stopped")
                    self.assertIsNotNone(self.latest()["ended_at"])
        asyncio.run(check())

    def test_overlapping_runs_and_variants_keep_counters_stores_and_checks_isolated(self):
        async def check():
            stores = []
            def fresh_store():
                stores.append(Store())
                return stores[-1]
            harness = replace(configured(), store_factory=fresh_store)
            started, release = asyncio.Event(), asyncio.Event()
            first_backend = ScriptedResponses()
            async def delayed(request):
                started.set()
                await release.wait()
                return first_backend(request)
            first_task = asyncio.create_task(self.execute(harness, delayed))
            await started.wait()
            try:
                second, _ = await self.execute(harness.without("cart_check"), FinalOnly())
            finally:
                release.set()
            first, _ = await first_task
            self.assertEqual((first["model_calls"], first["tool_calls"]), (3, 2))
            self.assertEqual((second["model_calls"], second["tool_calls"]), (1, 0))
            self.assertEqual(first["cart_check_status"], "passed")
            self.assertEqual(second["cart_check_status"], "not_run")
            self.assertIsNot(stores[0], stores[1])
            self.assertNotEqual(first["run_id"], second["run_id"])
            for result in (first, second):
                events = self.recorder.timeline(result["run_id"])
                self.assertEqual(sum(e["event_type"] == "model.request" for e in events), result["model_calls"])
                spans = self.recorder.query("SELECT DISTINCT trace_id FROM spans WHERE run_id=?", (result["run_id"],))
                self.assertEqual(len(spans), 1)
        asyncio.run(check())

    def test_reused_context_configuration_rebuilds_from_this_runs_store(self):
        async def check():
            stock = [900, 0]
            def fresh_store():
                store = Store()
                store.inventory["epoisses"] = stock.pop(0)
                return store
            harness = replace(configured(mode="enriched"), store_factory=fresh_store)
            first, _ = await self.execute(harness, FinalOnly())
            second, _ = await self.execute(harness, FinalOnly())
            def candidates(result):
                return {p["product_id"] for p in result["context"]["sources"][1]["content"]["candidates"]}
            self.assertIn("epoisses", candidates(first))
            self.assertNotIn("epoisses", candidates(second))
            self.assertTrue(first["report"].ok)
            self.assertFalse(second["report"].ok)
        asyncio.run(check())

    def test_required_audit_failure_stops_tools_with_or_without_trace(self):
        async def check():
            original = self.recorder.event
            def fail(run_id, event_type, payload=None, **kwargs):
                if event_type == "tool.requested":
                    raise RuntimeError("injected audit failure")
                return original(run_id, event_type, payload, **kwargs)
            for trace in (False, True):
                harness = configured() if trace else configured().without("trace")
                backend = ScriptedResponses()
                with patch.object(self.recorder, "event", side_effect=fail), self.assertRaises(Exception):
                    await self.execute(harness, backend)
                self.assertEqual(len(backend.requests), 1)
                events = self.recorder.timeline(self.latest()["run_id"])
                self.assertFalse(any(e["event_type"] == "tool.started" for e in events))
                self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_cart_check_reports_invalid_cart_without_repair_and_distinguishes_no_cart(self):
        async def check():
            for items, status in (([LineItem(product="invented-cheese", grams=300)], "failed"), ([], "no_cart")):
                backend = FinalOnly()
                backend.items = items
                result, _ = await self.execute(configured(), backend)
                self.assertEqual(result["reply"].items, items)
                self.assertEqual(result["cart_check_status"], status)
                self.assertEqual(len(backend.requests), 1)
        asyncio.run(check())

    def test_layer_configuration_is_immutable_and_mistakes_fail_early(self):
        harness = configured()
        smaller = harness.without("trace")
        self.assertEqual(len(harness.layers), 4)
        self.assertEqual(len(smaller.layers), 3)
        with self.assertRaises(FrozenInstanceError):
            harness.layers[1].model_calls = 100
        with self.assertRaises(ValueError):
            harness.add(Trace())
        with self.assertRaises(ValueError):
            harness.without("typo")
        for options in ({"model_calls": 0}, {"tool_calls": True}, {"seconds": float("inf")}):
            with self.assertRaises(ValueError):
                Budget(**options)
        with self.assertRaises(ValueError):
            Context("typo")

    def test_compact_and_json_display_do_not_change_full_request_sequence(self):
        async def check():
            outputs, requests = [], []
            for show_json in (False, True):
                lines = []
                _, backend = await self.execute(configured(), progress=ConsoleProgress(lines.append, show_json=show_json))
                outputs.append("\n".join(lines))
                requests.append(backend.requests)
            self.assertEqual(requests[0], requests[1])
            self.assertIn("👤 Customer ask:", outputs[0])
            self.assertNotIn('"customer_id":', outputs[0])
            self.assertIn('"customer_id":', outputs[1])
            for output in outputs:
                self.assertNotIn("🔎 Run:", output)  # CLI owns footer placement.
                self.assertNotIn("unit-test-credential", output)
        asyncio.run(check())
