"""Typed Act 3 steps. Validation, approval and checkout are mandatory graph nodes."""
from agent_framework import Executor, WorkflowContext, handler, response_handler

from formaggio.agents.context import build_context, customer_ask
from formaggio.operations.workflow_view import proposal_details, proposal_lines
from formaggio.shop.data_models import ApprovalTicket, CartProposal, WorkflowOutcome, WorkflowState
from formaggio.operations.governance import PolicyBlocked, PolicyCheckError


class ShopStep(Executor):
    """Record entry synchronously; streamed SDK events may arrive after a handler."""
    def __init__(self, id, checkout, recorder, run_id, progress=None):
        super().__init__(id=id)
        self.checkout, self.recorder, self.run_id, self.progress = checkout, recorder, run_id, progress

    def enter(self):
        self.recorder.event(self.run_id, "workflow.step", {"executor_id": self.id})
        if self.progress:
            self.progress("Step: " + self.id)


class ConfirmRequest(ShopStep):

    @handler
    async def run(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState, WorkflowOutcome]):
        if self.progress:
            self.progress("\n👤 Customer ask:\n" + self.recorder.redactor.clean(customer_ask(state.request)))
        self.enter()
        if state.request.intent == "complaint":
            await ctx.yield_output(WorkflowOutcome(status="escalated",
                message="Mock escalation recorded for a staff member; no order placed."))
            return
        issues = self.checkout.store.request_violations(state.request)
        details = [v.detail for v in issues]
        if state.request.intent == "order" and not state.request.order_authorized:
            details.append("Confirm customer authorization to place an order.")
        if details:
            await ctx.yield_output(WorkflowOutcome(status="clarification", message=" ".join(details)))
            return
        await ctx.send_message(state)


class ProposeCart(ShopStep):
    def __init__(self, proposer, **kwargs):
        super().__init__(id="propose_cart", **kwargs)
        self.proposer = proposer

    @handler
    async def run(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState, WorkflowOutcome]):
        self.enter()
        packet = build_context(state.request, self.checkout.store, "enriched")
        proposal = CartProposal.model_validate(await self.proposer(state, packet))
        details = {"attempt": state.attempts + 1, **proposal_details(self.checkout.store, proposal.items, state.items)}
        self.recorder.event(self.run_id, "cart.proposed", details)
        if self.progress:
            for line in proposal_lines(details):
                self.progress(self.recorder.redactor.clean(line))
        await ctx.send_message(state.model_copy(update={"items": tuple(proposal.items),
            "attempts": state.attempts + 1, "pairings": (), "ticket_id": None}))


class ValidateCart(ShopStep):
    @handler
    async def run(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState, WorkflowOutcome]):
        self.enter()
        report = self.checkout.store.validate(state.request, list(state.items))
        self.recorder.event(self.run_id, "cart.validated", {"attempt": state.attempts, **report.model_dump(mode="json")})
        if self.progress:
            self.progress(f"Cart attempt {state.attempts}: " + (f"valid; ${report.subtotal_cents / 100:.2f}" if report.ok
                          else "; ".join(f"{v.rule}: {v.product_id + ' — ' if v.product_id else ''}{v.detail}" for v in report.violations)))
            products = [self.checkout.store.resolve(item.product) for item in state.items]
            destination = (state.request.state or "").strip().upper()
            if (destination in self.checkout.store.policy.raw_milk_blocked_states
                    and products and all(p is not None for p in products)
                    and not any(v.rule == "shipping" for v in report.violations)):
                self.progress(f"  ✅ Shipping policy for {destination}: passed; no raw-milk cheese in this proposal.")
            if not report.ok:
                action = ("Revision limit reached; no order will be placed." if state.attempts >= 1 + self.checkout.store.policy.max_revisions
                          else "Cart rejected; requesting a revised proposal before checkout.")
                self.progress("  🛡️ " + action)
        if not report.ok and state.attempts >= 1 + self.checkout.store.policy.max_revisions:
            explanation = self.checkout.store.catalog_allergy_conflict(state.request)
            await ctx.yield_output(WorkflowOutcome(status="unresolved", attempts=state.attempts, report=report,
                message=explanation or "No valid cart obtained within the revision limit. Ask staff to review the request; no order placed."))
            return
        # Preserve the raw proposal on failures so unknown lines cannot vanish from feedback.
        await ctx.send_message(state.model_copy(update={"report": report,
            "items": report.items if report.ok else state.items}))


class SelectPairings(ShopStep):
    @handler
    async def run(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState, WorkflowOutcome]):
        self.enter()
        pairings = self.checkout.pairings(state.request, list(state.items))
        self.recorder.event(self.run_id, "pairings.selected", {"pairings": [p.model_dump(mode="json") for p in pairings]})
        if state.request.intent == "recommendation":
            await ctx.yield_output(WorkflowOutcome(status="recommendation", attempts=state.attempts,
                message="Validated recommendation only; no order placed.", report=state.report, pairings=pairings))
            return
        await ctx.send_message(state.model_copy(update={"pairings": pairings}))


class ManagerApproval(ShopStep):
    def __init__(self, decision_source, **kwargs):
        super().__init__(id="manager_approval", **kwargs)
        self.decision_source = decision_source

    @handler
    async def run(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState, WorkflowOutcome]):
        self.enter()
        if not state.report.needs_manager_approval:
            self.recorder.event(self.run_id, "approval.not_required", {"subtotal_cents": state.report.subtotal_cents})
            await ctx.send_message(state)
            return
        ticket = self.checkout.open_ticket(state.request, list(state.items), state.checkout_key, self.recorder, self.run_id)
        ctx.set_state("pending", state.model_copy(update={"ticket_id": ticket.ticket_id}).model_dump(mode="json"))
        # SDK pauses here. The external host supplies the response; the agent cannot.
        await ctx.request_info(request_data=ticket, response_type=str)

    @response_handler
    async def resume(self, ticket: ApprovalTicket, decision: str, ctx: WorkflowContext[WorkflowState, WorkflowOutcome]):
        self.enter()
        state = WorkflowState.model_validate(ctx.get_state("pending"))
        if state.ticket_id != ticket.ticket_id:
            raise PolicyBlocked("Approval response does not match the pending workflow ticket.")
        self.checkout.decide(ticket.ticket_id, decision, self.recorder, self.run_id, source=self.decision_source)
        if decision == "decline":
            await ctx.yield_output(WorkflowOutcome(status="declined", attempts=state.attempts, report=state.report,
                message="Manager declined this cart. No order placed."))
            return
        await ctx.send_message(state)


class PlaceOrder(ShopStep):
    @handler
    async def run(self, state: WorkflowState, ctx: WorkflowContext[WorkflowState, WorkflowOutcome]):
        self.enter()
        try:
            receipt = self.checkout.place(state.request, list(state.items), state.checkout_key,
                self.recorder, self.run_id, ticket_id=state.ticket_id)
        except PolicyCheckError:
            raise  # An unavailable policy is an error, not an expected business rejection.
        except PolicyBlocked as exc:
            report = self.checkout.store.validate(state.request, list(state.items))
            await ctx.yield_output(WorkflowOutcome(status="blocked", attempts=state.attempts, report=report,
                message=f"Final checkout blocked: {exc} No order placed."))
            return
        await ctx.yield_output(WorkflowOutcome(status="placed", attempts=state.attempts, receipt=receipt,
            report=receipt.report, pairings=receipt.pairings, message="Mock order placed. No purchase or email was sent."))
