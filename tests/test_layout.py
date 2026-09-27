"""Regression coverage for package moves and reproducible source snapshots."""
import json
import subprocess
import sys
from unittest.mock import patch

from formaggio.config import ROOT, source_hashes
from formaggio.agents.runtime import run_snapshot
from formaggio.evaluation.evaluation import snapshot
from formaggio.evaluation.live_evaluation import source_state
from tests.support import RecordingTest, fixture


class LayoutTests(RecordingTest):
    def test_nested_source_edits_change_every_snapshot_without_basename_collisions(self):
        files = {
            "cli.py": "# entry point\n",
            "acts/shared.py": "# lesson\n",
            "formaggio/shop/shared.py": "# shop\n",
            "formaggio/agents/shared.py": "# agent\n",
            "scripts/check_sdk.py": "# inspection\n",
            "outputs/generated.py": "# generated; excluded\n",
            "tests/test_example.py": "# test; excluded\n",
            ".env": "NOT_A_REAL_SECRET=fixture\n",
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        request, _ = fixture()

        def snapshots():
            return [snapshot()["source_hashes"], source_state()["source_hashes"],
                    run_snapshot("fixture", "basic", "test", request, {}, [])["source_hashes"]]

        with patch("formaggio.config.ROOT", self.root):
            before = source_hashes()
            self.assertEqual(set(before), {"cli.py", "acts/shared.py", "formaggio/shop/shared.py",
                                           "formaggio/agents/shared.py", "scripts/check_sdk.py"})
            self.assertTrue(all(value == before for value in snapshots()))
            after = before
            for name in ["formaggio/shop/shared.py", "acts/shared.py"]:
                previous = after
                (self.root / name).write_text("# changed behavior\n")
                after = source_hashes()
                self.assertEqual([key for key in previous if previous[key] != after[key]], [name])
                self.assertTrue(all(value == after for value in snapshots()))
            (self.root / "outputs/generated.py").write_text("# changed output\n")
            self.assertEqual(after, source_hashes())

    def test_cli_resolves_resources_outside_tutorial_working_directory(self):
        result = subprocess.run([sys.executable, str(ROOT / "cli.py"), "--preview-context"],
                                cwd=self.root, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout))
        self.assertFalse((self.root / "outputs").exists())
