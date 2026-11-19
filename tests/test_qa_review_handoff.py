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
from bcs.orchestrator import WorkerResult, WorkerResultStatus, WorkerTask
from bcs.providers import ProviderResponse
from bcs.workspace import Workspace


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

    def test_blocked_review_cannot_be_converted_to_success(self):
        review = QAReview(
            task_id="task-blocked", status=ReviewStatus.BLOCKED,
            requirements=(), findings=(), summary="Could not inspect files.",
            recommended_specialist=None,
        )
        with self.assertRaises(ValueError):
            build_qa_report(review, EvidenceStore())


if __name__ == "__main__":
    unittest.main()
