"""Tests for durable background execution workflow handlers."""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.execution_jobs import ExecutionJobService
from bcs.storage import SQLiteOrchestrationStore


class ExecutionJobWorkflowTests(unittest.TestCase):
    def test_job_runner_persists_build_and_qa_handler_outcomes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOrchestrationStore(Path(folder) / "jobs.sqlite3")
            store.enqueue_execution_job(
                job_id="qa-job", task_ids=("task-one",),
                requested_by="owner", now=datetime.now(timezone.utc),
            )
            invoked = []
            def workflow(job):
                invoked.append(job["task_ids"])
                return [{
                    "task_id": "task-one",
                    "state": "completed",
                    "worker_id": "builder",
                    "reason": "",
                    "result": {"summary": "Built feature."},
                    "qa": {
                        "status": "source_review_complete",
                        "reviews": 2,
                        "repair_task_ids": [],
                        "reason": "No executable tests were configured.",
                    },
                }]
            service = ExecutionJobService(
                store, scheduler=object(), execution_handler=workflow,
                worker_id="workflow-runner",
            )
            try:
                self.assertEqual(service.run_once(), "qa-job")
                record = store.get_execution_job("qa-job")
                self.assertEqual(record["state"], "completed")
                self.assertEqual(record["outcomes"][0]["qa"]["reviews"], 2)
                self.assertEqual(invoked, [("task-one",)])
            finally:
                store.close()

    def test_job_runner_marks_qa_blocked_work_as_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOrchestrationStore(Path(folder) / "jobs.sqlite3")
            store.enqueue_execution_job(
                job_id="blocked-job", task_ids=("task-one",),
                requested_by="owner", now=datetime.now(timezone.utc),
            )
            service = ExecutionJobService(
                store, scheduler=object(),
                execution_handler=lambda job: [{
                    "task_id": "task-one", "state": "completed",
                    "qa": {"status": "blocked"},
                }],
                worker_id="workflow-runner",
            )
            try:
                service.run_once()
                self.assertEqual(store.get_execution_job("blocked-job")["state"], "partial")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
