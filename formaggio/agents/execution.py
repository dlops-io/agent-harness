"""Shared execution mechanics; act adapters own prompts, tools and business outcomes.

Create state, timers and a recorded execution for every invocation. A workflow
can measure several active segments on the same timer, leaving human review
outside those segments without resetting its time or call allowance.
"""
import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from inspect import isawaitable
from math import isfinite
from time import monotonic

from agent_framework import ChatMiddleware, FunctionMiddleware, MiddlewareFailure

from formaggio.operations.observability import sdk_tracing


class ActiveBudget:
    """Cumulative active time. One timer belongs to one run, across its resumes."""

    def __init__(self, seconds, *, clock=monotonic):
        if isinstance(seconds, bool) or not isfinite(seconds) or seconds <= 0:
            raise ValueError("The time budget must be finite and positive.")
        self._remaining = seconds
        self._clock = clock
        self._started = None

    @property
    def remaining(self):
        elapsed = 0 if self._started is None else max(0, self._clock() - self._started)
        return max(0, self._remaining - elapsed)

    @asynccontextmanager
    async def measure(self):
        if self._started is not None:
            raise RuntimeError("A run's active time segments must not overlap.")
        if self.remaining <= 0:
            raise TimeoutError("Active execution time budget exhausted.")
        self._started = self._clock()
        try:
            async with asyncio.timeout(self._remaining):
                yield
            # Also catch synchronous work or code that swallowed cancellation.
            if self.remaining <= 0:
                raise TimeoutError("Active execution time budget exhausted.")
        finally:
            self._remaining = self.remaining
            self._started = None


@dataclass(kw_only=True)
class ExecutionState:
    """Per-invocation admission state, independent of optional trace collection."""

    # SDK loop fallback when no explicit Budget layer is installed.
    limits: dict = field(default_factory=lambda: {"max_iterations": 40, "max_function_calls": None,
                                                 "max_duration_seconds": None, "allow_concurrent_invocation": False})
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    model_calls: int = 0
    tool_calls: int = 0
    failure: str | None = None
    active_budget: ActiveBudget | None = None

    def admit(self, kind):
        if kind not in {"model", "tool"}:
            raise ValueError("Call kind must be model or tool.")
        if self.failure:
            raise MiddlewareFailure(self.failure)
        maximum = getattr(self, f"max_{kind}_calls")
        count = getattr(self, f"{kind}_calls")
        if maximum is not None and count >= maximum:
            self.failure = f"Maximum of {maximum} {kind} calls reached."
            raise MiddlewareFailure(self.failure)
        setattr(self, f"{kind}_calls", count + 1)


class ModelControl(ChatMiddleware):
    def __init__(self, run):
        self.run = run

    async def process(self, context, call_next):
        if context.stream:
            raise MiddlewareFailure("This teaching act uses non-streaming responses.")
        self.run.admit("model")
        await call_next()


class ToolControl(FunctionMiddleware):
    def __init__(self, run):
        self.run = run

    async def process(self, context, call_next):
        self.run.admit("tool")
        await call_next()


@contextmanager
def recorded_trace(recorder, run_id, name, kind="agent", *, expected_outcomes=None):
    """Keep act-specific span names and route SDK spans to the right recorder."""
    with recorder.span(run_id, name, kind, expected_outcomes=expected_outcomes), sdk_tracing(recorder):
        yield


class RecordedExecution:
    """Single-use run lifecycle and ownership of explicitly registered resources.

The act supplies its snapshot and outcome. Exceptions take precedence over a
planned outcome. Cleanup happens before finalization so a failed close cannot
leave a run marked completed. Borrowed clients must not be registered with own.
"""

    def __init__(self, recorder, snapshot, **metadata):
        self.recorder = recorder
        self.snapshot = snapshot
        self.metadata = metadata
        self.run_id = None
        self._entered = False
        self._active = False
        self._resources = AsyncExitStack()
        self._owned = []
        self._status, self._reason = "completed", None

    async def __aenter__(self):
        if self._entered:
            raise RuntimeError("Create a new recorded execution for each invocation.")
        self._entered = True
        self.run_id = self.recorder.start_run(self.recorder.version(self.snapshot), **self.metadata)
        self._active = True
        return self

    def _require_active(self):
        if not self._active:
            raise RuntimeError("The recorded execution is not active.")

    def own(self, resource):
        """Register an owned resource with close/aclose; return it for immediate use."""
        self._require_active()
        if any(resource is existing for existing in self._owned):
            return resource
        close = getattr(resource, "aclose", None) or getattr(resource, "close", None)
        if not callable(close):
            raise TypeError("Owned resources must provide close() or aclose().")

        async def cleanup():
            result = close()
            if isawaitable(result):
                await result

        self._resources.push_async_callback(cleanup)
        self._owned.append(resource)
        return resource

    def set_outcome(self, status, reason=None):
        """Adapters may report a normal completion or an expected policy block."""
        self._require_active()
        if status not in {"completed", "blocked"}:
            raise ValueError("An explicit outcome must be completed or blocked.")
        self._status, self._reason = status, reason

    async def __aexit__(self, exc_type, exc, traceback):
        self._require_active()
        self._active = False
        failure = exc
        try:
            await self._resources.aclose()
        except BaseException as cleanup_error:
            if failure is None:
                failure = cleanup_error
            else:
                # Preserve the primary exception without exposing resource details.
                failure.add_note(f"Resource cleanup also failed ({type(cleanup_error).__name__}).")
        finally:
            self._owned.clear()
        status, reason = self._status, self._reason
        if isinstance(failure, (TimeoutError, asyncio.CancelledError)):
            status, reason = "stopped", "Run timed out or was cancelled."
        elif failure is not None:
            status, reason = "error", str(failure)
        try:
            self.recorder.finish_run(self.run_id, status, reason)
        except BaseException as recording_error:
            if failure is None:
                raise
            failure.add_note(f"Run finalization also failed ({type(recording_error).__name__}).")
        if failure is not None and failure is not exc:
            raise failure
        return False
