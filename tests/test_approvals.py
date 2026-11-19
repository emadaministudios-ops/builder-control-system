"""Unit tests for human approval lifecycle."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.approvals import (
    ActorType,
    ApprovalError,
    ApprovalService,
    ApprovalStatus,
)
from bcs.policy import (
    DecisionStatus,
    OperatingMode,
    PolicyContext,
    ToolRequest,
    evaluate_tool_request,
)


NOW = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def make_request(path="src/app.py"):
    return ToolRequest(
        request_id="approval-1",
        task_id="task-1",
        worker_id="worker-dev",
        workspace_id="workspace-1",
        capability="workspace.edit",
        arguments={"path": path},
    )


class ApprovalServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = ApprovalService(ttl=timedelta(minutes=10))
        self.request = make_request()
        self.pending = self.service.submit(self.request, now=NOW)

    def test_submission_creates_pending_expiring_request(self):
        self.assertEqual(self.pending.status, ApprovalStatus.PENDING)
        self.assertEqual(self.pending.expires_at, NOW + timedelta(minutes=10))

    def test_duplicate_submission_is_idempotent(self):
        duplicate = self.service.submit(self.request, now=NOW + timedelta(minutes=1))
        self.assertEqual(duplicate, self.pending)

    def test_same_id_with_changed_arguments_is_rejected(self):
        with self.assertRaises(ApprovalError):
            self.service.submit(make_request("src/other.py"), now=NOW)

    def test_worker_cannot_approve(self):
        with self.assertRaises(ApprovalError):
            self.service.decide(
                self.request.request_id,
                actor_id="worker-dev",
                actor_type=ActorType.WORKER,
                approved=True,
                now=NOW + timedelta(seconds=1),
            )

    def test_human_can_approve_and_policy_receives_bound_approval(self):
        approved = self.service.decide(
            self.request.request_id,
            actor_id="human-1",
            actor_type=ActorType.HUMAN,
            approved=True,
            reason="Reviewed the requested change.",
            now=NOW + timedelta(seconds=1),
        )
        self.assertEqual(approved.status, ApprovalStatus.APPROVED)
        policy_approval = self.service.policy_approval(
            self.request.request_id, now=NOW + timedelta(seconds=2)
        )
        context = PolicyContext(
            mode=OperatingMode.SUPERVISED,
            task_id="task-1",
            worker_id="worker-dev",
            workspace_id="workspace-1",
            expected_workspace_id="workspace-1",
            task_active=True,
            worker_active=True,
            task_capabilities=frozenset({"workspace.edit"}),
            worker_capabilities=frozenset({"workspace.edit"}),
        )
        result = evaluate_tool_request(
            self.request, context, policy_approval, now=NOW + timedelta(seconds=2)
        )
        self.assertEqual(result.status, DecisionStatus.ALLOW)

    def test_rejection_does_not_produce_policy_approval(self):
        self.service.decide(
            self.request.request_id,
            actor_id="human-1",
            actor_type=ActorType.HUMAN,
            approved=False,
            reason="Not safe yet.",
            now=NOW + timedelta(seconds=1),
        )
        self.assertIsNone(
            self.service.policy_approval(
                self.request.request_id, now=NOW + timedelta(seconds=2)
            )
        )

    def test_expired_request_cannot_be_approved(self):
        with self.assertRaises(ApprovalError):
            self.service.decide(
                self.request.request_id,
                actor_id="human-1",
                actor_type=ActorType.HUMAN,
                approved=True,
                now=NOW + timedelta(minutes=10),
            )
        self.assertEqual(
            self.service.get(self.request.request_id, now=NOW + timedelta(minutes=10)).status,
            ApprovalStatus.EXPIRED,
        )

    def test_decision_is_final(self):
        self.service.decide(
            self.request.request_id,
            actor_id="human-1",
            actor_type=ActorType.HUMAN,
            approved=True,
            now=NOW + timedelta(seconds=1),
        )
        with self.assertRaises(ApprovalError):
            self.service.decide(
                self.request.request_id,
                actor_id="human-2",
                actor_type=ActorType.HUMAN,
                approved=False,
                now=NOW + timedelta(seconds=2),
            )

    def test_unknown_request_returns_none(self):
        self.assertIsNone(self.service.get("missing", now=NOW))

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(ApprovalError):
            self.service.submit(self.request, now=datetime(2026, 11, 19, 10, 0))


if __name__ == "__main__":
    unittest.main()
