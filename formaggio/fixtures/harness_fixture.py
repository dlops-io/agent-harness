"""Explicit local scripted endpoint for the CLI lesson and real-SDK offline tests."""
import json

import httpx
from openai import AsyncOpenAI


class HarnessFixture:
    def __init__(self, recipient="vendor@example.com"):
        self.requests, self.recipient = [], recipient

    def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        count = len(self.requests)
        if count == 1:
            name, args = "todos_add", {"todos": [{"title": title} for title in
                ["Assess the confirmed event menu", "Review vendor evidence", "Resolve the mock email action"]]}
        elif count == 2:
            name, args = "assess_event", {}
        elif count == 3:
            name, args = "read_vendor_document", {}
        elif count == 4:
            name, args = "draft_vendor_email", {"recipient": self.recipient}
        elif count == 5 and self.recipient != "vendor@example.com":
            name, args = None, None  # Controlled forbidden-recipient attempt was blocked.
        elif count == 5:
            # Read the actual tool result; do not predict random draft IDs.
            draft_id = None
            for item in payload.get("input", []):
                if item.get("type") == "function_call_output":
                    try:
                        result = json.loads(item.get("output", "{}"))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(result, dict) and "draft_id" in result:
                        draft_id = result["draft_id"]
            if not draft_id:
                raise AssertionError("Fixture did not receive the draft tool result.")
            name, args = "save_vendor_email", {"draft_id": draft_id}
        elif count == 6:
            name, args = "todos_complete", {"items": [{"id": i, "reason": "The host resolved this demonstration step."} for i in [1, 2, 3]]}
        else:
            name, args = None, None
        if name:
            output = [{"type": "function_call", "id": f"fc_{count}", "call_id": f"call_{count}",
                       "name": name, "arguments": json.dumps(args), "status": "completed"}]
        else:
            output = [{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "Scripted sourcing demonstration finished. See the host's authoritative action status.", "annotations": []}]}]
        return httpx.Response(200, json={"id": f"resp_{count}", "object": "response", "created_at": 1,
            "model": "fixture-model", "status": "completed", "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}})

    def client(self):
        return AsyncOpenAI(api_key="local-fixture-key", base_url="https://fixture.invalid/v1", max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(self)))
