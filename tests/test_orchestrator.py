import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from bcs.qa import EvidenceStore, QAReportBuilder
from bcs.sandbox_executor import ExecutionResult
from bcs.orchestrator import Orchestrator, WorkerResult, WorkerResultStatus, TaskState

class OrchestratorTests(unittest.TestCase):
    def report(self):
        b=QAReportBuilder(task_id="parent",qa_run_id="run",evidence_store=EvidenceStore(),
            started_at=datetime(2026,11,19,10,0,tzinfo=timezone.utc))
        b.add_execution(check_id="ui",name="Frontend UI tests",command=("npm","test"),
            result=ExecutionResult("failed","Assertion failed",exit_code=1))
        return b.build()
    def test_ingest_creates_queued_specialist_task(self):
        o=Orchestrator(); ids=o.ingest_qa_report(self.report())
        self.assertEqual(len(ids),1); self.assertEqual(o.get_task(ids[0]).state,TaskState.QUEUED)
        self.assertEqual(o.get_task(ids[0]).task.role,"frontend-developer")
    def test_dispatch_runs_registered_role_handler(self):
        o=Orchestrator(); tid=o.ingest_qa_report(self.report())[0]; seen=[]
        o.register_worker(worker_id="w1",role="frontend-developer",
            handler=lambda t:(seen.append(t.task_id) or WorkerResult(WorkerResultStatus.COMPLETED,"fixed",("src/ui.tsx",))))
        result=o.dispatch(tid); self.assertEqual(result.state,TaskState.COMPLETED)
        self.assertEqual(seen,[tid])
    def test_no_worker_leaves_task_queued(self):
        o=Orchestrator(); tid=o.ingest_qa_report(self.report())[0]
        self.assertEqual(o.dispatch(tid).state,TaskState.QUEUED)
    def test_handler_exception_is_failure_without_exception_detail(self):
        o=Orchestrator(); tid=o.ingest_qa_report(self.report())[0]
        def broken(_): raise RuntimeError("secret detail")
        o.register_worker(worker_id="w1",role="frontend-developer",handler=broken)
        result=o.dispatch(tid); self.assertEqual(result.state,TaskState.FAILED)
        self.assertNotIn("secret detail",result.result.summary)
    def test_invalid_result_contract_fails(self):
        o=Orchestrator(); tid=o.ingest_qa_report(self.report())[0]
        o.register_worker(worker_id="w1",role="frontend-developer",handler=lambda _:{"status":"completed"})
        self.assertEqual(o.dispatch(tid).state,TaskState.FAILED)
    def test_retry_budget_is_bounded(self):
        o=Orchestrator(max_attempts=2); tid=o.ingest_qa_report(self.report())[0]
        o.register_worker(worker_id="w1",role="frontend-developer",
            handler=lambda _:WorkerResult(WorkerResultStatus.ERROR,"failed"))
        o.dispatch(tid); self.assertEqual(o.retry(tid).task.attempt,2)
        o.dispatch(tid)
        with self.assertRaises(ValueError): o.retry(tid)
    def test_blocked_qa_is_escalated(self):
        b=QAReportBuilder(task_id="p",qa_run_id="r",evidence_store=EvidenceStore())
        b.add_execution(check_id="c",name="Protected",command=("pytest",),
            result=ExecutionResult("blocked","Approval needed"))
        o=Orchestrator(); o.ingest_qa_report(b.build())
        self.assertEqual(len(o.snapshot().escalations),1)
    def test_traversal_changed_path_is_rejected(self):
        o=Orchestrator(); tid=o.ingest_qa_report(self.report())[0]
        o.register_worker(worker_id="w1",role="frontend-developer",
            handler=lambda _:WorkerResult(WorkerResultStatus.COMPLETED,"done",("../outside",)))
        self.assertEqual(o.dispatch(tid).state,TaskState.FAILED)
    def test_duplicate_report_ingestion_is_idempotent(self):
        o=Orchestrator(); report=self.report()
        self.assertEqual(o.ingest_qa_report(report),o.ingest_qa_report(report))
        self.assertEqual(len(o.snapshot().tasks),1)

if __name__=="__main__": unittest.main()
