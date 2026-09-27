"""Invocation-local Acts 4–5 runtime; lesson code supplies the planner factory."""
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from agent_framework import ContextWindowCompactionStrategy, Message, TodoProvider
from openai import AsyncOpenAI

from formaggio.agents.context import load_scenario
from formaggio.agents.execution import ExecutionState, ModelControl, RecordedExecution, recorded_trace
from formaggio.agents.harness_state import ApplicationContext, HarnessHistory, PreferenceMemory
from formaggio.agents.planner_layers import Compaction, Memory, PlannerBudget, PlannerTrace, Planning
from formaggio.agents.planner_review import run_with_review
from formaggio.agents.prompt_template import render_prompt
from formaggio.agents.skill_layer import Skills
from formaggio.agents.skill_support import SKILL_TOOLS
from formaggio.agents.planner_tools import HarnessToolTrace, RecordedTodos, TODO_NAMES, build_event_tools, build_stock_tool
from formaggio.agents.runtime import CONTEXT_WINDOW_TOKENS, MAX_OUTPUT_TOKENS, ModelTrace, console_progress, make_client, run_snapshot
from formaggio.agents.tasting_delivery import DELIVERY_INSTRUCTIONS, deliver_tasting_reply, tasting_reply_schema
from formaggio.config import MODEL, OUTPUT_DIR, ROOT, load_json
from formaggio.operations.governance import PolicyBlocked, PolicyCheckError
from formaggio.shop.store import Store
from formaggio.shop.vendor_outreach import VendorOutreach, detect_injection


@dataclass(frozen=True)
class PlannerHarness:
    """Reusable configuration. Clients, sessions, task state and reviews are local to run()."""
    agent_factory: Callable
    act: int = 4
    scenario_loader: Callable = load_scenario
    model: str = MODEL
    scenario: str = "event-shortage"
    fixture: bool = False
    document: str = "clean"
    demo_compaction: bool = False
    simulate_detector_miss: bool = False
    customer_id: str | None = None
    remember_preferences: tuple = ()
    memory_path: object = None
    output_root: object = None
    decision_source: str = "human"
    execution_mode: str = "live"
    prompt: str | None = None
    layers: tuple = ()

    def __post_init__(self):
        if self.act not in {4, 5}:
            raise ValueError("The planner harness supports Acts 4–5.")
        if self.scenario not in ({"event-shortage", "tasting-plan", "stock-question"} if self.act == 5 else {"event-shortage"}):
            raise ValueError("Act 4 supports event-shortage; Act 5 also supports tasting-plan and stock-question.")
        if self.execution_mode not in {"live", "fixture"}:
            raise ValueError("Unsupported execution mode.")
        if self.simulate_detector_miss and not self.fixture:
            raise ValueError("The forced detector-miss demonstration is restricted to --fixture.")
        if any(not isinstance(layer, (PlannerTrace, PlannerBudget, Planning, Memory, Compaction, Skills)) for layer in self.layers):
            raise TypeError("Unsupported planner layer.")
        if self.act == 4 and any(isinstance(layer, Skills) for layer in self.layers):
            raise ValueError("Use build_act5 to add skills to the planner.")
        if len({layer.name for layer in self.layers}) != len(self.layers):
            raise ValueError("Use only one layer of each name.")

    def add(self, layer):
        return replace(self, layers=(*self.layers, layer))

    def without(self, name):
        if not any(layer.name == name for layer in self.layers):
            raise ValueError(f"No layer named {name!r}.")
        return replace(self, layers=tuple(layer for layer in self.layers if layer.name != name))

    async def run(self, recorder, *, reviewer=None, progress=None, api_client=None):
        if self.execution_mode == "fixture" and not self.fixture and api_client is None:
            raise ValueError("Fixture mode requires an explicitly local client or --fixture.")
        if self.document not in load_json("vendor_documents.json"):
            raise ValueError("Unknown vendor document.")
        features = {layer.name: layer for layer in self.layers}
        if self.demo_compaction and "compaction" not in features:
            raise ValueError("The synthetic compaction demonstration requires the Compaction layer.")
        files = features["skills"].snapshot() if "skills" in features else None
        request = self.scenario_loader("standard" if self.scenario in {"tasting-plan", "stock-question"} else self.scenario)
        customer_id = request.customer_id if self.customer_id is None else self.customer_id
        if customer_id not in {c["customer_id"] for c in load_json("customers.json")}:
            raise ValueError("Choose a synthetic customer from customers.json.")
        request = request.model_copy(update={"customer_id": customer_id, "preferences": self.remember_preferences})
        store, brief = Store(), load_json("event_brief.json")
        if self.scenario == "tasting-plan":
            brief = {**brief, "items": load_json("workflow_proposals.json")["standard"][0],
                     "task": "Prepare a host tasting plan for the confirmed 12-person menu, including serving sequence, pairings, quote and open questions. This task does not request vendor outreach or order placement."}
        elif self.scenario == "stock-question":
            brief = {**brief, "items": [], "task": "How many grams of Epoisses are currently in stock? Answer just this stock question."}
        progress = console_progress(progress)
        prompt_file = "prompts/skills_planner.md" if self.act == 5 else "prompts/event_planner.md"
        prompt = (ROOT / prompt_file).read_text(encoding="utf-8") if self.prompt is None else self.prompt
        prompt = render_prompt(prompt, features)
        if "planning" not in features:
            prompt += "\nThe task-list feature is disabled for this run. Use the available event tools directly."
        if self.act == 5 and "skills" not in features:
            prompt += "\nSkill discovery is disabled. A sourcing draft still requires the approved email template; report that limitation if unavailable."
        needs_plan = self.act == 5 and self.scenario == "tasting-plan"
        reply_schema = tasting_reply_schema(store) if needs_plan else None
        if needs_plan:
            prompt += DELIVERY_INSTRUCTIONS
        prompt += "\nAuthoritative classroom shop policy:\n" + store.policy.model_dump_json()
        state = ExecutionState()
        if "budget" in features:
            features["budget"].configure(state)
        model = "fixture-model" if self.fixture else self.model
        snapshot = run_snapshot(self.model, "harness", prompt, request, brief, [])
        snapshot.update(reply_schema=reply_schema.model_json_schema() if reply_schema else None, document=self.document, demo_compaction=self.demo_compaction,
            simulate_detector_miss=self.simulate_detector_miss, decision_source=self.decision_source,
            layers=[layer.configuration() for layer in self.layers], function_limits=dict(state.limits),
            max_model_calls=state.max_model_calls, max_tool_calls=state.max_tool_calls,
            harness={"todo": "planning" in features, "memory": "memory" in features,
                "compaction_strategy": "ContextWindowCompactionStrategy" if "compaction" in features else None,
                "context_tokens": CONTEXT_WINDOW_TOKENS, "output_tokens": MAX_OUTPUT_TOKENS,
                "max_active_seconds": features["budget"].seconds if "budget" in features else None,
                "max_approval_requests": 2, "max_message_characters": 64000, "web_shell_arbitrary_file_access": False})
        if self.act == 5:
            snapshot["harness"]["skills"] = files is not None
            snapshot["skill_files"] = files.files if files is not None else {}
        async with RecordedExecution(recorder, snapshot, case_id=self.scenario, act=self.act, model=model,
                                     mode="fixture" if self.fixture else self.execution_mode) as execution:
            run_id = execution.run_id
            outreach = VendorOutreach(store, request, brief["items"], recorder, run_id, Path(self.output_root or OUTPUT_DIR))
            outreach.require_template = self.act == 5
            tool_trace = None
            try:
                with (recorded_trace(recorder, run_id, "harness.event_planning") if "trace" in features else nullcontext()):
                    preferences = []
                    if "memory" in features:
                        default_memory = (outreach.output_root / run_id / "memory.sqlite"
                                          if self.fixture or self.execution_mode == "fixture" else OUTPUT_DIR / "customer_memory.sqlite")
                        memory = execution.own(PreferenceMemory(Path(self.memory_path or default_memory)))
                        if self.remember_preferences:
                            memory.remember(customer_id, customer_id, self.remember_preferences, recorder, run_id)
                        preferences = memory.load(customer_id, customer_id, recorder, run_id)
                        recorder.event(run_id, "memory.selected", {"customer_id": customer_id, "preferences": preferences})
                        if progress:
                            progress(f"Memory for {customer_id}: " + "; ".join(preferences))
                    todos = RecordedTodos(recorder, run_id, progress) if "planning" in features else None
                    todo_provider = TodoProvider(store=todos) if todos is not None else None
                    session = None
                    async def task_state():
                        return [i.to_dict() for i in await todos.load_items(session, source_id=todo_provider.source_id)] if todos else []
                    async def capsule():
                        if self.scenario == "stock-question":
                            return {"task": brief["task"], "scope": "Stock lookup only; no event or outreach requested."}
                        return {"confirmed_request": request.model_dump(mode="json"), "confirmed_menu": brief["items"],
                            "saved_preferences": preferences,
                            "precedence": "Current request wins. Saved preferences are oldest to newest; later conflicting soft preferences win. Hard constraints stay mandatory.",
                            "approved_vendor_recipient": brief["vendor_recipient"], "tasks": await task_state(),
                            "pending_email_reviews": [r.model_dump() for t, r in outreach.reviews.items() if t not in outreach.decisions],
                            "email_decisions": dict(outreach.decisions),
                            "artifacts": {key: str(path) for key, path in outreach.artifacts.items()},
                            "open_work": "Vendor availability remains unconfirmed; no order has been placed."}
                    native = (ContextWindowCompactionStrategy(max_context_window_tokens=CONTEXT_WINDOW_TOKENS,
                              max_output_tokens=MAX_OUTPUT_TOKENS, keep_last_tool_call_groups=1) if "compaction" in features else None)
                    context = ApplicationContext(recorder, run_id, capsule, progress=progress, native=native,
                                                 details="trace" in features)
                    document, detector = self.document, detect_injection
                    if self.simulate_detector_miss:
                        document = "malicious"
                        def detector(text):
                            return {"flagged": False, "signals": [], "detector": "forced-miss-test"}
                        recorder.event(run_id, "injection.test_override", {"forced_miss": True})
                    tools = build_event_tools(outreach, document, detector=detector, progress=progress)
                    skill_provider, skill_access, stock = None, None, []
                    if files is not None:
                        skill_provider, skill_access = features["skills"].attach(files, recorder, run_id, outreach, progress)
                    if self.act == 5:
                        get_stock = build_stock_tool(store, recorder, run_id, stock)
                        tools = [get_stock] if self.scenario == "stock-question" else [*tools, get_stock]
                    tool_trace = HarnessToolTrace(recorder, run_id,
                        [t.name for t in tools] + (TODO_NAMES if todos else []) + (SKILL_TOOLS if files is not None else []),
                        progress if "trace" in features else None, details="trace" in features, execution_state=state)
                    middleware = [ModelControl(state)]
                    if "trace" in features:
                        middleware.append(ModelTrace(recorder, run_id, progress,
                                                      model=model, label=features["trace"].label))
                    middleware.append(tool_trace)
                    if skill_access is not None:
                        middleware.append(skill_access)
                    if self.fixture:
                        recipient = "other@example.com" if self.simulate_detector_miss else brief["vendor_recipient"]
                        if self.act == 5:
                            from formaggio.fixtures.skills_fixture import SkillsFixture
                            fixture = SkillsFixture(self.scenario, recipient=recipient,
                                                    planning=todos is not None, skills=files is not None)
                        else:
                            from formaggio.fixtures.harness_fixture import HarnessFixture
                            fixture = HarnessFixture(recipient, planning=todos is not None)
                        api = execution.own(fixture.client())
                    else:
                        api = api_client if api_client is not None else execution.own(AsyncOpenAI(timeout=45, max_retries=0))
                    client = make_client(model, middleware, api, function_limits=state.limits)
                    history = HarnessHistory()
                    skill_options = {"skills_provider": skill_provider} if self.act == 5 else {}
                    agent = self.agent_factory(client, tools, prompt, history=history, context=context,
                                               todo_provider=todo_provider, **skill_options)
                    session = agent.create_session()
                    if self.demo_compaction:
                        old = []
                        for index in range(12):
                            old.extend([Message(role="user", contents=[f"SYNTHETIC OLD PLANNING NOTE {index}: " + "table layout discussion; " * 600]),
                                        Message(role="assistant", contents=["Synthetic acknowledgement; the current brief remains authoritative."])])
                        await history.save_messages(session.session_id, old, state=session.state.setdefault(history.source_id, {}))
                        recorder.event(run_id, "compaction.history_seeded", {"synthetic": True, "messages": len(old)})
                    text = await run_with_review(agent, session, brief["task"], outreach, reviewer, recorder, run_id,
                                                 decision_source=self.decision_source, active_budget=state.active_budget,
                                                 reply_schema=reply_schema)
                    assessment = outreach.assess() if self.scenario != "stock-question" else None
                    delivered = False
                    if needs_plan:
                        menus = {"event": (request, assessment, "Proposal only; no order placed")} if assessment["valid_for_sourcing"] else {}
                        text, delivered = deliver_tasting_reply(text, menus, store, recorder, run_id)
                    status = "saved" if outreach.artifacts else "blocked" if tool_trace.blocked_reason else "declined" if "decline" in outreach.decisions.values() else "draft_only" if outreach.drafts else "needs_followup"
                    if not tool_trace.blocked_reason and self.scenario in {"stock-question", "tasting-plan"}:
                        status = ("answered" if stock else "needs_followup") if self.scenario == "stock-question" else "plan_proposed" if delivered and menus else "needs_followup"
                    result = {"run_id": run_id, "status": status, "agent_text": text, "tasks": await task_state(),
                        "model_calls": state.model_calls, "scripted": self.fixture or self.execution_mode == "fixture",
                        "compactions": context.count, "preferences": preferences, "reason": tool_trace.blocked_reason,
                        "assessment": assessment, "stock": stock, "artifacts": [str(p) for p in outreach.artifacts.values()],
                        "order_placed": False, "email_transmitted": False}
                    if self.act == 5:
                        result.update(skills_loaded=skill_access.loaded if skill_access else [],
                                      skill_resources=skill_access.resources if skill_access else [])
                    recorder.event(run_id, "harness.result", result)
            except PolicyCheckError:
                raise
            except PolicyBlocked as exc:
                # Only a current, expected policy rejection is a blocked result.
                # A prior blocked tool must never hide a later provider/audit failure.
                result = {"run_id": run_id, "status": "blocked", "reason": str(exc),
                    "model_calls": state.model_calls, "artifacts": [str(p) for p in outreach.artifacts.values()],
                    "order_placed": False, "email_transmitted": False}
                recorder.event(run_id, "harness.result", result)
                execution.set_outcome("blocked", result["reason"])
                return result
            execution.set_outcome("blocked" if result["status"] == "blocked" else "completed")
            return result
