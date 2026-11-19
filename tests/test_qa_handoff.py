"""Tests for deterministic QA-to-repair handoffs."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.qa import EvidenceStore, QAReportBuilder, QACheckStatus
from bcs.sandbox_executor import ExecutionResult
from bcs.qa_handoff import create_qa_handoff


class QAHandoffTests(unittest.TestCase):
    def setUp(self):
        self.store = EvidenceStore()
        self.builder = QAReportBuilder(
            task_id="task-parent", qa_run_id="qa-run-7", evidence_store=self.store,
            started_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
        )

    def test_failed_frontend_check_creates_repair_task_with_evidence(self):
        check = self.builder.add_execution(
            check_id="ui-tests", name="Frontend UI tests",
            command=("npm", "run", "test"),
            result=ExecutionResult("failed", "Button interaction assertion failed.",
                                   exit_code=1, stderr="expected click handler"),
        )
        handoff = create_qa_handoff(self.builder.build())
        self.assertEqual(len(handoff.repair_tasks), 1)
        task = handoff.repair_tasks[0]
        self.assertEqual(task.assigned_role, "frontend-developer")
        self.assertEqual(task.parent_task_id, "task-parent")
        self.assertEqual(task.source_check_id, "ui-tests")
        self.assertEqual(task.evidence_refs, (check.evidence_ref,))
        self.assertEqual(task.original_command, ("npm", "run", "test"))
        self.assertIn("fresh QA run", " ".join(task.acceptance_criteria))

    def test_blocked_and_error_checks_escalate_not_repair(self):
        self.builder.add_execution(
            check_id="blocked", name="Protected test",
            command=("pytest",),
            result=ExecutionResult("blocked", "Human approval required."),
        )
        self.builder.add_execution(
            check_id="docker", name="Docker startup",
            command=("pytest",),
            result=ExecutionResult("error", "Docker daemon unavailable."),
        )
        handoff = create_qa_handoff(self.builder.build())
        self.assertEqual(handoff.repair_tasks, ())
        self.assertEqual(len(handoff.escalations), 2)
        self.assertIn("approval", handoff.escalations[0].reason.lower())

    def test_passed_check_creates_no_handoff(self):
        self.builder.add_execution(
            check_id="unit", name="Unit tests", command=("pytest",),
            result=ExecutionResult("completed", "Passed", exit_code=0),
        )
        handoff = create_qa_handoff(self.builder.build())
        self.assertEqual(handoff.repair_tasks, ())
        self.assertEqual(handoff.escalations, ())

    def test_mixed_report_creates_only_failed_repair_tasks(self):
        self.builder.add_execution(
            check_id="api", name="Backend API tests", command=("pytest",),
            result=ExecutionResult("failed", "Endpoint returns 500.", exit_code=1),
        )
        self.builder.add_execution(
            check_id="lint", name="Lint", command=("npm", "run", "lint"),
            result=ExecutionResult("completed", "Clean", exit_code=0),
        )
        handoff = create_qa_handoff(self.builder.build())
        self.assertEqual(len(handoff.repair_tasks), 1)
        self.assertEqual(handoff.repair_tasks[0].assigned_role, "backend-developer")

    def test_invalid_report_is_rejected(self):
        with self.assertRaises(TypeError):
            create_qa_handoff({"task_id": "fake"})

    def test_unknown_failure_routes_to_general_builder(self):
        self.builder.add_execution(
            check_id="misc", name="General regression",
            command=("pytest",),
            result=ExecutionResult("failed", "Unexpected regression.", exit_code=1),
        )
        task = create_qa_handoff(self.builder.build()).repair_tasks[0]
        self.assertEqual(task.assigned_role, "builder")


if __name__ == "__main__":
    unittest.main()
