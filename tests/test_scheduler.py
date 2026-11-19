"""Tests for bounded parallel BCS task scheduling."""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.orchestrator import Orchestrator, WorkerResult, WorkerResultStatus, TaskState
from bcs.scheduler import TaskScheduler


class TaskSchedulerTests(unittest.TestCase):
    def test_dispatches_independent_tasks_in_parallel_within_limit(self):
        orchestrator = Orchestrator()
        for index in range(6):
            orchestrator.create_task(
                task_id=f"task-{index}", objective=f"Work item {index}",
                acceptance_criteria=["Complete assigned work."],
            )
        lock = threading.Lock()
        active = 0
        peak = 0

        def worker(task):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return WorkerResult(WorkerResultStatus.COMPLETED, task.objective)

        orchestrator.register_worker(
            worker_id="builder", role="builder", handler=worker
        )
        scheduler = TaskScheduler(orchestrator, max_concurrency=2)
        result = scheduler.dispatch_independent([f"task-{i}" for i in range(6)])
        self.assertEqual(result.requested, 6)
        self.assertEqual(len(result.outcomes), 6)
        self.assertTrue(all(item.state == TaskState.COMPLETED for item in result.outcomes))
        self.assertEqual(peak, 2)

    def test_dispatch_plan_runs_dependency_waves_in_order(self):
        orchestrator = Orchestrator()
        orchestrator.create_task(
            task_id="design", objective="Design API", role="architect",
            acceptance_criteria=["API contract is ready."],
        )
        orchestrator.create_task(
            task_id="frontend", objective="Build frontend", role="frontend-developer",
            acceptance_criteria=["Frontend works."],
            dependency_ids=["design"],
        )
        orchestrator.create_task(
            task_id="backend", objective="Build backend", role="backend-developer",
            acceptance_criteria=["Backend works."],
            dependency_ids=["design"],
        )
        orchestrator.create_task(
            task_id="integration", objective="Integrate app", role="integration-engineer",
            acceptance_criteria=["Integration works."],
            dependency_ids=["frontend", "backend"],
        )
        finished = []
        orchestrator.register_worker(
            worker_id="architect", role="architect",
            handler=lambda task: (
                finished.append(task.task_id)
                or WorkerResult(WorkerResultStatus.COMPLETED, "designed")
            ),
        )
        orchestrator.register_worker(
            worker_id="builder", role="frontend-developer",
            handler=lambda task: (
                finished.append(task.task_id)
                or WorkerResult(WorkerResultStatus.COMPLETED, "frontend")
            ),
        )
        orchestrator.register_worker(
            worker_id="backend", role="backend-developer",
            handler=lambda task: (
                finished.append(task.task_id)
                or WorkerResult(WorkerResultStatus.COMPLETED, "backend")
            ),
        )
        orchestrator.register_worker(
            worker_id="integrator", role="integration-engineer",
            handler=lambda task: (
                finished.append(task.task_id)
                or WorkerResult(WorkerResultStatus.COMPLETED, "integrated")
            ),
        )
        scheduler = TaskScheduler(orchestrator, max_concurrency=2)
        result = scheduler.dispatch_plan(["design", "frontend", "backend", "integration"])
        self.assertTrue(all(item.state == TaskState.COMPLETED for item in result.outcomes))
        self.assertLess(finished.index("design"), finished.index("frontend"))
        self.assertLess(finished.index("design"), finished.index("backend"))
        self.assertLess(finished.index("frontend"), finished.index("integration"))
        self.assertLess(finished.index("backend"), finished.index("integration"))

    def test_dispatch_plan_leaves_dependants_queued_after_failed_prerequisite(self):
        orchestrator = Orchestrator()
        orchestrator.create_task(
            task_id="design", objective="Design", role="architect",
            acceptance_criteria=["Design is ready."],
        )
        orchestrator.create_task(
            task_id="build", objective="Build", role="backend-developer",
            acceptance_criteria=["Build works."],
            dependency_ids=["design"],
        )
        orchestrator.register_worker(
            worker_id="architect", role="architect",
            handler=lambda task: WorkerResult(WorkerResultStatus.ERROR, "failed"),
        )
        orchestrator.register_worker(
            worker_id="builder", role="builder",
            handler=lambda task: WorkerResult(WorkerResultStatus.COMPLETED, "built"),
        )
        result = TaskScheduler(orchestrator).dispatch_plan(["design", "build"])
        self.assertEqual(result.outcomes[0].state, TaskState.FAILED)
        self.assertEqual(result.outcomes[1].state, TaskState.QUEUED)
        self.assertIn("prerequisite", result.outcomes[1].reason)

    def test_empty_batch_is_a_valid_noop(self):
        scheduler = TaskScheduler(Orchestrator())
        result = scheduler.dispatch_independent([])
        self.assertEqual(result.outcomes, ())
        self.assertEqual(result.requested, 0)

    def test_duplicate_ids_are_rejected(self):
        scheduler = TaskScheduler(Orchestrator())
        with self.assertRaises(ValueError):
            scheduler.dispatch_independent(["same", "same"])

    def test_batch_queue_limit_is_enforced(self):
        scheduler = TaskScheduler(Orchestrator(), max_batch_size=2)
        with self.assertRaises(ValueError):
            scheduler.dispatch_independent(["a", "b", "c"])

    def test_invalid_concurrency_is_rejected(self):
        with self.assertRaises(ValueError):
            TaskScheduler(Orchestrator(), max_concurrency=0)


if __name__ == "__main__":
    unittest.main()
