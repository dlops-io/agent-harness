"""Synchronous retrieval checks and approved HTML artifacts; never transmits email."""
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import json
from string import Template
from uuid import uuid4

from formaggio.config import load_json
from formaggio.shop.data_models import EmailReview, LineItem, VendorDraft
from formaggio.operations.governance import Governance, PolicyBlocked


def validate_email_template(text):
    """Small inert classroom template format, with escaped text substitutions."""
    class CheckHTML(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in {"html", "head", "meta", "title", "body", "h1", "h2", "p", "pre", "footer"}:
                raise ValueError("Email template contains an unsupported element.")
            for key, value in attrs:
                if (tag, key, value) not in {("html", "lang", "en"), ("meta", "charset", "utf-8")}:
                    raise ValueError("Email template attributes cannot contain links, code or substitutions.")
        def handle_endtag(self, tag):
            self.handle_starttag(tag, [])
    if len(text) > 32000 or not Template(text).is_valid() or set(Template(text).get_identifiers()) != {"recipient", "subject", "body"}:
        raise ValueError("Email template must contain only recipient, subject and body placeholders.")
    CheckHTML().feed(text)


def detect_injection(text):
    """Deliberately small heuristic, including an intentional quoted-text false positive."""
    lower = text.casefold()
    signals = [name for name, present in {
        "ignore_allergy": "ignore" in lower and "allerg" in lower,
        "redirect_details": "send" in lower and "details" in lower and "@" in lower,
        "self_approval": "document as approval" in lower,
    }.items() if present]
    return {"flagged": bool(signals), "signals": signals, "detector": "teaching-heuristic-v1"}


def retrieve_vendor_document(kind, recorder, run_id, *, detector=detect_injection):
    text = load_json("vendor_documents.json")[kind]
    recorder.event(run_id, "retrieval.received", {"source": f"vendor_documents.json:{kind}",
                                                 "trust": "untrusted_vendor", "text": text})
    gate = Governance(recorder, run_id)
    try:
        verdict = detector(text)
        if not isinstance(verdict.get("flagged"), bool):
            raise ValueError("Detector must return a boolean flagged verdict.")
    except Exception as exc:
        recorder.event(run_id, "injection.checked", {"error": str(exc), "outcome": "quarantine"})
        decision = gate.decide("vendor_content", "retrieval", "block", "Detector unavailable; quarantine content.")
        recorder.event(run_id, "policy.decision", decision.model_dump())
        return {"status": "quarantined", "reason": decision.reason, "source": kind}
    decision = gate.decide("vendor_content", "retrieval", "block" if verdict["flagged"] else "allow",
                          "Suspicious instructions quarantined." if verdict["flagged"] else "No heuristic signal; still untrusted data.")
    recorder.event(run_id, "injection.checked", {**verdict, "source": kind})
    recorder.event(run_id, "policy.decision", decision.model_dump())
    if verdict["flagged"]:
        return {"status": "quarantined", "reason": decision.reason, "source": kind}
    return {"status": "included", "source": kind, "trust": "untrusted_vendor", "text": text}


class VendorOutreach:
    def __init__(self, store, request, items, recorder, run_id, output_root):
        self.store, self.request = store, request
        self.items = [LineItem.model_validate(i) for i in items]
        self.recorder, self.run_id, self.output_root = recorder, run_id, output_root
        self.gate = Governance(recorder, run_id, store.policy.version)
        self.drafts, self.reviews, self.decisions, self.artifacts = {}, {}, {}, {}
        self.email_template = None
        self.require_template = False

    def assess(self):
        report = self.store.validate(self.request, self.items)
        shortfalls = [{"product": i.product, "grams": i.grams - self.store.inventory[i.product]}
                      for i in report.items if i.grams > self.store.inventory[i.product]]
        valid_for_sourcing = not any(v.rule != "stock" for v in report.violations)
        pairings = self.store.eligible_pairings(self.request, self.items, allow_stock_shortage=True) if valid_for_sourcing else {}
        result = {"report": report.model_dump(mode="json"), "shortfalls": shortfalls,
            "inventory_source": "shop_inventory",
            "inventory_g": {i.product: self.store.inventory[i.product] for i in report.items},
            "vendor_availability": "unconfirmed",
            "valid_for_sourcing": valid_for_sourcing,
            "conditional_pairings": {k: [p.model_dump(mode="json") for p in rows] for k, rows in pairings.items()},
            "scope": "Sourcing quote only. Stock is not reserved; vendor availability is unconfirmed. Excludes tax, shipping and pairings."}
        self.recorder.event(self.run_id, "event.assessed", result)
        return result

    def draft_fields(self, recipient):
        if self.require_template and self.email_template is None:
            raise PolicyBlocked("Load vendor-outreach/assets/email-template.html before requesting this draft.")
        assessment = self.assess()
        if not assessment["valid_for_sourcing"] or not assessment["shortfalls"]:
            raise PolicyBlocked("A sourcing draft requires a compliant menu with a stock shortfall.")
        lines = [f"- {self.store.products[i['product']].name}: {i['grams']} g additional"
                 for i in assessment["shortfalls"]]
        subject = "Availability inquiry — corporate cheese tasting"
        body = ("Hello,\nPlease confirm availability for our corporate tasting:\n" + "\n".join(lines)
            + f"\nDestination: {self.request.state}. Confirmed allergy constraints: {', '.join(self.request.allergies) or 'none'}."
            + "\nPlease provide current pricing and delivery timing. This is an inquiry, not a purchase order.\nThank you.")
        fingerprint = sha256(json.dumps({"request": self.request.model_dump(mode="json"),
            "items": [i.model_dump() for i in self.items], "assessment": assessment,
            "policy": self.store.policy.model_dump(mode="json"), "recipient": recipient, "subject": subject, "body": body,
            "email_template": self.email_template}, sort_keys=True).encode()).hexdigest()
        return {"recipient": recipient, "subject": subject, "body": body, "fingerprint": fingerprint}

    def prepare(self, recipient):
        recipient = recipient.strip().casefold()
        def create():
            draft = VendorDraft(draft_id=uuid4().hex, **self.draft_fields(recipient))
            self.recorder.event(self.run_id, "email.drafted", draft.model_dump())
            self.drafts[draft.draft_id] = draft
            return draft.model_dump()
        return self.gate.execute("email.prepare", {"recipient": recipient},
            lambda: self.gate.recipient(recipient, self.store.policy.allowed_vendor_recipients), create)

    def review(self, draft_id, ticket_id):
        draft = self.drafts[draft_id]
        review = EmailReview(ticket_id=ticket_id, draft=draft)
        self.recorder.event(self.run_id, "email.approval_requested", review.model_dump())
        self.reviews[ticket_id] = review
        return review

    def decide(self, ticket_id, decision, *, source="human"):
        if decision not in {"approve", "decline"} or ticket_id in self.decisions:
            raise PolicyBlocked("Invalid or already decided email approval.")
        review = self.reviews[ticket_id]
        self.recorder.event(self.run_id, "email.approval_decided", {"ticket_id": ticket_id,
            "fingerprint": review.draft.fingerprint, "decision": decision, "source": source})
        self.decisions[ticket_id] = decision

    def save(self, draft_id):
        draft = self.drafts[draft_id]
        def check():
            recipient = self.gate.recipient(draft.recipient, self.store.policy.allowed_vendor_recipients)
            if recipient.outcome != "allow":
                return recipient
            approved = any(self.decisions.get(t) == "approve" and r.draft == draft for t, r in self.reviews.items())
            if not approved:
                return self.gate.decide("email_approval", "artifact_delivery", "block", "No external approval for this exact draft.")
            # The confirmed request/menu is immutable in this service. Changing
            # inventory or policy requires a new draft and a new review.
            try:
                fresh = self.draft_fields(draft.recipient)
            except PolicyBlocked as exc:
                return self.gate.decide("email_binding", "artifact_delivery", "block", str(exc))
            if fresh != draft.model_dump(exclude={"draft_id"}):
                return self.gate.decide("email_binding", "artifact_delivery", "block", "Sourcing facts changed; draft again for approval.")
            return self.gate.decide("email_approval", "artifact_delivery", "allow", "Exact draft approved; recipient and sourcing facts rechecked.")
        def write():
            if draft_id in self.artifacts:
                return {"status": "saved", "path": str(self.artifacts[draft_id]), "replayed": True, "email_transmitted": False}
            directory = self.output_root / self.run_id
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"vendor-email-{draft_id}.html"
            page = ("<!doctype html><html lang='en'><meta charset='utf-8'><title>Mock vendor email</title>"
                    "<body><h1>Mock email — nothing transmitted</h1><p>To: " + escape(draft.recipient)
                    + "</p><h2>" + escape(draft.subject) + "</h2><pre style='white-space:pre-wrap'>"
                    + escape(draft.body) + "</pre></body></html>")
            if self.email_template is not None:
                validate_email_template(self.email_template)
                page = Template(self.email_template).substitute(recipient=escape(draft.recipient),
                    subject=escape(draft.subject), body=escape(draft.body))
            created = False
            try:
                with path.open("x", encoding="utf-8") as output:
                    created = True
                    output.write(page)
            except Exception:
                if created:
                    path.unlink(missing_ok=True)  # Remove only this action's partial file.
                raise
            self.artifacts[draft_id] = path
            return {"status": "saved", "path": str(path), "replayed": False, "email_transmitted": False}
        try:
            return self.gate.execute("email.save_html", {"draft_id": draft_id}, check, write)
        except Exception:
            # Filesystem and trace DB are not one transaction. Preserve any written
            # file and expose its status for recovery; retry cannot duplicate it.
            if draft_id in self.artifacts:
                try:
                    self.recorder.event(self.run_id, "artifact.audit_incomplete", {"path": str(self.artifacts[draft_id])})
                except Exception:
                    pass
            raise
