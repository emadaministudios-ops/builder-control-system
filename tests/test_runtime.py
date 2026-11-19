import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.approvals import ActorType, ApprovalStatus
from bcs.orchestrator import TaskState, WorkerTask
from bcs.policy import ToolRequest
from bcs.qa import EvidenceStore, QAReportBuilder
from bcs.sandbox_executor import ExecutionResult
from bcs.qa_runner import QACheckSpec
from bcs.workspace import Workspace
from bcs.policy import OperatingMode
from bcs.runtime import create_runtime
from bcs.consultation import ConsultationRecord
from bcs.providers import ProviderConfig, ProviderError


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

    def test_runtime_registers_explicit_local_and_cloud_providers(self):
        configs = (
            ProviderConfig("ollama", "http://127.0.0.1:11434/v1", "llama3.2"),
            ProviderConfig(
                "cloud", "https://api.example.com/v1", "cloud-model",
                api_key_env="BCS_TEST_CLOUD_KEY",
            ),
        )
        with tempfile.TemporaryDirectory() as folder:
            with create_runtime(
                Path(folder) / "runtime.sqlite3",
                provider_configs=configs,
                default_provider_id="ollama",
            ) as runtime:
                self.assertEqual(runtime.providers.provider_ids(), ("cloud", "ollama"))
                self.assertEqual(runtime.providers.default_provider_id, "ollama")

    def test_runtime_rejects_unconfigured_default_provider(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ValueError):
                create_runtime(
                    Path(folder) / "runtime.sqlite3",
                    provider_configs=(),
                    default_provider_id="cloud",
                )

    def test_runtime_plans_and_persists_dependency_aware_tasks(self):
        class FakePlannerProvider:
            def complete(self, messages, **kwargs):
                from bcs.providers import ProviderResponse
                return ProviderResponse(
                    "fake", "fake-model",
                    '{"steps":['
                    '{"step_id":"design","role":"architect","objective":"Design the API.",'
                    '"acceptance_criteria":["API contract is ready."],"depends_on":[]},'
                    '{"step_id":"build","role":"backend-developer","objective":"Build the API.",'
                    '"acceptance_criteria":["API endpoint works."],"depends_on":["design"]}]}'
                )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "planned.sqlite3"
            runtime = create_runtime(path)
            runtime.planner.providers = FakePlannerProvider()
            try:
                plan, records = runtime.plan_and_create_tasks("Build an API.")
                self.assertEqual(plan.execution_order, ("design", "build"))
                self.assertEqual(len(records), 2)
                self.assertEqual(records[1].task.dependency_ids, (records[0].task.task_id,))
            finally:
                runtime.close()
            with create_runtime(path) as reopened:
                restored = reopened.orchestrator.get_task(records[1].task.task_id)
                self.assertEqual(restored.task.dependency_ids, (records[0].task.task_id,))

    def test_runtime_wires_sandboxed_executable_qa_and_evidence(self):
        captured = {}
        class FakeExecutor:
            def __init__(self, **kwargs):
                captured.update(kwargs)
            def execute(self, request_id, argv, now=None):
                return ExecutionResult(
                    "completed", "Container command completed.",
                    exit_code=0, stdout="3 tests passed",
                )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            task = WorkerTask(
                task_id="exec-qa", parent_task_id="", role="builder",
                objective="Run the unit tests.",
                acceptance_criteria=("All unit tests pass.",),
                evidence_refs=(),
            )
            with create_runtime(Path(folder) / "runtime.sqlite3") as runtime:
                with patch("bcs.runtime.DockerSandboxExecutor", FakeExecutor):
                    report = runtime.run_executable_qa(
                        task,
                        (QACheckSpec("unit", "Unit tests", "unit-request", ("python", "-m", "unittest")),),
                        workspace_factory=lambda item: Workspace("workspace-" + item.task_id, root),
                        docker_executable="docker",
                        image="python:3.12-slim",
                        allowed_images=frozenset({"python:3.12-slim"}),
                        mode=OperatingMode.AUTONOMOUS,
                    )
                self.assertEqual(report.task_id, "exec-qa")
                self.assertEqual(report.status.value, "passed")
                self.assertTrue(report.checks[0].executed)
                self.assertIsNotNone(runtime.evidence_store.get(report.checks[0].evidence_ref))
                self.assertEqual(captured["context"].task_id, "exec-qa")
                self.assertIn("tests.run", captured["context"].task_capabilities)

    def test_runtime_records_worker_disposition_separately_from_qa_result(self):
        with create_runtime(":memory:") as runtime:
            consultation = ConsultationRecord(
                consultation_id="consult-runtime", task_id="task-runtime-feedback",
                requesting_role="backend-developer", should_consult=True,
                specialist_role="architect", question="Where should this live?",
                reason="Boundary is unclear.", advice="Use an adapter.",
                risks=(), next_steps=("Define interface.",), confidence="high",
                created_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
            )
            runtime.store.save_consultation(consultation)
            outcome = runtime.record_consultation_outcome(
                consultation_id="consult-runtime", disposition="followed",
                worker_reason="Implemented the adapter boundary.",
                qa_status="passed", qa_run_id="qa-runtime-1",
                evidence_refs=("evidence:unit-tests",),
            )
            self.assertEqual(outcome.disposition, "followed")
            self.assertEqual(outcome.qa_status, "passed")
            self.assertEqual(runtime.consultation_outcome_history("task-runtime-feedback"), (outcome,))
            with self.assertRaises(KeyError):
                runtime.record_consultation_outcome(
                    consultation_id="missing-consultation", disposition="unknown"
                )

    def test_invalid_approval_ttl_is_rejected(self):
        with self.assertRaises(ValueError):
            create_runtime(":memory:", approval_ttl_seconds=0)


if __name__ == "__main__":
    unittest.main()
