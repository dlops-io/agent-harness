"""Act 5: reuse the planning agent and add progressive skill loading."""
from acts.act4_harness import build_planner
from formaggio.agents.context import load_scenario
from formaggio.agents.planner_layers import Compaction, Memory, PlannerBudget, PlannerTrace, Planning
from formaggio.agents.planner_runtime import PlannerHarness
from formaggio.agents.skill_layer import Skills
from formaggio.agents.skill_support import SKILLS_ROOT
from formaggio.config import MODEL


# %% The same planner, with skill metadata and governed, on-demand resource reads.
def build_act5(*, model=MODEL, scenario="event-shortage", fixture=False, skills_root=SKILLS_ROOT,
               document="clean", demo_compaction=False, simulate_detector_miss=False,
               customer_id=None, remember_preferences=(), memory_path=None, output_root=None,
               decision_source="human", execution_mode="live", prompt=None):
    return (
        PlannerHarness(build_planner, act=5, scenario_loader=load_scenario, model=model, scenario=scenario,
            fixture=fixture, document=document, demo_compaction=demo_compaction,
            simulate_detector_miss=simulate_detector_miss, customer_id=customer_id,
            remember_preferences=tuple(remember_preferences), memory_path=memory_path,
            output_root=output_root, decision_source=decision_source, execution_mode=execution_mode, prompt=prompt)
        .add(PlannerTrace())
        .add(PlannerBudget(model_calls=16, tool_calls=20, seconds=120))
        .add(Planning())
        .add(Memory())
        .add(Compaction())
        .add(Skills(skills_root))
    )


# Stable CLI/evaluator API; notebook cells call build_act5(...).run(...) explicitly.
async def run_act5(recorder, *, reviewer=None, progress=None, api_client=None, **kwargs):
    return await build_act5(**kwargs).run(recorder, reviewer=reviewer, progress=progress, api_client=api_client)
