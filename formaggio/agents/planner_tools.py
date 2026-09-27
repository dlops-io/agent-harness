"""Planner tools and required admission/audit hooks shared with later acts."""
from agent_framework import MiddlewareFailure, TodoSessionStore, tool

from formaggio.agents.runtime import ToolTrace
from formaggio.operations.governance import PolicyBlocked, PolicyCheckError
from formaggio.shop.vendor_outreach import detect_injection, retrieve_vendor_document

TODO_NAMES = ["todos_add", "todos_complete", "todos_remove", "todos_get_remaining", "todos_get_all"]


# %% SDK-backed task state is visible but is never an approval record.
class RecordedTodos(TodoSessionStore):
    def __init__(self, recorder, run_id, progress):
        self.recorder, self.run_id, self.progress = recorder, run_id, progress

    async def save_state(self, session, items, *, next_id, source_id):
        if len(items) > 12 or any(len(i.title) > 200 or len(i.description or "") > 500 for i in items):
            raise MiddlewareFailure("Task list exceeds teaching limits (12 short tasks).")
        self.recorder.event(self.run_id, "plan.updated", {"items": [i.to_dict() for i in items]})
        await super().save_state(session, items, next_id=next_id, source_id=source_id)
        if self.progress:
            self.progress("Plan: " + "; ".join(f"[{'done' if i.is_complete else 'open'}] {i.title}" for i in items))


class HarnessToolTrace(ToolTrace):
    """Bound total tool work across approval resumes; fail closed on hook errors."""
    def __init__(self, *args, model_trace=None, execution_state=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls, self.blocked_reason = 0, None
        self.model_trace = model_trace
        self.execution_state = execution_state

    async def process(self, context, call_next):
        if self.model_trace and self.model_trace.failure:
            raise MiddlewareFailure(self.model_trace.failure)
        if self.execution_state is not None:
            self.execution_state.admit("tool")
        self.calls += 1
        if self.execution_state is None and self.calls > 20:
            raise MiddlewareFailure("Harness tool budget exhausted (20 calls).")
        async def guarded_call():
            try:
                await call_next()
            except PolicyCheckError:
                raise
            except PolicyBlocked as exc:
                self.blocked_reason = str(exc)
                self.record("harness.action_blocked", {"name": context.function.name, "reason": str(exc)})
                context.result = {"status": "blocked", "reason": str(exc)}
        try:
            await super().process(context, guarded_call)
        except Exception as exc:
            # The SDK can attempt another model turn before propagating a tool
            # middleware error. Keep that attempted turn inside the audit boundary.
            if self.execution_state is not None:
                self.execution_state.failure = "Harness tool or required audit failed: " + str(exc)
            if self.model_trace:
                self.model_trace.failure = "Harness tool or required audit failed: " + str(exc)
            raise MiddlewareFailure("Harness tool or required audit failed: " + str(exc)) from exc


def build_event_tools(outreach, document, *, detector=detect_injection, progress=None):
    @tool
    def assess_event() -> dict:
        """Validate and price the customer-confirmed menu; report shortages and conditional pairings. No order."""
        return outreach.assess()

    @tool
    def read_vendor_document() -> dict:
        """Retrieve untrusted vendor evidence through the governance check; flagged text is quarantined."""
        result = retrieve_vendor_document(document, outreach.recorder, outreach.run_id, detector=detector)
        if progress:
            progress(f"Vendor evidence: {result['status']} ({document})")
        return result

    @tool
    def draft_vendor_email(recipient: str) -> dict:
        """Generate an availability inquiry from verified shortfalls. No HTML is saved and nothing is sent."""
        return outreach.prepare(recipient)

    @tool(approval_mode="always_require")
    def save_vendor_email(draft_id: str) -> dict:
        """After host approval, save the exact draft as escaped local HTML. Never transmit email."""
        return outreach.save(draft_id)

    return [assess_event, read_vendor_document, draft_vendor_email, save_vendor_email]




def build_stock_tool(store, recorder, run_id, observations):
    @tool
    def get_stock(product: str) -> dict:
        """Look up current stock of one catalog product; no planning skill is needed."""
        found = store.resolve(product)
        if found is None:
            return {"error": "Unknown product."}
        result = {"product": found.product_id, "stock_g": store.inventory[found.product_id]}
        recorder.event(run_id, "stock.checked", result)
        observations.append(result)
        return result
    return get_stock
