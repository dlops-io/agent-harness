"""Immutable Act 4 teaching features; each run creates its own runtime state."""
from dataclasses import asdict, dataclass
from typing import ClassVar

from formaggio.agents.layers import Budget


class PlannerLayer:
    def configuration(self):
        return {"name": self.name, **asdict(self)}


@dataclass(frozen=True)
class PlannerTrace(PlannerLayer):
    name: ClassVar[str] = "trace"
    label: str = "Event planner"


@dataclass(frozen=True)
class PlannerBudget(Budget):
    """The shared call counters and active timer persist across email reviews."""
    def configure(self, run):
        super().configure(run)
        # Let mandatory admission count executed tools across resumes. The SDK's
        # own tool cap can otherwise return synthetic final prose before admission.
        run.limits["max_function_calls"] = None


@dataclass(frozen=True)
class Planning(PlannerLayer):
    name: ClassVar[str] = "planning"


@dataclass(frozen=True)
class Memory(PlannerLayer):
    """Only customer-confirmed preferences may be persisted by the host."""
    name: ClassVar[str] = "memory"


@dataclass(frozen=True)
class Compaction(PlannerLayer):
    """Evict older history; current application state is supplied independently."""
    name: ClassVar[str] = "compaction"
