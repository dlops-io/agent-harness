"""Act 6 layer, lifecycle and complete-request contracts through the real SDK."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import itertools
import json
from unittest.mock import patch

import httpx
from agent_framework import MiddlewareFailure

from acts.act6_composition import build_act6
from formaggio.agents.composition_layers import CompositionBudget
from formaggio.agents.execution import ActiveBudget
from formaggio.agents.runtime import ConsoleProgress
from formaggio.fixtures.composition_fixture import CompositionFixture
from formaggio.fixtures.skills_fixture import SkillsFixture
from formaggio.shop.checkout import Checkout
from tests.support import RecordingTest
from tests.test_layers import local_api
from tests.test_planner_layers import normalized
from tests.test_workflow_layers import ProposalResponses


class CompositionLayerTests(RecordingTest):
    def configured(self, **kwargs):
        return build_act6(model="fixture-model", execution_mode="fixture", decision_source="test", **kwargs)

    def events(self, result, name):
        return [e["payload"] for e in self.recorder.timeline(result["run_id"]) if e["event_type"] == name]

    def latest(self):
        return self.recorder.query("SELECT * FROM runs WHERE act=6 ORDER BY started_at DESC LIMIT 1")[0]

    async def execute(self, lesson=None, *, backend=None, decision="approve", manager=None, **kwargs):
        lesson = lesson or self.configured()
        features = {layer.name for layer in lesson.layers}
        backend = backend or CompositionFixture(lesson.scenario, planning="planning" in features, skills="skills" in features)
        async def review(ticket): return decision
        async with backend.client() as api:
            result = await lesson.run(self.recorder, api_client=api, manager=manager or review, **kwargs)
            self.assertFalse(api.is_closed())
        return result, backend

    def test_complete_requests_match_reviewed_composition_contract(self):
        # Reviewed contract: explicit final-plan schema and compact authoritative context.
        cases = [
            ("standard", "approve", "257c883699bdf3f19d2635209e3378056b27f2252bc73ddb19799864a1193b3d"),
            ("manager-approval", "approve", "793615c314d279bd1121fe9baea9735b13455b1eb04bae1022228f5ea3f470a3"),
            ("manager-approval", "decline", "b3c88c3d3f4af9009bc7c6902e3b4e5794bdb474eea26a2a648644241443933f"),
            ("two-orders", "approve", "75192ba5a2283581c5c5599ba7d780e61ce8341f41d642d46c2d2dffda040878"),
            ("out-of-stock", "approve", "72a113ee3f953158aaa50a6ee3e124c935685d883dbcfbfbce372fcae10e6b93"),
            ("missing-details", "approve", "e846bea4ad131ab20e70d135bee80e584eb187df90ba7ddf13d05d94bd10984b"),
            ("complaint", "approve", "1823a0ebb5903a396794d2141423828b0a7cdd099db5d3149513d260e84e576c"),
            ("dairy-allergy", "approve", "328a8545a51a74b5629d6dacb969c9aad7e5a34b751a50575f99a81bdeb8bdfb"),
            ("under-sized", "approve", "185d83a2b58cb8630c7bb67d1a1795c4a582f53ccd5108f0cfeb956a501ed32e"),
        ]
        async def check():
            for scenario, decision, expected in cases:
                result, backend = await self.execute(self.configured(scenario=scenario), decision=decision)
                self.assertEqual(hashlib.sha256(normalized(backend.requests, self.root).encode()).hexdigest(), expected,
                                 (scenario, decision))
                self.assertEqual(set(result), {"run_id", "agent_text", "orders", "status", "reason", "model_calls",
                    "workflow_model_calls", "skills_loaded", "skill_resources", "tasks", "compactions",
                    "order_placed", "scripted", "email_transmitted"})
        asyncio.run(check())

    def test_real_inner_proposer_requests_and_counts_survive_trace_and_budget_removal(self):
        async def check():
            for trace, budget in itertools.product((False, True), repeat=2):
                lesson = replace(self.configured(), execution_mode="live")
                for name, enabled in (("trace", trace), ("budget", budget)):
                    if not enabled: lesson = lesson.without(name)
                inner = ProposalResponses()
                async with local_api(inner) as api:
                    result, outer = await self.execute(lesson, proposer_api_client=api)
                    self.assertFalse(api.is_closed())
                self.assertEqual(result["workflow_model_calls"], 2)
                self.assertEqual(result["orders"][0]["status"], "placed")
                for requests, expected in ((outer.requests, "72a113ee3f953158aaa50a6ee3e124c935685d883dbcfbfbce372fcae10e6b93"),
                                           (inner.requests, "338263c20f5a258010b93b5cb06798f271f3c10e259ed46870eb124993c96fa1")):
                    self.assertEqual(hashlib.sha256(normalized(requests, self.root).encode()).hexdigest(), expected)
                self.assertEqual(len(self.events(result, "model.request")), result["model_calls"] + 2 if trace else 0)
        asyncio.run(check())

    def test_all_32_layer_combinations_keep_two_order_approval_and_shared_inventory_checks(self):
        names = ("trace", "budget", "planning", "compaction", "skills")
        async def check():
            for enabled in itertools.product((False, True), repeat=5):
                with self.subTest(enabled=enabled):
                    features = dict(zip(names, enabled))
                    lesson = self.configured(scenario="two-orders")
                    for name, keep in features.items():
                        if not keep: lesson = lesson.without(name)
                    checkout, reviewed = Checkout(), []
                    async def manager(ticket):
                        reviewed.append(ticket)
                        self.assertEqual(len(checkout.orders), len(reviewed) - 1)
                        return "approve"
                    result, backend = await self.execute(lesson, checkout=checkout, manager=manager)
                    self.assertEqual([o["status"] for o in result["orders"]], ["placed", "blocked"])
                    self.assertEqual(len(checkout.orders), 1)
                    self.assertEqual(len({t.ticket_id for t in reviewed}), 2)
                    self.assertEqual(bool(result["tasks"]), features["planning"])
                    self.assertEqual(result["skills_loaded"], ["tasting-planning"] if features["skills"] else [])
                    self.assertEqual(result["model_calls"], len(backend.requests))
                    self.assertEqual(bool(self.events(result, "model.request")), features["trace"])
                    spans = self.recorder.query("SELECT * FROM spans WHERE run_id=?", (result["run_id"],))
                    self.assertEqual(any(s["name"] == "harness.composition" for s in spans), features["trace"])
                    self.assertEqual(any(s["name"] == "workflow.resume" for s in spans), features["trace"])
                    # Mandatory governance actions retain their audit spans.
                    self.assertEqual(len(self.events(result, "order.placed")), 1)
                    self.assertEqual(len(self.events(result, "composition.host_review")), 2)
                    self.assertTrue(self.events(result, "cart.validated"))
                    for request in backend.requests:
                        tools = {t["name"] for t in request["tools"]}
                        self.assertEqual("todos_add" in tools, features["planning"])
                        self.assertEqual("load_skill" in tools, features["skills"])
                        self.assertTrue({"start_order", "get_order_status", "assess_event"} <= tools)
                        self.assertFalse({"approve", "resume_order", "run_skill_script", "place_mock_order"} & tools)
                    capsule = self.events(result, "context.model_input")[-1]["capsule"]
                    self.assertEqual([o["status"] for o in capsule["orders"]], ["placed", "blocked"])
                    self.assertEqual(len(capsule["confirmed_requests"]), 2)
                    snapshot = json.loads(self.recorder.query("SELECT snapshot_json FROM versions WHERE version_id=?",
                                                             (self.latest()["version_id"],))[0]["snapshot_json"])
                    self.assertEqual(snapshot["max_workflow_model_calls"], 8 if features["budget"] else None)
        asyncio.run(check())

    def test_minimal_layers_keep_decline_invalid_scope_and_audit_rollback(self):
        async def check():
            lesson = replace(self.configured(scenario="manager-approval"), layers=())
            result, _ = await self.execute(lesson, decision="decline")
            self.assertEqual(result["orders"][0]["status"], "declined")
            self.assertFalse(result["order_placed"])
            backend = SkillsFixture(calls=[("start_order", {"request_id": "other-customer"})])
            result, _ = await self.execute(lesson, backend=backend)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(self.events(result, "composition.started"), [])
            checkout = Checkout()
            before, event = dict(checkout.store.inventory), self.recorder.event
            def fail(run_id, name, payload=None, **kwargs):
                if name == "order.placed": raise RuntimeError("Required audit unavailable")
                return event(run_id, name, payload, **kwargs)
            with patch.object(self.recorder, "event", side_effect=fail), self.assertRaises(Exception):
                await self.execute(replace(lesson, scenario="standard"), checkout=checkout)
            self.assertFalse(checkout.orders)
            self.assertEqual(checkout.store.inventory, before)
            self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_outer_model_and_tool_budgets_are_cumulative_across_reviews(self):
        async def check():
            for tracing in (False, True):
                for budget, count in ((CompositionBudget(model_calls=3), 3), (CompositionBudget(tool_calls=2), 4)):
                    lesson = self.configured(scenario="manager-approval").without("budget").add(budget)
                    if not tracing: lesson = lesson.without("trace")
                    lesson = replace(lesson, layers=tuple(reversed(lesson.layers)))
                    backend, reviews = CompositionFixture("manager-approval"), []
                    async def manager(ticket): reviews.append(ticket); return "decline"
                    with self.assertRaises(Exception):
                        await self.execute(lesson, backend=backend, manager=manager)
                    self.assertEqual(len(backend.requests), count)
                    self.assertEqual(len(reviews), 1)
                    self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_inner_model_budget_stops_revisions_without_optional_trace(self):
        async def check():
            lesson = replace(self.configured(), execution_mode="live").without("trace").without("budget").add(
                CompositionBudget(workflow_model_calls=1))
            inner, outer, checkout = ProposalResponses(), CompositionFixture(), Checkout()
            async with local_api(inner) as api:
                with self.assertRaises(Exception):
                    await self.execute(lesson, backend=outer, proposer_api_client=api, checkout=checkout)
                self.assertFalse(api.is_closed())
            self.assertEqual(len(inner.requests), 1)
            self.assertEqual(len(outer.requests), 2)
            self.assertFalse(checkout.orders)
            self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_inner_call_budget_is_per_workflow_not_shared_between_requests(self):
        async def check():
            lesson = replace(self.configured(scenario="two-orders"), execution_mode="live").without("trace").without("budget").add(
                CompositionBudget(workflow_model_calls=1))
            inner = ProposalResponses("manager-approval", revise=False)
            async with local_api(inner) as api:
                result, _ = await self.execute(lesson, proposer_api_client=api)
            self.assertEqual(result["workflow_model_calls"], 2)
            self.assertEqual([o["status"] for o in result["orders"]], ["placed", "blocked"])
            self.assertEqual(inner.requests[0], inner.requests[1])
        asyncio.run(check())

    def test_active_time_includes_nested_work_and_resumes_but_excludes_manager_wait(self):
        async def check():
            clock, timers = [0.0], []
            def timer(seconds):
                value = ActiveBudget(seconds, clock=lambda: clock[0]); timers.append(value); return value
            class Timed(CompositionFixture):
                def __call__(self, request):
                    clock[0] += 1
                    return super().__call__(request)
            def progress(text):
                if text == "Step: propose_cart": clock[0] += 2
                if text == "Step: revalidate_and_place": clock[0] += 3
            async def manager(ticket):
                self.assertEqual(timers[0].remaining, 15)
                clock[0] += 3600
                self.assertEqual(timers[0].remaining, 15)
                return "approve"
            lesson = self.configured(scenario="manager-approval").without("budget").add(CompositionBudget(seconds=20))
            with patch("formaggio.agents.layers.ActiveBudget", side_effect=timer):
                result, _ = await self.execute(lesson, backend=Timed("manager-approval"), manager=manager, progress=progress)
            self.assertEqual(result["orders"][0]["status"], "placed")
            self.assertEqual(timers[0].remaining, 6)
        asyncio.run(check())

    def test_cancel_and_review_failure_finalize_without_placing(self):
        async def check():
            for error in (asyncio.CancelledError(), RuntimeError("manager unavailable")):
                checkout = Checkout()
                async def manager(ticket): raise error
                with self.assertRaises(type(error)):
                    await self.execute(self.configured(scenario="manager-approval"), manager=manager, checkout=checkout)
                self.assertFalse(checkout.orders)
                self.assertEqual(self.latest()["status"], "stopped" if isinstance(error, asyncio.CancelledError) else "error")
        asyncio.run(check())

    def test_proposal_timeout_stops_nested_work_and_closes_owned_client(self):
        async def check():
            async def slow(request): await asyncio.Event().wait()
            api = local_api(slow)
            lesson = replace(self.configured(), execution_mode="live").without("budget").add(CompositionBudget(proposal_seconds=.02))
            checkout = Checkout()
            with patch("formaggio.agents.workflow_runtime.AsyncOpenAI", return_value=api), self.assertRaises(MiddlewareFailure) as failure:
                await self.execute(lesson, checkout=checkout)
            self.assertIsInstance(failure.exception.__cause__, TimeoutError)
            self.assertTrue(api.is_closed())
            self.assertFalse(checkout.orders)
            # A nested proposal timeout crosses the SDK tool boundary as a fatal
            # middleware error, preserving the original driver's error contract.
            self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_initialization_failures_close_owned_outer_and_inner_clients(self):
        async def check():
            for target in ("formaggio.agents.composition_runtime.make_client", "acts.act6_composition.create_harness_agent",
                           "formaggio.agents.workflow_runtime.make_client", "formaggio.agents.workflow_runtime.Agent"):
                outer, inner = CompositionFixture().client(), local_api(ProposalResponses())
                with patch("formaggio.agents.composition_runtime.AsyncOpenAI", return_value=outer), \
                     patch("formaggio.agents.workflow_runtime.AsyncOpenAI", return_value=inner) as constructor, \
                     patch(target, side_effect=RuntimeError("Initialization failed")):
                    with self.assertRaises(Exception):
                        await build_act6(model="fixture-model").run(self.recorder)
                self.assertTrue(outer.is_closed())
                if constructor.called:
                    self.assertTrue(inner.is_closed())
                else:
                    await inner.close()
                self.assertEqual(self.latest()["status"], "error")
            with patch("formaggio.agents.skill_support.ReadOnlySkillsProvider.from_paths", side_effect=RuntimeError("Skill init failed")), \
                 patch("formaggio.agents.composition_runtime.AsyncOpenAI") as outer, \
                 patch("formaggio.agents.workflow_runtime.AsyncOpenAI") as inner:
                with self.assertRaises(RuntimeError): await build_act6().run(self.recorder)
                outer.assert_not_called(); inner.assert_not_called()
            self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_overlapping_and_repeated_runs_keep_handles_inventory_and_counters_local(self):
        async def check():
            lesson = self.configured(scenario="manager-approval")
            ready, tickets = asyncio.Event(), []
            async def manager(ticket):
                tickets.append(ticket)
                if len(tickets) == 2: ready.set()
                await asyncio.wait_for(ready.wait(), 5)
                return "approve"
            first, second = await asyncio.gather(self.execute(lesson, manager=manager), self.execute(lesson, manager=manager))
            for result, backend in (first, second):
                self.assertEqual(result["orders"][0]["status"], "placed")
                self.assertEqual(result["model_calls"], len(backend.requests))
                self.assertEqual(result["skills_loaded"], ["tasting-planning"])
            self.assertNotEqual(first[0]["run_id"], second[0]["run_id"])
            self.assertNotEqual(tickets[0].ticket_id, tickets[1].ticket_id)
            self.assertNotEqual(tickets[0].checkout_key, tickets[1].checkout_key)
            third, _ = await self.execute(lesson)
            self.assertEqual(third["orders"][0]["status"], "placed")
            self.assertEqual(third["model_calls"], first[0]["model_calls"])
            self.assertIsNot(first[0]["tasks"], third["tasks"])
            self.assertEqual(normalized(first[1].requests, self.root), normalized(second[1].requests, self.root))
        asyncio.run(check())

    def test_later_provider_error_is_not_masked_by_prior_policy_block(self):
        class Failed(SkillsFixture):
            def __call__(self, request):
                if self.requests:
                    self.requests.append(json.loads(request.content))
                    return httpx.Response(500, json={"error": {"message": "Provider failed", "type": "server_error"}})
                return super().__call__(request)
        async def check():
            backend = Failed(calls=[("start_order", {"request_id": "other-customer"})])
            with self.assertRaises(Exception): await self.execute(backend=backend)
            self.assertEqual(len(backend.requests), 2)
            self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_compact_output_and_json_keep_complete_requests_identical(self):
        async def check():
            sequences = []
            for detailed in (False, True):
                output = io.StringIO()
                _, backend = await self.execute(progress=ConsoleProgress(output.write, show_json=detailed))
                sequences.append(normalized(backend.requests, self.root))
                self.assertEqual('"request_id"' in output.getvalue(), detailed)
                self.assertNotIn("local-fixture-key", output.getvalue())
                self.assertIn("Step: confirm_request", output.getvalue())
            self.assertEqual(*sequences)
        asyncio.run(check())

    def test_configuration_validation_and_missing_skill_snapshot_happen_before_run(self):
        lesson = self.configured()
        with self.assertRaises(FrozenInstanceError): lesson.scenario = "two-orders"
        with self.assertRaises(ValueError): lesson.add(CompositionBudget())
        for name in ("validation", "approval", "checkout", "request_scope"):
            with self.assertRaises(ValueError): lesson.without(name)
        for value in (0, True, -1):
            with self.assertRaises(ValueError): CompositionBudget(workflow_model_calls=value)
        for value in (0, True, float("inf")):
            with self.assertRaises(ValueError): CompositionBudget(proposal_seconds=value)
        async def check():
            before = len(self.recorder.query("SELECT * FROM runs"))
            with self.assertRaises(ValueError): await lesson.run(self.recorder)
            broken = build_act6(fixture=True, skills_root=self.root / "missing")
            with self.assertRaises(ValueError): await broken.run(self.recorder)
            self.assertEqual(len(self.recorder.query("SELECT * FROM runs")), before)
            result = await broken.without("skills").run(self.recorder)
            self.assertEqual(result["orders"][0]["status"], "placed")
        asyncio.run(check())

    def test_expired_shared_timer_stops_before_resuming_checkout(self):
        async def check():
            clock = [0.0]
            def timer(seconds): return ActiveBudget(seconds, clock=lambda: clock[0])
            async def manager(ticket): return "approve"
            # Exhaust the first active segment before a host review can run.
            def progress(text):
                if text == "Step: propose_cart": clock[0] += 11
            checkout = Checkout()
            lesson = self.configured(scenario="manager-approval").without("budget").add(CompositionBudget(seconds=10))
            with patch("formaggio.agents.layers.ActiveBudget", side_effect=timer), self.assertRaises(TimeoutError):
                await self.execute(lesson, checkout=checkout, manager=manager, progress=progress)
            self.assertFalse(checkout.orders)
            self.assertEqual(self.latest()["status"], "stopped")
        asyncio.run(check())

    def test_explicit_shared_checkout_retains_inventory_across_runs(self):
        async def check():
            checkout = Checkout()
            lesson = self.configured(scenario="manager-approval")
            first, _ = await self.execute(lesson, checkout=checkout)
            second, _ = await self.execute(lesson, checkout=checkout)
            self.assertEqual(first["orders"][0]["status"], "placed")
            self.assertEqual(second["orders"][0]["status"], "unresolved")
            self.assertTrue(first["order_placed"])
            self.assertFalse(second["order_placed"])
            unstarted, _ = await self.execute(lesson, checkout=checkout, backend=SkillsFixture(calls=[]))
            self.assertFalse(unstarted["order_placed"])
            self.assertEqual(len(checkout.orders), 1)
            self.assertNotEqual(first["orders"][0]["workflow_id"], second["orders"][0]["workflow_id"])
        asyncio.run(check())
