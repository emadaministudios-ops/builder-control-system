"""Integration tests for runtime-registered model-backed specialists."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.policy import OperatingMode
from bcs.providers import ProviderResponse
from bcs.runtime import create_runtime
from bcs.workspace import Workspace
from bcs.orchestrator import TaskState


class RuntimeWorkerIntegrationTests(unittest.TestCase):
    def test_task_dispatch_calls_model_worker_and_writes_to_isolated_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = create_runtime(database_path=":memory:")
            try:
                runtime.providers.complete = lambda messages, **kwargs: ProviderResponse(
                    "fake", "fake-model",
                    '{"summary":"Created requested module","edits":['
                    '{"path":"src/generated.py","content":"VALUE = 42\\n"}],'
                    '"blockers":[]}',
                )
                runtime.orchestrator.create_task(
                    task_id="task-runtime",
                    objective="Create a Python module exposing VALUE.",
                    acceptance_criteria=["The module defines VALUE as 42."],
                    role="backend-developer",
                )
                registered = runtime.register_model_worker(
                    worker_id="backend-worker",
                    role="backend-developer",
                    workspace_factory=lambda task: Workspace(
                        "workspace-" + task.task_id, root
                    ),
                    mode=OperatingMode.AUTONOMOUS,
                )
                self.assertEqual(registered.worker_id, "backend-worker")
                outcome = runtime.orchestrator.dispatch("task-runtime")
                self.assertEqual(outcome.state, TaskState.COMPLETED)
                self.assertEqual(outcome.result.changed_files, ("src/generated.py",))
                self.assertEqual((root / "src/generated.py").read_text(), "VALUE = 42\n")
            finally:
                runtime.close()

    def test_supervised_runtime_worker_creates_approval_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = create_runtime(database_path=":memory:")
            try:
                runtime.providers.complete = lambda messages, **kwargs: ProviderResponse(
                    "fake", "fake-model",
                    '{"summary":"Proposed module","edits":['
                    '{"path":"module.py","content":"VALUE = 1"}],"blockers":[]}',
                )
                runtime.orchestrator.create_task(
                    task_id="task-supervised", objective="Create a module.",
                    acceptance_criteria=["A module exists."],
                    role="builder",
                )
                runtime.register_model_worker(
                    worker_id="builder-worker", role="builder",
                    workspace_factory=lambda task: Workspace(
                        "workspace-" + task.task_id, root
                    ),
                    mode=OperatingMode.SUPERVISED,
                )
                outcome = runtime.orchestrator.dispatch("task-supervised")
                self.assertEqual(outcome.state, TaskState.BLOCKED)
                self.assertFalse((root / "module.py").exists())
                self.assertEqual(len(runtime.store.list_approvals()), 1)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
