"""Scripted skill selection proves integration, not autonomous model judgment."""
import json

import httpx

from formaggio.fixtures.harness_fixture import HarnessFixture


class SkillsFixture(HarnessFixture):
    def __init__(self, scenario="event-shortage", *, recipient="vendor@example.com", calls=None):
        super().__init__(recipient)
        def skill(name):
            return [("load_skill", {"skill_name": name}),
                    ("read_skill_resource", {"skill_name": name, "resource_name": "references/serving-guide.md" if name == "tasting-planning" else "references/vendor-guide.md"}),
                    ("read_skill_resource", {"skill_name": name, "resource_name": "assets/tasting-plan.md" if name == "tasting-planning" else "assets/email-template.html"})]
        self.calls = calls if calls is not None else (
            [("get_stock", {"product": "epoisses"})] if scenario == "stock-question" else
            skill("tasting-planning") + [("todos_add", {"todos": [{"title": "Prepare the requested plan"}]}), ("assess_event", {})]
            + (skill("vendor-outreach") + [("read_vendor_document", {}), ("draft_vendor_email", {"recipient": recipient}),
                ("save_vendor_email", None)] if scenario == "event-shortage" else [])
            + [("todos_complete", {"items": [{"id": 1, "reason": "Scripted planning/review demonstration finished."}]})])

    def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        index = len(self.requests) - 1
        if index < len(self.calls):
            name, args = self.calls[index]
            if name == "save_vendor_email":
                drafts = []
                for item in payload.get("input", []):
                    if item.get("type") == "function_call_output":
                        try:
                            value = json.loads(item.get("output", "{}"))
                        except (ValueError, TypeError):
                            continue
                        if isinstance(value, dict) and "draft_id" in value:
                            drafts.append(value["draft_id"])
                if not drafts:
                    # The deliberate recipient attack was blocked; report it without attempting a fabricated save.
                    name = None
                else:
                    args = {"draft_id": drafts[-1]}
            output = [{"type": "function_call", "id": f"fc_{index}", "call_id": f"call_{index}",
                       "name": name, "arguments": json.dumps(args), "status": "completed"}] if name else []
        else:
            output = []
        if not output:
            output = [{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Scripted skill demonstration finished; consult the authoritative tool results.", "annotations": []}]}]
        return httpx.Response(200, json={"id": f"resp_{index}", "object": "response", "created_at": 1,
            "model": "fixture-model", "status": "completed", "output": output,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                      "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}})
