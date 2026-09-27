"""Act 6: a customer assistant invokes the governed ordering workflow as a tool."""
from agent_framework import create_harness_agent, tool

from formaggio.agents.composition_layers import CompositionBudget, CompositionTrace
from formaggio.agents.composition_runtime import CompositionHarness
from formaggio.agents.context import load_scenario
from formaggio.agents.harness_state import no_post_turn_compaction
from formaggio.agents.planner_layers import Compaction, Planning
from formaggio.agents.runtime import MODEL_OPTIONS
from formaggio.agents.skill_layer import Skills
from formaggio.agents.skill_support import SKILLS_ROOT
from formaggio.config import MODEL


# %% The host confirms requests; the model can only select their IDs.
def confirmed_requests(scenario):
    if scenario == "two-orders":
        return {"request-1": load_scenario("manager-approval"), "request-2": load_scenario("manager-approval")}
    return {"request-1": load_scenario(scenario)}


def build_order_tools(orders):
    @tool
    async def start_order(request_id: str) -> dict:
        """Run the governed ordering workflow for a host-confirmed request. May return pending_approval."""
        return await orders.start(request_id)

    @tool
    def get_order_status(request_id: str) -> dict:
        """Read authoritative status, pending ticket or final receipt. Never approve or restart an order."""
        return orders.view(request_id)

    @tool
    def assess_event(request_id: str) -> dict:
        """Get the accepted menu, product styles and pairings for a follow-up tasting plan."""
        return orders.assessment(request_id)

    return [start_order, get_order_status, assess_event]


# %% The outer agent has narrow workflow tools, tasks, and on-demand skills.
def build_assistant(client, tools, prompt, *, history, context, todo_provider=None, skills_provider=None):
    return create_harness_agent(client=client, name="CustomerAssistant",
        harness_instructions=("Use tasks, scoped tools and skills. Respect host-owned workflow decisions."
                              if todo_provider is not None and skills_provider is not None else
                              "Use the available tools. Respect host-owned workflow decisions."),
        agent_instructions=prompt, tools=tools, todo_provider=todo_provider, disable_todo=todo_provider is None,
        skills_provider=skills_provider, history_provider=history,
        max_context_window_tokens=16000, max_output_tokens=2400,
        before_compaction_strategy=context, after_compaction_strategy=no_post_turn_compaction,
        disable_mode=True, disable_file_memory=True, disable_web_search=True, default_options=MODEL_OPTIONS)


# %% Optional teaching layers; workflow validation and approval stay mandatory.
def build_act6(*, scenario="standard", model=MODEL, fixture=False, decision_source="human",
               execution_mode="live", skills_root=SKILLS_ROOT, prompt=None, proposer_prompt=None):
    return (
        CompositionHarness(build_assistant, build_order_tools, confirmed_requests,
            scenario=scenario, model=model, fixture=fixture, decision_source=decision_source,
            execution_mode=execution_mode, prompt=prompt, proposer_prompt=proposer_prompt)
        .add(CompositionTrace())
        .add(CompositionBudget())
        .add(Planning())
        .add(Compaction())
        .add(Skills(skills_root))
    )


# Stable CLI/evaluator API; notebooks call build_act6(...).run(...) explicitly.
async def run_act6(recorder, *, manager=None, progress=None, api_client=None,
                   proposer_api_client=None, checkout=None, **kwargs):
    return await build_act6(**kwargs).run(recorder, manager=manager, progress=progress,
        api_client=api_client, proposer_api_client=proposer_api_client, checkout=checkout)
