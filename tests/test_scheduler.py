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
