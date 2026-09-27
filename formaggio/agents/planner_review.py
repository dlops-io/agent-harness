"""Host-owned review loop; approval messages never come from the model."""
from contextlib import nullcontext
import json

from agent_framework import Message, MiddlewareFailure

from formaggio.operations.governance import PolicyBlocked

async def run_with_review(agent, session, task, outreach, reviewer, recorder, run_id, *, decision_source, active_budget, reply_schema=None):
    message, approval_count = task, 0
    while True:
        async with active_budget.measure() if active_budget is not None else nullcontext():
            response = await agent.run(message, session=session,
                **({"options": {"response_format": reply_schema}} if reply_schema else {}))
        pending = [c for c in response.user_input_requests if c.type == "function_approval_request"]
        if not pending:
            return response.text
        replies = []
        for request in pending:
            approval_count += 1
            if approval_count > 2:
                raise MiddlewareFailure("At most two email approval requests are allowed per run.")
            call = request.function_call
            if call.name != "save_vendor_email":
                raise PolicyBlocked("Unexpected approval request.")
            args = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
            if set(args) != {"draft_id"}:
                raise PolicyBlocked("Unexpected email approval arguments.")
            review = outreach.review(args["draft_id"], request.id)
            if reviewer is None:
                raise ValueError("A human review callback is required to approve or decline the mock email.")
            decision = await reviewer(review)
            outreach.decide(review.ticket_id, decision, source=decision_source)
            replies.append(request.to_function_approval_response(decision == "approve"))
        message = Message(role="user", contents=replies)
