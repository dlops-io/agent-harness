import json
import sqlite3
from contextlib import closing

from formaggio.operations.observability import Recorder
from tests.support import RecordingTest


class ObservabilityTests(RecordingTest):
    def test_native_trace_parentage_and_event_order(self):
        with self.recorder.span(self.run_id,"outer"):
            self.recorder.event(self.run_id,"before",{})
            # Models/tools in later acts produce native spans with no custom run attribute.
            with self.recorder.tracer.start_as_current_span("native-child"):
                self.recorder.event(self.run_id,"inside",{})
            self.recorder.event(self.run_id,"after",{})
        self.recorder.finish_run(self.run_id)
        spans={s["name"]:s for s in self.recorder.query("SELECT * FROM spans")}
        self.assertEqual(spans["native-child"]["parent_span_id"],spans["outer"]["span_id"])
        self.assertEqual(spans["native-child"]["trace_id"],spans["outer"]["trace_id"])
        self.assertGreaterEqual(spans["outer"]["duration_ms"],0)
        events=self.recorder.timeline(self.run_id)
        self.assertEqual([e["event_type"] for e in events],["run.started","before","inside","after","run.finished"])
        self.assertEqual(events[2]["span_id"],spans["native-child"]["span_id"])

    def test_snapshot_stability_changes_and_secret_exclusion(self):
        a=self.recorder.version({"prompt":"first","api_key":"secret-one"})
        b=self.recorder.version({"api_key":"secret-two","prompt":"first"})
        c=self.recorder.version({"prompt":"second","api_key":"secret-one"})
        self.assertEqual(a,b)
        self.assertNotEqual(a,c)
        for field in ["skills","policies","model"]:
            self.assertNotEqual(a,self.recorder.version({"prompt":"first",field:"changed"}))
        self.recorder.event(self.run_id,"content",{"api_key":"secret-one","nested":["unit-test-credential"],
                                                   "text":"sk-unitTestCredential123 Bearer abcd1234", "tokens":123})
        with self.recorder.span(self.run_id,"unit-test-credential") as span:
            span.set_attribute("authorization","Bearer abcd1234")
        contents=json.dumps(self.recorder.query("SELECT payload_json FROM events") +
                            self.recorder.query("SELECT snapshot_json FROM versions") +
                            self.recorder.query("SELECT name,attributes_json FROM spans"))
        for secret in ["secret-one","secret-two","unit-test-credential","sk-unitTestCredential123","abcd1234"]:
            self.assertNotIn(secret,contents)
        self.assertIn("REDACTED",contents)
        self.assertIn("123",contents)

    def test_required_event_failure_is_not_swallowed(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.recorder.event("nonexistent-run","invalid",{})

    def test_backup_contains_independent_readable_history(self):
        self.recorder.event(self.run_id,"saved",{"answer":42})
        target=self.root / "backup.sqlite"
        self.recorder.backup(target)
        self.recorder.event(self.run_id,"later",{})
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0],2)
        with self.assertRaises(ValueError):
            self.recorder.backup(self.recorder.path)

    def test_records_survive_reopen(self):
        with Recorder(self.root/"another.sqlite") as rec:
            v=rec.version({"prompt":"saved"}); run=rec.start_run(v); rec.finish_run(run)
        with Recorder(self.root/"another.sqlite") as rec:
            self.assertEqual(rec.query("SELECT status FROM runs")[0]["status"],"completed")
