"""Host-owned workflow handles exposed through narrow harness tools.

One handle per confirmed request prevents accidental duplicate orders. Pending
SDK workflows stay in memory. Only the host can resume them with a decision.
"""
from dataclasses import dataclass, field
from uuid import uuid4

from opentelemetry import trace

from act3_workflow import build_workflow
from formaggio.shop.data_models import ApprovalTicket, WorkflowOutcome, WorkflowState
from formaggio.operations.governance import Governance, PolicyBlocked


@dataclass
class OrderHandle:
    request_id: str
    workflow: object
    checkout_key: str = field(default_factory=lambda: uuid4().hex)
    pending: dict = field(default_factory=dict)
    outcome: WorkflowOutcome | None = None
    origin: object = None
    failed: bool = False


class WorkflowOrders:
    def __init__(self, checkout, requests, proposers, recorder, run_id, *, progress=None, decision_source="human"):
        if not 1 <= len(requests) <= 2 or set(requests) != set(proposers):
            raise ValueError("Provide one or two confirmed requests and matching proposers.")
        if len({r.customer_id for r in requests.values()}) != 1:
            raise ValueError("One harness session must remain scoped to one customer.")
        self.checkout, self.requests, self.proposers = checkout, dict(requests), dict(proposers)
        self.recorder, self.run_id, self.progress = recorder, run_id, progress
        self.decision_source, self.handles = decision_source, {}

    def scoped(self, request_id):
        gate = Governance(self.recorder, self.run_id)
        allowed = request_id in self.requests
        decision = gate.decide("confirmed_request_scope", "workflow_tool", "allow" if allowed else "block",
                               "Use the host-confirmed request." if allowed else "Unknown request in this session.")
        self.recorder.event(self.run_id, "policy.decision", decision.model_dump())
        if not allowed:
            raise PolicyBlocked(decision.reason)

    def view(self, request_id):
        self.scoped(request_id)
        handle = self.handles.get(request_id)
        if handle is None:
            return {"request_id": request_id, "status": "not_started", "order_placed": False}
        value = {"request_id": request_id, "workflow_id": handle.checkout_key,
                 "order_placed": bool(handle.outcome and handle.outcome.receipt)}
        if handle.failed:
            return {**value, "status": "error", "message": "Workflow failed; inspect recorded events. Do not retry this handle."}
        if handle.outcome:
            return {**value, **handle.outcome.model_dump(mode="json")}
        return {**value, "status": "pending_approval", "order_placed": False,
                "tickets": [t.model_dump(mode="json") for t in handle.pending.values()],
                "message": "Return control to the host for manager review. No order has been placed."}

    async def advance(self, handle, stream):
        pending, outcomes = {}, []
        try:
            async for event in stream:
                self.recorder.event(self.run_id, "workflow.event", {"workflow_id": handle.checkout_key,
                    "request_id": handle.request_id, "type": event.type,
                    "executor_id": event.executor_id if event.type.startswith("executor_") else None,
                    "response_id": event.request_id if event.type == "request_info" else None})
                if event.type == "request_info":
                    ticket = ApprovalTicket.model_validate(event.data)
                    if ticket.checkout_key != handle.checkout_key or ticket.request != self.requests[handle.request_id]:
                        raise PolicyBlocked("Workflow returned a ticket for a different request.")
                    pending[event.request_id] = ticket
                elif event.type == "output":
                    outcomes.append(WorkflowOutcome.model_validate(event.data))
            if (len(outcomes), len(pending)) not in {(1, 0), (0, 1)}:
                raise RuntimeError("Workflow must return one outcome or one pending manager request.")
            handle.pending = pending
            handle.outcome = outcomes[0] if outcomes else None
            value = self.view(handle.request_id)
            self.recorder.event(self.run_id, "composition.order_state", value)
            if self.progress:
                self.progress(f"Order {handle.request_id}: {value['status']}")
            return value
        except BaseException:
            handle.failed = True
            raise

    async def start(self, request_id):
        self.scoped(request_id)
        if request_id in self.handles:
            value = self.view(request_id)
            self.recorder.event(self.run_id, "composition.replayed", value)
            return value
        workflow = build_workflow(self.checkout, self.recorder, self.run_id, self.proposers[request_id],
                                  progress=self.progress, decision_source=self.decision_source)
        handle = OrderHandle(request_id, workflow)
        with self.recorder.span(self.run_id, "workflow.order", "workflow") as span:
            handle.origin = span.get_span_context()
            span.set_attribute("formaggio.workflow_id", handle.checkout_key)
            span.set_attribute("formaggio.request_id", request_id)
            self.recorder.event(self.run_id, "composition.started", {"request_id": request_id,
                "workflow_id": handle.checkout_key, "request": self.requests[request_id].model_dump(mode="json")})
            self.handles[request_id] = handle
            state = WorkflowState(request=self.requests[request_id], checkout_key=handle.checkout_key)
            return await self.advance(handle, workflow.run(state, stream=True))

    def pending_reviews(self):
        return [(h.request_id, response_id, ticket) for h in self.handles.values() if not h.failed
                for response_id, ticket in h.pending.items()]

    async def resume(self, request_id, response_id, ticket_id, decision):
        """Host API only: none of these approval parameters are model tool arguments."""
        self.scoped(request_id)
        handle = self.handles.get(request_id)
        if handle is None or handle.failed or handle.outcome or response_id not in handle.pending:
            raise PolicyBlocked("No matching pending workflow review.")
        ticket = handle.pending[response_id]
        if ticket.ticket_id != ticket_id or decision not in {"approve", "decline"}:
            raise PolicyBlocked("Decision does not match this pending ticket.")
        # Resume under the original workflow span, even after its tool call ended.
        with trace.use_span(trace.NonRecordingSpan(handle.origin), end_on_exit=False):
            with self.recorder.span(self.run_id, "workflow.resume", "workflow") as span:
                span.set_attribute("formaggio.workflow_id", handle.checkout_key)
                self.recorder.event(self.run_id, "composition.resuming", {"request_id": request_id,
                    "workflow_id": handle.checkout_key, "response_id": response_id, "ticket_id": ticket_id})
                return await self.advance(handle, handle.workflow.run(stream=True, responses={response_id: decision}))

    def assessment(self, request_id):
        """Follow-up tasting context comes from the accepted receipt, not a new cart."""
        value = self.view(request_id)
        handle = self.handles.get(request_id)
        if not handle or not handle.outcome or handle.outcome.status not in {"placed", "recommendation"}:
            return {"request_id": request_id, "status": value["status"], "message": "No accepted menu to present yet."}
        outcome = handle.outcome
        return {"request_id": request_id, "status": outcome.status, "report": outcome.report.model_dump(mode="json"),
                "products": [self.checkout.store.products[i.product].model_dump(mode="json", exclude={"stock_g"})
                             for i in outcome.report.items],
                "conditional_pairings": {p.product: [s.model_dump(mode="json") for s in p.suggestions] for p in outcome.pairings},
                "scope": "Accepted menu; receipt quantities and prices. Pairing costs, tax and shipping excluded."}
