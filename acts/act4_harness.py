"""Act 4: a planning agent plus named harness features and host-owned review."""
from agent_framework import create_harness_agent

from formaggio.agents.context import load_scenario
from formaggio.agents.harness_state import no_post_turn_compaction
from formaggio.agents.planner_layers import Compaction, Memory, PlannerBudget, PlannerTrace, Planning
from formaggio.agents.planner_runtime import PlannerHarness
# Compatibility imports used by Act 6 and existing callers.
from formaggio.agents.planner_tools import HarnessToolTrace, RecordedTodos, TODO_NAMES, build_event_tools
from formaggio.agents.planner_review import run_with_review
from formaggio.agents.runtime import MODEL_OPTIONS
from formaggio.config import MODEL


# %% The agent: narrow tools, session history, a visible plan and current application state.
def build_planner(client, tools, prompt, *, history, context, todo_provider=None, skills_provider=None):
    return create_harness_agent(
        client=client, name="EventPlanner",
        harness_instructions=("Use the visible task list and tools. Host policies and external approvals govern actions."
                              if todo_provider is not None else
                              "Use the supplied tools. Host policies and external approvals govern actions."),
        agent_instructions=prompt, tools=tools, history_provider=history,
        todo_provider=todo_provider, disable_todo=todo_provider is None, skills_provider=skills_provider,
        max_context_window_tokens=16000, max_output_tokens=2400,
        before_compaction_strategy=context, after_compaction_strategy=no_post_turn_compaction,
        disable_mode=True, disable_file_memory=True, disable_web_search=True,
        default_options=MODEL_OPTIONS,
    )


# %% The harness: remove one feature at a time to see its contribution.
def build_act4(*, model=MODEL, scenario="event-shortage", fixture=False, document="clean",
               demo_compaction=False, simulate_detector_miss=False, customer_id=None,
               remember_preferences=(), memory_path=None, output_root=None,
               decision_source="human", execution_mode="live", prompt=None):
    return (
        PlannerHarness(build_planner, scenario_loader=load_scenario, model=model, scenario=scenario,
            fixture=fixture, document=document, demo_compaction=demo_compaction,
            simulate_detector_miss=simulate_detector_miss, customer_id=customer_id,
            remember_preferences=tuple(remember_preferences), memory_path=memory_path,
            output_root=output_root, decision_source=decision_source, execution_mode=execution_mode, prompt=prompt)
        .add(PlannerTrace())
        .add(PlannerBudget(model_calls=8, tool_calls=20, seconds=120))
        .add(Planning())
        .add(Memory())
        .add(Compaction())
    )


# Stable CLI/evaluator API; notebooks call build_act4(...).run(...) explicitly.
async def run_act4(recorder, *, reviewer=None, progress=None, api_client=None, **kwargs):
    return await build_act4(**kwargs).run(recorder, reviewer=reviewer, progress=progress, api_client=api_client)


async def run_harness(recorder, *, skills_files=None, **kwargs):
    if skills_files is not None:
        from acts.act5_skills import run_act5
        return await run_act5(recorder, skills_root=skills_files.root, **kwargs)
    return await run_act4(recorder, **kwargs)
