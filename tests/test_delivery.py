import asyncio
from copy import deepcopy
import json

from acts.act5_skills import build_act5
from acts.act6_composition import build_act6
from formaggio.agents.prompt_template import render_prompt
from formaggio.agents.tasting_delivery import deliver_tasting_reply, tasting_reply_schema
from pydantic import ValidationError
from formaggio.evaluation.evaluation_checks import assess_run
from formaggio.fixtures.composition_fixture import CompositionFixture
from formaggio.fixtures.skills_fixture import SkillsFixture
from formaggio.shop.store import Store
from tests.support import RecordingTest, fixture


class DeliveryTests(RecordingTest):
    def menu(self):
        request, items = fixture()
        store = Store()
        assessment = {"report": store.validate(request, items).model_dump(mode="json"),
                      "conditional_pairings": {k: [p.model_dump(mode="json") for p in values]
                          for k, values in store.eligible_pairings(request, items).items()}}
        reply = {"message": "Here is the proposed tasting.", "plans": [{"request_id": "event",
                 "courses": [{"product": i.product, "reason": "Start with a mild course.",
                              "pairing_ids": [assessment["conditional_pairings"][i.product][0]["pairing_id"]]}
                             for i in items], "serving_notes": "Use separate utensils.", "open_questions": []}]}
        return store, {"event": (request, assessment, "proposal")}, reply

    def test_provider_schema_restricts_products_and_pairings_to_catalog_ids(self):
        store, _, reply = self.menu()
        schema = tasting_reply_schema(store)
        schema.model_validate(reply)
        for product in ("Bûcheron — 300 g", "invented"):
            invalid = deepcopy(reply)
            invalid["plans"][0]["courses"][0]["product"] = product
            with self.assertRaises(ValidationError): schema.model_validate(invalid)
        invalid = deepcopy(reply)
        invalid["plans"][0]["courses"][0]["pairing_ids"] = ["invented"]
        with self.assertRaises(ValidationError): schema.model_validate(invalid)

    def test_rendered_plan_uses_authoritative_quantities_prices_and_allergens(self):
        store, menus, reply = self.menu()
        text, ready = deliver_tasting_reply(json.dumps(reply), menus, store, self.recorder, self.run_id)
        self.assertTrue(ready)
        report = menus["event"][1]["report"]
        self.assertIn(f"${report['subtotal_cents'] / 100:.2f}", text)
        for course in reply["plans"][0]["courses"]:
            self.assertIn(store.products[course["product"]].name, text)
        self.assertIn("listed allergens:", text)
        self.assertIn("### Open questions and next actions", text)
        self.assertIn("Excludes tax, shipping and pairing costs.", text)

    def test_pairings_can_be_omitted_only_when_no_suggestions_are_available(self):
        store, menus, reply = self.menu()
        for course in reply["plans"][0]["courses"]:
            course["pairing_ids"] = []
        _, ready = deliver_tasting_reply(json.dumps(reply), menus, store, self.recorder, self.run_id)
        self.assertFalse(ready)
        request, assessment, status = menus["event"]
        assessment["conditional_pairings"] = {}
        menus["event"] = (request.model_copy(update={"wants_pairings": False}), assessment, status)
        text, ready = deliver_tasting_reply(json.dumps(reply), menus, store, self.recorder, self.run_id)
        self.assertTrue(ready)
        self.assertIn("Pairings not requested.", text)

    def test_display_names_resolve_without_allowing_duplicate_or_changed_products(self):
        store, menus, reply = self.menu()
        courses = reply["plans"][0]["courses"]
        for course in courses:
            course["product"] = store.products[course["product"]].name
        text, ready = deliver_tasting_reply(json.dumps(reply), menus, store, self.recorder, self.run_id)
        self.assertTrue(ready)
        self.assertIn("### Tasting sequence", text)
        duplicate = deepcopy(courses[0])
        duplicate["product"] = store.resolve(duplicate["product"]).product_id
        courses.append(duplicate)
        _, ready = deliver_tasting_reply(json.dumps(reply), menus, store, self.recorder, self.run_id)
        self.assertFalse(ready)

    def test_missing_duplicate_wrong_menu_and_unverified_pairings_are_not_ready(self):
        store, menus, reply = self.menu()
        candidates = ["Done.", json.dumps({"message": "Complete", "plans": []})]
        for change in ("duplicate", "wrong_product", "wrong_pairing", "wrong_request", "blank_notes"):
            value = deepcopy(reply)
            plan = value["plans"][0]
            if change == "duplicate": plan["courses"].append(plan["courses"][0])
            if change == "wrong_product": plan["courses"][0]["product"] = "invented"
            if change == "wrong_pairing": plan["courses"][0]["pairing_ids"] = ["invented"]
            if change == "wrong_request": plan["request_id"] = "other-customer"
            if change == "blank_notes": plan["serving_notes"] = " "
            candidates.append(json.dumps(value))
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                text, ready = deliver_tasting_reply(candidate, menus, store, self.recorder, self.run_id)
                self.assertFalse(ready)
                self.assertIn("Tasting plan incomplete", text)

    def test_missing_plan_fails_even_after_all_skills_and_resources_were_read(self):
        async def check():
            for calls in ([], None):
                backend = SkillsFixture("tasting-plan", calls=calls)
                backend.final_reply = {"message": "The plan is complete.", "plans": []}
                lesson = build_act5(scenario="tasting-plan", execution_mode="fixture", output_root=self.root)
                async with backend.client() as api:
                    result = await lesson.run(self.recorder, api_client=api)
                self.assertEqual(result["status"], "needs_followup")
                self.assertIn("Tasting plan incomplete", result["agent_text"])
                checks, _ = assess_run(5, {"scenario": "tasting-plan", "expect": "plan_proposed"}, result,
                                       self.recorder.timeline(result["run_id"]))
                self.assertEqual(next(c.status for c in checks if c.check_id == "outcome.tasting_delivery"), "fail")
                if calls is None:
                    self.assertEqual(next(c.status for c in checks if c.check_id == "context.required_resources"), "pass")
        asyncio.run(check())

    def test_two_order_plan_delivery_is_separate_from_successful_checkout(self):
        async def check():
            for missing in (False, True):
                backend = CompositionFixture("two-orders")
                if missing:
                    backend.final_reply = {"message": "The plan is complete.", "plans": []}
                async def approve(ticket): return "approve"
                async with backend.client() as api:
                    result = await build_act6(scenario="two-orders", execution_mode="fixture").run(
                        self.recorder, api_client=api, manager=approve)
                self.assertEqual([o["status"] for o in result["orders"]], ["placed", "blocked"])
                self.assertTrue(result["order_placed"])
                self.assertEqual(result["status"], "needs_followup" if missing else "completed")
                events = self.recorder.timeline(result["run_id"])
                delivery = next(e["payload"] for e in events if e["event_type"] == "delivery.checked")
                self.assertEqual(delivery["ready"], not missing)
                self.assertEqual(delivery["required_menus"], ["request-1"])
                if not missing:
                    self.assertIn("Host tasting plan — request-1", result["agent_text"])
                    self.assertNotIn("Host tasting plan — request-2", result["agent_text"])
                capsule = [e["payload"]["capsule"] for e in events if e["event_type"] == "context.model_input"][-1]
                self.assertLess(len(json.dumps(capsule["orders"])), len(json.dumps(result["orders"])) / 2)
                self.assertIn("report", result["orders"][0]["receipt"])
        asyncio.run(check())

    def test_feature_blocks_are_independent_of_wording(self):
        template = "Policy. <!-- if:planning -->Use\nreworded todos.<!-- endif --> <!-- if:skills -->Load skills.<!-- endif -->"
        self.assertNotIn("todos", render_prompt(template, {"skills"}))
        self.assertIn("Load skills", render_prompt(template, {"skills"}))
        self.assertIn("Policy.", render_prompt(template, set()))
        for broken in ("<!-- if:unknown -->a<!-- endif -->", "<!-- if:planning -->a", "<!-- endif -->"):
            with self.assertRaises(ValueError): render_prompt(broken, set())
