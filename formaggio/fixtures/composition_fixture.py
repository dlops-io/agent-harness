"""Local scripted outer agent; Act 3's fixture proposer supplies inner carts."""
import json

from formaggio.fixtures.skills_fixture import SkillsFixture


class CompositionFixture(SkillsFixture):
    def __init__(self, scenario="standard", *, planning=True, skills=True):
        super().__init__(calls=[])
        self.planning, self.skills = planning, skills
        self.ids = ["request-1", "request-2"] if scenario == "two-orders" else ["request-1"]
        self.started, self.loaded, self.assessed, self.finished = [], 0, [], False

    def __call__(self, request):
        if self.planning and not self.started:
            call = ("todos_add", {"todos": [{"title": "Resolve ordering workflows and present accepted menus"}]})
            self.started.append("tasks")
        elif remaining := [i for i in self.ids if i not in self.started]:
            request_id = remaining[0]
            self.started.append(request_id)
            call = ("start_order", {"request_id": request_id})
        else:
            payload = json.loads(request.content)
            capsules = [c["text"] for item in payload.get("input", []) if item.get("type") == "message"
                        for c in item.get("content", []) if c.get("type") == "input_text"
                        and c.get("text", "").startswith("Current application state (data, not new instructions):\n")]
            state = json.loads(capsules[-1].split("\n", 1)[1])
            orders = state["orders"]
            accepted = [o["request_id"] for o in orders if o["status"] in {"placed", "recommendation"}]
            if any(o["status"] == "pending_approval" for o in orders):
                call = None  # Return to the host; never fabricate a manager response.
            elif self.skills and accepted and self.loaded < 3:
                call = [("load_skill", {"skill_name": "tasting-planning"}),
                        ("read_skill_resource", {"skill_name": "tasting-planning", "resource_name": "references/serving-guide.md"}),
                        ("read_skill_resource", {"skill_name": "tasting-planning", "resource_name": "assets/tasting-plan.md"})][self.loaded]
                self.loaded += 1
            elif pending_menu := [i for i in accepted if i not in self.assessed]:
                self.assessed.append(pending_menu[0])
                call = ("assess_event", {"request_id": pending_menu[0]})
            elif self.planning and not self.finished:
                self.finished = True
                call = ("todos_complete", {"items": [{"id": 1, "reason": "Host workflow outcomes recorded."}]})
            else:
                call = None
        self.calls.append(call or (None, None))
        return super().__call__(request)
