"""Mandatory checks and audit boundaries shared by all acts."""
from uuid import uuid4

from formaggio.shop.data_models import Decision


class PolicyBlocked(RuntimeError):
    pass


class ApprovalRequired(PolicyBlocked):
    pass


class PolicyCheckError(PolicyBlocked):
    """An unavailable policy is a run error, not an expected business rejection."""


class Governance:
    def __init__(self, recorder, run_id, policy_version="1"):
        self.recorder = recorder
        self.run_id = run_id
        self.policy_version = policy_version

    def decide(self, policy_id, boundary, outcome, reason):
        return Decision(policy_id=policy_id, policy_version=self.policy_version,
                        boundary=boundary, outcome=outcome, reason=reason)

    def execute(self, name, arguments, check, operation):
        """check and operation are trusted Python callbacks, never model arguments.

        Audit failure or policy failure propagates before operation runs. Logging a
        forged 'allow' event elsewhere does not authorize this call.
        """
        call_id = uuid4().hex
        with self.recorder.span(self.run_id, name, "tool", expected_outcomes={
                PolicyBlocked: "blocked", ApprovalRequired: "require_approval"}):
            self.recorder.event(self.run_id, "tool.requested", {"call_id": call_id, "name": name, "arguments": arguments})
            try:
                decision = check()
                if not isinstance(decision, Decision):
                    raise TypeError("Policy must return a Decision.")
            except Exception as exc:
                self.recorder.event(self.run_id, "policy.error", {"call_id": call_id, "error": str(exc)})
                self.recorder.event(self.run_id, "tool.blocked", {"call_id": call_id, "reason": "policy_error"})
                raise PolicyCheckError("Policy check failed; action did not run.") from exc
            self.recorder.event(self.run_id, "policy.decision", {"call_id": call_id, **decision.model_dump()})
            if decision.outcome != "allow":
                event = "tool.approval_required" if decision.outcome == "require_approval" else "tool.blocked"
                self.recorder.event(self.run_id, event, {"call_id": call_id, "reason": decision.reason})
                error = ApprovalRequired if decision.outcome == "require_approval" else PolicyBlocked
                raise error(decision.reason)
            self.recorder.event(self.run_id, "tool.permitted", {"call_id": call_id})
            self.recorder.event(self.run_id, "tool.started", {"call_id": call_id})
            try:
                result = operation()
            except Exception as exc:
                self.recorder.event(self.run_id, "tool.failed", {"call_id": call_id, "error": str(exc)})
                raise
            self.recorder.event(self.run_id, "tool.completed", {"call_id": call_id, "result": result})
            return result

    def recipient(self, address, allowed):
        ok = address.strip().casefold() in {a.casefold() for a in allowed}
        return self.decide("vendor_recipient", "tool_invocation", "allow" if ok else "block",
                           "Approved vendor." if ok else "Recipient is not in the shop allowlist.")

    def customer_scope(self, requester, owner):
        return self.decide("customer_scope", "memory_load", "allow" if requester == owner else "block",
                           "Current customer only.")

    def checkout(self, request, report, *, manager_approved=False):
        """manager_approved is resolved by trusted checkout code, never a model tool argument."""
        if not report.ok or request.intent != "order" or not request.order_authorized:
            return self.decide("checkout", "tool_invocation", "block", "Invalid or unauthorized order.")
        if report.needs_manager_approval and not manager_approved:
            return self.decide("checkout", "tool_invocation", "require_approval", "Manager must decide externally.")
        return self.decide("checkout", "tool_invocation", "allow", "Validated and customer-authorized.")
