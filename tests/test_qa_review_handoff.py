"""Tests connecting QA supervisor output to feedback and repair handoff."""
import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.feedback import FeedbackState
from bcs.qa_supervisor import (
    QAReview, ReviewFinding, ReviewStatus, build_qa_report,
)
from bcs.runtime import create_runtime
from bcs.qa import EvidenceStore
from bcs.orchestrator import WorkerResult, WorkerResultStatus, WorkerTask, TaskState
from bcs.providers import ProviderResponse
from bcs.workspace import Workspace
from bcs.qa_runner import QACheckSpec
from bcs.sandbox_executor import ExecutionResult
from bcs.policy import OperatingMode
from unittest.mock import patch


class QAReviewHandoffTests(unittest.TestCase):
    def review(self):
        return QAReview(
            task_id="task-review",
            status=ReviewStatus.REPAIR_REQUIRED,
            requirements=(("R1", "The UI modal closes after selection."),),
            findings=(
                ReviewFinding(
                    requirement_id="R1",
                    severity="high",
                    issue="Frontend modal state is not updated.",
                    expected="The modal closes after selection.",
                    observed="The modal remains visible.",
                    suggestion="Update frontend component state and add a regression test.",
                    file_path="src/modal.tsx",
                ),
            ),
            summary="A frontend UI gap remains.",
            recommended_specialist="frontend-developer",
        )

    def test_review_conversion_marks_checks_as_non_executed(self):
        report = build_qa_report(self.review(), EvidenceStore())
        self.assertEqual(report.status.value, "failed")
        self.assertFalse(report.checks[0].executed)
        self.assertIn("not an executed test", report.checks[0].non_execution_basis)

    def test_runtime_persists_feedback_and_creates_repair_task(self):
        runtime = create_runtime(database_path=":memory:")
        try:
            report, snapshot, repair_ids = runtime.apply_qa_review(self.review())
            self.assertEqual(snapshot.state, FeedbackState.REPAIR_REQUIRED)
            self.assertEqual(len(repair_ids), 1)
            repair = runtime.orchestrator.get_task(repair_ids[0])
            self.assertEqual(repair.task.role, "frontend-developer")
            self.assertIn("Frontend modal state", repair.task.objective)
            self.assertFalse(report.checks[0].executed)
        finally:
            runtime.close()

    def test_runtime_runs_qa_and_enqueues_repairs_without_manual_translation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "src").mkdir()
            (root / "src" / "modal.tsx").write_text("export const Modal = () => null;")
            runtime = create_runtime(database_path=":memory:")
            try:
                outputs = [
                    '{"requirements":[{"id":"R1","criterion":"Modal closes after selection."}]}',
                    '{"summary":"Review found no issue","findings":[],"recommended_specialist":null}',
                    '{"summary":"Final audit found a modal gap","findings":['
                    '{"requirement_id":"R1","severity":"high",'
                    '"issue":"Frontend modal remains open.",'
                    '"expected":"Modal closes after selection.",'
                    '"observed":"No close state is implemented.",'
                    '"suggestion":"Update modal state and add a regression test.",'
                    '"file_path":"src/modal.tsx"}],'
                    '"recommended_specialist":"frontend-developer"}',
                ]
                runtime.providers.complete = lambda messages, **kwargs: ProviderResponse(
                    "fake", "fake-model", outputs.pop(0)
                )
                task = WorkerTask(
                    task_id="runtime-qa", parent_task_id="", role="frontend-developer",
                    objective="Modal closes after selection.",
                    acceptance_criteria=("The modal closes after selection.",),
                    evidence_refs=(),
                )
                result = WorkerResult(
                    WorkerResultStatus.COMPLETED, "Implemented modal.",
                    changed_files=("src/modal.tsx",),
                )
                supervisor = runtime.create_qa_supervisor(
                    workspace_factory=lambda task: Workspace("qa-" + task.task_id, root)
                )
                review, report, snapshot, repair_ids = runtime.review_and_handoff(
                    task, result, supervisor
                )
                self.assertEqual(review.status, ReviewStatus.REPAIR_REQUIRED)
                self.assertEqual(snapshot.state, FeedbackState.REPAIR_REQUIRED)
                self.assertEqual(len(repair_ids), 1)
                self.assertEqual(runtime.orchestrator.get_task(repair_ids[0]).task.role, "frontend-developer")
                self.assertFalse(report.checks[0].executed)
            finally:
                runtime.close()

    def test_execute_with_qa_dispatches_repair_and_rechecks_original_task(self):
        class SequenceSupervisor:
            def __init__(self):
                self.calls = 0
            def review(self, task, result):
                self.calls += 1
                if self.calls == 1:
                    return QAReview(
                        task_id=task.task_id,
                        status=ReviewStatus.REPAIR_REQUIRED,
                        requirements=(("R1", "The modal closes after selection."),),
                        findings=(ReviewFinding(
                            requirement_id="R1", severity="high",
                            issue="Frontend modal remains open.",
                            expected="Modal closes after selection.",
                            observed="Modal stays open.",
                            suggestion="Fix modal state.", file_path="src/modal.tsx",
                        ),),
                        summary="Repair needed.",
                        recommended_specialist="frontend-developer",
                    )
                return QAReview(
                    task_id=task.task_id,
                    status=ReviewStatus.READY_FOR_TESTS,
                    requirements=(("R1", "The modal closes after selection."),),
                    findings=(), summary="No source-level gap identified.",
                    recommended_specialist=None,
                )

        runtime = create_runtime(database_path=":memory:")
        try:
            runtime.orchestrator.create_task(
                task_id="auto-parent", objective="Implement modal close behavior.",
                acceptance_criteria=["The modal closes after selection."],
                role="builder",
            )
            calls = []
            runtime.orchestrator.register_worker(
                worker_id="builder", role="builder",
                handler=lambda task: (
                    calls.append(("builder", task.task_id))
                    or WorkerResult(
                        WorkerResultStatus.COMPLETED, "Initial implementation.",
                        changed_files=("src/modal.tsx",),
                    )
                ),
            )
            runtime.orchestrator.register_worker(
                worker_id="frontend", role="frontend-developer",
                handler=lambda task: (
                    calls.append(("frontend", task.task_id))
                    or WorkerResult(
                        WorkerResultStatus.COMPLETED, "Fixed modal state.",
                        changed_files=("src/modal.tsx",),
                    )
                ),
            )
            outcome = runtime.execute_with_qa(
                "auto-parent", SequenceSupervisor(), max_repair_rounds=2
            )
            self.assertEqual(outcome.state, FeedbackState.VERIFIED)
            self.assertEqual(len(outcome.reviews), 2)
            self.assertEqual(len(outcome.repair_task_ids), 1)
            repair_record = runtime.orchestrator.get_task(outcome.repair_task_ids[0])
            parent_record = runtime.orchestrator.get_task("auto-parent")
            self.assertEqual(
                repair_record.task.workspace_group_id,
                parent_record.task.workspace_group_id,
            )
            self.assertEqual(calls[0][0], "builder")
            self.assertEqual(calls[1][0], "frontend")
            self.assertEqual(runtime.orchestrator.get_task("auto-parent").state, TaskState.COMPLETED)
            self.assertIn("No executable tests were configured", outcome.reason)
        finally:
            runtime.close()

    def test_execute_with_qa_escalates_when_fallback_repair_worker_is_blocked(self):
        class RepairSupervisor:
            def review(self, task, result):
                return QAReview(
                    task_id=task.task_id,
                    status=ReviewStatus.REPAIR_REQUIRED,
                    requirements=(("R1", "API works."),),
                    findings=(ReviewFinding(
                        requirement_id="R1", severity="high",
                        issue="Backend API endpoint is missing.",
                        expected="API exists.", observed="No endpoint.",
                        suggestion="Implement endpoint.", file_path="src/api.py",
                    ),),
                    summary="Backend repair needed.",
                    recommended_specialist="backend-developer",
                )

        runtime = create_runtime(database_path=":memory:")
        try:
            runtime.orchestrator.create_task(
                task_id="blocked-auto", objective="Build API",
                acceptance_criteria=["API works."], role="builder",
            )
            runtime.orchestrator.register_worker(
                worker_id="builder", role="builder",
                handler=lambda task: (
                    WorkerResult(
                        WorkerResultStatus.BLOCKED, "Repair needs additional context.",
                        blockers=("Missing API design decision.",),
                    )
                    if task.parent_task_id == "blocked-auto"
                    else WorkerResult(
                        WorkerResultStatus.COMPLETED, "Initial work.",
                        changed_files=("src/api.py",),
                    )
                ),
            )
            outcome = runtime.execute_with_qa(
                "blocked-auto", RepairSupervisor(), max_repair_rounds=1
            )
            self.assertEqual(outcome.state, FeedbackState.HUMAN_ATTENTION)
            self.assertIn("repair worker failed, was blocked", outcome.reason)
            self.assertEqual(runtime.orchestrator.get_task("blocked-auto").state, TaskState.COMPLETED)
        finally:
            runtime.close()

    def test_blocked_review_cannot_be_converted_to_success(self):
        review = QAReview(
            task_id="task-blocked", status=ReviewStatus.BLOCKED,
            requirements=(), findings=(), summary="Could not inspect files.",
            recommended_specialist=None,
        )
        with self.assertRaises(ValueError):
            build_qa_report(review, EvidenceStore())


    def test_execute_with_qa_runs_tests_repairs_failure_and_retests(self):
        class SequenceSupervisor:
            def review(self, task, result):
                return QAReview(
                    task_id=task.task_id,
                    status=ReviewStatus.READY_FOR_TESTS,
                    requirements=(("R1", "Unit tests pass."),),
                    findings=(), summary="No source-level gap identified.",
                    recommended_specialist=None,
                )

        class FakeExecutor:
            calls = 0
            def __init__(self, **kwargs):
                pass
            def execute(self, request_id, argv, now=None):
                self.__class__.calls += 1
                if self.__class__.calls == 1:
                    return ExecutionResult(
                        "failed", "Container command returned a non-zero exit code.",
                        exit_code=1, stdout="1 failed",
                    )
                return ExecutionResult(
                    "completed", "Container command completed successfully.",
                    exit_code=0, stdout="3 passed",
                )

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = create_runtime(database_path=":memory:")
            try:
                runtime.orchestrator.create_task(
                    task_id="tested-parent", objective="Build feature and pass tests.",
                    acceptance_criteria=["Unit tests pass."], role="builder",
                )
                def worker(task):
                    if task.parent_task_id:
                        return WorkerResult(
                            WorkerResultStatus.COMPLETED, "Repaired failing tests.",
                            changed_files=("src/feature.py",),
                        )
                    return WorkerResult(
                        WorkerResultStatus.COMPLETED, "Initial implementation.",
                        changed_files=("src/feature.py",),
                    )
                runtime.orchestrator.register_worker(
                    worker_id="builder", role="builder", handler=worker,
                )
                with patch("bcs.runtime.DockerSandboxExecutor", FakeExecutor):
                    outcome = runtime.execute_with_qa(
                        "tested-parent", SequenceSupervisor(), max_repair_rounds=2,
                        executable_checks=(QACheckSpec(
                            "unit", "Unit test suite", "unit-run",
                            ("python", "-m", "unittest"),
                        ),),
                        qa_workspace_factory=lambda task: Workspace(
                            "workspace-" + task.workspace_group_id, root,
                        ),
                        docker_executable="docker",
                        qa_image="python:3.12-slim",
                        allowed_qa_images=frozenset({"python:3.12-slim"}),
                        qa_mode=OperatingMode.AUTONOMOUS,
                    )
                self.assertEqual(FakeExecutor.calls, 2)
                self.assertEqual(outcome.state, FeedbackState.VERIFIED)
                self.assertEqual(len(outcome.repair_task_ids), 1)
                self.assertEqual(
                    [cycle.report_status.value for cycle in outcome.feedback.cycles],
                    ["failed", "passed"],
                )
                self.assertIn("Controller-defined sandbox tests were executed", outcome.reason)
            finally:
                runtime.close()


if __name__ == "__main__":
    unittest.main()
