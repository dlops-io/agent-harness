"""Acts 1–2 run lifecycle. Layer configuration is reusable; all runtime state is local."""
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from typing import Callable

from openai import AsyncOpenAI

from formaggio.agents.context import customer_ask, customer_message, instructions, load_scenario
from formaggio.agents.execution import ExecutionState, ModelControl, RecordedExecution, ToolControl
from formaggio.agents.layers import Layer
from formaggio.agents.runtime import ToolTrace, console_progress, make_client, run_snapshot
from formaggio.agents.tools import build_tools
from formaggio.config import MODEL
from formaggio.shop.data_models import AgentReply
from formaggio.shop.store import Store


@dataclass
class Run(ExecutionState):
    recorder: object
    model: str
    store: Store
    request: object
    progress: object
    run_id: str | None = None
    mode: str = "none"
    packet: dict = field(default_factory=lambda: {"mode": "none", "sources": [], "excluded_products": [],
                                                  "note": "Context layer disabled."})
    trace_enabled: bool = False
    middleware: list = field(default_factory=list)
    context_providers: list = field(default_factory=list)
    report: object = None
    cart_check_status: str = "not_run"


@dataclass(frozen=True)
class Harness:
    """Reusable Acts 1–2 configuration. add/without return independent configurations.

    Built-in layers are immutable. Custom layers must also keep invocation state on
    Run, not on themselves. SDK execution, counters and required tool audit remain
    runtime responsibilities even when detailed Trace telemetry is removed.
    """
    agent_factory: Callable
    model: str = MODEL
    scenario: str = "standard"
    act: int = 1
    execution_mode: str = "live"
    prompt: str | None = None
    layers: tuple[Layer, ...] = ()
    store_factory: Callable = Store

    def __post_init__(self):
        if self.act not in {1, 2}:
            raise ValueError("This harness supports Acts 1–2 only.")
        if self.execution_mode not in {"live", "fixture"}:
            raise ValueError("Execution mode must be live or fixture.")
        names = [layer.name for layer in self.layers]
        if len(set(names)) != len(names):
            raise ValueError("Use only one layer of each name.")

    def add(self, layer):
        return replace(self, layers=(*self.layers, layer))

    def without(self, name):
        if not any(layer.name == name for layer in self.layers):
            raise ValueError(f"No layer named {name!r}.")
        return replace(self, layers=tuple(layer for layer in self.layers if layer.name != name))

    async def run(self, recorder, *, api_client=None, progress=None):
        if self.execution_mode == "fixture" and api_client is None:
            raise ValueError("Fixture mode requires an explicitly supplied local test client.")
        run = Run(recorder, self.model, self.store_factory(), load_scenario(self.scenario), console_progress(progress))
        for layer in self.layers:
            layer.configure(run)
        schemas = build_tools(run.store, run.request, None, None)
        snapshot = run_snapshot(self.model, run.mode, instructions(run.store, self.prompt), run.request,
                                run.packet, schemas)
        snapshot.update(function_limits=dict(run.limits), max_model_calls=run.max_model_calls,
                        max_tool_calls=run.max_tool_calls, layers=[layer.configuration() for layer in self.layers])
        async with RecordedExecution(recorder, snapshot, case_id=self.scenario, act=self.act,
                                     model=self.model, mode=self.execution_mode) as execution:
            run.run_id = execution.run_id
            run.middleware = [ModelControl(run), ToolControl(run)]
            for layer in self.layers:
                layer.attach(run)
            # Admission and fail-closed tool auditing are never optional telemetry.
            run.middleware.append(ToolTrace(recorder, run.run_id, [t.name for t in schemas],
                                             run.progress if run.trace_enabled else None,
                                             details=run.trace_enabled))
            async with AsyncExitStack() as scopes:
                for layer in self.layers:
                    await scopes.enter_async_context(layer.scope(run))
                api = api_client
                if api is None:
                    api = execution.own(AsyncOpenAI(timeout=45, max_retries=0))
                client, _, _ = make_client(self.model, run.middleware, api, function_limits=run.limits)
                agent = self.agent_factory(client, run.store, run.request, recorder=recorder, run_id=run.run_id,
                                           context_providers=run.context_providers, prompt=self.prompt)
                message = customer_message(run.request)
                if run.progress:
                    run.progress(f"\n🧪 Act {self.act} · {run.mode.upper()} context · scenario: {self.scenario}")
                    run.progress("👤 Customer ask:\n" + recorder.redactor.clean(customer_ask(run.request)))
                    if run.progress.show_json:
                        run.progress("\n📋 Exact customer message sent to the model:\n" + recorder.redactor.clean(message))
                response = await agent.run(message, session=agent.create_session())
                reply = response.value if isinstance(response.value, AgentReply) else AgentReply.model_validate_json(response.text)
                for layer in self.layers:
                    layer.finish(run, reply)
                recorder.event(run.run_id, "agent.result", {"message": reply.message,
                    "items": [i.model_dump() for i in reply.items], "order_placed": False,
                    "cart_report": run.report.model_dump() if run.report else None,
                    "cart_check_status": run.cart_check_status})
            return {"run_id": run.run_id, "mode": run.mode, "reply": reply, "report": run.report,
                    "context": run.packet, "model_calls": run.model_calls, "tool_calls": run.tool_calls,
                    "cart_check_status": run.cart_check_status}
