"""SQLite classroom audit records plus an OpenTelemetry span exporter.

Policy events are committed synchronously. Telemetry never grants permission.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import RLock
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Redactor:
    """Explicit field redaction + known credentials; not a general PII detector."""

    SENSITIVE = {"api_key", "openai_api_key", "authorization", "password", "secret",
                 "access_token", "refresh_token", "credential", "credentials"}

    def __init__(self, secrets=()):
        known = [os.environ.get(key, "") for key in ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY")]
        self.secrets = sorted({s for s in (*known, *secrets) if len(s) >= 4}, key=len, reverse=True)

    def clean(self, value):
        if isinstance(value, dict):
            return {str(k): "[REDACTED]" if str(k).lower() in self.SENSITIVE else self.clean(v)
                    for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", value)
            return re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", value)
        return value


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS versions (
 version_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, snapshot_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS experiments (
 experiment_id TEXT PRIMARY KEY, label TEXT NOT NULL UNIQUE,
 version_id TEXT NOT NULL REFERENCES versions, evaluator_version TEXT NOT NULL,
 cases_json TEXT NOT NULL, repeats INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, experiment_id TEXT REFERENCES experiments,
 case_id TEXT, act INTEGER NOT NULL, repetition INTEGER NOT NULL,
 version_id TEXT NOT NULL REFERENCES versions, model TEXT NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('fixture','live','recorded')),
 started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS spans (
 span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT,
 run_id TEXT NOT NULL REFERENCES runs, name TEXT NOT NULL, kind TEXT NOT NULL,
 started_ns INTEGER NOT NULL, ended_ns INTEGER NOT NULL,
 duration_ms REAL NOT NULL, status TEXT NOT NULL, attributes_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 run_id TEXT NOT NULL REFERENCES runs, span_id TEXT, timestamp TEXT NOT NULL,
 event_type TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evaluations (
 evaluation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs,
 check_id TEXT NOT NULL, evaluator_version TEXT NOT NULL,
 expected_json TEXT NOT NULL, observed_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pass','fail','error','not_applicable')),
 explanation TEXT NOT NULL, UNIQUE(run_id, check_id));
CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, sequence);
CREATE INDEX IF NOT EXISTS spans_by_run ON spans(run_id);
"""


class SQLiteSpanExporter(SpanExporter):
    def __init__(self, recorder):
        self.recorder = recorder
        self.errors: list[str] = []

    def export(self, spans):
        try:
            for span in spans:
                run_id = (span.attributes or {}).get("formaggio.run_id")
                # SDK children can inherit the run through their native trace ID.
                trace_id = f"{span.context.trace_id:032x}"
                run_id = run_id or self.recorder.trace_runs.get(trace_id)
                if run_id is None:
                    raise ValueError("Unassociated span: start a recorder.span root first.")
                self.recorder.write_span(span, run_id)
            return SpanExportResult.SUCCESS
        except Exception as exc:
            self.errors.append(self.recorder.redactor.clean(str(exc)))
            return SpanExportResult.FAILURE

    def shutdown(self):
        pass


class SDKSpanRouter(SpanExporter):
    """Route SDK spans to the recorder owning a trace; ignore unrelated spans."""
    def __init__(self):
        self.recorders = {}

    def export(self, spans):
        result = SpanExportResult.SUCCESS
        for span in spans:
            recorder = self.recorders.get(f"{span.context.trace_id:032x}")
            if recorder and recorder.exporter.export([span]) != SpanExportResult.SUCCESS:
                result = SpanExportResult.FAILURE
        return result

    def shutdown(self):
        pass


_sdk_router = None


@contextmanager
def sdk_tracing(recorder):
    """Configure SDK tracing once; route each run without replacing global providers.

    Call inside recorder.span(). SDK instrumentation uses the global OTel provider;
    its provider-name setting is a service label, not a separate provider registry.
    """
    global _sdk_router
    from agent_framework.observability import enable_instrumentation
    if _sdk_router is None:
        provider = trace.get_tracer_provider()
        if isinstance(provider, trace.ProxyTracerProvider):
            provider = TracerProvider()
            trace.set_tracer_provider(provider)
        if not hasattr(provider, "add_span_processor"):
            raise RuntimeError("The active OTel provider cannot accept the tutorial span exporter.")
        _sdk_router = SDKSpanRouter()
        provider.add_span_processor(SimpleSpanProcessor(_sdk_router))
    enable_instrumentation(enable_sensitive_data=False, enable_message_events=False)
    current = trace.get_current_span().get_span_context()
    if not current.is_valid:
        raise RuntimeError("SDK tracing requires a surrounding recorded run span.")
    trace_id = f"{current.trace_id:032x}"
    _sdk_router.recorders[trace_id] = recorder
    try:
        yield
    finally:
        _sdk_router.recorders.pop(trace_id, None)


class Recorder:
    def __init__(self, path: str | Path, *, secrets=()):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.redactor = Redactor(secrets)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.trace_runs: dict[str, str] = {}
        self.exporter = SQLiteSpanExporter(self)
        # Local provider avoids mutating application-wide OTel configuration.
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("formaggio.foundation", "1")

    def _json(self, value):
        return canonical(self.redactor.clean(value))

    def write(self, sql, parameters=()):
        with self.lock, self.db:
            self.db.execute(sql, parameters)

    def query(self, sql, parameters=()):
        """Trusted instructor inspection only; never expose arbitrary SQL as an agent tool."""
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, parameters)]

    def version(self, snapshot: dict) -> str:
        payload = self._json(snapshot)
        version_id = sha256(payload.encode()).hexdigest()
        self.write("INSERT OR IGNORE INTO versions VALUES (?, ?, ?)", (version_id, now(), payload))
        return version_id

    def experiment(self, label, version_id, evaluator_version, case_ids, repeats):
        if not label.strip() or not 1 <= repeats <= 100:
            raise ValueError("Use a nonempty label and 1–100 repetitions.")
        experiment_id = uuid4().hex
        self.write("INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (experiment_id, self.redactor.clean(label), version_id, evaluator_version,
                    self._json(case_ids), repeats, now()))
        return experiment_id

    def start_run(self, version_id, *, case_id=None, act=0, repetition=1,
                  model="none", mode="fixture", experiment_id=None, session_id=None):
        run_id = uuid4().hex
        self.write("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'running', NULL)",
                   (run_id, session_id or uuid4().hex, experiment_id, case_id, act, repetition,
                    version_id, model, mode, now()))
        self.event(run_id, "run.started", {"mode": mode, "act": act})
        return run_id

    def finish_run(self, run_id, status="completed", error=None):
        if status not in {"completed", "blocked", "error", "stopped"}:
            raise ValueError("Invalid run status")
        self.flush()
        self.event(run_id, "run.finished", {"status": status, "error": error})
        self.write("UPDATE runs SET status=?, ended_at=?, error=? WHERE run_id=?",
                   (status, now(), self.redactor.clean(error), run_id))

    def event(self, run_id, event_type, payload=None, *, span_id=None):
        context = trace.get_current_span().get_span_context()
        span_id = span_id or (f"{context.span_id:016x}" if context.is_valid else None)
        self.write("INSERT INTO events(event_id,run_id,span_id,timestamp,event_type,payload_json) VALUES(?,?,?,?,?,?)",
                   (uuid4().hex, run_id, span_id, now(), event_type, self._json(payload or {})))

    @contextmanager
    def span(self, run_id, name, kind="application", *, expected_outcomes=None):
        with self.tracer.start_as_current_span(name, attributes={"formaggio.run_id": run_id,
                                                                 "formaggio.kind": kind},
                                              record_exception=False, set_status_on_exception=False) as span:
            self.trace_runs[f"{span.get_span_context().trace_id:032x}"] = run_id
            try:
                yield span
            except Exception as exc:
                # A planned approval pause is not a software failure. Exact types
                # ensure PolicyCheckError still records an error, despite inheritance.
                outcome = (expected_outcomes or {}).get(type(exc))
                if outcome:
                    span.set_attribute("formaggio.outcome", outcome)
                else:
                    span.record_exception(exc)
                    span.set_status(trace.Status(trace.StatusCode.ERROR))
                raise

    def write_span(self, span, run_id):
        parent = f"{span.parent.span_id:016x}" if span.parent else None
        attrs = dict(span.attributes or {})
        self.write("INSERT INTO spans VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (f"{span.context.span_id:016x}", f"{span.context.trace_id:032x}", parent, run_id,
                    self.redactor.clean(span.name), attrs.get("formaggio.kind", str(span.kind)),
                    span.start_time, span.end_time, (span.end_time - span.start_time) / 1_000_000,
                    span.status.status_code.name, self._json(attrs)))
        for event in span.events:
            self.event(run_id, "span.event", {"name": event.name, "timestamp_ns": event.timestamp,
                                               "attributes": dict(event.attributes or {})},
                       span_id=f"{span.context.span_id:016x}")

    def evaluation(self, run_id, result, evaluator_version):
        self.write("INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?)",
                   (uuid4().hex, run_id, result.check_id, evaluator_version,
                    self._json(result.expected), self._json(result.observed), result.status,
                    self.redactor.clean(result.explanation)))

    def timeline(self, run_id):
        rows = self.query("SELECT sequence,event_type,span_id,payload_json FROM events WHERE run_id=? ORDER BY sequence", (run_id,))
        return [{**row, "payload": json.loads(row["payload_json"])} for row in rows]

    def flush(self):
        self.provider.force_flush()
        if self.exporter.errors:
            raise RuntimeError("Trace export failed: " + "; ".join(self.exporter.errors))

    def backup(self, destination):
        self.flush()
        destination = Path(destination)
        if destination.resolve() == self.path.resolve():
            raise ValueError("Backup must use another path.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, sqlite3.connect(destination) as target:
            self.db.backup(target)

    def close(self):
        try:
            self.flush()
        finally:
            self.provider.shutdown()
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
