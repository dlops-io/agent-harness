"""Optional teaching features for Acts 1–2; instances hold configuration only."""
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from math import isfinite
from typing import ClassVar

from formaggio.agents.context import ShopContextProvider, build_context
from formaggio.agents.runtime import ModelTrace
from formaggio.agents.execution import ActiveBudget, recorded_trace


class Layer:
    name = "layer"

    def configuration(self):
        return {"name": self.name}

    def configure(self, run):
        pass

    def attach(self, run):
        pass

    @asynccontextmanager
    async def scope(self, run):
        yield

    def finish(self, run, reply):
        pass


@dataclass(frozen=True)
class Trace(Layer):
    """Detailed model/tool telemetry. Required tool audit is owned by the runtime."""
    name: ClassVar[str] = "trace"
    label: str = "Shop assistant"

    def configuration(self):
        return {"name": self.name, **asdict(self)}

    def configure(self, run):
        run.trace_enabled = True

    def attach(self, run):
        run.middleware.append(ModelTrace(run.recorder, run.run_id, run.progress,
                                         max_calls=None, model=run.model, label=self.label))

    @asynccontextmanager
    async def scope(self, run):
        with recorded_trace(run.recorder, run.run_id, "agent.context_demo"):
            yield


@dataclass(frozen=True)
class Budget(Layer):
    """Exact model/tool admission limits plus a timeout around execution and checks."""
    name: ClassVar[str] = "budget"
    model_calls: int = 8
    tool_calls: int = 20
    seconds: float = 120

    def __post_init__(self):
        for value in (self.model_calls, self.tool_calls):
            if type(value) is not int or value < 1:
                raise ValueError("Call budgets must be positive integers.")
        if isinstance(self.seconds, bool) or not isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("The time budget must be finite and positive.")

    def configuration(self):
        return {"name": self.name, **asdict(self)}

    def configure(self, run):
        run.active_budget = ActiveBudget(self.seconds)
        run.max_model_calls, run.max_tool_calls = self.model_calls, self.tool_calls
        run.limits.update(max_iterations=self.model_calls, max_function_calls=self.tool_calls,
                          max_duration_seconds=self.seconds)

    @asynccontextmanager
    async def scope(self, run):
        async with run.active_budget.measure():
            yield


@dataclass(frozen=True)
class Context(Layer):
    """Select context afresh for this invocation; never cache shop state on a layer."""
    name: ClassVar[str] = "context"
    mode: str = "basic"

    def __post_init__(self):
        if self.mode not in {"basic", "enriched"}:
            raise ValueError("Context mode must be basic or enriched.")

    def configuration(self):
        return {"name": self.name, **asdict(self)}

    def configure(self, run):
        run.mode = self.mode
        run.packet = build_context(run.request, run.store, self.mode)

    def attach(self, run):
        run.recorder.event(run.run_id, "context.mode", {"mode": self.mode})
        run.context_providers.append(ShopContextProvider(run.packet, run.recorder, run.run_id))


@dataclass(frozen=True)
class CartCheck(Layer):
    """Assess the final proposal; does not repair it or authorize a purchase."""
    name: ClassVar[str] = "cart_check"

    def finish(self, run, reply):
        run.report = run.store.validate(run.request, reply.items) if reply.items else None
        run.cart_check_status = ("passed" if run.report.ok else "failed") if run.report else "no_cart"
