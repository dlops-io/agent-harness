"""Act 1: build a normal SDK agent, then add named harness features."""
from agent_framework import Agent

from formaggio.agents.context import instructions
from formaggio.agents.harness import Harness
from formaggio.agents.layers import Budget, CartCheck, Context, Trace
from formaggio.agents.runtime import MODEL_OPTIONS
from formaggio.agents.tools import build_tools
from formaggio.config import MODEL
from formaggio.shop.data_models import AgentReply


# %% The agent: instructions + tools + a structured reply. Also usable without a harness.
def build_agent(client, store, request, *, recorder=None, run_id=None, context_providers=(), prompt=None):
    return Agent(
        client=client,
        name="FormaggioAssistant",
        instructions=instructions(store, prompt),
        tools=build_tools(store, request, recorder, run_id),
        context_providers=list(context_providers),
        default_options={**MODEL_OPTIONS, "response_format": AgentReply},
    )


# %% The harness: each named layer adds one teaching feature to a fresh invocation.
def build_act1(*, scenario="standard", mode="basic", model=MODEL, act=1, execution_mode="live", prompt=None):
    return (
        Harness(build_agent, model=model, scenario=scenario, act=act, execution_mode=execution_mode, prompt=prompt)
        .add(Trace())
        .add(Budget(model_calls=8, tool_calls=20, seconds=120))
        .add(Context(mode))
        .add(CartCheck())
    )


# Existing CLI/evaluator entry points; notebooks can call build_act1().run(...) directly.
async def run_agent(recorder, *, scenario="standard", mode="basic", model=MODEL,
                    api_client=None, progress=None, act=1, execution_mode="live", prompt=None):
    harness = build_act1(scenario=scenario, mode=mode, model=model, act=act,
                         execution_mode=execution_mode, prompt=prompt)
    return await harness.run(recorder, api_client=api_client, progress=progress)


async def run_act1(recorder, **kwargs):
    return await run_agent(recorder, mode="basic", **kwargs)
