from dataclasses import replace
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
from bcs.qa_supervisor import QAReview, ReviewStatus
from bcs.orchestrator import WorkerResult, WorkerResultStatus
from bcs.workspace import Workspace
from bcs.policy import OperatingMode
from bcs.runtime import create_runtime
from bcs.consultation import ConsultationRecord, ConsultationRoute, SpecialistAdvice
from bcs.consultation_feedback import ConsultationOutcome
from bcs.providers import ProviderConfig, ProviderError, ProviderResponse


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


    def test_dependency_plan_runs_qa_cycles_and_reports_source_only_status(self):
        class FakeSupervisor:
            def __init__(self):
                self.calls = 0
            def review(self, task, result):
                self.calls += 1
                return QAReview(
                    task_id=task.task_id,
                    status=ReviewStatus.READY_FOR_TESTS,
                    requirements=(("R1", "The changed module exists."),),
                    findings=(),
                    summary="No source-level gaps identified.",
                    recommended_specialist=None,
                )

        with tempfile.TemporaryDirectory() as folder:
            runtime = create_runtime(Path(folder) / "workflow.sqlite3")
            try:
                runtime.orchestrator.register_worker(
                    worker_id="workflow-builder", role="builder",
                    handler=lambda task: WorkerResult(
                        WorkerResultStatus.COMPLETED, "Created module.",
                        changed_files=("module.py",),
                    ),
                )
                task = runtime.orchestrator.create_task(
                    task_id="workflow-task", objective="Create module.",
                    acceptance_criteria=("Module exists.",),
                )
                supervisor = FakeSupervisor()
                outcomes = runtime.execute_plan_with_qa(
                    [task.task.task_id], supervisor, max_repair_rounds=2,
                )
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0]["state"], "completed")
                self.assertEqual(outcomes[0]["qa"]["status"], "source_review_complete")
                self.assertEqual(outcomes[0]["qa"]["reviews"], 2)
                self.assertEqual(supervisor.calls, 2)
                self.assertIn("No executable tests", outcomes[0]["qa"]["reason"])
            finally:
                runtime.close()

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

    def test_brain_learning_context_is_scoped_to_same_project_lineage(self):
        with create_runtime(":memory:") as runtime:
            root = runtime.orchestrator.create_task(
                task_id="learning-root", objective="Build a project",
                acceptance_criteria=["Project works."], role="backend-developer",
            )
            consultation = ConsultationRecord(
                consultation_id="learning-consult", task_id="learning-root",
                requesting_role="backend-developer", should_consult=True,
                specialist_role="architect", question="How should access be structured?",
                reason="Architecture risk.", advice="Use a global singleton client.",
                risks=(), next_steps=("Add singleton.",), confidence="high",
                created_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
            )
            runtime.store.save_consultation(consultation)
            runtime.store.save_consultation(ConsultationRecord(
                consultation_id="learning-skip", task_id="learning-root",
                requesting_role="backend-developer", should_consult=False,
                specialist_role=None, question="", reason="Routine implementation; no design gap.",
                advice=None, risks=(), next_steps=(), confidence=None,
                created_at=datetime(2026, 11, 19, 10, 30, tzinfo=timezone.utc),
            ))
            runtime.record_consultation_outcome(
                consultation_id="learning-consult", disposition="followed",
                worker_reason="Implemented the singleton client.",
                qa_status="failed", qa_run_id="learning-qa-fail",
                evidence_refs=("evidence:concurrency",),
            )
            child = WorkerTask(
                task_id="learning-child", parent_task_id="learning-root",
                role="backend-developer", objective="Repair the client.",
                acceptance_criteria=("No shared mutable state.",),
                evidence_refs=(), workspace_group_id=root.task.workspace_group_id,
            )
            context = runtime.consultation_learning_context(child)
            self.assertIn("QA=failed", context)
            self.assertIn("Use a global singleton client.", context)
            self.assertIn("Consultation=skipped", context)
            self.assertIn("Routine implementation; no design gap.", context)
            isolated = replace(child, workspace_group_id="another-project")
            self.assertEqual(runtime.consultation_learning_context(isolated), "")

    def test_repair_worker_receives_only_failed_advice_from_same_project_lineage(self):
        with create_runtime(":memory:") as runtime:
            root = runtime.orchestrator.create_task(
                task_id="project-root", objective="Build project",
                acceptance_criteria=["Project works."], role="backend-developer",
            )
            consultation = ConsultationRecord(
                consultation_id="consult-failed-advice", task_id="project-root",
                requesting_role="backend-developer", should_consult=True,
                specialist_role="architect", question="How should modules connect?",
                reason="A design boundary is unclear.",
                advice="Put all data access directly in the UI layer.",
                risks=(), next_steps=("Move access into UI.",), confidence="high",
                created_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
            )
            runtime.store.save_consultation(consultation)
            runtime.store.save_consultation_outcome(ConsultationOutcome(
                outcome_id="outcome-failed-advice",
                consultation_id=consultation.consultation_id, task_id="project-root",
                disposition="followed", worker_reason="The UI now owns all data access.",
                qa_status="failed", qa_run_id="qa-failed",
                evidence_refs=("evidence:integration-failure",),
                created_at=datetime(2026, 11, 19, 11, 0, tzinfo=timezone.utc),
            ))
            repair = WorkerTask(
                task_id="repair-task", parent_task_id="project-root",
                role="backend-developer", objective="Repair integration failure.",
                acceptance_criteria=("Data access is correctly layered.",),
                evidence_refs=(), workspace_group_id=root.task.workspace_group_id,
            )
            caution = runtime.consultation_cautions_for_task(repair)
            self.assertIn("Previously attempted specialist advice", caution)
            self.assertIn("failed", caution)
            self.assertIn("evidence:integration-failure", caution)

            unrelated = WorkerTask(
                task_id="other-project-repair", parent_task_id="project-root",
                role="backend-developer", objective="Unrelated repair.",
                acceptance_criteria=("No cross-project context.",),
                evidence_refs=(), workspace_group_id="different-project",
            )
            self.assertEqual(runtime.consultation_cautions_for_task(unrelated), "")

    def test_registered_worker_automatically_records_consultation_adoption(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            with create_runtime(":memory:") as runtime:
                task = runtime.orchestrator.create_task(
                    task_id="auto-worker-task", objective="Implement an API adapter.",
                    acceptance_criteria=["Adapter is implemented."],
                    role="backend-developer",
                )
                class FakeProviders:
                    def complete(self, messages, **kwargs):
                        return ProviderResponse(
                            "fake", "fake-model",
                            '{"summary":"Implemented adapter","edits":['
                            '{"path":"src/adapter.py","content":"class Adapter: pass\\n"}],'
                            '"blockers":[],"consultation_disposition":"followed",'
                            '"consultation_reason":"Used the adapter boundary recommendation."}',
                        )
                runtime.providers = FakeProviders()

                def fake_consult(task_id, **kwargs):
                    record = ConsultationRecord(
                        consultation_id="auto-consultation", task_id=task_id,
                        requesting_role="backend-developer", should_consult=True,
                        specialist_role="architect", question="Where should adapter live?",
                        reason="Boundary needs design input.", advice="Keep adapter in backend.",
                        risks=(), next_steps=("Add backend adapter.",), confidence="high",
                        created_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
                    )
                    runtime.store.save_consultation(record)
                    return (
                        ConsultationRoute(True, "architect", "Where should adapter live?", "Boundary risk."),
                        SpecialistAdvice(
                            "backend-developer", "architect", "Where should adapter live?",
                            "Keep adapter in backend.", (), ("Add backend adapter.",), "high",
                        ),
                    )
                runtime.consult_for_task = fake_consult
                runtime.register_model_worker(
                    worker_id="backend-worker", role="backend-developer",
                    workspace_factory=lambda item: Workspace("workspace-" + item.task_id, root),
                )
                outcome = runtime.orchestrator.dispatch(task.task.task_id)
                self.assertEqual(outcome.state, TaskState.COMPLETED)
                history = runtime.consultation_outcome_history(task.task.task_id)
                self.assertEqual(len(history), 1)
                self.assertEqual(history[0].disposition, "followed")
                self.assertIn("adapter boundary recommendation", history[0].worker_reason)

    def test_qa_report_automatically_assesses_consultation_outcome(self):
        with create_runtime(":memory:") as runtime:
            consultation = ConsultationRecord(
                consultation_id="consult-auto-qa", task_id="runtime-parent",
                requesting_role="backend-developer", should_consult=True,
                specialist_role="architect", question="Which boundary?",
                reason="Design boundary is unclear.", advice="Use an adapter.",
                risks=(), next_steps=("Define interface.",), confidence="high",
                created_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
            )
            runtime.store.save_consultation(consultation)
            runtime.record_consultation_outcome(
                consultation_id="consult-auto-qa",
                disposition="followed",
                worker_reason="Implemented adapter boundary.",
            )
            report = self.report()
            runtime._record_qa_consultation_assessments(report)
            history = runtime.consultation_outcome_history("runtime-parent")
            self.assertEqual(history[-1].qa_status, "failed")
            self.assertEqual(history[-1].qa_run_id, "runtime-run")
            self.assertTrue(history[-1].evidence_refs)

    def test_static_only_positive_review_does_not_claim_test_pass(self):
        from bcs.qa import QAReport, QACheck, QACheckStatus
        with create_runtime(":memory:") as runtime:
            consultation = ConsultationRecord(
                consultation_id="consult-static-qa", task_id="static-parent",
                requesting_role="backend-developer", should_consult=True,
                specialist_role="architect", question="Which boundary?",
                reason="Design boundary is unclear.", advice="Use an adapter.",
                risks=(), next_steps=("Define interface.",), confidence="high",
                created_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
            )
            runtime.store.save_consultation(consultation)
            runtime.record_consultation_outcome(
                consultation_id="consult-static-qa", disposition="followed",
                worker_reason="Implemented adapter boundary.",
            )
            now = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
            report = QAReport(
                contract_version="1.0", task_id="static-parent", qa_run_id="static-run",
                status=QACheckStatus.PASSED,
                checks=(QACheck(
                    check_id="review", name="Static review",
                    status=QACheckStatus.PASSED, executed=False,
                    details="No source gaps found.",
                    non_execution_basis="Model-assisted review only.",
                ),),
                started_at=now, finished_at=now,
            )
            runtime._record_qa_consultation_assessments(report)
            latest = runtime.consultation_outcome_history("static-parent")[-1]
            self.assertEqual(latest.qa_status, "not_assessed")
            self.assertIsNone(latest.qa_run_id)

    def test_invalid_approval_ttl_is_rejected(self):
        with self.assertRaises(ValueError):
            create_runtime(":memory:", approval_ttl_seconds=0)


if __name__ == "__main__":
    unittest.main()
