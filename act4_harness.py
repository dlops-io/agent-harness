"""Act 4: a bounded SDK harness for planning and human-approved mock outreach."""
import asyncio
import json
from time import monotonic

from agent_framework import (Message, MiddlewareFailure, TodoProvider,
                             TodoSessionStore, create_harness_agent, tool)

from formaggio.config import MODEL, OUTPUT_DIR, ROOT, load_json
from formaggio.agents.context import load_scenario
from formaggio.operations.governance import PolicyBlocked, PolicyCheckError
from formaggio.agents.harness_state import HarnessCompaction, HarnessHistory, PreferenceMemory, no_post_turn_compaction
from formaggio.operations.observability import sdk_tracing
from formaggio.agents.runtime import FUNCTION_LIMITS, MODEL_OPTIONS, ModelTrace, ToolTrace, make_client, run_snapshot
from formaggio.shop.store import Store
from formaggio.shop.vendor_outreach import VendorOutreach, detect_injection, retrieve_vendor_document

TODO_NAMES = ["todos_add", "todos_complete", "todos_remove", "todos_get_remaining", "todos_get_all"]


# %% SDK-backed task state is visible but is never an approval record.
class RecordedTodos(TodoSessionStore):
    def __init__(self, recorder, run_id, progress):
        self.recorder, self.run_id, self.progress = recorder, run_id, progress

    async def save_state(self, session, items, *, next_id, source_id):
        if len(items) > 12 or any(len(i.title) > 200 or len(i.description or "") > 500 for i in items):
            raise MiddlewareFailure("Task list exceeds teaching limits (12 short tasks).")
        self.recorder.event(self.run_id, "plan.updated", {"items": [i.to_dict() for i in items]})
        await super().save_state(session, items, next_id=next_id, source_id=source_id)
        if self.progress:
            self.progress("Plan: " + "; ".join(f"[{'done' if i.is_complete else 'open'}] {i.title}" for i in items))


class HarnessToolTrace(ToolTrace):
    """Bound total tool work across approval resumes; fail closed on hook errors."""
    def __init__(self, *args, model_trace=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls, self.blocked_reason = 0, None
        self.model_trace = model_trace

    async def process(self, context, call_next):
        if self.model_trace and self.model_trace.failure:
            raise MiddlewareFailure(self.model_trace.failure)
        self.calls += 1
        if self.calls > 20:
            raise MiddlewareFailure("Harness tool budget exhausted (20 calls).")
        async def guarded_call():
            try:
                await call_next()
            except PolicyCheckError:
                raise
            except PolicyBlocked as exc:
                self.blocked_reason = str(exc)
                self.record("harness.action_blocked", {"name": context.function.name, "reason": str(exc)})
                context.result = {"status": "blocked", "reason": str(exc)}
        try:
            await super().process(context, guarded_call)
        except Exception as exc:
            # The SDK can attempt another model turn before propagating a tool
            # middleware error. Keep that attempted turn inside the audit boundary.
            if self.model_trace:
                self.model_trace.failure = "Harness tool or required audit failed: " + str(exc)
            raise MiddlewareFailure("Harness tool or required audit failed: " + str(exc)) from exc


def build_event_tools(outreach, document, *, detector=detect_injection, progress=None):
    @tool
    def assess_event() -> dict:
        """Validate and price the customer-confirmed menu; report shortages and conditional pairings. No order."""
        return outreach.assess()

    @tool
    def read_vendor_document() -> dict:
        """Retrieve untrusted vendor evidence through the governance check; flagged text is quarantined."""
        result = retrieve_vendor_document(document, outreach.recorder, outreach.run_id, detector=detector)
        if progress:
            progress(f"Vendor evidence: {result['status']} ({document})")
        return result

    @tool
    def draft_vendor_email(recipient: str) -> dict:
        """Generate an availability inquiry from verified shortfalls. No HTML is saved and nothing is sent."""
        return outreach.prepare(recipient)

    @tool(approval_mode="always_require")
    def save_vendor_email(draft_id: str) -> dict:
        """After host approval, save the exact draft as escaped local HTML. Never transmit email."""
        return outreach.save(draft_id)

    return [assess_event, read_vendor_document, draft_vendor_email, save_vendor_email]


# %% Human decisions are handled outside model execution, with independent binding.
async def run_with_review(agent, session, task, outreach, reviewer, recorder, run_id, *, decision_source):
    message, remaining, approval_count = task, 120.0, 0
    while True:
        started = monotonic()
        try:
            async with asyncio.timeout(remaining):
                response = await agent.run(message, session=session)
        finally:
            remaining -= monotonic() - started
        pending = [c for c in response.user_input_requests if c.type == "function_approval_request"]
        if not pending:
            return response.text
        replies = []
        for request in pending:
            approval_count += 1
            if approval_count > 2:
                raise MiddlewareFailure("At most two email approval requests are allowed per run.")
            call = request.function_call
            if call.name != "save_vendor_email":
                raise PolicyBlocked("Unexpected approval request.")
            args = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
            if set(args) != {"draft_id"}:
                raise PolicyBlocked("Unexpected email approval arguments.")
            review = outreach.review(args["draft_id"], request.id)
            if reviewer is None:
                raise ValueError("A human review callback is required to approve or decline the mock email.")
            decision = await reviewer(review)
            outreach.decide(review.ticket_id, decision, source=decision_source)
            replies.append(request.to_function_approval_response(decision == "approve"))
        message = Message(role="user", contents=replies)


async def run_harness(recorder, *, model=MODEL, scenario="event-shortage", fixture=False,
                   document="clean", demo_compaction=False, simulate_detector_miss=False,
                   customer_id=None, remember_preferences=(), memory_path=None,
                   output_root=None, reviewer=None, decision_source="human", progress=None,
                   api_client=None, execution_mode="live", skills_files=None, prompt=None):
    skills_enabled = skills_files is not None
    if scenario != "event-shortage" and not (skills_enabled and scenario in {"tasting-plan", "stock-question"}):
        raise ValueError("Act 4 supports event-shortage; Act 5 also supports tasting-plan and stock-question.")
    if document not in load_json("vendor_documents.json"):
        raise ValueError("Unknown vendor document.")
    request = load_scenario("standard" if scenario in {"tasting-plan", "stock-question"} else scenario)
    customer_id = request.customer_id if customer_id is None else customer_id
    if customer_id not in {c["customer_id"] for c in load_json("customers.json")}:
        raise ValueError("Choose a synthetic customer from customers.json.")
    if simulate_detector_miss and not fixture:
        raise ValueError("The forced detector-miss demonstration is restricted to --fixture.")
    if execution_mode not in {"live", "fixture"} or (execution_mode == "fixture" and not fixture and api_client is None):
        raise ValueError("Fixture mode requires an explicitly local client or --fixture.")
    request = request.model_copy(
        update={"customer_id": customer_id, "preferences": tuple(remember_preferences)})
    store, brief = Store(), load_json("event_brief.json")
    if scenario == "tasting-plan":
        brief = {**brief, "items": load_json("workflow_proposals.json")["standard"][0],
                 "task": "Prepare a host tasting plan for the confirmed 12-person menu, including serving sequence, pairings, quote and open questions. This task does not request vendor outreach or order placement."}
    elif scenario == "stock-question":
        brief = {**brief, "items": [], "task": "How many grams of Epoisses are currently in stock? Answer just this stock question."}
    prompt = ((ROOT / ("prompts/skills_planner.md" if skills_enabled else "prompts/event_planner.md")).read_text(encoding="utf-8")
              if prompt is None else prompt)
    prompt += "\nAuthoritative classroom shop policy:\n" + store.policy.model_dump_json()
    snapshot = run_snapshot(model, "harness", prompt, request, brief, [])
    snapshot.update({"reply_schema": None, "document": document, "demo_compaction": demo_compaction,
        "simulate_detector_miss": simulate_detector_miss, "decision_source": decision_source,
        "harness": {"todo": True, "compaction_strategy": "ContextWindowCompactionStrategy", "context_tokens": 16000,
                    "output_tokens": 2400, "max_active_seconds": 120, "max_approval_requests": 2,
                    "max_message_characters": 64000, "web_shell_arbitrary_file_access": False}})
    function_limits = {**FUNCTION_LIMITS, "max_iterations": 16} if skills_enabled else FUNCTION_LIMITS
    if skills_enabled:
        snapshot.update({"skill_files": skills_files.files, "function_limits": function_limits, "max_model_calls": 16})
        snapshot["harness"]["skills"] = True
    run_id = recorder.start_run(recorder.version(snapshot), case_id=scenario, act=5 if skills_enabled else 4,
        model="fixture-model" if fixture else model, mode="fixture" if fixture else execution_mode)
    memory, api, owned, fixture_api = None, None, False, None
    outreach = VendorOutreach(store, request, brief["items"], recorder, run_id, output_root or OUTPUT_DIR)
    outreach.require_template = skills_enabled
    model_trace = ModelTrace(recorder, run_id, progress, max_calls=16 if skills_enabled else 8,
                             model="fixture-model" if fixture else model, label="Event planner")
    tool_trace = None
    try:
        with recorder.span(run_id, "harness.event_planning", "agent"), sdk_tracing(recorder):
            default_memory = (outreach.output_root / run_id / "memory.sqlite"
                              if fixture or execution_mode == "fixture" else OUTPUT_DIR / "customer_memory.sqlite")
            memory = PreferenceMemory(memory_path or default_memory)
            if remember_preferences:
                memory.remember(customer_id, customer_id, remember_preferences, recorder, run_id)
            preferences = memory.load(customer_id, customer_id, recorder, run_id)
            recorder.event(run_id, "memory.selected", {"customer_id": customer_id, "preferences": preferences})
            if progress:
                progress(f"Memory for {customer_id}: " + "; ".join(preferences))
            todos = RecordedTodos(recorder, run_id, progress)
            todo_provider = TodoProvider(store=todos)
            session = None
            async def capsule():
                if scenario == "stock-question":
                    return {"task": brief["task"], "scope": "Stock lookup only; no event or outreach requested."}
                return {"confirmed_request": request.model_dump(mode="json"), "confirmed_menu": brief["items"],
                        "saved_preferences": preferences,
                        "precedence": "Current request wins. Saved preferences are oldest to newest; later conflicting soft preferences win. Hard constraints stay mandatory.",
                        "approved_vendor_recipient": brief["vendor_recipient"],
                        "tasks": [i.to_dict() for i in await todos.load_items(session, source_id=todo_provider.source_id)],
                        "pending_email_reviews": [r.model_dump() for t, r in outreach.reviews.items() if t not in outreach.decisions],
                        "email_decisions": dict(outreach.decisions),
                        "artifacts": {key: str(path) for key, path in outreach.artifacts.items()},
                        "open_work": "Vendor availability remains unconfirmed; no order has been placed."}
            compaction = HarnessCompaction(recorder, run_id, capsule, progress=progress)
            detector = detect_injection
            if simulate_detector_miss:
                document = "malicious"
                def detector(text):
                    return {"flagged": False, "signals": [], "detector": "forced-miss-test"}
                recorder.event(run_id, "injection.test_override", {"forced_miss": True})
            event_tools = build_event_tools(outreach, document, detector=detector, progress=progress)
            skill_provider, skill_access = None, None
            stock_observations = []
            if skills_enabled:
                from formaggio.agents.skill_support import SKILL_TOOLS, build_skills
                skill_provider, skill_access = build_skills(skills_files, recorder, run_id, outreach, progress)
                @tool
                def get_stock(product: str) -> dict:
                    """Look up current stock of one catalog product; no planning skill is needed."""
                    found = store.resolve(product)
                    if found is None:
                        return {"error": "Unknown product."}
                    result = {"product": found.product_id, "stock_g": store.inventory[found.product_id]}
                    recorder.event(run_id, "stock.checked", result)
                    stock_observations.append(result)
                    return result
                event_tools = [get_stock] if scenario == "stock-question" else [*event_tools, get_stock]
            tool_trace = HarnessToolTrace(recorder, run_id,
                [t.name for t in event_tools] + TODO_NAMES + (SKILL_TOOLS if skills_enabled else []), progress,
                model_trace=model_trace)
            if fixture:
                from formaggio.fixtures.harness_fixture import HarnessFixture
                if skills_enabled:
                    from formaggio.fixtures.skills_fixture import SkillsFixture
                    fixture_api = SkillsFixture(scenario, recipient="other@example.com" if simulate_detector_miss else brief["vendor_recipient"]).client()
                else:
                    fixture_api = HarnessFixture("other@example.com" if simulate_detector_miss else brief["vendor_recipient"]).client()
                api_client = fixture_api
                model = "fixture-model"
            middleware = [model_trace, tool_trace] + ([skill_access] if skills_enabled else [])
            client, api, owned = make_client(model, middleware, api_client, function_limits=function_limits)
            history = HarnessHistory()
            agent = create_harness_agent(client=client, name="EventPlanner",
                harness_instructions="Use the visible task list and tools. Host policies and external approvals govern actions.",
                agent_instructions=prompt, tools=event_tools, todo_provider=todo_provider, history_provider=history,
                skills_provider=skill_provider,
                max_context_window_tokens=16000, max_output_tokens=2400,
                before_compaction_strategy=compaction, after_compaction_strategy=no_post_turn_compaction,
                disable_mode=True, disable_file_memory=True, disable_web_search=True,
                default_options=MODEL_OPTIONS)
            session = agent.create_session()
            if demo_compaction:
                # Deliberately labeled history, not fabricated previous live interactions.
                old = []
                for index in range(12):
                    old.extend([Message(role="user", contents=[f"SYNTHETIC OLD PLANNING NOTE {index}: " + "table layout discussion; " * 600]),
                                Message(role="assistant", contents=["Synthetic acknowledgement; the current brief remains authoritative."])])
                await history.save_messages(session.session_id, old, state=session.state.setdefault(history.source_id, {}))
                recorder.event(run_id, "compaction.history_seeded", {"synthetic": True, "messages": len(old)})
            text = await run_with_review(agent, session, brief["task"], outreach, reviewer, recorder, run_id,
                                         decision_source=decision_source)
            task_state = [i.to_dict() for i in await todos.load_items(session, source_id=todo_provider.source_id)]
            status = "saved" if outreach.artifacts else "blocked" if tool_trace.blocked_reason else "declined" if "decline" in outreach.decisions.values() else "draft_only" if outreach.drafts else "needs_followup"
            if not tool_trace.blocked_reason and scenario in {"stock-question", "tasting-plan"}:
                status = ("answered" if stock_observations else "needs_followup") if scenario == "stock-question" else "plan_proposed"
            result = {"run_id": run_id, "status": status, "agent_text": text, "tasks": task_state,
                "model_calls": model_trace.calls, "scripted": fixture or execution_mode == "fixture",
                "compactions": compaction.count, "preferences": preferences,
                "reason": tool_trace.blocked_reason,
                "assessment": outreach.assess() if scenario != "stock-question" else None,
                "stock": stock_observations, "artifacts": [str(p) for p in outreach.artifacts.values()],
                "order_placed": False, "email_transmitted": False}
            if skill_access:
                result.update({"skills_loaded": skill_access.loaded, "skill_resources": skill_access.resources})
            recorder.event(run_id, "harness.result", result)
        recorder.finish_run(run_id, "blocked" if result["status"] == "blocked" else "completed")
        return result
    except (TimeoutError, asyncio.CancelledError):
        recorder.finish_run(run_id, "stopped", "Active execution budget exhausted or run cancelled.")
        raise
    except Exception as exc:
        reason = tool_trace.blocked_reason if tool_trace else None
        if (reason or isinstance(exc, PolicyBlocked)) and not isinstance(exc, PolicyCheckError):
            result = {"run_id": run_id, "status": "blocked", "reason": reason or str(exc),
                      "model_calls": model_trace.calls, "artifacts": [str(p) for p in outreach.artifacts.values()],
                      "order_placed": False, "email_transmitted": False}
            recorder.event(run_id, "harness.result", result)
            recorder.finish_run(run_id, "blocked", result["reason"])
            return result
        recorder.finish_run(run_id, "error", str(exc))
        raise
    finally:
        if memory:
            memory.close()
        if owned and api:
            await api.close()
        if fixture_api:
            await fixture_api.close()


async def run_act4(recorder, **kwargs):
    return await run_harness(recorder, **kwargs)
