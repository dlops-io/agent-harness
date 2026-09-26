"""Act 1 recap: a prompt-and-tools agent with a proposal-only checkout."""
import asyncio

from agent_framework import Agent

from formaggio.config import MODEL
from formaggio.agents.context import ShopContextProvider, build_context, customer_message, instructions, load_scenario
from formaggio.shop.data_models import AgentReply
from formaggio.operations.observability import sdk_tracing
from formaggio.agents.runtime import MODEL_OPTIONS, ModelTrace, ToolTrace, make_client, run_snapshot
from formaggio.shop.store import Store
from formaggio.agents.tools import build_tools


# %% Shared execution: both acts use precisely this agent, prompt, and tool set.
async def run_agent(recorder, *, scenario="standard", mode="basic", model=MODEL,
                    api_client=None, progress=None, act=1, execution_mode="live", prompt=None):
    if execution_mode not in {"live", "fixture"} or (execution_mode == "fixture" and api_client is None):
        raise ValueError("Fixture mode requires an explicitly supplied local test client.")
    store = Store()
    request = load_scenario(scenario)
    packet = build_context(request, store, mode)
    prompt = instructions(store, prompt)
    # Inspect tool schemas first, then bind fresh callbacks to the recorded run.
    tools = build_tools(store, request, recorder, None)
    version_id = recorder.version(run_snapshot(model, mode, prompt, request, packet, tools))
    run_id = recorder.start_run(version_id, case_id=scenario, act=act, model=model, mode=execution_mode)
    tools = build_tools(store, request, recorder, run_id)
    recorder.event(run_id, "context.mode", {"mode": mode})
    model_trace = ModelTrace(recorder, run_id, progress, model=model, label="Shop assistant")
    tool_trace = ToolTrace(recorder, run_id, [t.name for t in tools], progress)
    api = None
    owned = False
    try:
        client, api, owned = make_client(model, [model_trace, tool_trace], api_client)
        agent = Agent(client=client, name="FormaggioAssistant", instructions=prompt, tools=tools,
                      context_providers=[ShopContextProvider(packet, recorder, run_id)],
                      default_options={**MODEL_OPTIONS, "response_format": AgentReply})
        with recorder.span(run_id, "agent.context_demo", "agent"), sdk_tracing(recorder):
            message = customer_message(request)
            if progress:
                progress(f"\n🧪 Act {act} · {mode.upper()} context · scenario: {scenario}")
                progress("👤 Customer question (actual message sent to the model):\n"
                         + recorder.redactor.clean(message))
                progress(f"\n🔎 Run: {run_id}. Tool arguments/results below; full model inputs: --inspect-run {run_id}")
            async with asyncio.timeout(120):
                response = await agent.run(message, session=agent.create_session())
            reply = response.value
            if not isinstance(reply, AgentReply):
                reply = AgentReply.model_validate_json(response.text)
            # Report the final cart as supplied; do not repair it or silently rerun the agent.
            report = store.validate(request, reply.items) if reply.items else None
            recorder.event(run_id, "agent.result", {"message": reply.message,
                "items": [i.model_dump() for i in reply.items], "order_placed": False,
                "cart_report": report.model_dump() if report else None})
        recorder.finish_run(run_id)
        return {"run_id": run_id, "mode": mode, "reply": reply, "report": report,
                "context": packet, "model_calls": model_trace.calls, "tool_calls": tool_trace.invocations}
    except (TimeoutError, asyncio.CancelledError):
        recorder.finish_run(run_id, "stopped", "Run timed out or was cancelled.")
        raise
    except Exception as exc:
        recorder.finish_run(run_id, "error", str(exc))
        raise
    finally:
        if owned and api is not None:
            await api.close()


# %% Baseline entry point (also callable with await in the future notebook).
async def run_act1(recorder, **kwargs):
    return await run_agent(recorder, mode="basic", **kwargs)
