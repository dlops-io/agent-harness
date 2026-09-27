"""SDK model/tool hooks shared by Acts 1–2; no evaluation suite is invoked."""
import json
from time import monotonic
from hashlib import sha256
from importlib.metadata import version
from uuid import uuid4

from agent_framework import ChatMiddleware, FunctionMiddleware, MiddlewareFailure
from agent_framework.openai import OpenAIChatClient
from openai import AsyncOpenAI

from formaggio.config import ROOT, source_hashes
from formaggio.shop.data_models import AgentReply

MODEL_OPTIONS = {"store": False, "max_tokens": 2400, "parallel_tool_calls": False}
FUNCTION_LIMITS = {"max_iterations": 8, "max_function_calls": 20,
                   "max_duration_seconds": 120, "allow_concurrent_invocation": False}


def json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, type) and hasattr(value, "model_json_schema"):
        return value.model_json_schema()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return str(value)


def visible_messages(messages):
    """Capture supplied/returned text and tool envelopes, not hidden reasoning."""
    result = []
    for message in messages:
        contents = []
        for content in message.contents:
            if content.type == "text":
                contents.append({"type": "text", "text": content.text})
            elif content.type == "function_call":
                contents.append({"type": content.type, "call_id": content.call_id,
                                 "name": content.name, "arguments": json_value(content.arguments)})
            elif content.type == "function_result":
                contents.append({"type": content.type, "call_id": content.call_id,
                                 "result": json_value(content.result)})
            else:
                contents.append({"type": content.type, "omitted": True})
        result.append({"role": str(message.role), "contents": contents})
    return result


def clean_progress_value(recorder, value):
    """Unwrap SDK text results before redacting structured payloads."""
    value = json_value(value)

    def decode_text(text):
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return text

    if isinstance(value, str):
        value = decode_text(value)
    elif isinstance(value, list) and value and all(
        isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        for part in value
    ):
        # The SDK returns text Content envelopes, often containing serialized JSON.
        # Decode before redaction so sensitive field names remain recognizable.
        parts = [decode_text(part["text"]) for part in value]
        value = parts[0] if len(parts) == 1 else parts
    return recorder.redactor.clean(value)


def progress_value(recorder, value, *, limit=1200):
    """Redact before formatting JSON previews; full payloads stay in the trace."""
    value = clean_progress_value(recorder, value)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) > limit:
        text = text[:limit] + "\n… [preview truncated; use --inspect-run for the recorded payload]"
    return "\n".join("    " + line for line in text.splitlines())


def compact_value(value, *, collections=False, depth=0):
    """Small readable payload summaries, without JSON braces or escaped strings."""
    if isinstance(value, dict):
        if not value:
            return "none"
        if depth >= 3:
            return f"{len(value)} fields"
        parts = [f"{key.replace('_', ' ')}: {compact_value(item, collections=collections, depth=depth + 1)}"
                 for key, item in list(value.items())[:6]]
        if len(value) > 6:
            parts.append(f"… {len(value) - 6} more fields")
        return "; ".join(parts)
    if isinstance(value, list):
        if collections or depth >= 3:
            return f"{len(value)} entries"
        parts = [compact_value(item, depth=depth + 1) for item in value[:4]]
        if len(value) > 4:
            parts.append(f"… {len(value) - 4} more entries")
        return " / ".join(parts) or "none"
    if value is None:
        return "not supplied"
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = " ".join(str(value).split())
    return text[:157] + "…" if len(text) > 160 else text


class ConsoleProgress:
    """Callable progress sink; detailed JSON is an explicit display option."""
    def __init__(self, write=print, *, show_json=False):
        self.write, self.show_json = write, show_json

    def __call__(self, message):
        self.write(message)

    def payload(self, recorder, label, value, *, limit=1200, result=False):
        if self.show_json:
            self.write(label + "\n" + progress_value(recorder, value, limit=limit))
        else:
            text = compact_value(clean_progress_value(recorder, value), collections=result)
            if len(text) > 300:
                text = text[:297] + "…"
            self.write(label + " " + text)


def console_progress(progress):
    if progress is None or isinstance(progress, ConsoleProgress):
        return progress
    return ConsoleProgress(write=progress)


class ModelTrace(ChatMiddleware):
    def __init__(self, recorder, run_id, progress=None, *, max_calls=8, model=None, label="Agent"):
        self.recorder, self.run_id, self.progress = recorder, run_id, progress
        self.calls = 0
        self.max_calls = max_calls
        self.failure = None
        self.model, self.label = model, label

    async def process(self, context, call_next):
        if self.failure:
            raise MiddlewareFailure(self.failure)
        if context.stream:
            raise MiddlewareFailure("This teaching act uses non-streaming responses.")
        if self.calls >= self.max_calls:
            raise MiddlewareFailure(f"Maximum of {self.max_calls} model calls reached.")
        self.calls += 1
        options = dict(context.options or {})
        tools = options.pop("tools", []) or []
        payload = {"call_number": self.calls, "messages": visible_messages(context.messages),
                   "options": json_value(options),
                   "tools": [json_value(t) for t in tools]}
        self.recorder.event(self.run_id, "model.request", payload)
        if self.progress:
            self.progress(self.recorder.redactor.clean(
                f"\n🤖 Model call {self.calls} · {self.label} · {self.model or 'configured model'}"))
            self.progress(f"  📥 Input: {len(payload['messages'])} conversation messages.")
            calls = {c["call_id"]: c["name"] for m in payload["messages"] for c in m["contents"]
                     if c["type"] == "function_call"}
            for message in reversed(payload["messages"]):
                results = [c for c in message["contents"] if c["type"] == "function_result"]
                if results:
                    names = [calls.get(c["call_id"], c["call_id"]) for c in results]
                    self.progress(self.recorder.redactor.clean("  ↳ Latest tool results in context: " + ", ".join(names)))
                    break
            if self.calls == 1:
                self.progress(self.recorder.redactor.clean(
                    "  🧰 Available tools: " + (", ".join(t.name for t in tools) or "none (structured proposal only)")))
        started = monotonic()
        try:
            await call_next()
        except Exception as exc:
            self.recorder.event(self.run_id, "model.failed", {"call_number": self.calls, "error": str(exc)})
            if self.progress:
                self.progress(self.recorder.redactor.clean(f"  ❌ Model call {self.calls} failed: {exc}"))
            raise
        response = context.result
        messages = visible_messages(response.messages)
        usage = json_value(response.usage_details)
        self.recorder.event(self.run_id, "model.response", {"call_number": self.calls,
                            "messages": messages,
                            "usage": usage, "model": response.model,
                            "finish_reason": json_value(response.finish_reason)})
        if self.progress:
            requested = [c["name"] for m in messages for c in m["contents"] if c["type"] == "function_call"]
            action = "Requested tools: " + ", ".join(requested) if requested else "Response received; no tool calls requested."
            self.progress(self.recorder.redactor.clean(f"  📤 {action}"))
            metrics = [f"{monotonic() - started:.2f}s"]
            if isinstance(usage, dict):
                for key, name in [("input_token_count", "input"), ("output_token_count", "output")]:
                    if isinstance(usage.get(key), (int, float)):
                        metrics.append(f"{usage[key]} {name} tokens")
            self.progress("  ⏱️ " + " · ".join(metrics))


class ToolTrace(FunctionMiddleware):
    def __init__(self, recorder, run_id, allowed_names, progress=None):
        self.recorder, self.run_id = recorder, run_id
        self.allowed_names, self.progress = set(allowed_names), console_progress(progress)
        self.invocations = 0

    def record(self, event, data):
        try:
            self.recorder.event(self.run_id, event, data)
        except Exception as exc:
            # Ordinary middleware errors may become recoverable SDK tool results;
            # audit failure must abort execution instead.
            raise MiddlewareFailure("Required tool audit write failed.") from exc

    async def process(self, context, call_next):
        name, invocation_id = context.function.name, uuid4().hex
        self.invocations += 1
        info = {"name": name, "invocation_id": invocation_id}
        self.record("tool.requested", {**info, "arguments": json_value(context.arguments)})
        if name not in self.allowed_names:
            self.record("tool.blocked", {**info, "reason": "Tool is not exposed by this teaching act."})
            if self.progress:
                self.progress(self.recorder.redactor.clean(f"  ⛔ Tool call {self.invocations}: {name} is not permitted."))
            raise MiddlewareFailure("Tool is not permitted.")
        self.record("tool.permitted", info)
        self.record("tool.started", info)
        if self.progress:
            self.progress(self.recorder.redactor.clean(f"  🔧 Tool call {self.invocations}: {name}"))
            self.progress.payload(self.recorder, "  📋 Arguments:", context.arguments, limit=2400)
        started = monotonic()
        try:
            await call_next()
        except Exception as exc:
            self.record("tool.failed", {**info, "error": str(exc)})
            if self.progress:
                self.progress(self.recorder.redactor.clean(f"  ❌ Tool failed: {exc}"))
            raise
        self.record("tool.completed", {**info, "result": json_value(context.result)})
        if self.progress:
            # A completed invocation can still return a business rejection.
            self.progress.payload(self.recorder, f"  📦 Result ({monotonic() - started:.2f}s):",
                                  context.result, limit=900, result=True)


def make_client(model, middleware, api_client=None, *, function_limits=None):
    owned = api_client is None
    api_client = api_client or AsyncOpenAI(timeout=45, max_retries=0)
    client = OpenAIChatClient(model=model, async_client=api_client, middleware=middleware,
                              function_invocation_configuration=function_limits or FUNCTION_LIMITS)
    return client, api_client, owned


def run_snapshot(model, mode, prompt, request, packet, tools):
    """Record live configuration without invoking or depending on the evaluator."""
    return {"model": model, "context_mode": mode, "instructions": prompt, "request": request.model_dump(mode="json"),
            "context": packet, "tools": [t.to_dict() for t in tools],
            "options": MODEL_OPTIONS, "function_limits": FUNCTION_LIMITS,
            "reply_schema": AgentReply.model_json_schema(),
            "source_hashes": source_hashes(),
            "data_hashes": {p.name: sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT / "data").glob("*.json"))},
            "dependencies": {n: version(n) for n in ("agent-framework", "openai", "pydantic")}}
