"""Host review loop for composed workflows; approval is never a model tool."""
from contextlib import nullcontext
import json

from agent_framework import Message, MiddlewareFailure


async def run_with_workflow_reviews(agent, session, orders, manager, recorder, run_id, *, reply_schema, active_budget=None):
    reviews = 0
    message = "Process all confirmed requests with the ordering workflow, then provide a short tasting plan for each accepted menu."
    while True:
        # This segment includes nested start_order workflows and proposer calls.
        async with active_budget.measure() if active_budget is not None else nullcontext():
            response = await agent.run(message, session=session, options={"response_format": reply_schema})
        if response.user_input_requests:
            raise MiddlewareFailure("Unexpected tool approval request; manager review belongs to the ordering workflow.")
        pending = orders.pending_reviews()
        if not pending:
            return response
        updates = []
        for request_id, response_id, ticket in pending:
            reviews += 1
            if reviews > 2:
                raise MiddlewareFailure("At most two manager reviews are allowed.")
            if manager is None:
                raise ValueError("A host manager callback is required to resume pending orders.")
            recorder.event(run_id, "composition.host_review", {"request_id": request_id, "ticket_id": ticket.ticket_id})
            decision = await manager(ticket)
            # Waiting for the manager consumes neither active time nor call caps.
            async with active_budget.measure() if active_budget is not None else nullcontext():
                updates.append(await orders.resume(request_id, response_id, ticket.ticket_id, decision))
        message = Message(role="user", contents=["Host-resumed workflow results (authoritative data):\n" + json.dumps(updates)])
