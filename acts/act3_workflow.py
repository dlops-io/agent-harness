"""Act 3: one proposing agent inside a code-controlled workflow.

Read the graph below, then add named execution layers. The model proposes a cart;
application code validates it, requests approval and performs mock checkout.
"""
from agent_framework import WorkflowBuilder

from formaggio.agents.workflow_steps import (
    ConfirmRequest, ManagerApproval, PlaceOrder, ProposeCart, SelectPairings, ValidateCart,
)
from formaggio.agents.workflow_runtime import (
    AgentProposer, FixtureProposer, WorkflowHarness, drive_workflow,
)
from formaggio.agents.workflow_layers import WorkflowBudget, WorkflowTrace
from formaggio.config import MODEL


# %% The workflow: required checks and explicit transitions, readable without its runtime.
def build_workflow(checkout, recorder, run_id, proposer, *, progress=None, decision_source="human"):
    common = {"checkout": checkout, "recorder": recorder, "run_id": run_id, "progress": progress}
    confirm = ConfirmRequest(id="confirm_request", **common)
    propose = ProposeCart(proposer, **common)
    validate = ValidateCart(id="validate_and_price", **common)
    pair = SelectPairings(id="select_pairings", **common)
    approve = ManagerApproval(decision_source, **common)
    place = PlaceOrder(id="revalidate_and_place", **common)
    return (WorkflowBuilder(start_executor=confirm, name="formaggio-order", output_from="all",
                            max_iterations=12 + 2 * checkout.store.policy.max_revisions)
        .add_edge(confirm, propose)
        .add_edge(propose, validate)
        .add_edge(validate, propose, condition=lambda state: not state.report.ok)
        .add_edge(validate, pair, condition=lambda state: state.report.ok)
        .add_edge(pair, approve)
        .add_edge(approve, place)
        .build())


# %% The harness: optional telemetry and execution limits around that same graph.
def build_act3(*, scenario="standard", model=MODEL, fixture=False,
               decision_source="human", execution_mode="live", prompt=None):
    return (
        WorkflowHarness(build_workflow, scenario=scenario, model=model, fixture=fixture,
                        decision_source=decision_source, execution_mode=execution_mode, prompt=prompt)
        .add(WorkflowTrace())
        .add(WorkflowBudget(model_calls=8, seconds=120))
    )


# Stable CLI/evaluator API. Notebook cells can use build_act3(...).run(...) directly.
async def run_act3(recorder, *, scenario="standard", model=MODEL, fixture=False,
                   manager=None, decision_source="human", progress=None, checkout=None,
                   request=None, proposer=None, api_client=None, execution_mode="live", prompt=None):
    harness = build_act3(scenario=scenario, model=model, fixture=fixture,
                         decision_source=decision_source, execution_mode=execution_mode, prompt=prompt)
    return await harness.run(recorder, manager=manager, progress=progress, checkout=checkout,
                             request=request, proposer=proposer, api_client=api_client)
