"""Migration contracts through the real workflow SDK and local model responses."""
import asyncio
from contextlib import redirect_stdout
from dataclasses import FrozenInstanceError, replace
import io
import itertools
import json
from unittest.mock import patch

import httpx

from acts.act3_workflow import AgentProposer, build_act3, build_workflow, drive_workflow
from cli import print_workflow_result
from formaggio.agents.context import load_scenario
from formaggio.agents.execution import ActiveBudget
from formaggio.agents.runtime import ConsoleProgress
from formaggio.agents.workflow_layers import WorkflowBudget
from formaggio.config import ROOT
from formaggio.shop.checkout import Checkout
from formaggio.shop.data_models import WorkflowState
from tests.support import RecordingTest, fixture
from tests.test_layers import local_api


class ProposalResponses:
    def __init__(self, scenario="standard", *, revise=True):
        self.requests = []
        self.items = fixture(scenario)[1]
        self.revise = revise

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        items = [] if self.revise and len(self.requests) == 1 else [i.model_dump() for i in self.items]
        return httpx.Response(200, json={"id": f"resp_{len(self.requests)}", "object": "response", "created_at": 1,
            "model": "fixture-model", "status": "completed", "output": [{"type": "message", "id": "msg_fixture",
                "role": "assistant", "status": "completed", "content": [{"type": "output_text",
                "text": json.dumps({"items": items}), "annotations": []}]}],
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}})


class WorkflowLayerTests(RecordingTest):
    def latest(self):
        return self.recorder.query("SELECT * FROM runs WHERE act=3 ORDER BY started_at DESC LIMIT 1")[0]

    async def execute(self, harness=None, backend=None, **kwargs):
        harness = harness or build_act3(model="fixture-model", execution_mode="fixture")
        backend = backend or ProposalResponses()
        async with local_api(backend) as api:
            result = await harness.run(self.recorder, api_client=api, **kwargs)
            self.assertFalse(api.is_closed())
        return result, backend

    async def native(self):
        checkout, request, backend = Checkout(), load_scenario("standard"), ProposalResponses()
        prompt = (ROOT / "prompts/cart_proposer.md").read_text()
        prompt += "\nAuthoritative classroom shop policy:\n" + checkout.store.policy.model_dump_json()
        async with local_api(backend) as api:
            proposer = AgentProposer(self.recorder, self.run_id, prompt, "fixture-model", api_client=api)
            workflow = build_workflow(checkout, self.recorder, self.run_id, proposer)
            result = await drive_workflow(workflow, WorkflowState(request=request, checkout_key="native"),
                                          self.recorder, self.run_id, None)
            self.assertEqual(result.status, "placed")
        return backend.requests

    def test_all_removal_combinations_preserve_complete_requests_and_output_contract(self):
        async def check():
            expected = await self.native()
            for trace, budget in itertools.product((False, True), repeat=2):
                harness = build_act3(model="fixture-model", execution_mode="fixture")
                for name, enabled in (("trace", trace), ("budget", budget)):
                    if not enabled:
                        harness = harness.without(name)
                output = io.StringIO()
                with redirect_stdout(output):
                    result, backend = await self.execute(harness, progress=ConsoleProgress())
                    print_workflow_result(result)
                self.assertEqual(expected, backend.requests)
                self.assertEqual(set(result), {"run_id", "outcome", "mode", "model_calls"})
                self.assertEqual(result["model_calls"], 2)
                self.assertEqual(result["outcome"].status, "placed")
                self.assertIn("Step: confirm_request", output.getvalue())
                self.assertEqual("🤖 Model call" in output.getvalue(), trace)
                self.assertNotIn('"messages":', output.getvalue())
                events = self.recorder.timeline(result["run_id"])
                names = [e["event_type"] for e in events]
                self.assertEqual(names.count("model.request"), 2 if trace else 0)
                for name in ("cart.validated", "policy.decision", "order.placed", "workflow.result", "run.finished"):
                    self.assertIn(name, names)
                snapshot = json.loads(self.recorder.query(
                    "SELECT snapshot_json FROM versions WHERE version_id=?", (self.latest()["version_id"],))[0]["snapshot_json"])
                self.assertEqual(snapshot["max_model_calls"], 8 if budget else None)
                self.assertEqual(snapshot["function_limits"]["max_duration_seconds"], 120 if budget else None)
                self.assertEqual([layer["name"] for layer in snapshot["layers"]],
                                 [name for name, enabled in (("trace", trace), ("budget", budget)) if enabled])
        asyncio.run(check())

    def test_removal_never_removes_validation_approval_or_final_stock_check(self):
        async def check():
            for trace, budget in itertools.product((False, True), repeat=2):
                base = build_act3(fixture=True, scenario="manager-approval")
                for name, enabled in (("trace", trace), ("budget", budget)):
                    if not enabled:
                        base = base.without(name)
                checkout = Checkout()
                async def decline(ticket):
                    self.assertFalse(checkout.orders)
                    return "decline"
                result = await base.run(self.recorder, checkout=checkout, manager=decline)
                self.assertEqual(result["outcome"].status, "declined")
                self.assertFalse(checkout.orders)
                async def drain_stock(ticket):
                    checkout.store.inventory["comte"] = 0
                    return "approve"
                result = await base.run(self.recorder, checkout=checkout, manager=drain_stock)
                self.assertEqual(result["outcome"].status, "blocked")
                self.assertFalse(checkout.orders)
                invalid = replace(base, scenario="under-sized")
                result = await invalid.run(self.recorder)
                self.assertEqual(result["outcome"].status, "unresolved")
                self.assertEqual(result["outcome"].attempts, 3)
        asyncio.run(check())

    def test_exact_model_budget_is_independent_of_trace_and_order(self):
        async def check():
            for trace in (False, True):
                for reverse in (False, True):
                    harness = build_act3(model="fixture-model", execution_mode="fixture").without("budget")
                    if not trace:
                        harness = harness.without("trace")
                    harness = harness.add(WorkflowBudget(model_calls=1))
                    if reverse:
                        harness = replace(harness, layers=tuple(reversed(harness.layers)))
                    backend, checkout = ProposalResponses(), Checkout()
                    with self.assertRaises(Exception):
                        await self.execute(harness, backend, checkout=checkout)
                    self.assertEqual(len(backend.requests), 1)
                    self.assertFalse(checkout.orders)
                    self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_manager_wait_is_excluded_and_resumed_work_consumes_remaining_time(self):
        async def check():
            clock, timers = [0.0], []
            def timer(seconds):
                value = ActiveBudget(seconds, clock=lambda: clock[0])
                timers.append(value)
                return value
            harness = build_act3(fixture=True, scenario="manager-approval").without("budget").add(WorkflowBudget(seconds=10))
            async def manager(ticket):
                self.assertEqual(timers[-1].remaining, 7)
                clock[0] += 3600
                self.assertEqual(timers[-1].remaining, 7)
                return "approve"
            def progress(text):
                if text == "Step: propose_cart":
                    clock[0] += 3
                if text == "Step: revalidate_and_place":
                    clock[0] += 2
            with patch("formaggio.agents.workflow_layers.ActiveBudget", side_effect=timer):
                result = await harness.run(self.recorder, manager=manager, progress=progress)
            self.assertEqual(result["outcome"].status, "placed")
            self.assertEqual(timers[0].remaining, 5)
        asyncio.run(check())

    def test_active_timeout_and_cancellation_stop_before_checkout(self):
        async def check():
            for cancel in (False, True):
                checkout, entered = Checkout(), asyncio.Event()
                async def proposer(state, packet):
                    entered.set()
                    await asyncio.Event().wait()
                harness = build_act3(execution_mode="fixture").without("budget").add(
                    WorkflowBudget(seconds=10 if cancel else .02))
                task = asyncio.create_task(harness.run(self.recorder, proposer=proposer, checkout=checkout))
                await entered.wait()
                if cancel:
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                    await task
                self.assertFalse(checkout.orders)
                self.assertEqual(self.latest()["status"], "stopped")
        asyncio.run(check())

    def test_cancellation_and_failure_during_manager_review_do_not_place_order(self):
        async def check():
            for failure in (asyncio.CancelledError(), RuntimeError("manager unavailable")):
                checkout = Checkout()
                async def manager(ticket):
                    raise failure
                with self.assertRaises(type(failure)):
                    await build_act3(fixture=True, scenario="manager-approval").run(
                        self.recorder, checkout=checkout, manager=manager)
                self.assertFalse(checkout.orders)
                self.assertEqual(self.latest()["status"], "stopped" if isinstance(failure, asyncio.CancelledError) else "error")
        asyncio.run(check())

    def test_owned_client_closes_on_client_and_agent_initialization_failure(self):
        async def check():
            for target in ("make_client", "Agent"):
                api = local_api(ProposalResponses())
                with patch("formaggio.agents.workflow_runtime.AsyncOpenAI", return_value=api), \
                     patch("formaggio.agents.workflow_runtime." + target, side_effect=RuntimeError("initialization failed")):
                    with self.assertRaises(Exception):
                        await build_act3(model="fixture-model").run(self.recorder)
                self.assertTrue(api.is_closed())
                self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_early_gates_never_allocate_model_client_and_borrowed_proposer_is_not_closed(self):
        async def check():
            with patch("formaggio.agents.workflow_runtime.AsyncOpenAI", side_effect=AssertionError("must not allocate")):
                for scenario in ("missing-details", "complaint"):
                    result = await build_act3(scenario=scenario).run(self.recorder)
                    self.assertEqual(result["model_calls"], 0)
            class BorrowedProposer:
                closed = False
                async def __call__(self, state, packet):
                    return {"items": fixture()[1]}
                async def close(self):
                    self.closed = True
            proposer = BorrowedProposer()
            result = await build_act3(execution_mode="fixture").run(self.recorder, proposer=proposer)
            self.assertEqual(result["outcome"].status, "placed")
            self.assertFalse(proposer.closed)
        asyncio.run(check())

    def test_reused_configuration_overlapping_runs_get_fresh_checkout_graph_and_counters(self):
        async def check():
            harness = build_act3(scenario="manager-approval", model="fixture-model", execution_mode="fixture")
            ready, tickets, backends = asyncio.Event(), [], []
            async def manager(ticket):
                tickets.append(ticket)
                if len(tickets) == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 3)
                return "approve"
            async def run():
                backend = ProposalResponses("manager-approval")
                backends.append(backend)
                return (await self.execute(harness, backend, manager=manager))[0]
            results = await asyncio.gather(run(), run())
            self.assertEqual([r["outcome"].status for r in results], ["placed", "placed"])
            self.assertEqual([r["model_calls"] for r in results], [2, 2])
            self.assertNotEqual(results[0]["run_id"], results[1]["run_id"])
            self.assertNotEqual(tickets[0].ticket_id, tickets[1].ticket_id)
            self.assertNotEqual(tickets[0].checkout_key, tickets[1].checkout_key)
            self.assertEqual(backends[0].requests, backends[1].requests)
            third, _ = await self.execute(harness, ProposalResponses("manager-approval"), manager=manager)
            self.assertEqual(third["outcome"].status, "placed")
            self.assertEqual(third["model_calls"], 2)
        asyncio.run(check())

    def test_display_flags_preserve_compact_model_logs_and_requests(self):
        async def check():
            sequences = []
            for detailed in (False, True):
                output = io.StringIO()
                result, backend = await self.execute(progress=ConsoleProgress(output.write, show_json=detailed))
                sequences.append(backend.requests)
                self.assertNotIn('"items"', output.getvalue())  # No tool payloads in this proposing agent.
                self.assertNotIn("unit-test-credential", output.getvalue())
                self.assertEqual(result["model_calls"], 2)
            self.assertEqual(*sequences)
        asyncio.run(check())

    def test_configuration_is_immutable_and_mistakes_fail_before_starting(self):
        harness = build_act3()
        with self.assertRaises(FrozenInstanceError):
            harness.scenario = "complaint"
        self.assertEqual(len(harness.layers), 2)
        self.assertEqual(len(harness.without("trace").layers), 1)
        for name in ("validation", "approval", "checkout"):
            with self.assertRaises(ValueError):
                harness.without(name)
        with self.assertRaises(ValueError):
            harness.add(WorkflowBudget())
        for value in (0, True, -1):
            with self.assertRaises(ValueError):
                WorkflowBudget(model_calls=value)
        for value in (0, True, float("inf")):
            with self.assertRaises(ValueError):
                WorkflowBudget(seconds=value)
        before = len(self.recorder.query("SELECT * FROM runs"))
        with self.assertRaises(ValueError):
            asyncio.run(build_act3(execution_mode="fixture").run(self.recorder))
        self.assertEqual(len(self.recorder.query("SELECT * FROM runs")), before)

    def test_time_budget_is_cumulative_across_proposal_revisions(self):
        async def check():
            clock, timers, attempts = [0.0], [], []
            def timer(seconds):
                value = ActiveBudget(seconds, clock=lambda: clock[0])
                timers.append(value)
                return value
            async def proposer(state, packet):
                attempts.append(state.attempts)
                clock[0] += 4
                return {"items": []}
            harness = build_act3(execution_mode="fixture").without("budget").add(WorkflowBudget(seconds=10))
            with patch("formaggio.agents.workflow_layers.ActiveBudget", side_effect=timer):
                with self.assertRaises(TimeoutError):
                    await harness.run(self.recorder, proposer=proposer)
            self.assertEqual(attempts, [0, 1, 2])
            self.assertEqual(timers[0].remaining, 0)
            self.assertEqual(self.latest()["status"], "stopped")
        asyncio.run(check())

    def test_required_audit_failure_without_trace_rolls_back_checkout(self):
        async def check():
            checkout = Checkout()
            initial = dict(checkout.store.inventory)
            original = self.recorder.event
            def record(run_id, event, payload=None, **kwargs):
                if event == "order.placed":
                    raise RuntimeError("audit unavailable")
                return original(run_id, event, payload, **kwargs)
            with patch.object(self.recorder, "event", side_effect=record):
                with self.assertRaises(Exception):
                    await build_act3(fixture=True).without("trace").run(self.recorder, checkout=checkout)
            self.assertEqual(checkout.orders, ())
            self.assertEqual(checkout.store.inventory, initial)
            self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())
