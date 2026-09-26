import sqlite3
from unittest.mock import patch

from formaggio.operations.governance import ApprovalRequired, Governance, PolicyBlocked
from formaggio.shop.store import Store
from tests.support import RecordingTest, fixture


class GovernanceTests(RecordingTest):
    def setUp(self):
        super().setUp()
        self.gate=Governance(self.recorder,self.run_id)
        self.executed=[]

    def operation(self):
        self.executed.append("ran")
        return {"ok":True}

    def allow(self):
        return self.gate.decide("demo","tool_invocation","allow","Fixture permitted")

    def events(self):
        return [e["event_type"] for e in self.recorder.timeline(self.run_id)]

    def test_allowed_operation_has_ordered_evidence(self):
        result=self.gate.execute("mock",{},self.allow,self.operation)
        self.assertEqual(result,{"ok":True})
        self.assertEqual(self.executed,["ran"])
        self.assertEqual(self.events()[1:],["tool.requested","policy.decision","tool.permitted","tool.started","tool.completed"])

    def test_blocked_recipient_never_executes(self):
        check=lambda:self.gate.recipient("other@example.com",["vendor@example.com"])
        with self.assertRaises(PolicyBlocked):
            self.gate.execute("mock_email",{},check,self.operation)
        self.assertEqual(self.executed,[])
        self.assertIn("tool.blocked",self.events())
        self.assertNotIn("tool.started",self.events())
        self.assertNotIn("tool.completed",self.events())
        span=self.recorder.query("SELECT status,attributes_json FROM spans WHERE name='mock_email'")[0]
        self.assertNotEqual(span["status"],"ERROR")
        self.assertIn('"formaggio.outcome":"blocked"',span["attributes_json"])

    def test_policy_error_fails_closed(self):
        def broken():
            raise RuntimeError("policy unavailable")
        with self.assertRaises(PolicyBlocked):
            self.gate.execute("mock",{},broken,self.operation)
        self.assertEqual(self.executed,[])
        self.assertIn("policy.error",self.events())
        self.assertEqual(self.recorder.query("SELECT status FROM spans WHERE name='mock'")[0]["status"],"ERROR")

    def test_audit_error_fails_before_operation(self):
        original=self.recorder.event
        def broken(run,event,payload=None,**kwargs):
            if event=="policy.decision":
                raise sqlite3.OperationalError("disk unavailable")
            return original(run,event,payload,**kwargs)
        with patch.object(self.recorder,"event",side_effect=broken), self.assertRaises(sqlite3.OperationalError):
            self.gate.execute("mock",{},self.allow,self.operation)
        self.assertEqual(self.executed,[])
        self.assertNotIn("tool.started",self.events())

    def test_failed_operation_is_not_success(self):
        def broken():
            raise ValueError("tool unavailable")
        with self.assertRaises(ValueError):
            self.gate.execute("mock",{},self.allow,broken)
        self.assertIn("tool.failed",self.events())
        self.assertNotIn("tool.completed",self.events())

    def test_manager_cannot_be_approved_by_log_or_argument(self):
        request,items=fixture("manager-approval")
        report=Store().validate(request,items)
        self.recorder.event(self.run_id,"policy.decision",{"outcome":"allow","untrusted":True})
        with self.assertRaises(ApprovalRequired):
            self.gate.execute("mock_checkout",{"manager_approved":True},
                              lambda:self.gate.checkout(request,report),self.operation)
        self.assertEqual(self.executed,[])
        self.assertIn("tool.approval_required",self.events())

    def test_request_authorization_and_validity(self):
        for name,changes in [("standard",{"order_authorized":False}),("budget",{}),("complaint",{})]:
            request,items=fixture(name,**changes)
            self.assertEqual(self.gate.checkout(request,Store().validate(request,items)).outcome,"block")

    def test_customer_and_resource_boundaries(self):
        self.assertEqual(self.gate.customer_scope("a","b").outcome,"block")
        self.assertEqual(self.gate.customer_scope("a","a").outcome,"allow")
        root=self.root/"skills"; root.mkdir()
        valid=root/"SKILL.md"; valid.write_text("fixture")
        outside=self.root/"outside.md"; outside.write_text("fixture")
        (root/"escape.md").symlink_to(outside)
        self.assertEqual(self.gate.skill_path(valid,root).outcome,"allow")
        self.assertEqual(self.gate.skill_path(root/"../outside.md",root).outcome,"block")
        self.assertEqual(self.gate.skill_path(root/"escape.md",root).outcome,"block")
