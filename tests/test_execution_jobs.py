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
                store, scheduler=type("Scheduler", (), {"dispatch_plan": lambda self, task_ids: None})(), execution_handler=workflow,
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


    def test_typed_qa_job_metadata_survives_claim_and_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOrchestrationStore(Path(folder) / "qa-jobs.sqlite3")
            store.enqueue_execution_job(
                job_id="integration-qa-job", task_ids=("integration-abc",),
                requested_by="owner", now=datetime.now(timezone.utc),
                job_type="integration_qa",
                metadata={
                    "integration_id": "integration-abc",
                    "profile_id": "python-unit",
                    "commit_sha": "a" * 40,
                },
            )
            seen = []
            def execute(job):
                seen.append((job["job_type"], job["metadata"]["commit_sha"]))
                return [{
                    "task_id": "integration-abc", "state": "completed",
                    "qa": {
                        "status": "passed", "commit_sha": "a" * 40,
                        "executed_checks": 2, "evidence_refs": ["ev_test"],
                    },
                }]
            service = ExecutionJobService(
                store,
                scheduler=type("Scheduler", (), {"dispatch_plan": lambda self, ids: None})(),
                execution_handler=execute, worker_id="qa-runner",
            )
            try:
                self.assertEqual(service.run_once(), "integration-qa-job")
                job = store.get_execution_job("integration-qa-job")
                self.assertEqual(job["state"], "completed")
                self.assertEqual(job["job_type"], "integration_qa")
                self.assertEqual(job["metadata"]["profile_id"], "python-unit")
                self.assertEqual(seen, [("integration_qa", "a" * 40)])
                self.assertEqual(job["outcomes"][0]["qa"]["evidence_refs"], ["ev_test"])
            finally:
                store.close()

    def test_qa_job_with_not_assessed_result_is_not_completed(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOrchestrationStore(Path(folder) / "qa-jobs.sqlite3")
            store.enqueue_execution_job(
                job_id="not-assessed-job", task_ids=("integration-abc",),
                requested_by="owner", now=datetime.now(timezone.utc),
                job_type="integration_qa", metadata={"integration_id": "integration-abc"},
            )
            service = ExecutionJobService(
                store,
                scheduler=type("Scheduler", (), {"dispatch_plan": lambda self, ids: None})(),
                execution_handler=lambda job: [{
                    "task_id": "integration-abc", "state": "completed",
                    "qa": {"status": "not_assessed"},
                }],
                worker_id="qa-runner",
            )
            try:
                service.run_once()
                self.assertEqual(store.get_execution_job("not-assessed-job")["state"], "failed")
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
                store, scheduler=type("Scheduler", (), {"dispatch_plan": lambda self, task_ids: None})(),
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
