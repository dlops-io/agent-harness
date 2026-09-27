"""Act 5 migration contracts through native skill tools and local model responses."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import itertools
import json
from pathlib import Path
import shutil
import sqlite3
from unittest.mock import patch

import httpx

from acts.act4_harness import run_harness
from acts.act5_skills import build_act5
from formaggio.agents.execution import ActiveBudget
from formaggio.agents.harness_state import PreferenceMemory
from formaggio.agents.planner_layers import PlannerBudget
from formaggio.agents.runtime import ConsoleProgress
from formaggio.agents.skill_layer import Skills
from formaggio.agents.skill_support import SKILLS_ROOT, SkillFiles
from formaggio.fixtures.skills_fixture import SkillsFixture
from tests.support import RecordingTest
from tests.test_planner_layers import normalized


class SkillLayerTests(RecordingTest):
    def configured(self, **kwargs):
        return build_act5(model="fixture-model", execution_mode="fixture", output_root=self.root / "artifacts", **kwargs)

    def events(self, result, name):
        return [e["payload"] for e in self.recorder.timeline(result["run_id"]) if e["event_type"] == name]

    def latest(self):
        return self.recorder.query("SELECT * FROM runs WHERE act=5 ORDER BY started_at DESC LIMIT 1")[0]

    async def execute(self, lesson=None, *, decision="approve", reviewer=None, backend=None, progress=None):
        lesson = lesson or self.configured()
        features = {layer.name for layer in lesson.layers}
        backend = backend or SkillsFixture(lesson.scenario, planning="planning" in features, skills="skills" in features)
        async def review(ticket): return decision
        async with backend.client() as api:
            result = await lesson.run(self.recorder, api_client=api, reviewer=reviewer or review, progress=progress)
            self.assertFalse(api.is_closed())
        return result, backend

    def test_complete_requests_match_original_driver_for_all_scenarios_and_reviews(self):
        cases = [
            ("event-shortage", "approve", {}, "161a6842c393fb992df93b826faa4d09f50221b6ab3a7df52d503678cda54478"),
            ("event-shortage", "decline", {}, "ce5217ba31da4393ff7ce6292c704ed5cb142dd3bc8afcbbcb727dd1e8bf26e0"),
            ("tasting-plan", "approve", {}, "94cc9dc120a9f87581a87d3874dfbf4894894ff3e230451f44f029cb46958998"),
            ("stock-question", "approve", {}, "04a67435df64c700f14fdfa59b444dabf6b23583651966d688a6ceacc871cef6"),
            ("event-shortage", "decline", {"demo_compaction": True, "document": "malicious"},
             "b2b741f95c75553f0fbdd13f91c9217c8717ea93e8ba15a655c9ab5d6baabd25"),
            ("event-shortage", "approve", {"customer_id": "shivas", "remember_preferences": ("Nonalcoholic today",)},
             "365a2b6f6545c83ff1a71ea466bbbe8ebedcc29196c5298d352b0f0f45739160"),
        ]
        async def check():
            for scenario, decision, options, expected in cases:
                result, backend = await self.execute(self.configured(scenario=scenario, decision_source="test", **options), decision=decision)
                self.assertEqual(hashlib.sha256(normalized(backend.requests, self.root).encode()).hexdigest(), expected,
                                 (scenario, decision, options))
                self.assertIn("skills_loaded", result)
                self.assertIn("skill_resources", result)
        asyncio.run(check())

    def test_all_64_layer_combinations_keep_template_and_approval_boundaries(self):
        names = ("trace", "budget", "planning", "memory", "compaction", "skills")
        async def check():
            for index, enabled in enumerate(itertools.product((False, True), repeat=6)):
                with self.subTest(enabled=enabled):
                    features = dict(zip(names, enabled))
                    memory = self.root / f"memory-{index}.sqlite"
                    lesson = self.configured(memory_path=memory)
                    for name, keep in features.items():
                        if not keep:
                            lesson = lesson.without(name)
                    reviews = []
                    async def reviewer(ticket):
                        reviews.append(ticket)
                        self.assertEqual(ticket.draft.recipient, "vendor@example.com")
                        return "approve"
                    result, backend = await self.execute(lesson, reviewer=reviewer)
                    self.assertEqual(result["status"], "saved" if features["skills"] else "blocked")
                    self.assertEqual(len(reviews), 1 if features["skills"] else 0)
                    self.assertEqual(len(result["artifacts"]), 1 if features["skills"] else 0)
                    self.assertEqual(result["skills_loaded"], ["tasting-planning", "vendor-outreach"] if features["skills"] else [])
                    self.assertEqual(len(result["skill_resources"]), 4 if features["skills"] else 0)
                    self.assertEqual(bool(result["tasks"]), features["planning"])
                    self.assertEqual(memory.exists(), features["memory"])
                    self.assertEqual(bool(self.events(result, "model.request")), features["trace"])
                    if not features["skills"]:
                        self.assertIn("email-template.html", result["reason"])
                        self.assertFalse(self.events(result, "skills.available"))
                    for request in backend.requests:
                        tools = {t["name"] for t in request["tools"]}
                        self.assertEqual("load_skill" in tools, features["skills"])
                        self.assertEqual("read_skill_resource" in tools, features["skills"])
                        self.assertEqual("todos_add" in tools, features["planning"])
                        self.assertNotIn("run_skill_script", tools)
                        self.assertIn("save_vendor_email", tools)
                    state = self.events(result, "context.model_input")[-1]["capsule"]
                    self.assertEqual(state["confirmed_request"]["allergies"], ["nuts"])
                    self.assertEqual(state["approved_vendor_recipient"], "vendor@example.com")
                    if features["skills"]:
                        self.assertIn("approve", state["email_decisions"].values())
                        self.assertEqual(len(self.events(result, "skill.loaded")), 2)
                        self.assertTrue(self.events(result, "policy.decision"))
                    self.assertFalse(result["order_placed"] or result["email_transmitted"])
        asyncio.run(check())

    def test_skill_removal_keeps_stock_and_tasting_tools_available(self):
        async def check():
            for scenario, status in (("stock-question", "answered"), ("tasting-plan", "plan_proposed")):
                lesson = self.configured(scenario=scenario).without("skills").without("planning")
                result, backend = await self.execute(lesson)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["skills_loaded"], [])
                self.assertEqual(result["model_calls"], 2)
                self.assertEqual(result["artifacts"], [])
                self.assertNotIn("Available skills (metadata only)", json.dumps(backend.requests))
                if scenario == "stock-question":
                    self.assertEqual(result["stock"], [{"product": "epoisses", "stock_g": 900}])
                    self.assertNotIn("save_vendor_email", {t["name"] for t in backend.requests[0]["tools"]})
        asyncio.run(check())

    def test_reused_configuration_resnapshots_edited_template_on_next_run(self):
        async def check():
            root = self.root / "skills"
            shutil.copytree(SKILLS_ROOT, root)
            lesson = self.configured(skills_root=root)
            first, _ = await self.execute(lesson)
            template = root / "vendor-outreach/assets/email-template.html"
            template.write_text(template.read_text().replace("Formaggio sourcing inquiry", "Updated classroom inquiry"))
            second, _ = await self.execute(lesson)
            self.assertIn("Formaggio sourcing inquiry", Path(first["artifacts"][0]).read_text())
            self.assertIn("Updated classroom inquiry", Path(second["artifacts"][0]).read_text())
            self.assertEqual(first["skills_loaded"], second["skills_loaded"])
            self.assertIsNot(first["skills_loaded"], second["skills_loaded"])
            versions = [self.recorder.query("SELECT version_id FROM runs WHERE run_id=?", (r["run_id"],))[0]["version_id"] for r in (first, second)]
            self.assertNotEqual(*versions)
        asyncio.run(check())

    def test_midrun_skill_change_is_blocked_before_read_without_trace(self):
        async def check():
            root = self.root / "skills"
            shutil.copytree(SKILLS_ROOT, root)
            class Edited(SkillsFixture):
                def __call__(self, request):
                    if not self.requests:
                        path = root / "tasting-planning/SKILL.md"
                        path.write_text(path.read_text() + "\nChanged during this invocation.\n")
                    return super().__call__(request)
            backend = Edited(calls=[("load_skill", {"skill_name": "tasting-planning"})])
            result, _ = await self.execute(self.configured(skills_root=root).without("trace"), backend=backend)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("changed during the run", result["reason"])
            self.assertEqual(self.events(result, "skill.load_requested"), [])
            self.assertNotIn("Changed during this invocation", json.dumps(backend.requests))
        asyncio.run(check())

    def test_skill_path_checks_and_required_audit_survive_trace_removal(self):
        async def check():
            lesson = self.configured().without("trace")
            backend = SkillsFixture(calls=[("read_skill_resource", {"skill_name": "tasting-planning", "resource_name": "../../config.py"})])
            result, _ = await self.execute(lesson, backend=backend)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(self.events(result, "skill.resource_read"), [])
            original = self.recorder.event
            def event(run_id, name, payload=None, **kwargs):
                if name == "skill.load_requested":
                    raise sqlite3.OperationalError("Required skill audit unavailable")
                return original(run_id, name, payload, **kwargs)
            backend = SkillsFixture()
            with patch.object(self.recorder, "event", side_effect=event):
                with self.assertRaises(Exception):
                    await self.execute(lesson, backend=backend)
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(self.latest()["status"], "error")
        asyncio.run(check())

    def test_skill_initialization_and_client_failure_clean_up_owned_resources(self):
        async def check():
            for target in ("formaggio.agents.skill_support.ReadOnlySkillsProvider.from_paths",
                           "formaggio.agents.planner_runtime.make_client"):
                api, memories = SkillsFixture().client(), []
                def memory(path):
                    value = PreferenceMemory(path)
                    memories.append(value)
                    return value
                with patch("formaggio.agents.planner_runtime.PreferenceMemory", side_effect=memory), \
                     patch("formaggio.agents.planner_runtime.AsyncOpenAI", return_value=api) as constructor, \
                     patch(target, side_effect=RuntimeError("Initialization failed")):
                    with self.assertRaises(RuntimeError):
                        await replace(self.configured(), execution_mode="live").run(self.recorder)
                if target.endswith("make_client"):
                    self.assertTrue(api.is_closed())
                else:
                    constructor.assert_not_called()
                    await api.close()  # Test client was never handed to the runtime.
                self.assertEqual(self.latest()["status"], "error")
                with self.assertRaises(sqlite3.ProgrammingError):
                    memories[0].db.execute("SELECT 1")
        asyncio.run(check())

    def test_invalid_snapshot_fails_before_run_but_removed_layer_does_not_read_it(self):
        async def check():
            lesson = build_act5(fixture=True, scenario="stock-question", skills_root=self.root / "missing", output_root=self.root)
            before = len(self.recorder.query("SELECT * FROM runs"))
            with self.assertRaises(ValueError):
                await lesson.run(self.recorder)
            self.assertEqual(len(self.recorder.query("SELECT * FROM runs")), before)
            result = await lesson.without("skills").run(self.recorder)
            self.assertEqual(result["status"], "answered")
        asyncio.run(check())

    def test_overlapping_runs_keep_loaded_skills_and_customer_state_isolated(self):
        async def check():
            ready, arrivals = asyncio.Event(), []
            class Overlap(SkillsFixture):
                async def __call__(self, request):
                    if not self.requests:
                        arrivals.append(self)
                        if len(arrivals) == 2: ready.set()
                        await asyncio.wait_for(ready.wait(), 5)
                    return super().__call__(request)
            lesson = self.configured(scenario="tasting-plan", customer_id="pavlos")
            first, second = await asyncio.gather(
                self.execute(lesson, backend=Overlap("tasting-plan")),
                self.execute(replace(lesson, scenario="stock-question", customer_id="shivas"), backend=Overlap("stock-question")))
            self.assertEqual(first[0]["skills_loaded"], ["tasting-planning"])
            self.assertEqual(second[0]["skills_loaded"], [])
            self.assertEqual(second[0]["model_calls"], 2)
            self.assertNotEqual(first[0]["run_id"], second[0]["run_id"])
            self.assertNotIn("shivas", json.dumps(first[1].requests))
            self.assertNotIn("pavlos", json.dumps(second[1].requests))
            third, _ = await self.execute(lesson)
            self.assertEqual(third["skills_loaded"], ["tasting-planning"])
            self.assertEqual(third["model_calls"], first[0]["model_calls"])
        asyncio.run(check())

    def test_skill_calls_consume_shared_budget_across_review(self):
        async def check():
            for trace in (False, True):
                for budget, expected in ((PlannerBudget(model_calls=11), 11), (PlannerBudget(model_calls=16, tool_calls=11), 12)):
                    lesson = self.configured().without("budget").add(budget)
                    if not trace: lesson = lesson.without("trace")
                    lesson = replace(lesson, layers=tuple(reversed(lesson.layers)))
                    backend, reviews = SkillsFixture(), []
                    async def reviewer(ticket): reviews.append(ticket); return "approve"
                    with self.assertRaises(Exception):
                        await self.execute(lesson, backend=backend, reviewer=reviewer)
                    self.assertEqual(len(backend.requests), expected)
                    self.assertEqual(len(reviews), 1)
                    self.assertEqual(self.latest()["status"], "error")
                    self.assertEqual(len(self.events({"run_id": self.latest()["run_id"]}, "skill.resource_read")), 4)
        asyncio.run(check())

    def test_active_time_is_cumulative_and_excludes_host_review(self):
        async def check():
            clock, timers = [0.0], []
            def timer(seconds):
                value = ActiveBudget(seconds, clock=lambda: clock[0]); timers.append(value); return value
            class Timed(SkillsFixture):
                def __call__(self, request):
                    clock[0] += 1
                    return super().__call__(request)
            async def reviewer(ticket):
                self.assertEqual(timers[0].remaining, 9)
                clock[0] += 3600
                self.assertEqual(timers[0].remaining, 9)
                return "decline"
            lesson = self.configured().without("budget").add(PlannerBudget(model_calls=16, seconds=20))
            with patch("formaggio.agents.layers.ActiveBudget", side_effect=timer):
                result, _ = await self.execute(lesson, backend=Timed(), reviewer=reviewer)
            self.assertEqual(result["status"], "declined")
            self.assertEqual(timers[0].remaining, 7)
        asyncio.run(check())

    def test_later_provider_error_is_not_hidden_by_missing_template_block(self):
        class Failed(SkillsFixture):
            def __call__(self, request):
                if self.requests:
                    self.requests.append(json.loads(request.content))
                    return httpx.Response(500, json={"error": {"message": "Provider failed", "type": "server_error"}})
                return super().__call__(request)
        async def check():
            backend = Failed(calls=[("draft_vendor_email", {"recipient": "vendor@example.com"})])
            with self.assertRaises(Exception):
                await self.execute(backend=backend)
            self.assertEqual(len(backend.requests), 2)
            self.assertEqual(self.latest()["status"], "error")
            self.assertFalse(list(self.root.rglob("*.html")))
        asyncio.run(check())

    def test_compact_display_and_json_preserve_complete_requests(self):
        async def check():
            requests = []
            for detailed in (False, True):
                output = io.StringIO()
                _, backend = await self.execute(progress=ConsoleProgress(output.write, show_json=detailed))
                requests.append(normalized(backend.requests, self.root))
                self.assertEqual('"skill_name"' in output.getvalue(), detailed)
                self.assertNotIn("local-fixture-key", output.getvalue())
            self.assertEqual(*requests)
        asyncio.run(check())

    def test_compatibility_wrapper_and_immutable_configuration(self):
        lesson = self.configured()
        with self.assertRaises(FrozenInstanceError): lesson.scenario = "stock-question"
        with self.assertRaises(ValueError): lesson.add(Skills())
        for name in ("skill_access", "approval", "template_check"):
            with self.assertRaises(ValueError): lesson.without(name)
        result = asyncio.run(run_harness(self.recorder, skills_files=SkillFiles(), fixture=True,
                                        scenario="stock-question", output_root=self.root / "compatibility"))
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["skills_loaded"], [])
