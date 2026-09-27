"""Offline contracts shared by agent and future workflow execution adapters."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from agent_framework import MiddlewareFailure

from formaggio.agents.execution import ActiveBudget, ExecutionState, RecordedExecution, recorded_trace
from tests.support import RecordingTest


class ExecutionTests(RecordingTest):
    def execution(self):
        return RecordedExecution(self.recorder, {"lesson": "generic execution"}, act=0, mode="fixture")

    def status(self, execution):
        rows = self.recorder.query("SELECT status, error FROM runs WHERE run_id=?", (execution.run_id,))
        return rows[0]

    def test_owned_resources_close_once_in_reverse_order_before_completion(self):
        async def check():
            closed = []
            def first_close():
                self.assertEqual(self.status(execution)["status"], "running")
                closed.append("first")
            async def second_close():
                closed.append("second")
            first = SimpleNamespace(close=first_close)
            second = SimpleNamespace(aclose=second_close)
            borrowed = SimpleNamespace(close=lambda: closed.append("borrowed"))
            async with self.execution() as execution:
                self.assertIs(execution.own(first), first)
                execution.own(second)
                execution.own(first)
                self.assertTrue(callable(borrowed.close))
            self.assertEqual(closed, ["second", "first"])
            self.assertEqual(self.status(execution)["status"], "completed")
            self.assertEqual([e["event_type"] for e in self.recorder.timeline(execution.run_id)],
                             ["run.started", "run.finished"])
        asyncio.run(check())

    def test_body_error_is_preserved_when_cleanup_also_fails(self):
        async def check():
            closed = []
            original = ValueError("operation failed")
            def bad_close():
                closed.append("bad")
                raise RuntimeError("unit-test-credential")
            with self.assertRaises(ValueError) as caught:
                async with self.execution() as execution:
                    execution.own(SimpleNamespace(close=lambda: closed.append("good")))
                    execution.own(SimpleNamespace(close=bad_close))
                    raise original
            self.assertIs(caught.exception, original)
            self.assertEqual(closed, ["bad", "good"])
            self.assertEqual(self.status(execution), {"status": "error", "error": "operation failed"})
            self.assertIn("Resource cleanup also failed", " ".join(original.__notes__))
            self.assertNotIn("unit-test-credential", " ".join(original.__notes__))
        asyncio.run(check())

    def test_cleanup_failure_prevents_success_and_attempts_remaining_closers(self):
        async def check():
            closed = []
            async def bad_close():
                raise RuntimeError("close failed")
            with self.assertRaisesRegex(RuntimeError, "close failed"):
                async with self.execution() as execution:
                    execution.own(SimpleNamespace(close=lambda: closed.append(True)))
                    execution.own(SimpleNamespace(aclose=bad_close))
            self.assertEqual(closed, [True])
            self.assertEqual(self.status(execution)["status"], "error")
        asyncio.run(check())

    def test_timeout_and_cancellation_close_resources_and_record_stopped(self):
        async def check():
            for failure in (TimeoutError("expired"), asyncio.CancelledError()):
                closed = []
                with self.assertRaises(type(failure)):
                    async with self.execution() as execution:
                        execution.own(SimpleNamespace(close=lambda: closed.append(True)))
                        raise failure
                self.assertEqual(closed, [True])
                self.assertEqual(self.status(execution)["status"], "stopped")
        asyncio.run(check())

    def test_expected_block_is_explicit_and_does_not_mask_later_failure(self):
        async def check():
            async with self.execution() as execution:
                execution.set_outcome("blocked", "Approval declined")
            self.assertEqual(self.status(execution), {"status": "blocked", "error": "Approval declined"})
            with self.assertRaisesRegex(RuntimeError, "later failure"):
                async with self.execution() as failed:
                    failed.set_outcome("blocked", "Approval declined")
                    raise RuntimeError("later failure")
            self.assertEqual(self.status(failed)["status"], "error")
        asyncio.run(check())

    def test_recording_failure_propagates_after_cleanup(self):
        async def check():
            for original in (None, ValueError("primary")):
                closed = []
                with patch.object(self.recorder, "finish_run", side_effect=RuntimeError("audit unavailable")):
                    with self.assertRaises(ValueError if original else RuntimeError) as caught:
                        async with self.execution() as execution:
                            execution.own(SimpleNamespace(close=lambda: closed.append(True)))
                            if original:
                                raise original
                self.assertEqual(closed, [True])
                if original:
                    self.assertIs(caught.exception, original)
                    self.assertIn("Run finalization also failed", " ".join(original.__notes__))
                self.assertEqual(self.status(execution)["status"], "running")
        asyncio.run(check())

    def test_initialization_failures_do_not_enter_execution_body(self):
        async def check():
            before = len(self.recorder.query("SELECT * FROM runs"))
            for method in ("version", "start_run"):
                execution = self.execution()
                with patch.object(self.recorder, method, side_effect=RuntimeError("initialization failed")):
                    with self.assertRaisesRegex(RuntimeError, "initialization failed"):
                        async with execution:
                            self.fail("The execution body must not run")
                with self.assertRaisesRegex(RuntimeError, "not active"):
                    execution.own(SimpleNamespace(close=lambda: None))
            self.assertEqual(len(self.recorder.query("SELECT * FROM runs")), before)
        asyncio.run(check())

    def test_lifecycle_misuse_fails_early(self):
        async def check():
            execution = self.execution()
            with self.assertRaisesRegex(RuntimeError, "not active"):
                execution.set_outcome("completed")
            async with execution:
                with self.assertRaises(ValueError):
                    execution.set_outcome("unknown")
                with self.assertRaises(TypeError):
                    execution.own(object())
                with self.assertRaisesRegex(RuntimeError, "new recorded execution"):
                    async with execution:
                        pass
            with self.assertRaisesRegex(RuntimeError, "new recorded execution"):
                async with execution:
                    pass
            with self.assertRaisesRegex(RuntimeError, "not active"):
                execution.own(SimpleNamespace(close=lambda: None))
        asyncio.run(check())

    def test_active_time_and_call_allowances_survive_review_pauses(self):
        async def check():
            now = [0.0]
            state = ExecutionState(max_model_calls=2, max_tool_calls=1,
                                   active_budget=ActiveBudget(10, clock=lambda: now[0]))
            async with state.active_budget.measure():
                state.admit("model")
                now[0] += 3
                self.assertEqual(state.active_budget.remaining, 7)
            now[0] += 3600  # Human review is outside the active execution segments.
            self.assertEqual(state.active_budget.remaining, 7)
            async with state.active_budget.measure():
                state.admit("model")
                state.admit("tool")
                now[0] += 2
            self.assertEqual(state.active_budget.remaining, 5)
            with self.assertRaises(MiddlewareFailure):
                state.admit("model")
            with self.assertRaises(MiddlewareFailure):
                state.admit("tool")
            self.assertEqual((state.model_calls, state.tool_calls), (2, 1))
        asyncio.run(check())

    def test_time_exhaustion_rejects_resume_and_synchronous_overrun(self):
        async def check():
            now = [0.0]
            budget = ActiveBudget(1, clock=lambda: now[0])
            with self.assertRaises(TimeoutError):
                async with budget.measure():
                    now[0] += 2
            self.assertEqual(budget.remaining, 0)
            with self.assertRaises(TimeoutError):
                async with budget.measure():
                    self.fail("An exhausted run must not resume")
        asyncio.run(check())

    def test_active_segment_failure_charges_time_and_releases_guard(self):
        async def check():
            for failure in (ValueError("operation failed"), asyncio.CancelledError()):
                now = [0.0]
                budget = ActiveBudget(10, clock=lambda: now[0])
                with self.assertRaises(type(failure)):
                    async with budget.measure():
                        now[0] += 3
                        raise failure
                async with budget.measure():
                    self.assertEqual(budget.remaining, 7)
        asyncio.run(check())

    def test_active_time_enforces_real_timeout(self):
        async def check():
            budget = ActiveBudget(0.01)
            with self.assertRaises(TimeoutError):
                async with budget.measure():
                    await asyncio.Event().wait()
            self.assertEqual(budget.remaining, 0)
        asyncio.run(check())

    def test_nested_and_overlapping_segments_are_rejected(self):
        async def check():
            budget = ActiveBudget(10)
            async with budget.measure():
                async def overlap():
                    with self.assertRaisesRegex(RuntimeError, "must not overlap"):
                        async with budget.measure():
                            pass
                await overlap()
                await asyncio.create_task(overlap())
        asyncio.run(check())

    def test_overlapping_runs_keep_counters_resources_and_traces_separate(self):
        async def check():
            arrived = asyncio.Event()
            closed, executions = [], []
            async def run(label, count):
                state = ExecutionState(active_budget=ActiveBudget(10))
                async with self.execution() as execution:
                    executions.append(execution)
                    execution.own(SimpleNamespace(close=lambda: closed.append(label)))
                    with recorded_trace(self.recorder, execution.run_id, label):
                        async with state.active_budget.measure():
                            for _ in range(count):
                                state.admit("model")
                            if len(executions) == 2:
                                arrived.set()
                            await arrived.wait()
                            self.recorder.event(execution.run_id, "segment.finished", {"label": label})
                return state
            first, second = await asyncio.wait_for(asyncio.gather(run("one", 1), run("two", 2)), 3)
            self.assertEqual((first.model_calls, second.model_calls), (1, 2))
            self.assertIsNot(first.limits, second.limits)
            self.assertIsNot(first.active_budget, second.active_budget)
            self.assertCountEqual(closed, ["one", "two"])
            self.assertNotEqual(executions[0].run_id, executions[1].run_id)
            for label, execution in zip(("one", "two"), executions):
                self.assertEqual(self.status(execution)["status"], "completed")
                event = next(e for e in self.recorder.timeline(execution.run_id)
                             if e["event_type"] == "segment.finished")
                span = self.recorder.query("SELECT name, run_id FROM spans WHERE span_id=?", (event["span_id"],))[0]
                self.assertEqual(span, {"name": label, "run_id": execution.run_id})
        asyncio.run(check())

    def test_invalid_time_and_admission_kind_fail_early(self):
        for seconds in (0, -1, True, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                ActiveBudget(seconds)
        state = ExecutionState()
        with self.assertRaises(ValueError):
            state.admit("unknown")
        self.assertEqual((state.model_calls, state.tool_calls), (0, 0))
