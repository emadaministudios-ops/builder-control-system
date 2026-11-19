"""Runtime guards for integrating only completed BCS work groups."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.git_worktrees import GitWorktreeManager
from bcs.approvals import ActorType, ApprovalError
from bcs.qa import EvidenceStore, QAReportBuilder
from bcs.qa_runner import QACheckSpec
from bcs.sandbox_executor import ExecutionResult
from bcs.orchestrator import WorkerResult, WorkerResultStatus
from bcs.runtime import create_runtime


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class RuntimeGitIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "development")
        git(self.repo, "config", "user.email", "bcs-tests@example.invalid")
        git(self.repo, "config", "user.name", "BCS Tests")
        (self.repo / "README.md").write_text("base\\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")
        self.manager = GitWorktreeManager(
            repository_root=self.repo,
            worktree_root=self.base / "worktrees",
        )
        self.runtime = create_runtime(self.base / "bcs.sqlite3")

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_runtime_integrates_only_completed_groups(self):
        record = self.runtime.orchestrator.create_task(
            task_id="feature-a", objective="Build feature A",
            acceptance_criteria=["Feature A works"], role="builder",
        )
        workspace = self.manager.workspace_for_group(record.task.workspace_group_id)
        (workspace.root / "feature.py").write_text("feature = True\\n", encoding="utf-8")
        self.runtime.orchestrator.register_worker(
            worker_id="builder-1", role="builder",
            handler=lambda task: WorkerResult(
                WorkerResultStatus.COMPLETED, "Implementation finished",
                changed_files=("feature.py",),
            ),
        )
        outcome = self.runtime.orchestrator.dispatch(record.task.task_id)
        self.assertEqual("completed", outcome.state.value)

        result = self.runtime.integrate_completed_work(
            manager=self.manager, integration_id="feature-a-integration",
            workspace_group_ids=[record.task.workspace_group_id],
        )
        self.assertEqual("awaiting_qa", result.status)
        self.assertRegex(result.commit_sha, r"^[0-9a-f]{40,64}$")
        self.assertEqual(result.commit_sha, git(result.path, "rev-parse", "HEAD"))
        self.assertTrue((result.path / "feature.py").exists())

    def test_integration_history_survives_runtime_restart(self):
        record = self.runtime.orchestrator.create_task(
            task_id="durable-feature", objective="Build durable feature",
            acceptance_criteria=["Feature works"], role="builder",
        )
        workspace = self.manager.workspace_for_group(record.task.workspace_group_id)
        (workspace.root / "durable.py").write_text("ready = True\\n", encoding="utf-8")
        self.runtime.orchestrator.register_worker(
            worker_id="durable-builder", role="builder",
            handler=lambda task: WorkerResult(
                WorkerResultStatus.COMPLETED, "Finished",
                changed_files=("durable.py",),
            ),
        )
        self.runtime.orchestrator.dispatch(record.task.task_id)
        result = self.runtime.integrate_completed_work(
            manager=self.manager, integration_id="durable-integration",
            workspace_group_ids=[record.task.workspace_group_id],
        )
        self.assertEqual("awaiting_qa", result.status)
        self.runtime.close()
        self.runtime = create_runtime(self.base / "bcs.sqlite3")
        history = self.runtime.integration_history()
        self.assertEqual(1, len(history))
        self.assertEqual("durable-integration", history[0]["integration_id"])
        self.assertEqual(result.branch, history[0]["branch"])
        self.assertIn(record.task.workspace_group_id, history[0]["included_groups"])
        self.assertEqual("awaiting_qa", history[0]["status"])
        self.assertEqual(result.commit_sha, history[0]["commit_sha"])

    def _completed_integration(self, task_id, integration_id):
        record = self.runtime.orchestrator.create_task(
            task_id=task_id, objective="Build integration QA fixture",
            acceptance_criteria=["Fixture works"], role="builder",
        )
        workspace = self.manager.workspace_for_group(record.task.workspace_group_id)
        (workspace.root / "fixture.py").write_text("ready = True\\n", encoding="utf-8")
        self.runtime.orchestrator.register_worker(
            worker_id=task_id + "-worker", role="builder",
            handler=lambda task: WorkerResult(
                WorkerResultStatus.COMPLETED, "Fixture complete",
                changed_files=("fixture.py",),
            ),
        )
        self.runtime.orchestrator.dispatch(task_id)
        result = self.runtime.integrate_completed_work(
            manager=self.manager, integration_id=integration_id,
            workspace_group_ids=[record.task.workspace_group_id],
        )
        return record, result

    def test_integration_qa_executes_in_exact_checkout_and_persists(self):
        record, integration = self._completed_integration("qa-task", "qa-integration")
        executor = Mock()
        executor.execute.return_value = ExecutionResult(
            "completed", "Tests passed", exit_code=0, stdout="ok"
        )
        with patch("bcs.runtime.DockerSandboxExecutor", return_value=executor):
            report = self.runtime.run_integration_qa(
                manager=self.manager, integration_id=integration.integration_id,
                checks=(QACheckSpec("unit", "Unit tests", "qa-request", ("python", "-m", "unittest")),),
                docker_executable="docker", image="bcs-test:latest",
                allowed_images=frozenset({"bcs-test:latest"}),
            )
        self.assertEqual(record.task.task_id, report.task_id)
        history = self.runtime.integration_qa_history(integration.integration_id)
        self.assertEqual(1, len(history))
        self.assertEqual(integration.commit_sha, history[0]["commit_sha"])
        self.assertEqual("passed", history[0]["status"])
        self.assertEqual(1, history[0]["executed_check_count"])
        self.assertTrue(history[0]["evidence_refs"])
        self.runtime.close()
        self.runtime = create_runtime(self.base / "bcs.sqlite3")
        self.assertEqual(history, self.runtime.integration_qa_history(integration.integration_id))
        evidence = self.runtime.evidence_store.get(history[0]["evidence_refs"][0])
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence.stdout, "ok")
        self.assertEqual(evidence.content_sha256, self.runtime.evidence_store.get(
            history[0]["evidence_refs"][0]
        ).content_sha256)

    def test_static_only_pass_is_not_assessed_as_executed_qa(self):
        record, integration = self._completed_integration("static-task", "static-integration")
        builder = QAReportBuilder(
            task_id=record.task.task_id, qa_run_id="static-only-run",
            evidence_store=EvidenceStore(),
        )
        builder.add_static_check(
            check_id="source-review", name="Source review", passed=True,
            details="No source issue found.", basis="Static inspection only.",
        )
        report = builder.build()
        assessment = self.runtime._attach_qa_report_to_integration(
            integration.integration_id, report
        )
        self.assertEqual("not_assessed", assessment["status"])
        self.assertEqual(0, assessment["executed_check_count"])

    def test_integration_qa_refuses_changed_checkout_head(self):
        _, integration = self._completed_integration("changed-task", "changed-integration")
        git(integration.path, "checkout", "--detach", "HEAD")
        git(integration.path, "commit", "--allow-empty", "-m", "move HEAD")
        executor = Mock()
        executor.execute.return_value = ExecutionResult(
            "completed", "Tests passed", exit_code=0, stdout="ok"
        )
        with patch("bcs.runtime.DockerSandboxExecutor", return_value=executor):
            with self.assertRaisesRegex(ValueError, "HEAD no longer matches"):
                self.runtime.run_integration_qa(
                    manager=self.manager, integration_id=integration.integration_id,
                    checks=(QACheckSpec("unit", "Unit", "changed-request", ("pytest",)),),
                    docker_executable="docker", image="bcs-test:latest",
                    allowed_images=frozenset({"bcs-test:latest"}),
                )
    def test_promotion_requires_human_approval_and_only_fast_forwards_development(self):
        _, integration = self._completed_integration("promote-task", "promote-integration")
        executor = Mock()
        executor.execute.return_value = ExecutionResult(
            "completed", "Tests passed", exit_code=0, stdout="all green"
        )
        with patch("bcs.runtime.DockerSandboxExecutor", return_value=executor):
            self.runtime.run_integration_qa(
                manager=self.manager, integration_id=integration.integration_id,
                checks=(QACheckSpec("unit", "Unit tests", "promote-qa", ("python", "-m", "unittest")),),
                docker_executable="docker", image="bcs-test:latest",
                allowed_images=frozenset({"bcs-test:latest"}),
            )
        approval = self.runtime.request_integration_promotion(
            manager=self.manager, integration_id=integration.integration_id,
        )
        self.assertEqual("pending", approval.status.value)
        with self.assertRaisesRegex(ValueError, "human-approved"):
            self.runtime.promote_integration(manager=self.manager, request_id=approval.request_id)
        with self.assertRaises(ApprovalError):
            self.runtime.approvals.decide(
                approval.request_id, actor_id="worker", actor_type=ActorType.WORKER,
                approved=True,
            )
        self.runtime.approvals.decide(
            approval.request_id, actor_id="human-reviewer", actor_type=ActorType.HUMAN,
            approved=True, reason="Reviewed the exact QA-passed commit.",
        )
        promoted = self.runtime.promote_integration(
            manager=self.manager, request_id=approval.request_id,
        )
        self.assertEqual("promoted", promoted["status"])
        self.assertEqual("human-reviewer", promoted["promoted_by"])
        self.assertEqual(integration.commit_sha, git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual("development", git(self.repo, "branch", "--show-current"))
        self.assertNotIn("main", git(self.repo, "branch", "--list").splitlines())

    def test_promotion_rejects_main_and_requires_latest_qa_pass(self):
        _, integration = self._completed_integration("reject-promotion", "reject-integration")
        with self.assertRaisesRegex(ValueError, "development branch"):
            self.runtime.request_integration_promotion(
                manager=self.manager, integration_id=integration.integration_id,
                target_branch="main",
            )
        with self.assertRaisesRegex(ValueError, "executed QA assessment"):
            self.runtime.request_integration_promotion(
                manager=self.manager, integration_id=integration.integration_id,
            )

    def test_promotion_request_survives_runtime_restart(self):
        _, integration = self._completed_integration("restart-promotion", "restart-integration")
        executor = Mock()
        executor.execute.return_value = ExecutionResult(
            "completed", "Tests passed", exit_code=0, stdout="ok"
        )
        with patch("bcs.runtime.DockerSandboxExecutor", return_value=executor):
            self.runtime.run_integration_qa(
                manager=self.manager, integration_id=integration.integration_id,
                checks=(QACheckSpec("unit", "Unit", "restart-qa", ("pytest",)),),
                docker_executable="docker", image="bcs-test:latest",
                allowed_images=frozenset({"bcs-test:latest"}),
            )
        approval = self.runtime.request_integration_promotion(
            manager=self.manager, integration_id=integration.integration_id,
        )
        self.runtime.close()
        self.runtime = create_runtime(self.base / "bcs.sqlite3")
        self.assertIsNotNone(self.runtime.store.get_integration_promotion_request(approval.request_id))
        self.assertEqual("pending", self.runtime.approvals.get(approval.request_id).status.value)
    def test_runtime_blocks_queued_or_failed_groups(self):
        record = self.runtime.orchestrator.create_task(
            task_id="not-ready", objective="Build pending feature",
            acceptance_criteria=["Feature works"], role="builder",
        )
        self.manager.workspace_for_group(record.task.workspace_group_id)
        with self.assertRaisesRegex(ValueError, "completed"):
            self.runtime.integrate_completed_work(
                manager=self.manager, integration_id="must-not-integrate",
                workspace_group_ids=[record.task.workspace_group_id],
            )


if __name__ == "__main__":
    unittest.main()
