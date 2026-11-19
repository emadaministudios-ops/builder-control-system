import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.approvals import ActorType, ApprovalStatus
from bcs.orchestrator import TaskState
from bcs.policy import ToolRequest
from bcs.qa import EvidenceStore, QAReportBuilder
from bcs.sandbox_executor import ExecutionResult
from bcs.runtime import create_runtime


class RuntimeCompositionTests(unittest.TestCase):
    def report(self):
        builder = QAReportBuilder(
            task_id="runtime-parent", qa_run_id="runtime-run",
            evidence_store=EvidenceStore(),
            started_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
        )
        builder.add_execution(
            check_id="api", name="Backend API tests", command=("pytest",),
            result=ExecutionResult("failed", "Test failure", exit_code=1),
        )
        return builder.build()

    def request(self):
        return ToolRequest(
            request_id="runtime-approval", task_id="runtime-task",
            worker_id="runtime-worker", workspace_id="workspace",
            capability="workspace.edit", arguments={"path": "src/app.py"},
        )

    def test_runtime_uses_one_store_and_restores_services_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runtime.sqlite3"
            with create_runtime(path) as runtime:
                self.assertIs(runtime.orchestrator._store, runtime.store)
                self.assertIs(runtime.approvals._store, runtime.store)
                task_ids = runtime.orchestrator.ingest_qa_report(self.report())
                runtime.approvals.submit(self.request())
                runtime.approvals.decide(
                    "runtime-approval", actor_id="human",
                    actor_type=ActorType.HUMAN, approved=True,
                )
                self.assertEqual(len(task_ids), 1)

            with create_runtime(path) as reopened:
                self.assertEqual(
                    reopened.orchestrator.get_task(task_ids[0]).state,
                    TaskState.QUEUED,
                )
                restored = reopened.approvals.get("runtime-approval")
                self.assertEqual(restored.status, ApprovalStatus.APPROVED)

    def test_environment_database_path_is_supported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "from-env.sqlite3"
            with patch.dict(os.environ, {"BCS_DATABASE_PATH": str(path)}):
                with create_runtime() as runtime:
                    runtime.approvals.submit(self.request())
            self.assertTrue(path.exists())

    def test_explicit_database_path_overrides_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            env_path = Path(folder) / "env.sqlite3"
            explicit_path = Path(folder) / "explicit.sqlite3"
            with patch.dict(os.environ, {"BCS_DATABASE_PATH": str(env_path)}):
                with create_runtime(explicit_path):
                    pass
            self.assertTrue(explicit_path.exists())
            self.assertFalse(env_path.exists())

    def test_invalid_approval_ttl_is_rejected(self):
        with self.assertRaises(ValueError):
            create_runtime(":memory:", approval_ttl_seconds=0)


if __name__ == "__main__":
    unittest.main()
