import asyncio
import json
import sqlite3
from unittest.mock import patch

from acts.act3_workflow import FixtureProposer
from acts.act6_composition import confirmed_requests, run_act6
from formaggio.shop.checkout import Checkout
from formaggio.agents.composition import WorkflowOrders
from formaggio.fixtures.composition_fixture import CompositionFixture
from formaggio.config import load_json
from formaggio.operations.governance import PolicyBlocked
from formaggio.operations.observability import sdk_tracing
from formaggio.fixtures.skills_fixture import SkillsFixture
from tests.support import RecordingTest


class ComposedHarnessTests(RecordingTest):
    def execute(self, scenario="standard", *, decision="approve", backend=None, manager=None, **kwargs):
        backend = backend or CompositionFixture(scenario)
        checkout = kwargs.pop("checkout", Checkout())
        reviewed = []
        async def review(ticket):
            # Approval is external; the first review must precede any placement.
            if not reviewed:
                self.assertEqual(checkout.orders, ())
            reviewed.append(ticket)
            return await manager(ticket) if manager else decision
        async def run():
            async with backend.client() as client:
                return await run_act6(self.recorder, scenario=scenario, api_client=client,
                    execution_mode="fixture", model="fixture-model", checkout=checkout,
                    manager=review, decision_source="test", **kwargs)
        return asyncio.run(run()), backend, checkout, reviewed

    def events(self, result, name):
        return [e["payload"] for e in self.recorder.timeline(result["run_id"]) if e["event_type"] == name]

    def test_outer_tool_runs_real_workflow_then_loads_skill_for_receipt(self):
        result, backend, checkout, reviewed = self.execute()
        order = result["orders"][0]
        self.assertEqual(order["status"], "placed")
        self.assertEqual(order["report"]["subtotal_cents"], 5010)
        self.assertEqual(len(checkout.orders), 1)
        self.assertEqual(reviewed, [])
        self.assertEqual(result["skills_loaded"], ["tasting-planning"])
        self.assertEqual(result["workflow_model_calls"], 0)
        self.assertEqual(len(self.events(result, "order.placed")), 1)
        for request in backend.requests:
            tools = {t["name"]: t for t in request["tools"]}
            self.assertIn("start_order", tools)
            self.assertEqual(set(tools["start_order"]["parameters"]["properties"]), {"request_id"})
            self.assertFalse({"place_mock_order", "approve", "resume_order", "save_vendor_email", "run_skill_script"} & tools.keys())
            self.assertFalse(request["store"])

    def test_pending_is_not_placed_and_host_resume_result_reaches_outer_model(self):
        result, backend, checkout, reviewed = self.execute("manager-approval")
        self.assertEqual(len(reviewed), 1)
        self.assertEqual(result["orders"][0]["status"], "placed")
        states = self.events(result, "composition.order_state")
        self.assertEqual([s["status"] for s in states], ["pending_approval", "placed"])
        self.assertFalse(states[0]["order_placed"])
        self.assertEqual(result["orders"][0]["receipt"]["approval_ticket_id"], reviewed[0].ticket_id)
        self.assertIn("Host-resumed workflow results", json.dumps(backend.requests))
        self.assertEqual(len(checkout.orders), 1)

    def test_decline_is_terminal_and_creates_no_order(self):
        result, backend, checkout, reviewed = self.execute("manager-decline", decision="decline")
        self.assertEqual(result["orders"][0]["status"], "declined")
        self.assertFalse(result["order_placed"])
        self.assertEqual(checkout.orders, ())
        self.assertEqual(result["skills_loaded"], [])
        self.assertIn('declined', json.dumps(backend.requests[-1]))

    def test_two_pending_tickets_share_stock_and_revalidate_after_each_decision(self):
        result, _, checkout, reviewed = self.execute("two-orders")
        self.assertEqual(len({t.ticket_id for t in reviewed}), 2)
        self.assertEqual([o["status"] for o in result["orders"]], ["placed", "blocked"])
        self.assertEqual(len(checkout.orders), 1)
        states = self.events(result, "composition.order_state")
        self.assertEqual([s["status"] for s in states[:2]], ["pending_approval", "pending_approval"])
        self.assertTrue(any(v["rule"] == "stock" for v in result["orders"][1]["report"]["violations"]))

    def test_workflow_revision_and_constraints_survive_tool_composition(self):
        for scenario, status in [("out-of-stock", "placed"), ("under-sized", "unresolved"),
                                 ("dairy-allergy", "unresolved"), ("missing-details", "clarification"),
                                 ("complaint", "escalated")]:
            with self.subTest(scenario=scenario):
                result, backend, checkout, _ = self.execute(scenario)
                self.assertEqual(result["orders"][0]["status"], status)
                self.assertEqual(len(checkout.orders), int(status == "placed"))
                if scenario == "out-of-stock":
                    self.assertEqual(result["orders"][0]["attempts"], 2)
                if scenario == "dairy-allergy":
                    self.assertIn("catalog cannot supply", result["orders"][0]["message"])
                    self.assertIn("milk", result["orders"][0]["message"])
                    self.assertIn("catalog cannot supply", json.dumps(backend.requests[-1]))
                    self.assertEqual(result["orders"][0]["attempts"], 3)

    def test_repeated_start_returns_one_receipt_without_second_stock_change(self):
        backend = SkillsFixture(calls=[("start_order", {"request_id": "request-1"})] * 2)
        result, _, checkout, _ = self.execute(backend=backend)
        self.assertEqual(len(checkout.orders), 1)
        self.assertEqual(len(self.events(result, "composition.started")), 1)
        self.assertEqual(len(self.events(result, "composition.replayed")), 1)
        self.assertEqual(checkout.store.inventory["epoisses"], 600)

    def test_unknown_request_does_not_start_workflow(self):
        backend = SkillsFixture(calls=[("start_order", {"request_id": "other-customer"})])
        result, _, checkout, _ = self.execute(backend=backend)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(checkout.orders, ())
        self.assertEqual(self.events(result, "composition.started"), [])

    def test_forged_manager_argument_cannot_approve(self):
        backend = SkillsFixture(calls=[("start_order", {"request_id": "request-1", "manager_approved": True})])
        checkout = Checkout()
        try:
            self.execute("manager-approval", backend=backend, checkout=checkout, decision="decline")
        except Exception:
            pass  # Strict SDK argument rejection is also a safe outcome.
        self.assertEqual(checkout.orders, ())

    def test_workflow_and_resumed_spans_link_to_outer_tool_in_same_trace(self):
        result, _, _, _ = self.execute("manager-approval")
        spans = self.recorder.query("SELECT * FROM spans WHERE run_id=?", (result["run_id"],))
        self.assertEqual(len({s["trace_id"] for s in spans}), 1)
        by_id = {s["span_id"]: s for s in spans}
        workflow = next(s for s in spans if s["name"] == "workflow.order")
        resume = next(s for s in spans if s["name"] == "workflow.resume")
        self.assertEqual(resume["parent_span_id"], workflow["span_id"])
        parent_names = []
        current = workflow
        while current["parent_span_id"] in by_id:
            current = by_id[current["parent_span_id"]]
            parent_names.append(current["name"])
        self.assertTrue(any("start_order" in name for name in parent_names), parent_names)
        self.assertIn("harness.composition", parent_names)
        self.assertTrue(any("confirm_request" in s["name"] for s in spans))

    def test_required_checkout_audit_failure_rolls_back_and_stops_outer_model(self):
        checkout, backend = Checkout(), CompositionFixture()
        before = dict(checkout.store.inventory)
        original = self.recorder.event
        def event(run_id, name, payload=None, **kwargs):
            if name == "order.placed":
                raise sqlite3.OperationalError("Injected nested checkout audit outage")
            return original(run_id, name, payload, **kwargs)
        with patch.object(self.recorder, "event", side_effect=event), self.assertRaises(Exception):
            self.execute(checkout=checkout, backend=backend)
        self.assertEqual(checkout.orders, ())
        self.assertEqual(checkout.store.inventory, before)
        self.assertEqual(len(backend.requests), 2)


class WorkflowHandleTests(RecordingTest):
    def service(self):
        requests = confirmed_requests("two-orders")
        proposers = {k: FixtureProposer(load_json("workflow_proposals.json")["manager-approval"], self.recorder, self.run_id)
                     for k in requests}
        return WorkflowOrders(Checkout(), requests, proposers, self.recorder, self.run_id, decision_source="test")

    def test_swapped_stale_and_replayed_decisions_are_rejected(self):
        orders = self.service()
        async def exercise():
            with self.recorder.span(self.run_id, "test.composition"), sdk_tracing(self.recorder):
                await orders.start("request-1")
                await orders.start("request-2")
                first, second = orders.pending_reviews()
                request_id, response_id, ticket = first
                with self.assertRaises(PolicyBlocked):
                    await orders.resume(request_id, second[1], second[2].ticket_id, "approve")
                with self.assertRaises(PolicyBlocked):
                    await orders.resume(request_id, response_id, second[2].ticket_id, "approve")
                await orders.resume(request_id, response_id, ticket.ticket_id, "decline")
                with self.assertRaises(PolicyBlocked):
                    await orders.resume(request_id, response_id, ticket.ticket_id, "approve")
                replay = await orders.start(request_id)
                self.assertEqual(replay["status"], "declined")
                self.assertEqual(len(orders.pending_reviews()), 1)
                self.assertEqual(orders.checkout.orders, ())
        asyncio.run(exercise())

    def test_policy_change_after_review_blocks_checkout(self):
        orders = self.service()
        async def exercise():
            with self.recorder.span(self.run_id, "test.composition"), sdk_tracing(self.recorder):
                await orders.start("request-1")
                request_id, response_id, ticket = orders.pending_reviews()[0]
                orders.checkout.store.policy = orders.checkout.store.policy.model_copy(update={"version": "new-policy"})
                outcome = await orders.resume(request_id, response_id, ticket.ticket_id, "approve")
                self.assertEqual(outcome["status"], "blocked")
                self.assertFalse(outcome["order_placed"])
        asyncio.run(exercise())
