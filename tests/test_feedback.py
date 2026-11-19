"""Tests for the evidence-driven QA feedback-cycle controller."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.feedback import FeedbackController, FeedbackState
from bcs.qa import EvidenceStore, QAReportBuilder
from bcs.sandbox_executor import ExecutionResult


class FeedbackControllerTests(unittest.TestCase):
    def report(self, run_id, result="completed"):
        builder = QAReportBuilder(
            task_id="project-task", qa_run_id=run_id,
            evidence_store=EvidenceStore(),
            started_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
        )
        builder.add_execution(
            check_id="acceptance", name="Acceptance tests",
            command=("pytest",),
            result=ExecutionResult(
                result, "Observed QA outcome",
                exit_code=0 if result == "completed" else (1 if result == "failed" else None),
            ),
        )
        return builder.build()

    def test_success_requires_two_distinct_verification_cycles(self):
        controller = FeedbackController()
        first = controller.record_qa_report(self.report("run-1"))
        self.assertEqual(first.state, FeedbackState.VERIFY_AGAIN)
        second = controller.record_qa_report(self.report("run-2"))
        self.assertEqual(second.state, FeedbackState.VERIFIED)
        self.assertEqual(len(second.cycles), 2)

    def test_failed_report_requests_repair_and_fresh_qa(self):
        controller = FeedbackController()
        result = controller.record_qa_report(self.report("run-1", "failed"))
        self.assertEqual(result.state, FeedbackState.REPAIR_REQUIRED)
        self.assertIn("fresh QA run", result.cycles[-1].instruction)

    def test_replayed_qa_run_is_idempotent(self):
        controller = FeedbackController()
        report = self.report("same-run")
        first = controller.record_qa_report(report)
        second = controller.record_qa_report(report)
        self.assertEqual(first, second)
        self.assertEqual(len(second.cycles), 1)

    def test_blocked_report_escalates_immediately(self):
        controller = FeedbackController()
        result = controller.record_qa_report(self.report("blocked-run", "blocked"))
        self.assertEqual(result.state, FeedbackState.HUMAN_ATTENTION)

    def test_repeated_failures_exhaust_budget_without_false_success(self):
        controller = FeedbackController(minimum_cycles=2, maximum_cycles=3)
        controller.record_qa_report(self.report("run-1", "failed"))
        controller.record_qa_report(self.report("run-2", "failed"))
        final = controller.record_qa_report(self.report("run-3", "failed"))
        self.assertEqual(final.state, FeedbackState.CYCLE_BUDGET_EXHAUSTED)
        self.assertEqual(final.cycles_remaining, 0)
        self.assertIn("do not mark", final.cycles[-1].instruction)

    def test_invalid_cycle_configuration_is_rejected(self):
        with self.assertRaises(ValueError):
            FeedbackController(minimum_cycles=3, maximum_cycles=2)


if __name__ == "__main__":
    unittest.main()
