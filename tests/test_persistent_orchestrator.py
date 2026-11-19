import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.orchestrator import (
    Orchestrator, TaskRecord, TaskState, WorkerResult, WorkerResultStatus, WorkerTask,
)
from bcs.qa import EvidenceStore, QAReportBuilder
from bcs.sandbox_executor import ExecutionResult
from bcs.storage import SQLiteOrchestrationStore


class PersistentOrchestratorTests(unittest.TestCase):
    def report(self):
        builder = QAReportBuilder(
            task_id="parent", qa_run_id="persistent-run",
            evidence_store=EvidenceStore(),
            started_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
        )
        builder.add_execution(
            check_id="ui", name="Frontend UI tests", command=("npm", "test"),
            result=ExecutionResult("failed", "Assertion failed", exit_code=1),
        )
        builder.add_execution(
            check_id="blocked", name="Protected deployment check",
            command=("deploy",), result=ExecutionResult("blocked", "Approval needed"),
        )
        return builder.build()

    def test_ingested_tasks_and_escalations_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            first_store = SQLiteOrchestrationStore(path)
            first = Orchestrator(store=first_store)
            task_ids = first.ingest_qa_report(self.report())
            first_store.close()

            second_store = SQLiteOrchestrationStore(path)
            second = Orchestrator(store=second_store)
            self.assertEqual(tuple(item.task.task_id for item in second.snapshot().tasks), task_ids)
            self.assertEqual(second.snapshot().tasks[0].state, TaskState.QUEUED)
            self.assertEqual(len(second.snapshot().escalations), 1)
            second_store.close()

    def test_dispatch_result_survives_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            store = SQLiteOrchestrationStore(path)
            orchestrator = Orchestrator(store=store)
            task_id = orchestrator.ingest_qa_report(self.report())[0]
            orchestrator.register_worker(
                worker_id="worker-ui", role="frontend-developer",
                handler=lambda _: WorkerResult(
                    WorkerResultStatus.COMPLETED, "Repair completed", ("src/ui.tsx",)
                ),
            )
            outcome = orchestrator.dispatch(task_id)
            self.assertEqual(outcome.state, TaskState.COMPLETED)
            store.close()

            reopened_store = SQLiteOrchestrationStore(path)
            reopened = Orchestrator(store=reopened_store)
            restored = reopened.get_task(task_id)
            self.assertEqual(restored.state, TaskState.COMPLETED)
            self.assertEqual(restored.result.summary, "Repair completed")
            self.assertEqual(restored.result.changed_files, ("src/ui.tsx",))
            reopened_store.close()

    def test_interrupted_running_task_is_failed_closed_on_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            store = SQLiteOrchestrationStore(path)
            now = datetime.now(timezone.utc)
            task = WorkerTask("interrupted", "parent", "builder", "Work", (), ())
            store.save_task(TaskRecord(
                task, TaskState.RUNNING, "worker-1", None, now, now
            ))
            recovered = Orchestrator(store=store).get_task("interrupted")
            self.assertEqual(recovered.state, TaskState.FAILED)
            self.assertIn("outcome is unknown", recovered.result.summary)
            store.close()

    def test_worker_handlers_are_not_restored_from_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            store = SQLiteOrchestrationStore(path)
            first = Orchestrator(store=store)
            first.ingest_qa_report(self.report())
            store.close()
            reopened_store = SQLiteOrchestrationStore(path)
            reopened = Orchestrator(store=reopened_store)
            task_id = reopened.snapshot().tasks[0].task.task_id
            self.assertIn("No registered worker", reopened.dispatch(task_id).reason)
            reopened_store.close()


if __name__ == "__main__":
    unittest.main()
