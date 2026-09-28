"""Evaluation teaching views preserve failures, uncertainty and read-only trace access."""
import asyncio
from copy import deepcopy
from html.parser import HTMLParser
from unittest.mock import patch

from formaggio.evaluation.live_evaluation import run_live_suite
from formaggio.evaluation.notebook_lab import golden_cases
from formaggio.operations.evaluation_view import evaluation_rows, render_evaluation, show_evaluation
from tests.support import RecordingTest


class EvaluationViewTests(RecordingTest):
    def suite(self, label="view", **kwargs):
        return asyncio.run(run_live_suite(self.recorder, label, act=kwargs.pop("act", 6),
            fixture=True, repeats=kwargs.pop("repeats", 2),
            case_ids=kwargs.pop("case_ids", ["standard", "missing-details"]),
            output_root=self.root, **kwargs))

    def test_golden_cases_describe_requests_and_expectations_for_each_act(self):
        for act in range(1, 7):
            cases = golden_cases(act)
            self.assertTrue(cases)
            self.assertEqual(len({c["case_id"] for c in cases}), len(cases))
            self.assertTrue(all(c["request"] and c["expected"] for c in cases))
        standard = golden_cases(6)[0]
        self.assertIn("12 guests", standard["request"])
        self.assertIn("one valid mock order", standard["expected"])
        self.assertIn("no cart", golden_cases(1)[1]["expected"])
        manager = next(c for c in golden_cases(6) if c["case_id"] == "manager-decline")
        self.assertIn("untested", manager["expected"])
        for invalid in (0, 7, True, "6"):
            with self.assertRaises(ValueError): golden_cases(invalid)

    def test_all_runs_checks_and_chat_traces_are_present_without_database_writes(self):
        report = self.suite()
        before = self.recorder.query("SELECT count(*) AS n FROM events")[0]["n"]
        html = render_evaluation(self.recorder, report)
        self.assertEqual(before, self.recorder.query("SELECT count(*) AS n FROM events")[0]["n"])
        for run in report["runs"]:
            self.assertIn(run["run_id"], html)
        self.assertIn("SCRIPTED RESPONSES", html)
        self.assertEqual(html.count('class="ev-run"'), 4)
        self.assertIn("outcome.tasting_delivery", html)
        self.assertIn("Inspect recorded events", html)
        self.assertIn("Request and response", html)
        self.assertIn("Human prose review remains separate", html)
        rows = evaluation_rows(report)
        self.assertTrue(all(row["outcome_agreement"] == 1 for row in rows))
        self.assertTrue(all(row["input_tokens"]["measured"] == 2 for row in rows))
        # Every scorecard link targets a real expandable run section.
        class Links(HTMLParser):
            ids, targets = set(), set()
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'id' in attrs: self.ids.add(attrs['id'])
                if tag == 'a': self.targets.add(attrs['href'].removeprefix('#'))
        parsed = Links()
        parsed.feed(html)
        self.assertEqual(parsed.ids, parsed.targets)
        with patch("formaggio.operations.chat_view._display_html") as display:
            self.assertIsNone(show_evaluation(self.recorder, report))
            display.assert_called_once()

    def test_errors_and_missing_runs_do_not_look_robust_or_free(self):
        async def broken(*args, **kwargs):
            raise TimeoutError("Deliberate evaluation failure")
        report = self.suite(case_ids=["standard"], repeats=5, runner=broken)
        row = evaluation_rows(report)[0]
        self.assertEqual(row["outcome_agreement"], 0)
        self.assertEqual(row["scheduled"], 5)
        self.assertEqual(row["recorded"], 3)
        self.assertIsNone(row["input_tokens"]["mean"])
        html = render_evaluation(self.recorder, report)
        self.assertIn("0/5", html)
        self.assertEqual(html.count("MISSING ·"), 2)
        self.assertIn("Unavailable (0/5 measured)", html)
        self.assertIn("Deliberate evaluation failure", html)

    def test_consistent_failures_are_not_mistaken_for_correctness(self):
        report = self.suite(case_ids=["standard"])
        changed = deepcopy(report)
        changed["passed"], changed["failed"] = 0, 2
        changed["case_summary"][0]["passed"] = 0
        for run in changed["runs"]:
            run["passed"] = False
            run["checks"][0]["status"] = "fail"
        row = evaluation_rows(changed)[0]
        self.assertEqual(row["passed"], 0)
        self.assertEqual(row["outcome_agreement"], 1)
        self.assertIn("100% (2/2)", render_evaluation(self.recorder, changed))

    def test_unexercised_review_missing_trace_and_html_escape_are_visible(self):
        report = self.suite(case_ids=["standard"], repeats=1)
        report["case_summary"][0]["unexercised_checks"] = ["coverage.manager_review"]
        report["label"] = '<img src=x onerror="alert(1)">'
        report["runs"][0]["checks"][0]["observed"] = '<script>alert(1)</script>'
        absent = self.root / 'absent.sqlite'
        html = render_evaluation(absent, report)
        self.assertFalse(absent.exists())
        self.assertIn("Untested paths", html)
        self.assertIn("coverage.manager_review", html)
        self.assertIn("Trace unavailable", html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img src=x', html)
        self.assertIn('&lt;script&gt;', html)
