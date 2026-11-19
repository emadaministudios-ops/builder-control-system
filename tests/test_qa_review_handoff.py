"""Tests connecting QA supervisor output to feedback and repair handoff."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.feedback import FeedbackState
from bcs.qa_supervisor import (
    QAReview, ReviewFinding, ReviewStatus, build_qa_report,
)
from bcs.runtime import create_runtime
from bcs.qa import EvidenceStore


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
            self.assertIn("frontend modal state", repair.task.objective)
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
