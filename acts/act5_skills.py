"""Act 5 adds progressive skill loading to the same Act 4 harness."""
from acts.act4_harness import run_harness
from formaggio.agents.skill_support import SKILLS_ROOT, SkillFiles


# %% Inspect skills/ first, then run live or use an explicitly scripted demonstration.
async def run_act5(recorder, *, skills_root=SKILLS_ROOT, **kwargs):
    return await run_harness(recorder, skills_files=SkillFiles(skills_root), **kwargs)
