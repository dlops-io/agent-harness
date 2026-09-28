"""Scripted final proposals for evaluating Acts 1–2 offline; no quality claim."""
import json
import httpx

from formaggio.config import load_json
from formaggio.fixtures.harness_fixture import HarnessFixture


class ProposalFixture(HarnessFixture):
    def __init__(self, expectation, scenario="standard"):
        super().__init__()
        self.expectation = expectation
        self.scenario = scenario

    def __call__(self, request):
        self.requests.append(json.loads(request.content))
        items = load_json("workflow_proposals.json")[self.scenario][0] if self.expectation == "accepted" else []
        text = json.dumps({"items": items, "message": "Scripted proposal or abstention; review the recorded checks."})
        output = ([{"type": "function_call", "id": "fc_preview", "call_id": "preview", "name": "preview_order",
                    "arguments": json.dumps({"items": items}), "status": "completed"}] if items and len(self.requests) == 1 else
                  [{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}])
        return httpx.Response(200, json={"id": "resp_fixture", "object": "response", "created_at": 1,
            "model": "fixture-model", "status": "completed", "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}})
