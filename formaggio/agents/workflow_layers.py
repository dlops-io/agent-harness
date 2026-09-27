"""Optional Act 3 features. Business checks remain mandatory workflow steps."""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from math import isfinite
from typing import ClassVar

from formaggio.agents.execution import ActiveBudget, recorded_trace
from formaggio.agents.runtime import ModelTrace


@dataclass(frozen=True)
class WorkflowTrace:
    name: ClassVar[str] = "trace"
    label: str = "Cart proposer"

    def configuration(self):
        return {"name": self.name, **asdict(self)}

    def configure(self, run):
        run.middleware.append(ModelTrace(run.recorder, run.run_id, run.progress,
                                        max_calls=None, model=run.model, label=self.label))

    @contextmanager
    def scope(self, run):
        with recorded_trace(run.recorder, run.run_id, "workflow.order", "workflow"):
            yield


@dataclass(frozen=True)
class WorkflowBudget:
    """One model-call allowance and active timer for the entire workflow invocation."""
    name: ClassVar[str] = "budget"
    model_calls: int = 8
    seconds: float = 120

    def __post_init__(self):
        if type(self.model_calls) is not int or self.model_calls < 1:
            raise ValueError("The model-call budget must be a positive integer.")
        if isinstance(self.seconds, bool) or not isfinite(self.seconds) or self.seconds <= 0:
            raise ValueError("The time budget must be finite and positive.")

    def configuration(self):
        return {"name": self.name, **asdict(self)}

    def configure(self, run):
        run.max_model_calls = self.model_calls
        run.active_budget = ActiveBudget(self.seconds)
        run.limits.update(max_iterations=self.model_calls, max_duration_seconds=self.seconds)
