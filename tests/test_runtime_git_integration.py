"""Runtime guards for integrating only completed BCS work groups."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.git_worktrees import GitWorktreeManager
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
        self.assertTrue((result.path / "feature.py").exists())

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
