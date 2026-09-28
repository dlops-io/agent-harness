"""Container-friendly saved HTML reports require no model calls or writable database."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import io
from unittest.mock import patch

import cli
from formaggio.config import load_json
from formaggio.evaluation.evaluation import run_suite
from formaggio.evaluation.live_evaluation import run_live_suite
from formaggio.operations.evaluation_view import export_evaluation_report
from tests.support import RecordingTest


class ReportCliTests(RecordingTest):
    def batch(self, label="act6-golden-live-1"):
        # Fixture execution verifies export mechanics; the label is not a mode indicator.
        return asyncio.run(run_live_suite(self.recorder, label, act=6, fixture=True,
            case_ids=["standard"], repeats=1, output_root=self.root))

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.argv", ["cli.py", *args]), redirect_stdout(out), redirect_stderr(err), \
             patch("cli.Recorder", side_effect=AssertionError("Report CLI must not open a writable recorder")), \
             patch("formaggio.evaluation.live_evaluation.run_live_suite", side_effect=AssertionError("No reruns")):
            cli.main()
        return out.getvalue()

    def test_cli_exports_default_path_and_preserves_database_and_traces(self):
        report = self.batch()
        self.recorder.close()
        before = self.recorder.path.read_bytes()
        output = self.command("--view-report", report["label"], "--db", str(self.recorder.path))
        path = self.root / "reports" / "act6-golden-live-1.html"
        html = path.read_text()
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("SCRIPTED RESPONSES", html)
        self.assertIn(report["runs"][0]["run_id"], html)
        self.assertIn("Inspect recorded events", html)
        self.assertIn(str(path), output)
        self.assertIn("No model calls", output)
        self.assertEqual(before, self.recorder.path.read_bytes())

    def test_cli_custom_destination_and_python_utility(self):
        self.batch("custom")
        destination = self.root / "nested" / "custom.html"
        self.command("--view-report", "custom", "--db", str(self.recorder.path), "--html-output", str(destination))
        self.assertTrue(destination.is_file())
        self.assertEqual(export_evaluation_report(self.recorder.path, "custom", destination), destination)

    def test_missing_database_unknown_label_and_invalid_flags_create_no_output(self):
        absent = self.root / "missing.sqlite"
        for args in (("--view-report", "absent", "--db", str(absent)),
                     ("--view-report", "absent", "--db", str(self.recorder.path)),
                     ("--list-runs", "--html-output", str(self.root / "bad.html"))):
            with self.subTest(args=args), self.assertRaises(SystemExit) as caught:
                self.command(*args)
            self.assertEqual(caught.exception.code, 2)
        self.assertFalse(absent.exists())
        self.assertFalse((self.root / "reports").exists())
        self.assertFalse((self.root / "bad.html").exists())

    def test_export_rejects_database_aliases_and_journal_destinations(self):
        self.batch("guard")
        alias = self.root / "alias.html"
        alias.hardlink_to(self.recorder.path)
        symlink = self.root / "link.html"
        symlink.symlink_to(self.recorder.path)
        for destination in (self.recorder.path, alias, symlink,
                            self.root / (self.recorder.path.name + "-wal"),
                            self.root / (self.recorder.path.name + "-shm")):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "must not overwrite"):
                export_evaluation_report(self.recorder.path, "guard", destination)

    def test_label_path_characters_stay_inside_reports_and_foundation_is_explained(self):
        report = self.batch("../unsafe/<label>")
        path = export_evaluation_report(self.recorder.path, report["label"])
        self.assertEqual(path.parent, self.root / "reports")
        self.assertNotIn("<label>", path.read_text())
        self.assertIn("&lt;label&gt;", path.read_text())
        run_suite(self.recorder, "foundation", cases=load_json("evaluation_cases.json")[:1])
        with self.assertRaisesRegex(ValueError, "--report for foundation"):
            export_evaluation_report(self.recorder.path, "foundation")
