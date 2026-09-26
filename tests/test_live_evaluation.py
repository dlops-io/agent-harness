import asyncio
import copy
import json
import shutil
from unittest.mock import patch

from formaggio.shop.data_models import LineItem
from formaggio.evaluation.evaluation import compare, report_experiment
from formaggio.evaluation.evaluation_checks import assess_run
from formaggio.evaluation.live_evaluation import dispatch, run_live_suite, select_cases
from formaggio.agents.skill_support import SKILLS_ROOT
from tests.support import RecordingTest


class AgentEvaluationTests(RecordingTest):
    def suite(self, label, **kwargs):
        return asyncio.run(run_live_suite(self.recorder, label, fixture=True, repeats=kwargs.pop("repeats", 1),
                                         output_root=self.root, **kwargs))

    def test_all_acts_core_cases_use_real_runners_and_pass_offline(self):
        for act in range(1, 7):
            with self.subTest(act=act):
                report = self.suite(f"act-{act}", act=act)
                self.assertEqual(report["passed"], report["expected_runs"], report["runs"])
                self.assertEqual(report["mode"], "fixture")
                self.assertTrue(report["excluded_cases"])
                self.assertEqual(report["missing_runs"], [])
                self.assertTrue(all(r["observation"] for r in report["runs"]))

    def test_prompt_override_reaches_model_and_comparison_preserves_baseline(self):
        first = self.suite("before", act=2, case_ids=["standard"], context="enriched", prompt="PROMPT_A use tools and return a proposal")
        second = self.suite("after", act=2, case_ids=["standard"], context="enriched", prompt="PROMPT_B use tools and return a proposal")
        for report, marker in [(first, "PROMPT_A"), (second, "PROMPT_B")]:
            events = self.recorder.timeline(report["runs"][0]["run_id"])
            self.assertIn(marker, json.dumps([e for e in events if e["event_type"] == "model.request"]))
        result = compare(self.recorder, "before", "after")
        self.assertEqual(result["changed_configuration"], ["prompt"])
        self.assertEqual(result["confounding_changes"], [])
        self.assertEqual(result["results"][0]["outcome"], "unchanged")
        self.assertFalse(result["results"][0]["structured_output_changed"])
        self.assertIn("total_token_count", result["results"][0]["metric_deltas"])

    def test_repeated_workflows_have_fresh_inventory_sessions_and_distinct_receipts(self):
        report = self.suite("repeats", act=6, case_ids=["standard"], repeats=3)
        self.assertEqual(report["passed"], 3)
        receipts, sessions = [], []
        for run in report["runs"]:
            events = self.recorder.timeline(run["run_id"])
            receipt = next(e["payload"] for e in events if e["event_type"] == "order.placed")
            receipts.append(receipt["order_id"])
            sessions.append(self.recorder.query("SELECT session_id FROM runs WHERE run_id=?", (run["run_id"],))[0]["session_id"])
        self.assertEqual(len(set(receipts)), 3)
        self.assertEqual(len(set(sessions)), 3)
        self.assertEqual(len(report["case_summary"][0]["output_variants"]), 1)

    def test_skill_edit_changes_snapshot_and_actual_resource_without_code_edits(self):
        skills = self.root / "skills"
        shutil.copytree(SKILLS_ROOT, skills)
        first = self.suite("skill-before", act=5, case_ids=["tasting-plan"], skills_root=skills)
        asset = skills / "tasting-planning/assets/tasting-plan.md"
        asset.write_text(asset.read_text() + "\nCLASSROOM_RESOURCE_V2\n")
        second = self.suite("skill-after", act=5, case_ids=["tasting-plan"], skills_root=skills)
        change = compare(self.recorder, "skill-before", "skill-after")
        self.assertEqual(change["changed_configuration"], ["skills"])
        self.assertNotEqual(first["version_id"], second["version_id"])
        self.assertTrue(any("assets/tasting-plan.md" in p for p in change["changed_paths"]))
        self.assertIn("CLASSROOM_RESOURCE_V2", json.dumps(self.recorder.timeline(second["runs"][0]["run_id"])))
        self.assertNotIn("CLASSROOM_RESOURCE_V2", json.dumps(self.recorder.timeline(first["runs"][0]["run_id"])))

    def test_known_bad_cart_is_reported_as_regression(self):
        async def bad(recorder, act, case, **kwargs):
            result = await dispatch(recorder, act, case, **kwargs)
            result["reply"] = result["reply"].model_copy(update={"items": [LineItem(product="walnut_chevre", grams=1000)]})
            return result
        good = self.suite("good", act=1, case_ids=["standard"])
        bad_report = self.suite("bad", act=1, case_ids=["standard"], runner=bad)
        self.assertEqual(good["passed"], 1)
        self.assertEqual(bad_report["failed"], 1)
        self.assertEqual(compare(self.recorder, "good", "bad")["results"][0]["outcome"], "regressed")

    def test_different_valid_carts_pass_without_exact_price_or_prose_matching(self):
        captured = []
        async def capture(recorder, act, case, **kwargs):
            result = await dispatch(recorder, act, case, **kwargs)
            captured.append((case, result, recorder.timeline(result["run_id"])))
            return result
        self.suite("variants", act=1, case_ids=["standard"], runner=capture)
        case, result, events = captured[0]
        items = [LineItem(product="epoisses", grams=400), LineItem(product="bucheron", grams=350), LineItem(product="taleggio", grams=350)]
        altered = copy.deepcopy(events)
        for e in altered:
            if e["event_type"] == "proposal.submitted":
                e["payload"]["items"] = [i.model_dump() for i in items]
        result["reply"] = result["reply"].model_copy(update={"items": items, "message": "Different valid wording"})
        checks, _ = assess_run(1, case, result, altered)
        self.assertTrue(all(c.status in {"pass", "not_applicable"} for c in checks))
        mismatch, _ = assess_run(1, case, result, events)
        self.assertEqual(next(c.status for c in mismatch if c.check_id == "process.preview_matches_final"), "fail")

    def test_execution_errors_stay_in_denominator_and_stop_after_three(self):
        async def broken(*args, **kwargs):
            raise TimeoutError("Injected model timeout")
        report = self.suite("errors", act=2, case_ids=["standard"], context="enriched", repeats=5, runner=broken)
        self.assertEqual((report["expected_runs"], report["errors"], report["passed"]), (5, 3, 0))
        self.assertEqual(len(report["missing_runs"]), 2)
        self.assertTrue(all(r["observation"]["usage"]["total_token_count"] is None for r in report["runs"]))
        self.assertEqual(report_experiment(self.recorder, "errors"), report)

    def test_midbatch_skill_edit_stops_and_keeps_original_snapshot(self):
        skills = self.root / "skills"
        shutil.copytree(SKILLS_ROOT, skills)
        async def changed(recorder, act, case, **kwargs):
            result = await dispatch(recorder, act, case, **kwargs)
            path = skills / "tasting-planning/SKILL.md"
            path.write_text(path.read_text() + "\nEdited during experiment\n")
            return result
        report = self.suite("changed", act=5, case_ids=["stock-question"], repeats=2, skills_root=skills, runner=changed)
        self.assertEqual(report["errors"], 1)
        self.assertEqual(len(report["missing_runs"]), 1)
        saved = self.recorder.query("SELECT snapshot_json FROM versions WHERE version_id=?", (report["version_id"],))[0]
        self.assertNotIn("Edited during experiment", saved["snapshot_json"])

    def test_labels_and_selection_validate_before_starting_more_runs(self):
        self.suite("unique", act=1, case_ids=["standard"])
        count = self.recorder.query("SELECT count(*) AS n FROM runs")[0]["n"]
        for args in [dict(act=1, case_ids=["stock-question"]), dict(act=5, repeats=21), dict(act=2, context="wrong")]:
            with self.assertRaises(ValueError):
                self.suite("invalid", **args)
        with self.assertRaises(ValueError):
            self.suite("unique", act=1, case_ids=["standard"])
        self.assertEqual(self.recorder.query("SELECT count(*) AS n FROM runs")[0]["n"], count)

    def test_comparison_reports_unmatched_cases_and_evaluator_changes(self):
        self.suite("subset", act=1, case_ids=["standard"])
        self.suite("larger", act=1, case_ids=["standard", "missing-details"])
        comparison = compare(self.recorder, "subset", "larger")
        self.assertTrue(comparison["evaluator_changed"])
        self.assertEqual({r["outcome"] for r in comparison["results"]}, {"unmatched", "not_comparable"})

    def test_stock_case_keeps_memory_isolated_and_does_not_use_saved_preferences(self):
        paths = []
        from formaggio.agents.harness_state import PreferenceMemory
        original = PreferenceMemory.__init__
        def opened(memory, path):
            paths.append(str(path))
            original(memory, path)
        with patch.object(PreferenceMemory, "__init__", opened):
            report = self.suite("memory", act=5, case_ids=["stock-question"], repeats=2)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(len(set(paths)), 2)
        self.assertFalse(any("outputs/customer_memory.sqlite" in p for p in paths))

    def test_posthoc_email_approval_does_not_erase_unauthorized_save(self):
        captured = []
        async def capture(recorder, act, case, **kwargs):
            result = await dispatch(recorder, act, case, **kwargs)
            captured.append((case, result, recorder.timeline(result["run_id"])))
            return result
        self.suite("approval", act=5, case_ids=["event-approve"], runner=capture)
        case, result, events = captured[0]
        altered = [e for e in events if e["event_type"] != "email.approval_decided"]
        altered.extend(e for e in events if e["event_type"] == "email.approval_decided")
        checks, _ = assess_run(5, case, result, altered)
        self.assertEqual(next(c.status for c in checks if c.check_id == "process.email_authority"), "fail")
