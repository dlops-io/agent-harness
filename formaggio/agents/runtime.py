"""SDK model/tool hooks shared by Acts 1–2; no evaluation suite is invoked."""
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


class ModelTrace(ChatMiddleware):
    def __init__(self, recorder, run_id, progress=None, *, max_calls=8):
        self.recorder, self.run_id, self.progress = recorder, run_id, progress
        self.calls = 0
        self.max_calls = max_calls
        self.failure = None

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
            self.progress(f"Model call {self.calls}")
        try:
            await call_next()
        except Exception as exc:
            self.recorder.event(self.run_id, "model.failed", {"call_number": self.calls, "error": str(exc)})
            raise
        response = context.result
        self.recorder.event(self.run_id, "model.response", {"call_number": self.calls,
                            "messages": visible_messages(response.messages),
                            "usage": json_value(response.usage_details), "model": response.model,
                            "finish_reason": json_value(response.finish_reason)})


class ToolTrace(FunctionMiddleware):
    def __init__(self, recorder, run_id, allowed_names, progress=None):
        self.recorder, self.run_id = recorder, run_id
        self.allowed_names, self.progress = set(allowed_names), progress

    def record(self, event, data):
        try:
            self.recorder.event(self.run_id, event, data)
        except Exception as exc:
            # Ordinary middleware errors may become recoverable SDK tool results;
            # audit failure must abort execution instead.
            raise MiddlewareFailure("Required tool audit write failed.") from exc

    async def process(self, context, call_next):
        name, invocation_id = context.function.name, uuid4().hex
        info = {"name": name, "invocation_id": invocation_id}
        self.record("tool.requested", {**info, "arguments": json_value(context.arguments)})
        if name not in self.allowed_names:
            self.record("tool.blocked", {**info, "reason": "Tool is not exposed by this teaching act."})
            raise MiddlewareFailure("Tool is not permitted.")
        self.record("tool.permitted", info)
        self.record("tool.started", info)
        if self.progress:
            self.progress(f"Tool: {name}")
        try:
            await call_next()
        except Exception as exc:
            self.record("tool.failed", {**info, "error": str(exc)})
            raise
        self.record("tool.completed", {**info, "result": json_value(context.result)})


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
