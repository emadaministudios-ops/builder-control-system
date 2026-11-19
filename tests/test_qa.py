"""Tests for evidence-backed QA reports."""
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.qa import EvidenceStore, QAReportBuilder, QACheckStatus
from bcs.sandbox_executor import ExecutionResult


class QAReportTests(unittest.TestCase):
    def setUp(self):
        self.store = EvidenceStore()
        self.builder = QAReportBuilder(
            task_id="task-1", qa_run_id="qa-1", evidence_store=self.store,
            started_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
        )

    def test_successful_execution_has_command_exit_code_and_evidence(self):
        check = self.builder.add_execution(
            check_id="unit", name="Unit tests",
            command=("python", "-m", "unittest"),
            result=ExecutionResult("completed", "Tests passed.", exit_code=0,
                                   stdout="Ran 3 tests", stderr=""),
        )
        self.assertEqual(check.status, QACheckStatus.PASSED)
        self.assertTrue(check.executed)
        self.assertEqual(check.exit_code, 0)
        self.assertIsNotNone(check.evidence_ref)
        artifact = self.store.get(check.evidence_ref)
        self.assertEqual(artifact.stdout, "Ran 3 tests")
        self.assertEqual(artifact.kind, "execution_output")

    def test_nonzero_exit_is_failed_with_evidence(self):
        check = self.builder.add_execution(
            check_id="lint", name="Lint",
            command=("npm", "run", "lint"),
            result=ExecutionResult("failed", "Non-zero exit.", exit_code=2,
                                   stdout="", stderr="lint errors"),
        )
        self.assertEqual(check.status, QACheckStatus.FAILED)
        self.assertTrue(check.executed)
        self.assertEqual(check.exit_code, 2)
        self.assertIsNotNone(check.evidence_ref)

    def test_timeout_is_failed_and_recorded_as_executed(self):
        check = self.builder.add_execution(
            check_id="timeout", name="Long test",
            command=("pytest",),
            result=ExecutionResult("timeout", "Timed out.", timed_out=True),
        )
        self.assertEqual(check.status, QACheckStatus.FAILED)
        self.assertTrue(check.executed)

    def test_blocked_command_is_not_claimed_as_executed(self):
        check = self.builder.add_execution(
            check_id="blocked", name="Protected tests",
            command=("pytest",),
            result=ExecutionResult("blocked", "Approval required."),
        )
        self.assertEqual(check.status, QACheckStatus.BLOCKED)
        self.assertFalse(check.executed)
        self.assertIsNone(check.evidence_ref)

    def test_error_is_not_claimed_as_executed(self):
        check = self.builder.add_execution(
            check_id="docker-error", name="Docker startup",
            command=("pytest",),
            result=ExecutionResult("error", "Docker unavailable."),
        )
        self.assertEqual(check.status, QACheckStatus.ERROR)
        self.assertFalse(check.executed)

    def test_static_check_has_explicit_non_execution_basis(self):
        check = self.builder.add_static_check(
            check_id="imports", name="Import scan", passed=True,
            details="No unresolved imports found.",
            basis="Static source inspection; no test command was run.",
        )
        self.assertFalse(check.executed)
        self.assertIsNone(check.command)
        self.assertIsNotNone(check.non_execution_basis)

    def test_report_aggregates_failure_over_blocked_and_passed(self):
        self.builder.add_static_check(
            check_id="static", name="Static scan", passed=True,
            details="Clean.", basis="Static inspection only.",
        )
        self.builder.add_execution(
            check_id="blocked", name="Blocked test", command=("pytest",),
            result=ExecutionResult("blocked", "Approval required."),
        )
        self.builder.add_execution(
            check_id="failed", name="Failed test", command=("pytest",),
            result=ExecutionResult("failed", "Assertion failed.", exit_code=1),
        )
        report = self.builder.build()
        self.assertEqual(report.status, QACheckStatus.FAILED)
        self.assertEqual(report.contract_version, "1.0.0-draft")
        self.assertEqual(len(report.checks), 3)

    def test_empty_report_is_blocked(self):
        self.assertEqual(self.builder.build().status, QACheckStatus.BLOCKED)

    def test_duplicate_check_ids_are_rejected(self):
        self.builder.add_static_check(
            check_id="same", name="First", passed=True,
            details="ok", basis="static",
        )
        with self.assertRaises(ValueError):
            self.builder.add_static_check(
                check_id="same", name="Second", passed=True,
                details="ok", basis="static",
            )

    def test_invalid_command_is_rejected(self):
        with self.assertRaises(ValueError):
            self.builder.add_execution(
                check_id="bad", name="Bad", command="pytest",
                result=ExecutionResult("completed", "ok", exit_code=0),
            )

    def test_evidence_size_limit_is_enforced(self):
        tiny = EvidenceStore(max_artifact_bytes=8)
        with self.assertRaises(ValueError):
            tiny.put_execution("output too long", "")

    def test_invalid_report_timestamps_are_rejected(self):
        with self.assertRaises(ValueError):
            self.builder.build(
                finished_at=datetime(2026, 11, 19, 9, 59, tzinfo=timezone.utc)
            )
        with self.assertRaises(ValueError):
            self.builder.build(finished_at=datetime(2026, 11, 19, 11, 0))


if __name__ == "__main__":
    unittest.main()
