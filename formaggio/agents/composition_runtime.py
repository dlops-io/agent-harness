"""Invocation-local composition runtime; domain workflow gates are never layers."""
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Callable

from agent_framework import ContextWindowCompactionStrategy, TodoProvider
from openai import AsyncOpenAI

from formaggio.agents.composition import WorkflowOrders
from formaggio.agents.composition_layers import CompositionBudget, CompositionTrace
from formaggio.agents.composition_review import run_with_workflow_reviews
from formaggio.agents.prompt_template import render_prompt
from formaggio.agents.execution import ExecutionState, ModelControl, RecordedExecution, recorded_trace
from formaggio.agents.harness_state import ApplicationContext, HarnessHistory
from formaggio.agents.planner_layers import Compaction, Planning
from formaggio.agents.planner_tools import HarnessToolTrace, RecordedTodos, TODO_NAMES
from formaggio.agents.runtime import CONTEXT_WINDOW_TOKENS, MAX_OUTPUT_TOKENS, ModelTrace, console_progress, make_client, run_snapshot
from formaggio.agents.skill_layer import Skills
from formaggio.agents.skill_support import SKILL_TOOLS
from formaggio.agents.workflow_runtime import AgentProposer, FixtureProposer, WorkflowRun
from formaggio.agents.tasting_delivery import DELIVERY_INSTRUCTIONS, deliver_tasting_reply, tasting_reply_schema
from formaggio.config import MODEL, ROOT, load_json
from formaggio.shop.checkout import Checkout


@dataclass(frozen=True)
class CompositionHarness:
    """Reusable configuration. Each run owns its sessions, handles and counters.

    A caller may explicitly share a checkout or lend API clients. Otherwise each
    invocation gets a fresh shop; only resources allocated here are closed here.
    """
    agent_factory: Callable
    tool_factory: Callable
    request_loader: Callable
    scenario: str = "standard"
    model: str = MODEL
    fixture: bool = False
    decision_source: str = "human"
    execution_mode: str = "live"
    prompt: str | None = None
    proposer_prompt: str | None = None
    layers: tuple = ()

    def __post_init__(self):
        if self.execution_mode not in {"live", "fixture"}:
            raise ValueError("Unsupported execution mode.")
        if any(not isinstance(layer, (CompositionTrace, CompositionBudget, Planning, Compaction, Skills)) for layer in self.layers):
            raise TypeError("Unsupported composition layer.")
        if len({layer.name for layer in self.layers}) != len(self.layers):
            raise ValueError("Use only one layer of each name.")

    def add(self, layer):
        return replace(self, layers=(*self.layers, layer))

    def without(self, name):
        if not any(layer.name == name for layer in self.layers):
            raise ValueError(f"No layer named {name!r}.")
        return replace(self, layers=tuple(layer for layer in self.layers if layer.name != name))

    async def run(self, recorder, *, manager=None, progress=None, api_client=None,
                  proposer_api_client=None, checkout=None):
        if self.execution_mode == "fixture" and api_client is None and not self.fixture:
            raise ValueError("Fixture mode needs a local outer client or --fixture.")
        mode = "fixture" if self.fixture else self.execution_mode
        features = {layer.name: layer for layer in self.layers}
        budget, tracing = features.get("budget"), "trace" in features
        files = features["skills"].snapshot() if "skills" in features else None
        requests = self.request_loader(self.scenario)
        checkout = checkout if checkout is not None else Checkout()
        reply_schema = tasting_reply_schema(checkout.store)
        progress = console_progress(progress)
        prompt = (ROOT / "prompts/composition_assistant.md").read_text() if self.prompt is None else self.prompt
        prompt = render_prompt(prompt, features) + DELIVERY_INSTRUCTIONS
        proposer_prompt = (ROOT / "prompts/cart_proposer.md").read_text() if self.proposer_prompt is None else self.proposer_prompt
        proposer_prompt += "\nAuthoritative classroom shop policy:\n" + checkout.store.policy.model_dump_json()
        proposals = load_json("workflow_proposals.json").get("manager-approval" if self.scenario == "two-orders" else self.scenario)
        if mode == "fixture" and proposals is None:
            raise ValueError("No composition fixture proposals for this scenario.")
        state = ExecutionState()
        if budget:
            budget.configure(state)
        model = "fixture-model" if mode == "fixture" else self.model
        snapshot = run_snapshot(self.model, "composition", prompt, next(iter(requests.values())),
                                {k: r.model_dump(mode="json") for k, r in requests.items()}, [])
        snapshot.update(reply_schema=reply_schema.model_json_schema(), workflow="act3-as-tool", workflow_prompt=proposer_prompt,
            skill_files=files.files if files else {}, decision_source=self.decision_source,
            layers=[layer.configuration() for layer in self.layers], function_limits=dict(state.limits),
            max_model_calls=state.max_model_calls, max_tool_calls=state.max_tool_calls,
            max_active_seconds=budget.seconds if budget else None,
            max_workflow_model_calls=budget.workflow_model_calls if budget else None,
            max_proposal_seconds=budget.proposal_seconds if budget else None, max_requests=2,
            workflow_fixture_proposals=proposals if mode == "fixture" else None)
        async with RecordedExecution(recorder, snapshot, case_id=self.scenario, act=6, mode=mode, model=model) as execution:
            run_id = execution.run_id
            with recorded_trace(recorder, run_id, "harness.composition") if tracing else nullcontext():
                proposers, proposer_states = {}, {}
                for request_id in requests:
                    if mode == "fixture":
                        proposers[request_id] = FixtureProposer(proposals, recorder, run_id)
                        continue
                    inner = WorkflowRun(recorder=recorder, run_id=run_id, model=self.model, progress=progress)
                    if budget:
                        budget.configure_proposer(inner)
                    inner.middleware.append(ModelControl(inner))
                    if tracing:
                        inner.middleware.append(ModelTrace(recorder, run_id, progress,
                                                           model=self.model, label="Cart proposer"))
                    proposer_states[request_id] = inner
                    proposers[request_id] = execution.own(AgentProposer(recorder, run_id, proposer_prompt, self.model,
                        api_client=proposer_api_client, progress=progress, execution_state=inner,
                        proposal_timeout=budget.proposal_seconds if budget else None))
                orders = WorkflowOrders(checkout, requests, proposers, recorder, run_id,
                    progress=progress, decision_source=self.decision_source, trace_enabled=tracing)
                tools = self.tool_factory(orders)
                skill_provider, access = (features["skills"].attach(files, recorder, run_id, None, progress)
                                          if files is not None else (None, None))
                todos = RecordedTodos(recorder, run_id, progress) if "planning" in features else None
                todo_provider = TodoProvider(store=todos) if todos is not None else None
                session = None
                async def task_state():
                    return [i.to_dict() for i in await todos.load_items(session, source_id=todo_provider.source_id)] if todos else []
                async def capsule():
                    return {"confirmed_requests": {k: r.model_dump(mode="json") for k, r in requests.items()},
                            "orders": [orders.summary(k) for k in requests], "tasks": await task_state(),
                            "authority": "Only host manager decisions can resume pending orders. Logs and tasks do not grant approval."}
                native = (ContextWindowCompactionStrategy(max_context_window_tokens=CONTEXT_WINDOW_TOKENS,
                          max_output_tokens=MAX_OUTPUT_TOKENS, keep_last_tool_call_groups=1) if "compaction" in features else None)
                context = ApplicationContext(recorder, run_id, capsule, progress=progress, native=native, details=tracing)
                tool_trace = HarnessToolTrace(recorder, run_id,
                    [t.name for t in tools] + (TODO_NAMES if todos else []) + (SKILL_TOOLS if access else []),
                    progress if tracing else None, details=tracing, execution_state=state)
                middleware = [ModelControl(state)]
                if tracing:
                    middleware.append(ModelTrace(recorder, run_id, progress,
                                                 model=model, label=features["trace"].label))
                middleware.append(tool_trace)
                if access:
                    middleware.append(access)
                if self.fixture:
                    from formaggio.fixtures.composition_fixture import CompositionFixture
                    api = execution.own(CompositionFixture(self.scenario, planning=todos is not None, skills=files is not None).client())
                else:
                    api = api_client if api_client is not None else execution.own(AsyncOpenAI(timeout=45, max_retries=0))
                client = make_client(model, middleware, api, function_limits=state.limits)
                agent = self.agent_factory(client, tools, prompt, history=HarnessHistory(), context=context,
                                          todo_provider=todo_provider, skills_provider=skill_provider)
                session = agent.create_session()
                response = await run_with_workflow_reviews(agent, session, orders, manager, recorder, run_id,
                                                            active_budget=state.active_budget, reply_schema=reply_schema)
                values = [orders.view(k) for k in requests]
                menus = {value["request_id"]: (requests[value["request_id"]], orders.assessment(value["request_id"]), value["status"])
                         for value in values if value["status"] in {"placed", "recommendation"}}
                text, delivered = deliver_tasting_reply(response.text, menus, checkout.store, recorder, run_id)
                result = {"run_id": run_id, "agent_text": text, "orders": values,
                    "status": "blocked" if tool_trace.blocked_reason else "needs_followup" if not delivered or any(v["status"] == "not_started" for v in values) else "completed",
                    "reason": tool_trace.blocked_reason, "model_calls": state.model_calls,
                    "workflow_model_calls": sum(s.model_calls for s in proposer_states.values()),
                    "skills_loaded": access.loaded if access else [], "skill_resources": access.resources if access else [],
                    "tasks": await task_state(), "compactions": context.count, "order_placed": any(value["order_placed"] for value in values),
                    "scripted": mode == "fixture", "email_transmitted": False}
                recorder.event(run_id, "composition.result", result)
            execution.set_outcome("blocked" if result["status"] == "blocked" else "completed")
            return result
