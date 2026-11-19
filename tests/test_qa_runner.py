"""Tests for the integrated QA execution runner."""
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.qa import EvidenceStore, QACheckStatus
from bcs.qa_runner import QACheckSpec, QAExecutionRunner
from bcs.sandbox_executor import ExecutionResult


class QARunnerTests(unittest.TestCase):
    def setUp(self):
        self.executor = Mock()
        self.store = EvidenceStore()
        self.runner = QAExecutionRunner(self.executor, self.store)
        self.executor.execute.side_effect = [
            ExecutionResult("completed", "Passed", exit_code=0, stdout="all green"),
            ExecutionResult("failed", "Failed", exit_code=1, stderr="assertion error"),
        ]

    def test_runner_executes_each_check_and_builds_report(self):
        report = self.runner.run(
            task_id="task-1",
            qa_run_id="run-1",
            checks=(
                QACheckSpec("unit", "Unit tests", "req-unit", ("python", "-m", "unittest")),
                QACheckSpec("lint", "Lint", "req-lint", ("npm", "run", "lint")),
            ),
            now=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(self.executor.execute.call_count, 2)
        self.assertEqual(report.status, QACheckStatus.FAILED)
        self.assertEqual(report.qa_run_id, "run-1")
        self.assertEqual(report.checks[0].status, QACheckStatus.PASSED)
        self.assertEqual(report.checks[1].status, QACheckStatus.FAILED)
        self.assertIsNotNone(self.store.get(report.checks[0].evidence_ref))

    def test_blocked_check_is_reported_without_claiming_execution(self):
        self.executor.execute.side_effect = [ExecutionResult("blocked", "Approval needed.")]
        report = self.runner.run(
            task_id="task-1",
            checks=(QACheckSpec("unit", "Unit tests", "req-1", ("pytest",)),),
        )
        self.assertEqual(report.status, QACheckStatus.BLOCKED)
        self.assertFalse(report.checks[0].executed)
        self.assertIsNone(report.checks[0].evidence_ref)

    def test_duplicate_check_ids_are_rejected_before_execution(self):
        checks = (
            QACheckSpec("same", "First", "req-1", ("pytest",)),
            QACheckSpec("same", "Second", "req-2", ("pytest",)),
        )
        with self.assertRaises(ValueError):
            self.runner.run(task_id="task-1", checks=checks)
        self.executor.execute.assert_not_called()

    def test_duplicate_request_ids_are_rejected_before_execution(self):
        checks = (
            QACheckSpec("one", "First", "same-request", ("pytest",)),
            QACheckSpec("two", "Second", "same-request", ("pytest",)),
        )
        with self.assertRaises(ValueError):
            self.runner.run(task_id="task-1", checks=checks)
        self.executor.execute.assert_not_called()

    def test_empty_check_list_is_rejected(self):
        with self.assertRaises(ValueError):
            self.runner.run(task_id="task-1", checks=())
        self.executor.execute.assert_not_called()

    def test_invalid_argv_is_rejected_before_execution(self):
        checks = (QACheckSpec("bad", "Bad command", "req-bad", ("pytest\x00",)),)
        with self.assertRaises(ValueError):
            self.runner.run(task_id="task-1", checks=checks)
        self.executor.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
