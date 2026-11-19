"""Persistence and restart tests for QA feedback-cycle history."""
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.feedback import FeedbackController, FeedbackState
from bcs.qa import EvidenceStore, QAReportBuilder
from bcs.sandbox_executor import ExecutionResult
from bcs.storage import SQLiteOrchestrationStore


class PersistentFeedbackTests(unittest.TestCase):
    def report(self, run_id):
        builder = QAReportBuilder(
            task_id="persisted-task", qa_run_id=run_id,
            evidence_store=EvidenceStore(),
            started_at=datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc),
        )
        builder.add_execution(
            check_id="acceptance", name="Acceptance checks",
            command=("pytest",),
            result=ExecutionResult("completed", "Passed", exit_code=0),
        )
        return builder.build()

    def test_feedback_history_survives_restart_and_completes_second_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            store = SQLiteOrchestrationStore(path)
            first = FeedbackController(store=store)
            snapshot = first.record_qa_report(self.report("qa-1"))
            self.assertEqual(snapshot.state, FeedbackState.VERIFY_AGAIN)
            store.close()

            reopened = SQLiteOrchestrationStore(path)
            second = FeedbackController(store=reopened)
            restored = second.get_snapshot("persisted-task")
            self.assertEqual(len(restored.cycles), 1)
            self.assertEqual(restored.cycles[0].qa_run_id, "qa-1")
            final = second.record_qa_report(self.report("qa-2"))
            self.assertEqual(final.state, FeedbackState.VERIFIED)
            self.assertEqual(len(final.cycles), 2)
            reopened.close()

    def test_duplicate_qa_run_is_idempotent_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bcs.sqlite3"
            store = SQLiteOrchestrationStore(path)
            controller = FeedbackController(store=store)
            first = controller.record_qa_report(self.report("same-run"))
            store.close()

            reopened = SQLiteOrchestrationStore(path)
            restored = FeedbackController(store=reopened)
            duplicate = restored.record_qa_report(self.report("same-run"))
            self.assertEqual(duplicate, first)
            self.assertEqual(len(duplicate.cycles), 1)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
