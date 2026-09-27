"""Immutable options for the outer assistant and its inner cart proposers."""
from dataclasses import dataclass
from math import isfinite
from typing import ClassVar

from formaggio.agents.planner_layers import PlannerBudget, PlannerLayer


@dataclass(frozen=True)
class CompositionTrace(PlannerLayer):
    name: ClassVar[str] = "trace"
    label: str = "Customer assistant"


@dataclass(frozen=True)
class CompositionBudget(PlannerBudget):
    """Outer call caps, a per-workflow proposer cap, and shared active time.

    The 180-second timer covers outer calls, nested workflows and host resumes;
    manager deliberation is excluded. Each proposal also keeps its 120-second cap.
    """
    model_calls: int = 16
    tool_calls: int = 20
    seconds: float = 180
    workflow_model_calls: int = 8
    proposal_seconds: float = 120

    def __post_init__(self):
        super().__post_init__()
        if type(self.workflow_model_calls) is not int or self.workflow_model_calls < 1:
            raise ValueError("Workflow model calls must be a positive integer.")
        if isinstance(self.proposal_seconds, bool) or not isfinite(self.proposal_seconds) or self.proposal_seconds <= 0:
            raise ValueError("Proposal time must be finite and positive.")

    def configure_proposer(self, state):
        state.max_model_calls = self.workflow_model_calls
        state.limits.update(max_iterations=self.workflow_model_calls, max_function_calls=20,
                            max_duration_seconds=self.proposal_seconds)
