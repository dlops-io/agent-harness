"""Act 4 migration contracts, using the real SDK and local scripted responses."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import itertools
import json
import re
import sqlite3
from unittest.mock import patch

import httpx
from agent_framework import Message, MiddlewareFailure

from acts.act4_harness import build_act4
from formaggio.agents.execution import ActiveBudget
from formaggio.agents.harness_state import ApplicationContext, PreferenceMemory
from formaggio.agents.planner_layers import PlannerBudget
from formaggio.agents.runtime import ConsoleProgress
from formaggio.fixtures.harness_fixture import HarnessFixture
from tests.support import RecordingTest


def normalized(value, root):
    """Replace only run-specific UUIDs and output roots, retaining full request content."""
    text = json.dumps(value, sort_keys=True).replace(str(root), '<ROOT>')
    ids = {}
    return re.sub(r'(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',
                  lambda match: ids.setdefault(match[0], f'<ID{len(ids)}>'), text)


class PlannerLayerTests(RecordingTest):
    def configured(self, **kwargs):
        return build_act4(model="fixture-model", execution_mode="fixture", output_root=self.root / "artifacts", **kwargs)

    def events(self, result, name):
        return [e["payload"] for e in self.recorder.timeline(result["run_id"]) if e["event_type"] == name]

    def latest(self):
        return self.recorder.query("SELECT * FROM runs WHERE act=4 ORDER BY started_at DESC LIMIT 1")[0]

    async def execute(self, lesson=None, *, decision="approve", backend=None, reviewer=None, progress=None):
        lesson = lesson or self.configured()
        backend = backend or HarnessFixture(planning=any(layer.name == "planning" for layer in lesson.layers))
        async def review(ticket):
            self.assertEqual(ticket.draft.recipient, "vendor@example.com")
            return decision
        async with backend.client() as api:
            result = await lesson.run(self.recorder, api_client=api, reviewer=reviewer or review, progress=progress)
            self.assertFalse(api.is_closed())
        return result, backend

    def test_reaffirmed_preference_is_latest_in_the_next_invocation(self):
        async def check():
            path = self.root / "shared-memory.sqlite"
            memory = PreferenceMemory(path)
            try:
                for value in ("Prefer nonalcoholic pairings", "Prefer wine pairings", "Prefer nonalcoholic pairings"):
                    memory.remember("pavlos", "pavlos", [value], self.recorder, self.run_id)
            finally:
                memory.close()
            result, _ = await self.execute(self.configured(customer_id="pavlos", memory_path=path))
            saved = self.events(result, "context.model_input")[-1]["capsule"]["saved_preferences"]
            self.assertEqual(saved[-2:], ["Prefer wine pairings", "Prefer nonalcoholic pairings"])
            self.assertEqual(saved.count("Prefer nonalcoholic pairings"), 1)
        asyncio.run(check())

    def test_complete_request_sequences_match_pre_migration_goldens(self):
        # Captured from the original Act 4, including all calls before/after review.
        cases = [
            ("approve", {}, "24961e0d897f4e13478145bd0758d20f4ff10e4e8209714145e2f8160948f9da"),
            ("decline", {}, "10753bb6ca3ae89bee55504bdd14ca51a582fc8c6858439167931256063f5773"),
            ("approve", {"document": "malicious"}, "70ad317c3628821d375b87dea499e41f60c47a18fc14941c8665b664240b2e91"),
            ("decline", {"demo_compaction": True}, "fdc1b3dc4df46481c6d68c6c91cf28b405af8b835cd7e0c0495a05f2cfb5d644"),
            ("approve", {"customer_id": "shivas", "remember_preferences": ["Nonalcoholic today"]},
             "7a0d3050b1a7055ce6e282b645e63770362e7aafd29cd81018a2010243126fff"),
        ]
        async def check():
            for decision, options, expected in cases:
                result, backend = await self.execute(self.configured(decision_source="test", **options), decision=decision)
                actual = hashlib.sha256(normalized(backend.requests, self.root).encode()).hexdigest()
                self.assertEqual(actual, expected, (decision, options))
                self.assertEqual(result["status"], "saved" if decision == "approve" else "declined")
        asyncio.run(check())

    def test_all_32_removal_combinations_preserve_checks_and_current_context(self):
        names = ("trace", "budget", "planning", "memory", "compaction")
        async def check():
            for index, enabled in enumerate(itertools.product((False, True), repeat=5)):
                with self.subTest(enabled=enabled):
                    features = dict(zip(names, enabled))
                    path = self.root / f"memory-{index}.sqlite"
                    lesson = self.configured(memory_path=path, remember_preferences=("Prefer mild today",))
                    for name, keep in features.items():
                        if not keep:
                            lesson = lesson.without(name)
                    reviews = []
                    async def reviewer(ticket):
                        reviews.append(ticket)
                        self.assertFalse(list((self.root / "artifacts" / self.latest()["run_id"]).glob("*.html")))
                        return "approve"
                    result, backend = await self.execute(lesson, reviewer=reviewer)
                    self.assertEqual(result["status"], "saved")
                    self.assertEqual(len(reviews), 1)
                    self.assertEqual(result["model_calls"], 7 if features["planning"] else 5)
                    self.assertEqual(len(result["tasks"]), 3 if features["planning"] else 0)
                    self.assertEqual(path.exists(), features["memory"])
                    self.assertEqual(bool(result["preferences"]), features["memory"])
                    self.assertFalse(result["order_placed"] or result["email_transmitted"])
                    self.assertEqual(bool(self.events(result, "model.request")), features["trace"])
                    for request in backend.requests:
                        tools = {tool["name"] for tool in request["tools"]}
                        self.assertEqual("todos_add" in tools, features["planning"])
                        self.assertIn("save_vendor_email", tools)
                        self.assertNotIn("place_mock_order", tools)
                    contexts = self.events(result, "context.model_input")
                    self.assertTrue(contexts)
                    for context in contexts:
                        capsule = context["capsule"]
                        self.assertEqual(capsule["confirmed_request"]["allergies"], ["nuts"])
                        self.assertEqual(capsule["confirmed_request"]["preferences"], ["Prefer mild today"])
                        self.assertEqual(capsule["approved_vendor_recipient"], "vendor@example.com")
                        self.assertEqual("messages" in context, features["trace"])
                    self.assertIn("approve", contexts[-1]["capsule"]["email_decisions"].values())
                    self.assertEqual(len(self.events(result, "email.approval_decided")), 1)
                    self.assertTrue(self.events(result, "policy.decision"))
        asyncio.run(check())

    def test_budget_counts_survive_review_and_do_not_depend_on_trace_or_layer_order(self):
        async def check():
            for trace in (False, True):
                for budget, expected_calls in [(PlannerBudget(model_calls=5), 5), (PlannerBudget(tool_calls=5), 6)]:
                    lesson = self.configured().without("budget").add(budget)
                    if not trace:
                        lesson = lesson.without("trace")
                    lesson = replace(lesson, layers=tuple(reversed(lesson.layers)))
                    backend, reviews = HarnessFixture(), []
                    async def reviewer(ticket):
                        reviews.append(ticket)
                        return "approve"
                    with self.assertRaises(Exception):
                        await self.execute(lesson, backend=backend, reviewer=reviewer)
                    self.assertEqual(len(backend.requests), expected_calls)
                    self.assertEqual(len(reviews), 1)
                    self.assertEqual(self.latest()["status"], "error")
                    if budget.tool_calls == 5:
                        calls = self.events({"run_id": self.latest()["run_id"]}, "tool.requested")
                        self.assertEqual(len([call for call in calls if "invocation_id" in call]), 5)
        asyncio.run(check())

    def test_active_budget_excludes_review_wait_and_charges_resumed_execution(self):
        async def check():
            clock, timers = [0.0], []
            def timer(seconds):
                value = ActiveBudget(seconds, clock=lambda: clock[0])
                timers.append(value)
                return value
            class Timed(HarnessFixture):
                def __call__(self, request):
                    clock[0] += 1
                    return super().__call__(request)
            async def reviewer(ticket):
                self.assertEqual(timers[0].remaining, 5)
                clock[0] += 3600
                self.assertEqual(timers[0].remaining, 5)
                return "approve"
            lesson = self.configured().without("budget").add(PlannerBudget(seconds=10))
            with patch("formaggio.agents.layers.ActiveBudget", side_effect=timer):
                result, _ = await self.execute(lesson, backend=Timed(), reviewer=reviewer)
            self.assertEqual(result["status"], "saved")
            self.assertEqual(timers[0].remaining, 3)
        asyncio.run(check())

    def test_compaction_off_keeps_decline_state_and_character_bound(self):
        async def check():
            result, backend = await self.execute(self.configured().without("compaction"), decision="decline")
            self.assertEqual(result["status"], "declined")
            self.assertEqual(result["compactions"], 0)
            self.assertEqual(result["artifacts"], [])
            self.assertIn("decline", self.events(result, "context.model_input")[-1]["capsule"]["email_decisions"].values())
            async def capsule(): return {"confirmed": True}
            context = ApplicationContext(self.recorder, self.run_id, capsule)
            with self.assertRaises(MiddlewareFailure):
                await context([Message(role="user", contents=["x" * 64001])])
        asyncio.run(check())

    def test_no_layers_still_blocks_detector_miss_and_required_audit_failure(self):
        async def check():
            lesson = build_act4(fixture=True, simulate_detector_miss=True, output_root=self.root / "artifacts")
            for layer in lesson.layers:
                lesson = lesson.without(layer.name)
            result = await lesson.run(self.recorder)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["artifacts"], [])
            original = self.recorder.event
            def event(run_id, name, payload=None, **kwargs):
                if name == "email.approval_decided":
                    raise RuntimeError("Audit unavailable")
                return original(run_id, name, payload, **kwargs)
            with patch.object(self.recorder, "event", side_effect=event):
                with self.assertRaises(RuntimeError):
                    await self.execute(replace(lesson, fixture=False, execution_mode="fixture", simulate_detector_miss=False))
            self.assertEqual(self.latest()["status"], "error")
            self.assertFalse(list(self.root.rglob("*.html")))
        asyncio.run(check())

    def test_later_provider_failure_is_error_even_after_a_blocked_tool(self):
        class FailedAfterBlock(HarnessFixture):
            def __call__(self, request):
                if len(self.requests) == 4:
                    self.requests.append(json.loads(request.content))
                    return httpx.Response(500, json={"error": {"message": "Provider unavailable", "type": "server_error"}})
                return super().__call__(request)
        async def check():
            backend = FailedAfterBlock("other@example.com")
            with self.assertRaises(Exception):
                await self.execute(backend=backend)
            self.assertEqual(len(backend.requests), 5)
            self.assertEqual(self.latest()["status"], "error")
            self.assertTrue(self.events({"run_id": self.latest()["run_id"]}, "harness.action_blocked"))
        asyncio.run(check())

    def test_initialization_failures_close_owned_clients_and_memory(self):
        async def check():
            for target in ("formaggio.agents.planner_runtime.make_client", "acts.act4_harness.create_harness_agent"):
                api, memories = HarnessFixture().client(), []
                def memory(path):
                    value = PreferenceMemory(path)
                    memories.append(value)
                    return value
                with patch("formaggio.agents.planner_runtime.PreferenceMemory", side_effect=memory), \
                     patch("formaggio.agents.planner_runtime.AsyncOpenAI", return_value=api), \
                     patch(target, side_effect=RuntimeError("Initialization failed")):
                    with self.assertRaises(RuntimeError):
                        await replace(self.configured(), execution_mode="live").run(self.recorder)
                self.assertTrue(api.is_closed())
                self.assertEqual(len(memories), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    memories[0].db.execute("SELECT 1")
                self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_review_cancellation_and_timeout_mark_stopped_without_saving(self):
        async def check():
            for failure in (asyncio.CancelledError(), TimeoutError()):
                async def reviewer(ticket): raise failure
                with self.assertRaises(type(failure)):
                    await self.execute(reviewer=reviewer)
                self.assertEqual(self.latest()["status"], "stopped")
                self.assertFalse(list(self.root.rglob("*.html")))
        asyncio.run(check())

    def test_overlapping_runs_isolate_customer_tasks_reviews_clients_and_artifacts(self):
        async def check():
            base = self.configured(memory_path=self.root / "shared.sqlite")
            ready, tickets = asyncio.Event(), []
            async def reviewer(ticket):
                tickets.append(ticket)
                if len(tickets) == 2: ready.set()
                await asyncio.wait_for(ready.wait(), 5)
                return "approve"
            async def run(customer):
                return await self.execute(replace(base, customer_id=customer), reviewer=reviewer)
            outputs = await asyncio.gather(run("pavlos"), run("shivas"))
            for customer, (result, backend) in zip(("pavlos", "shivas"), outputs):
                self.assertEqual(result["status"], "saved")
                self.assertEqual(result["model_calls"], 7)
                self.assertEqual(len(result["tasks"]), 3)
                self.assertEqual(self.events(result, "context.model_input")[0]["capsule"]["confirmed_request"]["customer_id"], customer)
                self.assertNotIn("shivas" if customer == "pavlos" else "pavlos", json.dumps(backend.requests))
            self.assertNotEqual(outputs[0][0]["run_id"], outputs[1][0]["run_id"])
            self.assertNotEqual(tickets[0].ticket_id, tickets[1].ticket_id)
            self.assertNotEqual(outputs[0][0]["artifacts"], outputs[1][0]["artifacts"])
        asyncio.run(check())

    def test_compact_and_json_output_do_not_change_complete_requests(self):
        async def check():
            sequences = []
            for detailed in (False, True):
                output = io.StringIO()
                result, backend = await self.execute(progress=ConsoleProgress(output.write, show_json=detailed))
                sequences.append(normalized(backend.requests, self.root))
                self.assertEqual('"recipient"' in output.getvalue(), detailed)
                self.assertNotIn("local-fixture-key", output.getvalue())
            self.assertEqual(*sequences)
        asyncio.run(check())

    def test_layer_configuration_is_immutable_and_rejects_invalid_requests_early(self):
        lesson = self.configured()
        with self.assertRaises(FrozenInstanceError): lesson.model = "changed"
        for name in ("approval", "audit", "context", "recipient_policy"):
            with self.assertRaises(ValueError): lesson.without(name)
        with self.assertRaises(ValueError): lesson.add(PlannerBudget())
        self.assertEqual(len(lesson.layers), 5)
        self.assertEqual(len(lesson.without("memory").layers), 4)
        before = len(self.recorder.query("SELECT * FROM runs"))
        with self.assertRaises(ValueError):
            asyncio.run(build_act4(execution_mode="fixture").run(self.recorder))
        with self.assertRaises(ValueError):
            asyncio.run(build_act4(fixture=True, demo_compaction=True).without("compaction").run(self.recorder))
        self.assertEqual(len(self.recorder.query("SELECT * FROM runs")), before)

    def test_active_provider_timeout_and_cancellation_close_resources(self):
        async def check():
            for cancel in (False, True):
                entered, clients = asyncio.Event(), []
                class Waiting(HarnessFixture):
                    async def __call__(self, request):
                        entered.set()
                        await asyncio.Event().wait()
                    def client(self):
                        api = super().client()
                        clients.append(api)
                        return api
                lesson = self.configured().without("budget").add(PlannerBudget(seconds=10 if cancel else .05))
                task = asyncio.create_task(self.execute(lesson, backend=Waiting()))
                await asyncio.wait_for(entered.wait(), 3)
                if cancel:
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else TimeoutError):
                    await task
                self.assertEqual(self.latest()["status"], "stopped")
                self.assertTrue(clients[0].is_closed())
                self.assertFalse(list(self.root.rglob("*.html")))
        asyncio.run(check())
