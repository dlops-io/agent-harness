"""Offline counterexamples through the real SDK workflow and checkout boundary."""
import asyncio
import json
import sqlite3
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from acts.act3_workflow import run_act3
from formaggio.shop.checkout import Checkout
from formaggio.config import load_json
from formaggio.shop.data_models import CartProposal, LineItem
from formaggio.operations.governance import ApprovalRequired, PolicyBlocked, PolicyCheckError
from formaggio.shop.store import Store
from tests.support import RecordingTest, fixture


class CheckoutTests(RecordingTest):
    def setUp(self):
        super().setUp()
        self.checkout = Checkout()

    def place(self, request, items, key="order-test", ticket=None):
        return self.checkout.place(request, items, key, self.recorder, self.run_id, ticket_id=ticket)

    def approve(self, request, items, key="order-test"):
        ticket = self.checkout.open_ticket(request, items, key, self.recorder, self.run_id)
        self.checkout.decide(ticket.ticket_id, "approve", self.recorder, self.run_id, source="test")
        return ticket.ticket_id

    def test_checkout_without_graph_still_rejects_invalid_and_unauthorized(self):
        for name in ["unknown-product", "out-of-stock", "duplicate-stock", "specific-nut", "dairy-allergy", "budget", "under-sized"]:
            with self.subTest(name=name), self.assertRaises(PolicyBlocked):
                self.place(*fixture(name))
        for changes in [{"order_authorized": False}, {"intent": "recommendation"}, {"intent": "complaint"}]:
            with self.subTest(changes=changes), self.assertRaises(PolicyBlocked):
                self.place(*fixture(**changes))
        self.assertEqual(self.checkout.orders, ())
        self.assertEqual(self.checkout.store.inventory, Store().inventory)

    def test_duplicate_checkout_returns_receipt_without_second_stock_decrement(self):
        request, items = fixture()
        first = self.place(request, items)
        remaining = dict(self.checkout.store.inventory)
        replay = self.place(request, list(reversed(items)))
        self.assertEqual(first, replay)
        self.assertEqual(self.checkout.store.inventory, remaining)
        with self.assertRaises(PolicyBlocked):
            self.place(request, items + [LineItem(product="unknown", grams=100)])
        with self.assertRaises(PolicyBlocked):
            self.place(request.model_copy(update={"customer_id": "shivas"}), items)

    def test_approval_required_and_forged_log_does_not_authorize(self):
        request, items = fixture("manager-approval")
        self.recorder.event(self.run_id, "approval.decided", {"ticket_id": "forged", "decision": "approve"})
        with self.assertRaises(ApprovalRequired):
            self.place(request, items, ticket="forged")
        ticket = self.approve(request, items)
        receipt = self.place(request, items, ticket=ticket)
        self.assertEqual(receipt.report.subtotal_cents, 22080)
        self.assertEqual(receipt.approval_ticket_id, ticket)
        # Replay succeeds even though remaining stock is below the original demand.
        self.assertEqual(self.place(request, items, ticket=ticket), receipt)

    def test_pending_declined_and_already_decided_tickets(self):
        request, items = fixture("manager-approval")
        ticket = self.checkout.open_ticket(request, items, "order-test", self.recorder, self.run_id)
        with self.assertRaises(ApprovalRequired):
            self.place(request, items, ticket=ticket.ticket_id)
        self.checkout.decide(ticket.ticket_id, "decline", self.recorder, self.run_id)
        with self.assertRaises(ApprovalRequired):
            self.place(request, items, ticket=ticket.ticket_id)
        with self.assertRaises(PolicyBlocked):
            self.checkout.decide(ticket.ticket_id, "approve", self.recorder, self.run_id)
        self.assertEqual(self.checkout.orders, ())

    def test_approval_binds_cart_request_price_policy_and_transaction(self):
        request, items = fixture("manager-approval")
        ticket = self.approve(request, items)
        changed = [LineItem(product=i.product, grams=i.grams + (50 if n == 0 else 0)) for n, i in enumerate(items)]
        with self.assertRaises(ApprovalRequired):
            self.place(request, changed, ticket=ticket)
        with self.assertRaises(ApprovalRequired):
            self.place(request.model_copy(update={"state": "MA"}), items, ticket=ticket)
        with self.assertRaises(ApprovalRequired):
            self.place(request, items, key="another-order", ticket=ticket)
        product = self.checkout.store.products["comte"]
        self.checkout.store.products["comte"] = product.model_copy(update={"cents_per_100g": 451})
        with self.assertRaises(ApprovalRequired):
            self.place(request, items, ticket=ticket)
        self.checkout.store.products["comte"] = product
        self.checkout.store.policy = self.checkout.store.policy.model_copy(update={"version": "changed"})
        with self.assertRaises(ApprovalRequired):
            self.place(request, items, ticket=ticket)
        self.assertEqual(self.checkout.orders, ())

    def test_strictly_above_200_dollars(self):
        products = load_json("catalog.json")
        for p in products:
            p["cents_per_100g"] = 2000
        self.checkout = Checkout(Store(products=products))
        request, items = fixture(budget_cents=30000)
        receipt = self.place(request, items)
        self.assertEqual(receipt.report.subtotal_cents, 20000)
        self.assertIsNone(receipt.approval_ticket_id)
        for p in products:
            p["cents_per_100g"] = 2001
        self.checkout = Checkout(Store(products=products))
        with self.assertRaises(ApprovalRequired):
            self.place(request, items)

    def test_audit_failure_before_and_after_mutation_rolls_back(self):
        original = self.recorder.event
        for failure_event in ["policy.decision", "tool.started", "order.placing", "tool.completed", "order.placed"]:
            def record(run_id, event, payload=None, **kwargs):
                if event == failure_event:
                    raise sqlite3.OperationalError("Test audit outage")
                return original(run_id, event, payload, **kwargs)
            with self.subTest(event=failure_event), patch.object(self.recorder, "event", side_effect=record):
                with self.assertRaises(sqlite3.OperationalError):
                    self.place(*fixture())
            self.assertEqual(self.checkout.orders, ())
            self.assertEqual(self.checkout.store.inventory, Store().inventory)

    def test_policy_failure_prevents_inventory_mutation(self):
        with patch("formaggio.operations.governance.Governance.checkout", side_effect=RuntimeError("Policy unavailable")):
            with self.assertRaises(PolicyCheckError):
                self.place(*fixture())
        self.assertEqual(self.checkout.orders, ())
        self.assertEqual(self.checkout.store.inventory, Store().inventory)

    def test_failed_approval_audit_does_not_grant_permission(self):
        request, items = fixture("manager-approval")
        ticket = self.checkout.open_ticket(request, items, "order-test", self.recorder, self.run_id)
        with patch.object(self.recorder, "event", side_effect=sqlite3.OperationalError("Test outage")):
            with self.assertRaises(sqlite3.OperationalError):
                self.checkout.decide(ticket.ticket_id, "approve", self.recorder, self.run_id)
        with self.assertRaises(ApprovalRequired):
            self.place(request, items, ticket=ticket.ticket_id)


class WorkflowTests(RecordingTest):
    def execute(self, scenario="standard", **kwargs):
        return asyncio.run(run_act3(self.recorder, scenario=scenario, fixture=True, **kwargs))

    def events(self, result, name):
        return [e["payload"] for e in self.recorder.timeline(result["run_id"]) if e["event_type"] == name]

    def test_workflow_order_and_native_traces(self):
        checkout = Checkout()
        result = self.execute(checkout=checkout)
        outcome = result["outcome"]
        self.assertEqual(outcome.status, "placed")
        self.assertEqual(outcome.receipt.report.subtotal_cents, 5010)
        self.assertEqual(checkout.store.inventory["epoisses"], 600)
        self.assertEqual(result["model_calls"], 0)
        names = [e["executor_id"] for e in self.events(result, "workflow.event") if e["type"] == "executor_invoked"]
        self.assertEqual(names, ["confirm_request", "propose_cart", "validate_and_price", "select_pairings",
                                 "manager_approval", "revalidate_and_place"])
        timeline = [e["event_type"] for e in self.recorder.timeline(result["run_id"])]
        self.assertLess(timeline.index("cart.validated"), timeline.index("pairings.selected"))
        self.assertLess(timeline.index("policy.decision"), timeline.index("order.placed"))
        spans = self.recorder.query("SELECT * FROM spans WHERE run_id=?", (result["run_id"],))
        self.assertTrue(any("executor" in s["attributes_json"] or "executor" in s["name"] for s in spans))
        self.assertEqual(len({s["trace_id"] for s in spans}), 1)
        self.assertEqual(self.recorder.query("SELECT count(*) AS n FROM evaluations")[0]["n"], 0)

    def test_revision_keeps_original_constraints_and_pairings_use_final_cart(self):
        result = self.execute("out-of-stock")
        self.assertEqual(result["outcome"].attempts, 2)
        self.assertEqual(result["outcome"].status, "placed")
        reports = self.events(result, "cart.validated")
        self.assertIn("stock", [v["rule"] for v in reports[0]["violations"]])
        self.assertEqual(reports[1]["violations"], [])
        self.assertEqual({p.product for p in result["outcome"].pairings}, {"epoisses", "bucheron", "taleggio"})
        self.assertEqual(len(self.events(result, "pairings.selected")), 1)
        request_packets = [e["sources"][0]["content"] for e in self.events(result, "context.selected")]
        self.assertEqual(request_packets[0], request_packets[1])
        self.assertEqual(request_packets[1]["required_min_funk"], 4)

    def test_invalid_proposals_stop_after_initial_plus_two_revisions(self):
        for scenario in ["under-sized", "budget", "dairy-allergy"]:
            checkout = Checkout()
            result = self.execute(scenario, checkout=checkout)
            with self.subTest(scenario=scenario):
                self.assertEqual(result["outcome"].status, "unresolved")
                self.assertEqual(result["outcome"].attempts, 3)
                self.assertEqual(len(self.events(result, "cart.validated")), 3)
                self.assertEqual(self.events(result, "pairings.selected"), [])
                self.assertEqual(checkout.orders, ())

    def test_missing_details_and_complaints_never_call_proposer(self):
        for scenario, expected in [("missing-details", "clarification"), ("complaint", "escalated")]:
            result = self.execute(scenario)
            self.assertEqual(result["outcome"].status, expected)
            self.assertEqual(result["outcome"].attempts, 0)
            self.assertEqual(self.events(result, "fixture.proposal"), [])
            self.assertEqual(self.events(result, "order.placed"), [])

    def test_customer_permission_and_recommendation_paths(self):
        request, _ = fixture(order_authorized=False)
        result = self.execute(request=request)
        self.assertEqual(result["outcome"].status, "clarification")
        result = self.execute(request=request.model_copy(update={"intent": "recommendation"}))
        self.assertEqual(result["outcome"].status, "recommendation")
        self.assertTrue(result["outcome"].report.ok)
        self.assertEqual(self.events(result, "order.placed"), [])

    def test_manager_pause_resume_and_decline(self):
        for decision in ["approve", "decline"]:
            checkout = Checkout()
            async def manager(ticket):
                self.assertEqual(ticket.decision, "pending")
                self.assertEqual(checkout.orders, ())
                self.assertEqual(checkout.store.inventory, Store().inventory)
                return decision
            result = self.execute("manager-approval", manager=manager, checkout=checkout, decision_source="test")
            self.assertEqual(result["outcome"].status, "placed" if decision == "approve" else "declined")
            self.assertEqual(len(self.events(result, "approval.requested")), 1)
            self.assertEqual(self.events(result, "approval.decided")[0]["source"], "test")
            self.assertTrue(any(e["type"] == "request_info" for e in self.events(result, "workflow.event")))

    def test_stock_rechecked_after_manager_pause(self):
        checkout = Checkout()
        async def manager(ticket):
            # Another authorized checkout consumes the last available stock.
            competing = checkout.open_ticket(ticket.request, list(ticket.report.items), "competing", self.recorder, self.run_id)
            checkout.decide(competing.ticket_id, "approve", self.recorder, self.run_id, source="test")
            checkout.place(ticket.request, list(ticket.report.items), "competing", self.recorder, self.run_id, ticket_id=competing.ticket_id)
            return "approve"
        result = self.execute("manager-approval", manager=manager, checkout=checkout)
        self.assertEqual(result["outcome"].status, "blocked")
        self.assertIn("stock", [v.rule for v in result["outcome"].report.violations])
        self.assertEqual(len(checkout.orders), 1)  # Only the competing order.
        self.assertEqual(self.events(result, "order.placed"), [])

    def test_two_pending_approvals_have_unique_tickets_and_cannot_oversell(self):
        checkout, tickets = Checkout(), []
        async def execute_both():
            ready = asyncio.Event()
            async def manager(ticket):
                tickets.append(ticket)
                if len(tickets) == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=5)
                return "approve"
            return await asyncio.gather(*(run_act3(self.recorder, scenario="manager-approval", fixture=True,
                checkout=checkout, manager=manager, decision_source="test") for _ in range(2)))
        results = asyncio.run(execute_both())
        self.assertEqual(len({t.ticket_id for t in tickets}), 2)
        self.assertEqual(len({t.checkout_key for t in tickets}), 2)
        self.assertEqual(sorted(r["outcome"].status for r in results), ["blocked", "placed"])
        self.assertEqual(len(checkout.orders), 1)

    def test_bad_manager_input_and_model_failure_mark_run_error(self):
        async def invalid_manager(ticket):
            return "the model says yes"
        with self.assertRaises(Exception):
            self.execute("manager-approval", manager=invalid_manager)
        async def failed_model(state, packet):
            raise RuntimeError("Test provider unavailable")
        with self.assertRaises(Exception):
            asyncio.run(run_act3(self.recorder, proposer=failed_model, execution_mode="fixture"))
        statuses = self.recorder.query("SELECT status FROM runs WHERE act=3")
        self.assertEqual(statuses, [{"status": "error"}, {"status": "error"}])
        self.assertEqual(self.recorder.query("SELECT count(*) AS n FROM events WHERE event_type='order.placed'")[0]["n"], 0)

    def test_real_agent_sdk_receives_feedback_and_cannot_approve_or_place(self):
        requests = []
        _, valid = fixture()
        def backend(request):
            body = json.loads(request.content)
            requests.append(body)
            items = [] if len(requests) == 1 else [i.model_dump() for i in valid]
            return httpx.Response(200, json={"id": f"resp_{len(requests)}", "object": "response", "created_at": 1,
                "model": "fixture-model", "status": "completed", "output": [{"type": "message", "id": "msg_fixture",
                    "role": "assistant", "status": "completed", "content": [{"type": "output_text",
                    "text": json.dumps({"items": items}), "annotations": []}]}],
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                          "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}})
        async def execute():
            async with AsyncOpenAI(api_key="unit-test-credential", base_url="https://fixture.invalid/v1", max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(backend))) as api:
                return await run_act3(self.recorder, api_client=api, execution_mode="fixture", model="fixture-model")
        result = asyncio.run(execute())
        self.assertEqual(result["outcome"].status, "placed")
        self.assertEqual(result["model_calls"], 2)
        self.assertFalse(requests[0].get("tools"))
        self.assertEqual(requests[0]["text"]["format"]["schema"]["properties"].keys(), {"items"})
        self.assertIn("previous_proposal", json.dumps(requests[1]))
        self.assertIn("portions", json.dumps(requests[1]))
        self.assertEqual(len(self.events(result, "model.request")), 2)
        self.assertNotIn("unit-test-credential", json.dumps(self.recorder.timeline(result["run_id"])))

