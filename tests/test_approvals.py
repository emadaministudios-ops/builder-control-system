"""Unit tests for human approval lifecycle."""
import sys
import tempfile
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
from bcs.storage import SQLiteOrchestrationStore
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


class PersistentApprovalServiceTests(unittest.TestCase):
    def test_pending_request_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "approvals.sqlite3"
            store = SQLiteOrchestrationStore(path)
            first = ApprovalService(ttl=timedelta(minutes=10), store=store)
            expected = first.submit(make_request(), now=NOW)
            store.close()

            reopened_store = SQLiteOrchestrationStore(path)
            second = ApprovalService(ttl=timedelta(minutes=10), store=reopened_store)
            restored = second.get("approval-1", now=NOW + timedelta(seconds=1))
            self.assertEqual(restored, expected)
            reopened_store.close()

    def test_human_decision_survives_restart_and_remains_fingerprint_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "approvals.sqlite3"
            store = SQLiteOrchestrationStore(path)
            first = ApprovalService(ttl=timedelta(minutes=10), store=store)
            first.submit(make_request(), now=NOW)
            approved = first.decide(
                "approval-1", actor_id="authenticated-human",
                actor_type=ActorType.HUMAN, approved=True,
                reason="Reviewed", now=NOW + timedelta(seconds=2),
            )
            store.close()

            reopened_store = SQLiteOrchestrationStore(path)
            second = ApprovalService(ttl=timedelta(minutes=10), store=reopened_store)
            self.assertEqual(second.get("approval-1", now=NOW + timedelta(seconds=3)), approved)
            policy_approval = second.policy_approval(
                "approval-1", now=NOW + timedelta(seconds=3)
            )
            self.assertIsNotNone(policy_approval)
            self.assertEqual(
                policy_approval.request_fingerprint, approved.request_fingerprint
            )
            with self.assertRaises(ApprovalError):
                second.submit(make_request("src/changed.py"), now=NOW + timedelta(seconds=4))
            reopened_store.close()

    def test_expiration_is_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "approvals.sqlite3"
            store = SQLiteOrchestrationStore(path)
            service = ApprovalService(ttl=timedelta(minutes=1), store=store)
            service.submit(make_request(), now=NOW)
            expired = service.get("approval-1", now=NOW + timedelta(minutes=1))
            self.assertEqual(expired.status, ApprovalStatus.EXPIRED)
            store.close()

            reopened_store = SQLiteOrchestrationStore(path)
            restored = ApprovalService(store=reopened_store).get(
                "approval-1", now=NOW + timedelta(minutes=2)
            )
            self.assertEqual(restored.status, ApprovalStatus.EXPIRED)
            reopened_store.close()

    def test_human_can_cancel_and_cancellation_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "approvals.sqlite3"
            store = SQLiteOrchestrationStore(path)
            first = ApprovalService(ttl=timedelta(minutes=10), store=store)
            first.submit(make_request(), now=NOW)
            cancelled = first.cancel(
                "approval-1", actor_id="authenticated-human",
                actor_type=ActorType.HUMAN, reason="No longer needed.",
                now=NOW + timedelta(seconds=1),
            )
            self.assertEqual(cancelled.status, ApprovalStatus.CANCELLED)
            store.close()

            reopened_store = SQLiteOrchestrationStore(path)
            second = ApprovalService(store=reopened_store)
            self.assertEqual(
                second.get("approval-1", now=NOW + timedelta(seconds=2)),
                cancelled,
            )
            self.assertIsNone(
                second.policy_approval("approval-1", now=NOW + timedelta(seconds=2))
            )
            reopened_store.close()

    def test_worker_cannot_cancel_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOrchestrationStore(Path(folder) / "approvals.sqlite3")
            service = ApprovalService(store=store)
            service.submit(make_request(), now=NOW)
            with self.assertRaises(ApprovalError):
                service.cancel(
                    "approval-1", actor_id="worker",
                    actor_type=ActorType.WORKER, now=NOW + timedelta(seconds=1),
                )
            store.close()

    def test_old_schema_is_migrated_without_losing_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite3"
            store = SQLiteOrchestrationStore(path)
            store.close()
            import sqlite3
            connection = sqlite3.connect(path)
            connection.execute("DROP TABLE approval_requests")
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
            connection.close()

            migrated = SQLiteOrchestrationStore(path)
            self.assertEqual(migrated.list_approvals(), ())
            version = migrated._connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertEqual(version, 8)
            migrated.close()


if __name__ == "__main__":
    unittest.main()
