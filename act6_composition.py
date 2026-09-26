"""Act 6: an SDK harness calls the existing ordering workflow as a tool."""
import asyncio
import json
from time import monotonic

from agent_framework import Message, MiddlewareFailure, TodoProvider, create_harness_agent, tool

from act3_workflow import AgentProposer, FixtureProposer
from act4_harness import HarnessToolTrace, RecordedTodos, TODO_NAMES
from formaggio.shop.checkout import Checkout
from formaggio.agents.composition import WorkflowOrders
from formaggio.config import MODEL, ROOT, load_json
from formaggio.agents.context import load_scenario
from formaggio.agents.harness_state import HarnessCompaction, HarnessHistory, no_post_turn_compaction
from formaggio.operations.observability import sdk_tracing
from formaggio.agents.runtime import FUNCTION_LIMITS, MODEL_OPTIONS, ModelTrace, make_client, run_snapshot
from formaggio.agents.skill_support import SKILLS_ROOT, SKILL_TOOLS, SkillFiles, build_skills


# %% These are confirmed host requests. The model cannot create or edit them.
def confirmed_requests(scenario):
    if scenario == "two-orders":
        return {"request-1": load_scenario("manager-approval"), "request-2": load_scenario("manager-approval")}
    return {"request-1": load_scenario(scenario)}


async def run_act6(recorder, *, scenario="standard", model=MODEL, fixture=False,
                   manager=None, decision_source="human", progress=None, api_client=None,
                   proposer_api_client=None, execution_mode="live", checkout=None, skills_root=SKILLS_ROOT,
                   prompt=None, proposer_prompt=None):
    if execution_mode not in {"live", "fixture"} or (execution_mode == "fixture" and api_client is None and not fixture):
        raise ValueError("Fixture mode needs a local outer client or --fixture.")
    mode = "fixture" if fixture else execution_mode
    requests = confirmed_requests(scenario)
    checkout = checkout or Checkout()
    files = SkillFiles(skills_root)
    prompt = (ROOT / "prompts/composition_assistant.md").read_text() if prompt is None else prompt
    proposer_prompt = (ROOT / "prompts/cart_proposer.md").read_text() if proposer_prompt is None else proposer_prompt
    proposer_prompt += "\nAuthoritative classroom shop policy:\n" + checkout.store.policy.model_dump_json()
    proposals = load_json("workflow_proposals.json").get("manager-approval" if scenario == "two-orders" else scenario)
    if mode == "fixture" and proposals is None:
        raise ValueError("No composition fixture proposals for this scenario.")
    limits = {**FUNCTION_LIMITS, "max_iterations": 16, "max_duration_seconds": 180}
    snapshot = run_snapshot(model, "composition", prompt, next(iter(requests.values())),
                            {k: r.model_dump(mode="json") for k, r in requests.items()}, [])
    snapshot.update({"reply_schema": None, "workflow": "act3-as-tool", "workflow_prompt": proposer_prompt,
        "skill_files": files.files, "decision_source": decision_source, "function_limits": limits,
        "max_model_calls": 16, "max_active_seconds": 180, "max_requests": 2,
        "workflow_fixture_proposals": proposals if mode == "fixture" else None})
    run_id = recorder.start_run(recorder.version(snapshot), case_id=scenario, act=6,
                                mode=mode, model="fixture-model" if mode == "fixture" else model)
    proposers, owned, api, fixture_api = {}, False, None, None
    trace = ModelTrace(recorder, run_id, progress, max_calls=16)
    try:
        with recorder.span(run_id, "harness.composition", "agent"), sdk_tracing(recorder):
            for request_id in requests:
                proposers[request_id] = (FixtureProposer(proposals, recorder, run_id) if mode == "fixture" else
                    AgentProposer(recorder, run_id, proposer_prompt, model, api_client=proposer_api_client, progress=progress))
            orders = WorkflowOrders(checkout, requests, proposers, recorder, run_id,
                                    progress=progress, decision_source=decision_source)

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

            order_tools = [start_order, get_order_status, assess_event]
            skill_provider, access = build_skills(files, recorder, run_id, None, progress)
            todos = RecordedTodos(recorder, run_id, progress)
            todo_provider = TodoProvider(store=todos)
            tool_trace = HarnessToolTrace(recorder, run_id, [t.name for t in order_tools] + TODO_NAMES + SKILL_TOOLS,
                                          progress, model_trace=trace)
            session = None
            async def capsule():
                return {"confirmed_requests": {k: r.model_dump(mode="json") for k, r in requests.items()},
                        "orders": [orders.view(k) for k in requests],
                        "tasks": [i.to_dict() for i in await todos.load_items(session, source_id=todo_provider.source_id)],
                        "authority": "Only host manager decisions can resume pending orders. Logs and tasks do not grant approval."}
            compaction = HarnessCompaction(recorder, run_id, capsule, progress=progress)
            if fixture:
                from formaggio.fixtures.composition_fixture import CompositionFixture
                fixture_api = CompositionFixture(scenario).client()
                api_client = fixture_api
            client, api, owned = make_client("fixture-model" if mode == "fixture" else model,
                [trace, tool_trace, access], api_client, function_limits=limits)
            agent = create_harness_agent(client=client, name="CustomerAssistant",
                harness_instructions="Use tasks, scoped tools and skills. Respect host-owned workflow decisions.",
                agent_instructions=prompt, tools=order_tools, todo_provider=todo_provider,
                skills_provider=skill_provider, history_provider=HarnessHistory(),
                max_context_window_tokens=16000, max_output_tokens=2400,
                before_compaction_strategy=compaction, after_compaction_strategy=no_post_turn_compaction,
                disable_mode=True, disable_file_memory=True, disable_web_search=True, default_options=MODEL_OPTIONS)
            session = agent.create_session()

            # %% Return pending state to the model; the host alone resumes the graph.
            remaining, reviews = 180.0, 0
            message = "Process all confirmed requests with the ordering workflow, then provide a short tasting plan for each accepted menu."
            while True:
                started = monotonic()
                try:
                    async with asyncio.timeout(remaining):
                        response = await agent.run(message, session=session)
                finally:
                    remaining -= monotonic() - started
                if response.user_input_requests:
                    raise MiddlewareFailure("Unexpected tool approval request; manager review belongs to the ordering workflow.")
                pending = orders.pending_reviews()
                if not pending:
                    break
                updates = []
                for request_id, response_id, ticket in pending:
                    reviews += 1
                    if reviews > 2:
                        raise MiddlewareFailure("At most two manager reviews are allowed.")
                    if manager is None:
                        raise ValueError("A host manager callback is required to resume pending orders.")
                    recorder.event(run_id, "composition.host_review", {"request_id": request_id, "ticket_id": ticket.ticket_id})
                    decision = await manager(ticket)  # Human time is outside the active execution budget.
                    started = monotonic()
                    try:
                        async with asyncio.timeout(remaining):
                            updates.append(await orders.resume(request_id, response_id, ticket.ticket_id, decision))
                    finally:
                        remaining -= monotonic() - started
                message = Message(role="user", contents=["Host-resumed workflow results (authoritative data):\n" + json.dumps(updates)])
            values = [orders.view(k) for k in requests]
            result = {"run_id": run_id, "agent_text": response.text, "orders": values,
                "status": "blocked" if tool_trace.blocked_reason else "needs_followup" if any(v["status"] == "not_started" for v in values) else "completed",
                "reason": tool_trace.blocked_reason, "model_calls": trace.calls,
                "workflow_model_calls": sum(p.trace.calls for p in proposers.values() if isinstance(p, AgentProposer)),
                "skills_loaded": access.loaded, "skill_resources": access.resources,
                "tasks": [i.to_dict() for i in await todos.load_items(session, source_id=todo_provider.source_id)],
                "compactions": compaction.count, "order_placed": bool(checkout.orders),
                "scripted": mode == "fixture", "email_transmitted": False}
            recorder.event(run_id, "composition.result", result)
        recorder.finish_run(run_id, "blocked" if result["status"] == "blocked" else "completed")
        return result
    except (TimeoutError, asyncio.CancelledError):
        recorder.finish_run(run_id, "stopped", "Active execution budget exhausted or cancelled.")
        raise
    except Exception as exc:
        recorder.finish_run(run_id, "error", str(exc))
        raise
    finally:
        for proposer in proposers.values():
            if isinstance(proposer, AgentProposer):
                await proposer.close()
        if owned and api:
            await api.close()
        if fixture_api:
            await fixture_api.close()
