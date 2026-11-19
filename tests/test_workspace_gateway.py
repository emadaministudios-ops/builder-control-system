"""Tests for policy-gated workspace access."""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.approvals import ActorType, ApprovalService, ApprovalStatus
from bcs.policy import OperatingMode, PolicyContext
from bcs.workspace import Workspace
from bcs.workspace_gateway import WorkspaceToolGateway


NOW = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def make_context(mode=OperatingMode.AUTONOMOUS):
    grants = frozenset({"workspace.read", "workspace.edit"})
    return PolicyContext(
        mode=mode,
        task_id="task-1",
        worker_id="worker-1",
        workspace_id="workspace-1",
        expected_workspace_id="workspace-1",
        task_active=True,
        worker_active=True,
        task_capabilities=grants,
        worker_capabilities=grants,
    )


class WorkspaceGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "hello.txt").write_text("hello", encoding="utf-8")
        self.workspace = Workspace("workspace-1", self.root)
        self.approvals = ApprovalService(ttl=timedelta(minutes=10))

    def gateway(self, mode=OperatingMode.AUTONOMOUS):
        return WorkspaceToolGateway(
            self.workspace, make_context(mode), self.approvals
        )

    def test_autonomous_granted_read_reaches_workspace(self):
        result = self.gateway().read_file("read-1", "hello.txt", now=NOW)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.content, "hello")

    def test_supervised_edit_is_blocked_pending_human_approval(self):
        result = self.gateway(OperatingMode.SUPERVISED).edit_file(
            "edit-1", "hello.txt", "changed", now=NOW
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.approval_request_id, "edit-1")
        self.assertEqual((self.root / "hello.txt").read_text(), "hello")
        self.assertEqual(
            self.approvals.get("edit-1", now=NOW).status, ApprovalStatus.PENDING
        )

    def test_human_approval_allows_exact_supervised_edit(self):
        gateway = self.gateway(OperatingMode.SUPERVISED)
        pending = gateway.edit_file("edit-2", "hello.txt", "changed", now=NOW)
        self.assertEqual(pending.status, "blocked")
        self.approvals.decide(
            "edit-2", actor_id="human-1", actor_type=ActorType.HUMAN,
            approved=True, now=NOW + timedelta(seconds=1),
        )
        result = gateway.edit_file(
            "edit-2", "hello.txt", "changed", now=NOW + timedelta(seconds=2)
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual((self.root / "hello.txt").read_text(), "changed")

    def test_changed_arguments_cannot_reuse_approval(self):
        gateway = self.gateway(OperatingMode.SUPERVISED)
        gateway.edit_file("edit-3", "hello.txt", "approved content", now=NOW)
        self.approvals.decide(
            "edit-3", actor_id="human-1", actor_type=ActorType.HUMAN,
            approved=True, now=NOW + timedelta(seconds=1),
        )
        result = gateway.edit_file(
            "edit-3", "hello.txt", "different content",
            now=NOW + timedelta(seconds=2),
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual((self.root / "hello.txt").read_text(), "hello")

    def test_rejected_approval_never_writes(self):
        gateway = self.gateway(OperatingMode.SUPERVISED)
        gateway.edit_file("edit-4", "hello.txt", "no", now=NOW)
        self.approvals.decide(
            "edit-4", actor_id="human-1", actor_type=ActorType.HUMAN,
            approved=False, now=NOW + timedelta(seconds=1),
        )
        result = gateway.edit_file(
            "edit-4", "hello.txt", "no", now=NOW + timedelta(seconds=2)
        )
        self.assertEqual(result.status, "blocked")
        self.assertIn("rejected", result.reason.lower())
        self.assertEqual((self.root / "hello.txt").read_text(), "hello")

    def test_unauthorized_capability_never_reads(self):
        context = make_context()
        context = PolicyContext(
            **{**context.__dict__, "worker_capabilities": frozenset()}
        )
        gateway = WorkspaceToolGateway(self.workspace, context, self.approvals)
        result = gateway.read_file("read-denied", "hello.txt", now=NOW)
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.content)

    def test_path_traversal_never_reads(self):
        result = self.gateway().read_file("read-traversal", "../outside.txt", now=NOW)
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.content)

    def test_expired_approval_never_writes(self):
        gateway = self.gateway(OperatingMode.SUPERVISED)
        gateway.edit_file("edit-expired", "hello.txt", "no", now=NOW)
        self.approvals.decide(
            "edit-expired", actor_id="human-1", actor_type=ActorType.HUMAN,
            approved=True, now=NOW + timedelta(seconds=1),
        )
        result = gateway.edit_file(
            "edit-expired", "hello.txt", "no",
            now=NOW + timedelta(minutes=11),
        )
        self.assertEqual(result.status, "blocked")
        self.assertEqual((self.root / "hello.txt").read_text(), "hello")

    def test_constructor_rejects_mismatched_workspace(self):
        context = PolicyContext(
            **{**make_context().__dict__, "workspace_id": "other"}
        )
        with self.assertRaises(ValueError):
            WorkspaceToolGateway(self.workspace, context, self.approvals)


if __name__ == "__main__":
    unittest.main()
