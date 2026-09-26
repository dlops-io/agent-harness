"""In-memory mock transactions, separate from SQLite observability.

Share ONE Checkout instance for competing workflows. Its lock covers final checks
and stock changes. Nothing here is an agent tool; only the application can decide
approval tickets. This classroom service has no durable process recovery.
"""
from hashlib import sha256
import json
from threading import RLock
from uuid import uuid4

from formaggio.shop.data_models import ApprovalTicket, LineItem, OrderReceipt, PairingSelection
from formaggio.operations.governance import Governance, PolicyBlocked
from formaggio.shop.store import Store


class Checkout:
    def __init__(self, store=None):
        self.store = store or Store()
        self._tickets = {}
        self._orders = {}
        self._lock = RLock()

    @property
    def orders(self):
        return tuple(self._orders.values())

    def fingerprint(self, request, report):
        # Inventory is rechecked, not approved. Prices, product attributes, policy,
        # quantities and all confirmed customer constraints ARE approval-bound.
        payload = {"request": request.model_dump(mode="json"),
                   "items": [i.model_dump() for i in report.items],
                   "subtotal_cents": report.subtotal_cents,
                   "policy": self.store.policy.model_dump(mode="json"),
                   "products": [self.store.products[i.product].model_dump(mode="json", exclude={"stock_g"})
                                for i in report.items]}
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def pairings(self, request, items):
        return tuple(PairingSelection(product=product, suggestions=tuple(suggestions))
                     for product, suggestions in self.store.eligible_pairings(request, items).items())

    def open_ticket(self, request, items, checkout_key, recorder, run_id):
        with self._lock:
            report = self.store.validate(request, items)
            if not report.ok or not report.needs_manager_approval or not request.order_authorized or request.intent != "order":
                raise PolicyBlocked("Only valid, authorized orders above the threshold can request approval.")
            ticket = ApprovalTicket(ticket_id=uuid4().hex, checkout_key=checkout_key,
                                    fingerprint=self.fingerprint(request, report), request=request, report=report)
            recorder.event(run_id, "approval.requested", ticket.model_dump(mode="json"))
            self._tickets[ticket.ticket_id] = ticket
            return ticket

    def decide(self, ticket_id, decision, recorder, run_id, *, source="human"):
        """Called by the host's response handler; not exposed to the model."""
        if decision not in {"approve", "decline"}:
            raise ValueError("Manager decision must be approve or decline.")
        with self._lock:
            ticket = self._tickets[ticket_id]
            if ticket.decision != "pending":
                raise PolicyBlocked("This approval ticket has already been decided.")
            recorder.event(run_id, "approval.decided", {"ticket_id": ticket_id,
                           "fingerprint": ticket.fingerprint, "decision": decision, "source": source})
            self._tickets[ticket_id] = ticket.model_copy(update={"decision": decision})

    def place(self, request, items, checkout_key, recorder, run_id, *, ticket_id=None):
        """Final enforcement, including direct calls that bypass the workflow graph."""
        if not checkout_key:
            raise ValueError("A nonempty idempotency key is required.")
        items = [LineItem.model_validate(item) for item in items]
        with self._lock:
            report = self.store.validate(request, items)
            fingerprint = self.fingerprint(request, report)
            existing = self._orders.get(checkout_key)
            governance = Governance(recorder, run_id, self.store.policy.version)

            def check():
                if existing:
                    # A replay may now exceed remaining stock. Other invalid input,
                    # including unknown lines, must not be hidden by normalization.
                    same = existing.fingerprint == fingerprint and all(v.rule == "stock" for v in report.violations)
                    return governance.decide("checkout_replay", "tool_invocation", "allow" if same else "block",
                                             "Return original receipt." if same else "Idempotency key belongs to another cart or request.")
                ticket = self._tickets.get(ticket_id)
                approved = bool(ticket and ticket.decision == "approve"
                                and ticket.checkout_key == checkout_key and ticket.fingerprint == fingerprint)
                return governance.checkout(request, report, manager_approved=approved)

            def commit():
                if existing:
                    recorder.event(run_id, "order.replayed", {"order_id": existing.order_id, "checkout_key": checkout_key})
                    return existing.model_dump(mode="json")
                # Recompute final pairings from current data, never trust an earlier proposal.
                receipt = OrderReceipt(order_id=uuid4().hex, checkout_key=checkout_key,
                    fingerprint=fingerprint, request=request, report=report,
                    pairings=self.pairings(request, list(report.items)),
                    approval_ticket_id=ticket_id if report.needs_manager_approval else None)
                recorder.event(run_id, "order.placing", receipt.model_dump(mode="json"))
                for item in report.items:
                    self.store.inventory[item.product] -= item.grams
                self._orders[checkout_key] = receipt
                return receipt.model_dump(mode="json")

            before = dict(self.store.inventory)
            try:
                result = governance.execute("place_mock_order", {"checkout_key": checkout_key,
                    "ticket_id": ticket_id, "items": [i.model_dump() for i in items]}, check, commit)
                recorder.event(run_id, "order.placed" if not existing else "order.replay_completed", result)
            except Exception:
                # In-process rollback also covers a failed completion audit write.
                # This is not a distributed transaction or crash-recovery guarantee.
                self.store.inventory.clear()
                self.store.inventory.update(before)
                mutated = not existing and checkout_key in self._orders
                if not existing:
                    self._orders.pop(checkout_key, None)
                if mutated:
                    try:
                        recorder.event(run_id, "order.rolled_back", {"checkout_key": checkout_key,
                            "reason": "Required completion audit failed; in-memory order and stock restored."})
                    except Exception:
                        pass  # The original audit error still propagates, even if storage remains unavailable.
                raise
            return OrderReceipt.model_validate(result)
