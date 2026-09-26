"""Console diagnostics must preserve audit boundaries and redact credentials."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

from agent_framework import Content, MiddlewareFailure

from formaggio.agents.runtime import ToolTrace, progress_value
from tests.support import RecordingTest


class ProgressTests(RecordingTest):
    def test_tool_subquery_arguments_and_serialized_results_are_redacted(self):
        lines = []
        trace = ToolTrace(self.recorder, self.run_id, ["search_catalog"], lines.append)
        context = SimpleNamespace(function=SimpleNamespace(name="search_catalog"),
            arguments={"query": "French cheese", "authorization": "private-header", "note": "unit-test-credential"},
            result=None)
        async def execute():
            context.result = [Content.from_text(json.dumps({"answer": "Found cheese", "password": "private-password"}))]
        asyncio.run(trace.process(context, execute))
        output = "\n".join(lines)
        self.assertIn('"query": "French cheese"', output)
        self.assertIn('"answer": "Found cheese"', output)
        self.assertIn("[REDACTED]", output)
        for secret in ["private-header", "private-password", "unit-test-credential"]:
            self.assertNotIn(secret, output)

    def test_failed_required_audit_does_not_execute_or_print_tool_arguments(self):
        lines, called = [], []
        trace = ToolTrace(self.recorder, self.run_id, ["search_catalog"], lines.append)
        context = SimpleNamespace(function=SimpleNamespace(name="search_catalog"),
                                  arguments={"query": "must not be displayed"}, result=None)
        async def execute():
            called.append(True)
        with patch.object(self.recorder, "event", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(MiddlewareFailure):
                asyncio.run(trace.process(context, execute))
        self.assertEqual(called, [])
        self.assertEqual(lines, [])

    def test_large_preview_is_labeled_and_does_not_modify_original(self):
        value = {"text": "x" * 2000, "secret": "private-value"}
        output = progress_value(self.recorder, value, limit=100)
        self.assertIn("preview truncated", output)
        self.assertIn("--inspect-run", output)
        self.assertNotIn("private-value", output)
        self.assertEqual(value["secret"], "private-value")
