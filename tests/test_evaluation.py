import copy
import sqlite3
from unittest.mock import patch

from formaggio.config import load_json
from formaggio.evaluation.evaluation import assess, compare, read_proposals, run_suite
from formaggio.operations.governance import Governance
from formaggio.shop.store import Store
from tests.support import RecordingTest, fixture


class EvaluationTests(RecordingTest):
    def test_entire_suite_including_expected_rejections(self):
        report=run_suite(self.recorder,"suite")
        self.assertEqual(report["expected_runs"],11)
        self.assertEqual(report["passed"],11)
        self.assertTrue(all(r["run_status"]=="completed" for r in report["runs"]))

    def test_evaluator_catches_known_wrong_expectation(self):
        case=copy.deepcopy(load_json("evaluation_cases.json")[0])
        case["expected_subtotal_cents"]=1
        request,items=fixture()
        checks=assess(case,Store().validate(request,items))
        self.assertEqual(next(c for c in checks if c.check_id=="subtotal").status,"fail")
        report=run_suite(self.recorder,"wrong",cases=[case])
        self.assertEqual(report["passed"],0)
        self.assertEqual(report["completed_records"],1)

    def test_each_repetition_has_fresh_inventory_and_session(self):
        seen=[]
        def propose(case,scenario,store):
            seen.append(store.inventory["gouda"])
            store.inventory["gouda"]=0
            return read_proposals()[case["fixture_id"]]
        case=load_json("evaluation_cases.json")[0]
        report=run_suite(self.recorder,"repeat",repeats=3,cases=[case],propose=propose)
        self.assertEqual(seen,[4500,4500,4500])
        self.assertEqual(report["passed"],3)
        rows=self.recorder.query("SELECT session_id FROM runs WHERE experiment_id IS NOT NULL")
        self.assertEqual(len({r["session_id"] for r in rows}),3)

    def test_prompt_comparison_retains_snapshots(self):
        case=load_json("evaluation_cases.json")[0]
        run_suite(self.recorder,"a",cases=[case],prompt="first")
        run_suite(self.recorder,"b",cases=[case],prompt="second")
        result=compare(self.recorder,"a","b")
        self.assertEqual(result["changed_configuration"],["prompt"])
        self.assertEqual(result["confounding_changes"],[])
        self.assertFalse(result["evaluator_changed"])
        self.assertEqual(result["results"][0]["outcome"],"unchanged")
        with self.assertRaises(sqlite3.IntegrityError):
            run_suite(self.recorder,"a",cases=[case])

    def test_regression_errors_and_unmatched_cases_are_visible(self):
        case=load_json("evaluation_cases.json")[0]
        run_suite(self.recorder,"good",cases=[case],repeats=2)
        def broken(*_):
            raise TimeoutError("fixture timeout")
        bad=run_suite(self.recorder,"bad",cases=[case],propose=broken)
        self.assertEqual(bad["passed"],0)
        self.assertEqual(bad["expected_runs"],1)
        self.assertEqual(bad["runs"][0]["run_status"],"error")
        result=compare(self.recorder,"good","bad")
        self.assertEqual([r["outcome"] for r in result["results"]],["regressed","unmatched"])

    def test_evaluator_change_is_not_silent_improvement(self):
        case=copy.deepcopy(load_json("evaluation_cases.json")[0])
        run_suite(self.recorder,"before",cases=[case])
        case["expected_subtotal_cents"]=1
        run_suite(self.recorder,"after",cases=[case])
        report=compare(self.recorder,"before","after")
        self.assertTrue(report["evaluator_changed"])
        self.assertEqual(report["results"][0]["outcome"],"not_comparable")

    def test_policy_failure_is_error_even_when_case_expected_block(self):
        case=next(c for c in load_json("evaluation_cases.json") if c["case_id"]=="budget")
        with patch.object(Governance,"checkout",side_effect=RuntimeError("policy broken")):
            report=run_suite(self.recorder,"policy-error",cases=[case])
        self.assertEqual(report["passed"],0)
        self.assertEqual(report["runs"][0]["run_status"],"error")

    def test_preview_governance_is_in_trace(self):
        report=run_suite(self.recorder,"trace-governance")
        for run in report["runs"]:
            events=self.recorder.timeline(run["run_id"])
            self.assertTrue(any(e["event_type"]=="policy.decision" for e in events))
            if run["case_id"]=="standard":
                completed=next(e for e in events if e["event_type"]=="tool.completed")
                self.assertEqual(completed["payload"]["result"],{"eligible":True,"order_placed":False})
            else:
                self.assertFalse(any(e["event_type"]=="tool.completed" for e in events))
