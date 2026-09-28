"""Act 3 execution adapter. Graph definition and mandatory checks live separately."""
import asyncio
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass, field, replace
import json
from typing import Callable
from uuid import uuid4

from agent_framework import Agent
from openai import AsyncOpenAI

from formaggio.agents.context import ShopContextProvider, build_context, customer_message, load_scenario
from formaggio.agents.execution import ExecutionState, ModelControl, RecordedExecution
from formaggio.agents.runtime import MODEL_OPTIONS, console_progress, make_client, run_snapshot
from formaggio.agents.workflow_layers import WorkflowBudget, WorkflowTrace
from formaggio.config import MODEL, ROOT, load_json
from formaggio.shop.checkout import Checkout
from formaggio.shop.data_models import CartProposal, WorkflowOutcome, WorkflowState


class AgentProposer:
    """A fresh proposal session for each revision; approval never enters its tool set.

    Act adapters supply invocation-local controls and measure active time at
    their execution boundary. A caller may also cap each proposal individually.
    """
    def __init__(self, recorder, run_id, prompt, model, *, api_client=None, progress=None, execution_state, proposal_timeout=None):
        self.recorder, self.run_id, self.prompt, self.model = recorder, run_id, prompt, model
        self.api_client, self.api, self.owned, self.client = api_client, None, False, None
        self.execution_state = execution_state
        self.proposal_timeout = proposal_timeout

    async def __call__(self, state, packet):
        if self.client is None:
            self.owned = self.api_client is None
            self.api = AsyncOpenAI(timeout=45, max_retries=0) if self.owned else self.api_client
            middleware = self.execution_state.middleware
            limits = self.execution_state.limits
            self.client = make_client(self.model, middleware, self.api, function_limits=limits)
        agent = Agent(client=self.client, name="CartProposer", instructions=self.prompt,
            context_providers=[ShopContextProvider(packet, self.recorder, self.run_id)],
            default_options={**MODEL_OPTIONS, "response_format": CartProposal})
        message = customer_message(state.request)
        if state.report:
            feedback = {"previous_proposal": [i.model_dump() for i in state.items],
                        "validation": state.report.model_dump(mode="json")}
            message += "\nRevise the complete cart using this validation feedback:\n" + json.dumps(feedback)
        async with asyncio.timeout(self.proposal_timeout):
            result = await agent.run(message, session=agent.create_session())
        return result.value if isinstance(result.value, CartProposal) else CartProposal.model_validate_json(result.text)

    async def close(self):
        if self.owned and self.api is not None:
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


async def drive_workflow(workflow, state, recorder, run_id, manager, *, progress=None, active_budget=None):
    """Measure each active segment; host approval waits do not consume the budget."""
    outcomes, responses = [], None
    while True:
        pending = []
        async with active_budget.measure() if active_budget is not None else nullcontext():
            stream = (workflow.run(state, stream=True) if responses is None
                      else workflow.run(stream=True, responses=responses))
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
    if len(outcomes) != 1:
        raise RuntimeError(f"Workflow ended without exactly one authoritative outcome ({len(outcomes)}).")
    recorder.event(run_id, "workflow.result", outcomes[0].model_dump(mode="json"))
    return outcomes[0]


@dataclass(kw_only=True)
class WorkflowRun(ExecutionState):
    recorder: object
    run_id: str | None
    model: str
    progress: object
    middleware: list = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowHarness:
    """Reusable configuration; graphs, proposers, timers and clients are local to run().

    An explicitly supplied checkout may be shared by a caller coordinating orders.
    Injected clients and proposers remain owned by that caller.
    """
    workflow_factory: Callable
    scenario: str = "standard"
    model: str = MODEL
    fixture: bool = False
    decision_source: str = "human"
    execution_mode: str = "live"
    prompt: str | None = None
    layers: tuple = ()

    def __post_init__(self):
        if self.execution_mode not in {"live", "fixture"}:
            raise ValueError("Unsupported execution mode.")
        if any(not isinstance(layer, (WorkflowTrace, WorkflowBudget)) for layer in self.layers):
            raise TypeError("Act 3 supports WorkflowTrace and WorkflowBudget layers.")
        if len({layer.name for layer in self.layers}) != len(self.layers):
            raise ValueError("Use only one layer of each name.")

    def add(self, layer):
        return replace(self, layers=(*self.layers, layer))

    def without(self, name):
        if not any(layer.name == name for layer in self.layers):
            raise ValueError(f"No layer named {name!r}.")
        return replace(self, layers=tuple(layer for layer in self.layers if layer.name != name))

    async def run(self, recorder, *, manager=None, progress=None, checkout=None,
                  request=None, proposer=None, api_client=None):
        if proposer is not None and self.execution_mode != "fixture":
            raise ValueError("Injected proposers must be labeled fixture mode.")
        if self.execution_mode == "fixture" and not self.fixture and proposer is None and api_client is None:
            raise ValueError("Fixture execution requires fixed proposals, an injected proposer, or a local test client.")
        checkout = checkout or Checkout()
        request = request or load_scenario(self.scenario)
        progress = console_progress(progress)
        if progress:
            progress(f"\n🧪 Scenario: {self.scenario} · {'SCRIPTED proposals' if self.fixture else 'LIVE proposals' if self.execution_mode == 'live' else 'LOCAL test proposals'}")
            if not self.fixture and self.execution_mode == "live" and self.scenario in {"pa-shipping", "pa-shipping-blocked"}:
                progress("ℹ️ The live model chooses its cart; this scenario does not force raw milk. Use --fixture to demonstrate the scripted shipping rejection.")
        prompt = (ROOT / "prompts/cart_proposer.md").read_text(encoding="utf-8") if self.prompt is None else self.prompt
        prompt += "\nAuthoritative classroom shop policy:\n" + checkout.store.policy.model_dump_json()
        proposals = None
        if self.fixture:
            fixtures = load_json("workflow_proposals.json")
            if self.scenario not in fixtures:
                raise ValueError(f"No workflow fixture for {self.scenario}. Available: {', '.join(fixtures)}")
            proposals = fixtures[self.scenario]
        mode = "fixture" if self.fixture else self.execution_mode
        run = WorkflowRun(recorder=recorder, run_id=None, model=self.model, progress=progress)
        budget = next((layer for layer in self.layers if isinstance(layer, WorkflowBudget)), None)
        if budget is not None:
            budget.configure(run)
        snapshot = run_snapshot(self.model, "enriched", prompt, request, build_context(request, checkout.store, "enriched"), [])
        snapshot.update(reply_schema=CartProposal.model_json_schema(), workflow="act3",
                        fixture_proposals=proposals, decision_source=self.decision_source,
                        layers=[layer.configuration() for layer in self.layers], function_limits=dict(run.limits),
                        max_model_calls=budget.model_calls if budget else None,
                        max_active_seconds=budget.seconds if budget else None)
        async with RecordedExecution(recorder, snapshot, case_id=self.scenario, act=3,
                                     model="none" if self.fixture or proposer else self.model, mode=mode) as execution:
            run.run_id = execution.run_id
            run.middleware.append(ModelControl(run))
            for layer in self.layers:
                if layer is not budget:
                    layer.configure(run)
            if self.fixture:
                proposer = FixtureProposer(proposals, recorder, run.run_id)
            elif proposer is None:
                proposer = execution.own(AgentProposer(recorder, run.run_id, prompt, self.model,
                    api_client=api_client, progress=progress, execution_state=run))
            workflow = self.workflow_factory(checkout, recorder, run.run_id, proposer,
                                             progress=progress, decision_source=self.decision_source)
            state = WorkflowState(request=request, checkout_key=uuid4().hex)
            recorder.event(run.run_id, "request.brief", {"source": "structured_input", "request": request.model_dump(mode="json")})
            with ExitStack() as scopes:
                for layer in self.layers:
                    if isinstance(layer, WorkflowTrace):
                        scopes.enter_context(layer.scope(run))
                outcome = await drive_workflow(workflow, state, recorder, run.run_id, manager,
                                               progress=progress, active_budget=run.active_budget)
            execution.set_outcome("blocked" if outcome.status == "blocked" else "completed")
            return {"run_id": run.run_id, "outcome": outcome, "mode": mode, "model_calls": run.model_calls}
