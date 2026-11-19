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
            bad = QAEscalation("", "parent", "run", "check", "bad", ())
            with self.assertRaises(sqlite3.IntegrityError):
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

    def test_memory_database_supported(self):
        with SQLiteOrchestrationStore(":memory:") as store:
            store.save_task(self.record())
            self.assertEqual(len(store.list_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
