"""Tests for validated top-level task creation."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.orchestrator import Orchestrator, TaskState
from bcs.storage import SQLiteOrchestrationStore


class TaskCreationTests(unittest.TestCase):
    def test_creates_queued_root_task_with_original_objective(self):
        orchestrator = Orchestrator()
        record = orchestrator.create_task(
            task_id="root-1",
            objective="Build a custom project and preserve the requested behavior.",
            acceptance_criteria=["Registration works", "QA evidence is recorded"],
        )
        self.assertEqual(record.state, TaskState.QUEUED)
        self.assertEqual(record.task.parent_task_id, "")
        self.assertIn("preserve the requested behavior", record.task.objective)
        self.assertEqual(len(record.task.acceptance_criteria), 2)

    def test_duplicate_task_id_is_rejected(self):
        orchestrator = Orchestrator()
        orchestrator.create_task(task_id="same", objective="First", acceptance_criteria=[])
        with self.assertRaises(ValueError):
            orchestrator.create_task(task_id="same", objective="Second", acceptance_criteria=[])

    def test_invalid_objectives_and_criteria_are_rejected(self):
        orchestrator = Orchestrator()
        for objective, criteria in (("", []), ("Valid", "not-a-list"), ("Valid", [" "])):
            with self.subTest(objective=objective, criteria=criteria):
                with self.assertRaises(ValueError):
                    orchestrator.create_task(
                        task_id="task-" + str(len(str(criteria))) + str(len(objective)),
                        objective=objective,
                        acceptance_criteria=criteria,
                    )

    def test_created_task_is_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SQLiteOrchestrationStore(Path(folder) / "bcs.sqlite3")
            orchestrator = Orchestrator(store=store)
            orchestrator.create_task(
                task_id="durable-root", objective="Persist this task",
                acceptance_criteria=["Can be restored"],
            )
            restored = store.get_task("durable-root")
            self.assertEqual(restored.state, TaskState.QUEUED)
            self.assertEqual(restored.task.objective, "Persist this task")
            store.close()


if __name__ == "__main__":
    unittest.main()
