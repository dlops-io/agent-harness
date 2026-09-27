"""Act 3: one proposing agent inside a typed, code-controlled workflow.

Read top to bottom: input gate, proposal, validation/revision, pairings, approval,
placement, then the CLI/notebook driver. No model decides the next workflow step.
"""
import asyncio
import json
from uuid import uuid4

from agent_framework import Agent, Executor, WorkflowBuilder, WorkflowContext, handler, response_handler

from formaggio.shop.checkout import Checkout
from formaggio.config import MODEL, ROOT, load_json
from formaggio.agents.context import ShopContextProvider, build_context, customer_message, load_scenario
from formaggio.shop.data_models import ApprovalTicket, CartProposal, WorkflowOutcome, WorkflowState
from formaggio.operations.governance import PolicyBlocked, PolicyCheckError
from formaggio.operations.observability import sdk_tracing
from formaggio.agents.runtime import MODEL_OPTIONS, ModelTrace, make_client, run_snapshot


# %% Typed graph nodes. Terminal paths yield an outcome instead of sending a cart.
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
                          else "; ".join(f"{v.rule}: {v.detail}" for v in report.violations)))
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


def build_workflow(checkout, recorder, run_id, proposer, *, progress=None, decision_source="human"):
    common = {"checkout": checkout, "recorder": recorder, "run_id": run_id, "progress": progress}
    confirm = ConfirmRequest(id="confirm_request", **common)
    propose = ProposeCart(proposer, **common)
    validate = ValidateCart(id="validate_and_price", **common)
    pair = SelectPairings(id="select_pairings", **common)
    approve = ManagerApproval(decision_source, **common)
    place = PlaceOrder(id="revalidate_and_place", **common)
    return (WorkflowBuilder(start_executor=confirm, name="formaggio-order", output_from="all",
                            max_iterations=12 + 2 * checkout.store.policy.max_revisions)
        .add_edge(confirm, propose)
        .add_edge(propose, validate)
        .add_edge(validate, propose, condition=lambda state: not state.report.ok)
        .add_edge(validate, pair, condition=lambda state: state.report.ok)
        .add_edge(pair, approve)
        .add_edge(approve, place)
        .build())


# %% The model sees current context and structured feedback, not approval tools.
class AgentProposer:
    def __init__(self, recorder, run_id, prompt, model, *, api_client=None, progress=None):
        self.recorder, self.run_id, self.prompt, self.model = recorder, run_id, prompt, model
        self.api_client, self.api, self.owned, self.client = api_client, None, False, None
        self.trace = ModelTrace(recorder, run_id, progress, model=model, label="Cart proposer")

    async def __call__(self, state, packet):
        if self.client is None:
            self.client, self.api, self.owned = make_client(self.model, [self.trace], self.api_client)
        agent = Agent(client=self.client, name="CartProposer", instructions=self.prompt,
            context_providers=[ShopContextProvider(packet, self.recorder, self.run_id)],
            default_options={**MODEL_OPTIONS, "response_format": CartProposal})
        message = customer_message(state.request)
        if state.report:
            feedback = {"previous_proposal": [i.model_dump() for i in state.items],
                        "validation": state.report.model_dump(mode="json")}
            message += "\nRevise the complete cart using this validation feedback:\n" + json.dumps(feedback)
        async with asyncio.timeout(120):
            result = await agent.run(message, session=agent.create_session())
        return result.value if isinstance(result.value, CartProposal) else CartProposal.model_validate_json(result.text)

    async def close(self):
        if self.owned and self.api:
            await self.api.close()


class FixtureProposer:
    """Deterministic classroom input; never presented as an LLM response."""
    def __init__(self, proposals, recorder, run_id):
        self.proposals, self.recorder, self.run_id = proposals, recorder, run_id

    async def __call__(self, state, packet):
        self.recorder.event(self.run_id, "context.selected", packet)
        proposal = CartProposal.model_validate({"items": self.proposals[min(state.attempts, len(self.proposals) - 1)]})
        self.recorder.event(self.run_id, "fixture.proposal", {"attempt": state.attempts + 1, **proposal.model_dump()})
        return proposal


# %% Host driver: works with `await` in a future notebook, no CLI input inside nodes.
async def drive_workflow(workflow, state, recorder, run_id, manager, *, progress=None):
    stream = workflow.run(state, stream=True)
    outcomes = []
    while True:
        pending = []
        async for event in stream:
            recorder.event(run_id, "workflow.event", {"type": event.type,
                "executor_id": event.executor_id if event.type.startswith("executor_") else None,
                "request_id": event.request_id if event.type == "request_info" else None})
            if event.type == "request_info":
                pending.append((event.request_id, event.data))
            elif event.type == "output":
                outcomes.append(WorkflowOutcome.model_validate(event.data))
        if not pending:
            break
        responses = {}
        for request_id, ticket in pending:
            if manager is None:
                raise RuntimeError("An external manager callback is required to resume this order.")
            responses[request_id] = await manager(ticket)
        stream = workflow.run(stream=True, responses=responses)
    if len(outcomes) != 1:
        raise RuntimeError(f"Workflow ended without exactly one authoritative outcome ({len(outcomes)}).")
    recorder.event(run_id, "workflow.result", outcomes[0].model_dump(mode="json"))
    return outcomes[0]


async def run_act3(recorder, *, scenario="standard", model=MODEL, fixture=False,
                   manager=None, decision_source="human", progress=None, checkout=None,
                   request=None, proposer=None, api_client=None, execution_mode="live", prompt=None):
    """Injected proposers/clients are for offline tests; normal CLI runs use the model."""
    if execution_mode not in {"live", "fixture"}:
        raise ValueError("Unsupported execution mode.")
    if proposer is not None and execution_mode != "fixture":
        raise ValueError("Injected proposers must be labeled fixture mode.")
    if execution_mode == "fixture" and not fixture and proposer is None and api_client is None:
        raise ValueError("Fixture execution requires fixed proposals, an injected proposer, or a local test client.")
    checkout = checkout or Checkout()
    request = request or load_scenario(scenario)
    prompt = (ROOT / "prompts/cart_proposer.md").read_text(encoding="utf-8") if prompt is None else prompt
    prompt += "\nAuthoritative classroom shop policy:\n" + checkout.store.policy.model_dump_json()
    proposals = None
    if fixture:
        fixtures = load_json("workflow_proposals.json")
        if scenario not in fixtures:
            raise ValueError(f"No workflow fixture for {scenario}. Available: {', '.join(fixtures)}")
        proposals = fixtures[scenario]
    mode = "fixture" if fixture else execution_mode
    snapshot = run_snapshot(model, "enriched", prompt, request, build_context(request, checkout.store, "enriched"), [])
    snapshot.update({"reply_schema": CartProposal.model_json_schema(), "workflow": "act3",
                     "fixture_proposals": proposals, "decision_source": decision_source})
    version_id = recorder.version(snapshot)
    run_id = recorder.start_run(version_id, case_id=scenario, act=3, model="none" if fixture or proposer else model, mode=mode)
    agent = None
    try:
        if fixture:
            proposer = FixtureProposer(proposals, recorder, run_id)
        elif proposer is None:
            agent = AgentProposer(recorder, run_id, prompt, model, api_client=api_client, progress=progress)
            proposer = agent
        workflow = build_workflow(checkout, recorder, run_id, proposer, progress=progress, decision_source=decision_source)
        state = WorkflowState(request=request, checkout_key=uuid4().hex)
        recorder.event(run_id, "request.brief", {"source": "structured_input", "request": request.model_dump(mode="json")})
        with recorder.span(run_id, "workflow.order", "workflow"), sdk_tracing(recorder):
            outcome = await drive_workflow(workflow, state, recorder, run_id, manager, progress=progress)
        recorder.finish_run(run_id, "blocked" if outcome.status == "blocked" else "completed")
        return {"run_id": run_id, "outcome": outcome, "mode": mode, "model_calls": agent.trace.calls if agent else 0}
    except (TimeoutError, asyncio.CancelledError):
        recorder.finish_run(run_id, "stopped", "Run timed out or was cancelled.")
        raise
    except Exception as exc:
        recorder.finish_run(run_id, "error", str(exc))
        raise
    finally:
        if agent:
            await agent.close()
