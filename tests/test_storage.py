import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.orchestrator import (
    Orchestrator, TaskRecord, TaskState, WorkerResult, WorkerResultStatus, WorkerTask,
)
from bcs.qa_handoff import QAEscalation
from bcs.storage import SQLiteOrchestrationStore, StorageError
from bcs.consultation import ConsultationRecord
from bcs.consultation_feedback import ConsultationOutcome


class SQLiteOrchestrationStoreTests(unittest.TestCase):
    def record(self, state=TaskState.QUEUED):
        now = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
        task = WorkerTask("task-1", "parent-1", "builder", "Build feature",
                          ("Tests pass",), ("evidence:1",), 1)
        result = None if state == TaskState.QUEUED else WorkerResult(
            WorkerResultStatus.COMPLETED, "Finished", ("src/app.py",),
            ("evidence:2",), (),
        )
        return TaskRecord(task, state, "worker-1" if result else None, result, now, now)

    def escalation(self):
        return QAEscalation("escalate:run:check", "parent-1", "run", "check",
                            "QA could not execute", ("evidence:3",))

    def test_task_survives_close_and_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            store = SQLiteOrchestrationStore(path)
            expected = self.record(TaskState.COMPLETED)
            store.save_task(expected)
            store.close()
            reopened = SQLiteOrchestrationStore(path)
            self.assertEqual(reopened.get_task("task-1"), expected)
            reopened.close()

    def test_escalations_survive_close_and_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            store = SQLiteOrchestrationStore(path)
            expected = self.escalation()
            store.save_escalation(expected)
            store.close()
            reopened = SQLiteOrchestrationStore(path)
            self.assertEqual(reopened.list_escalations(), (expected,))
            reopened.close()

    def test_upsert_is_idempotent_and_updates_state(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOrchestrationStore(Path(folder) / "bcs.sqlite3")
            queued = self.record()
            completed = self.record(TaskState.COMPLETED)
            store.save_task(queued)
            store.save_task(queued)
            store.save_task(completed)
            self.assertEqual(store.list_tasks(), (completed,))
            store.close()

    def test_save_state_rolls_back_all_rows_on_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOrchestrationStore(Path(folder) / "bcs.sqlite3")
            good = self.record()
            bad = QAEscalation("bad-id", None, "run", "check", "bad", ())
            with self.assertRaises(StorageError):
                store.save_state((good,), (bad,))
            self.assertEqual(store.list_tasks(), ())
            store.close()

    def test_invalid_non_json_record_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOrchestrationStore(Path(folder) / "bcs.sqlite3")
            bad = WorkerResult(WorkerResultStatus.COMPLETED, "bad", (object(),))
            record = self.record()
            record = TaskRecord(record.task, TaskState.COMPLETED, "worker", bad,
                                record.created_at, record.updated_at)
            with self.assertRaises(StorageError):
                store.save_task(record)
            self.assertEqual(store.list_tasks(), ())
            store.close()

    def test_uses_json_not_pickle(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            store = SQLiteOrchestrationStore(path)
            store.save_task(self.record())
            raw = sqlite3.connect(path).execute(
                "SELECT payload FROM orchestration_tasks"
            ).fetchone()[0]
            parsed = json.loads(raw)
            self.assertEqual(parsed["version"], 1)
            self.assertIn("task", parsed)
            store.close()

    def test_schema_version_is_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 99")
            connection.close()
            with self.assertRaises(StorageError):
                SQLiteOrchestrationStore(path)

    def test_consultation_history_survives_close_and_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            store = SQLiteOrchestrationStore(path)
            expected = ConsultationRecord(
                consultation_id="consult-1", task_id="task-1",
                requesting_role="frontend-developer", should_consult=True,
                specialist_role="architect", question="Where should this boundary live?",
                reason="A design dependency exists.", advice="Use an adapter boundary.",
                risks=("Extra abstraction",), next_steps=("Define the interface.",),
                confidence="high", created_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
            )
            store.save_consultation(expected)
            store.close()
            reopened = SQLiteOrchestrationStore(path)
            self.assertEqual(reopened.list_consultations(), (expected,))
            self.assertEqual(reopened.list_consultations("task-1"), (expected,))
            self.assertEqual(reopened.list_consultations("missing"), ())
            reopened.close()

    def test_consultation_outcome_is_durable_and_requires_existing_consultation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            store = SQLiteOrchestrationStore(path)
            consultation = ConsultationRecord(
                consultation_id="consult-feedback-1", task_id="task-feedback",
                requesting_role="backend-developer", should_consult=True,
                specialist_role="architect", question="Which boundary?",
                reason="A design risk exists.", advice="Use an adapter.",
                risks=(), next_steps=("Define interface.",), confidence="medium",
                created_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
            )
            store.save_consultation(consultation)
            outcome = ConsultationOutcome(
                outcome_id="outcome-1", consultation_id=consultation.consultation_id,
                task_id=consultation.task_id, disposition="partially_followed",
                worker_reason="Used the adapter boundary but changed its shape.",
                qa_status="failed", qa_run_id="qa-run-1",
                evidence_refs=("evidence:failed-check",),
                created_at=datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc),
            )
            store.save_consultation_outcome(outcome)
            store.close()
            reopened = SQLiteOrchestrationStore(path)
            self.assertEqual(reopened.list_consultation_outcomes("task-feedback"), (outcome,))
            self.assertEqual(reopened.list_consultation_outcomes("other-task"), ())
            reopened.close()

    def test_consultation_outcome_cannot_claim_qa_without_evidence(self):
        with self.assertRaises(ValueError):
            ConsultationOutcome(
                outcome_id="outcome-invalid", consultation_id="consult-unknown",
                task_id="task-1", disposition="followed", worker_reason="Used it.",
                qa_status="passed", qa_run_id=None, evidence_refs=(),
                created_at=datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc),
            )

    def test_consultation_outcome_requires_consultation_foreign_key(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOrchestrationStore(Path(folder) / "bcs.sqlite3")
            outcome = ConsultationOutcome(
                outcome_id="outcome-orphan", consultation_id="missing",
                task_id="task-1", disposition="unknown", worker_reason="",
                qa_status="not_assessed", qa_run_id=None, evidence_refs=(),
                created_at=datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc),
            )
            with self.assertRaises(StorageError):
                store.save_consultation_outcome(outcome)
            store.close()

    def test_schema_v3_database_is_migrated_for_consultation_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA user_version = 3")
            connection.close()
            with SQLiteOrchestrationStore(path) as store:
                version = store._connection.execute("PRAGMA user_version").fetchone()[0]
                self.assertEqual(version, 8)
                self.assertEqual(store.list_consultations(), ())

    def test_memory_database_supported(self):
        with SQLiteOrchestrationStore(":memory:") as store:
            store.save_task(self.record())
            self.assertEqual(len(store.list_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
