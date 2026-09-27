"""Recorded visuals must preserve task identity, outcomes and audit boundaries."""
import asyncio
from hashlib import sha256
import json
from unittest.mock import patch

from acts.act1_agent import build_act1
from acts.act3_workflow import build_act3
from acts.act4_harness import build_act4
from acts.act5_skills import build_act5
from acts.act6_composition import build_act6
from formaggio.operations.chat_view import build_chat, load_run, render_chat, render_run, show_chat
from formaggio.operations.observability import Recorder
from tests.support import RecordingTest
from tests.test_context import ScriptedResponses
from tests.test_layers import local_api


def event(name, payload):
    return {"event_type": name, "payload": payload}


async def approve(_):
    return "approve"


class ChatViewTests(RecordingTest):
    def view(self, result):
        return build_chat(**load_run(self.recorder, result["run_id"]))

    def test_final_answer_survives_trace_and_cart_check_removal(self):
        async def check():
            for checked in (True, False):
                lesson = build_act1(execution_mode="fixture").without("trace")
                if not checked:
                    lesson = lesson.without("cart_check")
                async with local_api(ScriptedResponses()) as api:
                    result = await lesson.run(self.recorder, api_client=api)
                view = self.view(result)
                self.assertEqual(view["answer"], result["reply"].message)
                self.assertFalse(view["trace_enabled"])
                self.assertTrue(view["events"])
                self.assertTrue(any(("passed" if checked else "not_run") in c["title"] for c in view["cards"]))
                html = render_run(self.recorder, result["run_id"])
                self.assertIn("Detailed model tracing disabled", html)
                self.assertIn("Proposal only", html)
        asyncio.run(check())

    def test_internal_model_responses_are_never_customer_answers(self):
        raw = '{"items":[{"product":"internal-only-product","grams":300}]}'
        events = [event("model.response", {"messages": [{"contents": [{"type": "text", "text": raw}]}]}),
                  event("harness.result", {"agent_text": "The final tasting plan.", "status": "plan_proposed"})]
        view = build_chat(events)
        self.assertEqual(view["answer"], "The final tasting plan.")
        html = render_chat(events, backstage=False)
        self.assertNotIn("internal-only-product", html)
        self.assertEqual(html.count("The final tasting plan."), 1)
        self.assertIn("internal-only-product", render_chat(events))
        self.assertEqual(build_chat(events[:1])["answer"], "")

    def test_workflow_receipt_is_application_output_and_revisions_are_evidenced(self):
        result = asyncio.run(build_act3(fixture=True, scenario="out-of-stock").run(self.recorder))
        view = self.view(result)
        self.assertEqual(view["answer"], "")
        self.assertTrue(any(c["title"] == "Mock order receipt" for c in view["cards"]))
        self.assertIn("Failed · attempt 1", view["groups"]["Cart checks"][0])
        self.assertIn("Passed · attempt 2", view["groups"]["Cart checks"][1])
        self.assertIn("Scripted responses", render_run(self.recorder, result["run_id"]))

    def test_planner_uses_task_brief_and_compaction_counts_from_current_schema(self):
        result = asyncio.run(build_act4(fixture=True, demo_compaction=True, document="malicious",
            output_root=self.root).run(self.recorder, reviewer=approve))
        view = self.view(result)
        self.assertEqual(view["requests"][0]["label"], "Recorded task brief")
        self.assertIn("sourcing plan", view["requests"][0]["text"])
        self.assertTrue(any("characters · input event" in line for line in view["groups"]["Context"]))
        self.assertTrue(any("Email reviewer approved" in line for line in view["groups"]["Human review"]))
        self.assertIn("HTML email saved locally", render_run(self.recorder, result["run_id"]))

    def test_stock_question_and_tasting_have_correct_tasks_and_one_final_answer(self):
        async def check():
            for scenario in ("stock-question", "tasting-plan"):
                result = await build_act5(fixture=True, scenario=scenario, output_root=self.root).run(self.recorder)
                view = self.view(result)
                self.assertEqual(view["answer"], result["agent_text"])
                html = render_run(self.recorder, result["run_id"], backstage=False)
                self.assertEqual(html.count("Assistant · final application response"), 1)
                if scenario == "stock-question":
                    self.assertIn("How many grams", view["requests"][0]["text"])
                    self.assertNotIn("help me order", view["requests"][0]["text"])
                    self.assertTrue(any(c["title"] == "Recorded stock" for c in view["cards"]))
                else:
                    resources = view["groups"]["Skills and supporting files"]
                    self.assertTrue(any(line.startswith("Skill: ") for line in resources))
                    self.assertTrue(any(line.startswith("Resource: ") for line in resources))
                    self.assertIn("Tasting-plan delivery check: passed", html)
        asyncio.run(check())

    def test_two_orders_keep_distinct_requests_outcomes_and_delivery(self):
        result = asyncio.run(build_act6(fixture=True, scenario="two-orders", decision_source="fixture").run(
            self.recorder, manager=approve))
        view = self.view(result)
        self.assertEqual(len(view["requests"]), 2)
        self.assertTrue(view["requests"][0]["label"].startswith("request-1"))
        self.assertTrue(view["requests"][1]["label"].startswith("request-2"))
        titles = [c["title"] for c in view["cards"]]
        self.assertIn("request-1: placed", titles)
        self.assertIn("request-2: blocked", titles)
        self.assertIn("Tasting-plan delivery check: passed", titles)
        self.assertEqual(view["answer"], result["agent_text"])
        self.assertTrue(any("request-1 · Manager approved" in row for row in view["groups"]["Human review"]))
        self.assertTrue(any("request-2 · Manager approved" in row for row in view["groups"]["Human review"]))

    def test_order_success_does_not_hide_failed_delivery(self):
        view = build_chat([
            event("delivery.checked", {"ready": False, "required_menus": ["request-1"],
                                       "delivered_menus": [], "errors": ["Missing tasting plan"]}),
            event("composition.result", {"status": "needs_followup", "agent_text": "Order placed.",
                "orders": [{"request_id": "request-1", "status": "placed", "receipt": {"order_id": "receipt-1"}}]})])
        cards = {card["title"]: card for card in view["cards"]}
        self.assertEqual(cards["request-1: placed"]["tone"], "good")
        self.assertEqual(cards["Tasting-plan delivery check: incomplete"]["tone"], "bad")
        self.assertIn("Missing tasting plan", cards["Tasting-plan delivery check: incomplete"]["body"])

    def test_failure_pending_unknown_and_missing_delivery_remain_visible(self):
        failure = [event("model.failed", {"call_number": 1, "error": "Fixture timeout"}),
                   event("run.finished", {"status": "error", "error": "Fixture timeout"})]
        html = render_chat(failure, backstage=False)
        self.assertIn("Execution: error", html)
        self.assertIn("Fixture timeout", html)
        self.assertIn("Final result not recorded", html)
        pending = build_chat([event("run.started", {"mode": "fixture"}),
            event("composition.order_state", {"request_id": "request-1", "status": "pending_approval"})],
            run={"status": "running"})
        self.assertEqual(pending["execution"], "running")
        self.assertTrue(any(c["title"] == "request-1: pending_approval" for c in pending["cards"]))
        self.assertIn("future.event", render_chat([event("future.event", {"new_schema": True})]))
        missing = render_chat([event("composition.result", {"status": "needs_followup", "orders": []})])
        self.assertIn("Tasting-plan delivery check: not recorded", missing)
        no_plan = render_chat([event("delivery.checked", {"ready": True, "required_menus": [], "delivered_menus": []})])
        self.assertIn("No accepted menus required a tasting plan", no_plan)

    def test_html_escapes_every_surface_and_customer_view_hides_internal_payloads(self):
        attack = '<script>alert("x")</script><img src=x onerror=alert(1)>'
        events = [event("agent.result", {"message": "## Heading\n**Readable** " + attack,
                                        "cart_check_status": "not_run"}),
                  event("tool.completed", {"name": attack, "result": {"private_detail": attack}})]
        html = render_chat(events, title=attack, ask=attack)
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<strong>Readable</strong>", html)
        self.assertIn("returned (see result for business outcome)", html)
        self.assertNotIn("private_detail", render_chat(events, backstage=False))
        self.assertNotIn("Inspect recorded events", render_chat(events, backstage=False))

    def test_sparse_old_events_and_oversized_evidence_are_inspectable(self):
        events = [event("cart.validated", {"subtotal_cents": 500, "violations": []}),
                  event("compaction.applied", {"before": [1, 2], "after": [1]}),
                  event("unrecognized", {"text": "x" * 9000 + "TAIL"})]
        html = render_chat(events)
        self.assertIn("Passed: $5.00", html)
        self.assertIn("? → ? characters", html)
        self.assertIn("Display shortened", html)
        self.assertNotIn("TAIL", html)

    def test_path_reads_are_read_only_and_work_after_recorder_closes(self):
        path = self.root / "path with spaces #1.sqlite"
        with Recorder(path) as recorder:
            version = recorder.version({"request": {}})
            run_id = recorder.start_run(version, act=1)
            recorder.event(run_id, "agent.result", {"message": "Visible answer", "cart_check_status": "not_run"})
            recorder.finish_run(run_id)
        before = sha256(path.read_bytes()).hexdigest()
        html = render_run(path, run_id)
        self.assertIn("Visible answer", html)
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), before)
        with self.assertRaisesRegex(ValueError, "database path"):
            render_run(recorder, run_id)
        with self.assertRaises(ValueError):
            render_run(path, "missing-run")
        missing = self.root / "nonexistent.sqlite"
        with self.assertRaises(FileNotFoundError):
            render_run(missing, run_id)
        self.assertFalse(missing.exists())
        with patch("formaggio.operations.chat_view._display_html") as display:
            self.assertIsNone(show_chat(path, run_id))
            display.assert_called_once_with(html)

    def test_rendering_retains_recorder_redaction(self):
        self.recorder.event(self.run_id, "agent.result", {"message": "unit-test-credential", "cart_check_status": "not_run"})
        self.recorder.event(self.run_id, "new.event", {"api_key": "hidden-value"})
        html = render_run(self.recorder, self.run_id)
        self.assertNotIn("unit-test-credential", html)
        self.assertNotIn("hidden-value", html)
        self.assertIn("[REDACTED]", html)
