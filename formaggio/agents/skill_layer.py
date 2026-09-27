"""Reusable skill configuration; snapshot contents and loading state belong to a run."""
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from formaggio.agents.skill_support import SKILLS_ROOT, SkillFiles, build_skills


@dataclass(frozen=True)
class Skills:
    name: ClassVar[str] = "skills"
    root: Path = SKILLS_ROOT

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root).absolute())

    def configuration(self):
        return {"name": self.name, "root": str(self.root)}

    def snapshot(self):
        return SkillFiles(self.root)

    def attach(self, files, recorder, run_id, outreach, progress):
        # Native discovery exposes metadata; bodies and resources load on demand.
        # The paired access hook is required, even without the Trace layer.
        return build_skills(files, recorder, run_id, outreach, progress)
