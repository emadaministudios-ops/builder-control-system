"""Tests for isolated BCS worker-branch integration."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.git_worktrees import GitWorktreeManager, GitWorktreeError
from bcs.git_integration import GitIntegrationCoordinator


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class GitIntegrationCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "development")
        git(self.repo, "config", "user.email", "bcs-tests@example.invalid")
        git(self.repo, "config", "user.name", "BCS Tests")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-m", "initial")
        self.manager = GitWorktreeManager(
            repository_root=self.repo,
            worktree_root=self.base / "worktrees",
        )
        self.coordinator = GitIntegrationCoordinator(self.manager)

    def tearDown(self):
        self.temp.cleanup()

    def test_integrates_independent_worker_changes_on_separate_branch(self):
        first = self.manager.workspace_for_group("frontend")
        second = self.manager.workspace_for_group("backend")
        (first.root / "frontend.py").write_text("frontend = True\n", encoding="utf-8")
        (second.root / "backend.py").write_text("backend = True\n", encoding="utf-8")
        result = self.coordinator.integrate(
            integration_id="feature-one",
            workspace_group_ids=["frontend", "backend"],
        )
        self.assertEqual("awaiting_qa", result.status)
        self.assertTrue((result.path / "frontend.py").exists())
        self.assertTrue((result.path / "backend.py").exists())
        self.assertEqual(2, len(result.commits))
        self.assertEqual("development", git(self.repo, "branch", "--show-current"))
        self.assertFalse((self.repo / "frontend.py").exists())

    def test_detects_conflicts_and_aborts_partial_integration(self):
        first = self.manager.workspace_for_group("agent-one")
        second = self.manager.workspace_for_group("agent-two")
        (first.root / "shared.py").write_text("value = 'one'\n", encoding="utf-8")
        (second.root / "shared.py").write_text("value = 'two'\n", encoding="utf-8")
        result = self.coordinator.integrate(
            integration_id="conflicting-feature",
            workspace_group_ids=["agent-one", "agent-two"],
        )
        self.assertEqual("conflict", result.status)
        self.assertEqual(("shared.py",), result.conflicts)
        self.assertFalse((result.path / ".git").exists() and (result.path / "shared.py").read_text() == "value = 'two'\n")
        self.assertEqual("value = 'one'\n", (first.root / "shared.py").read_text())
        self.assertEqual("value = 'two'\n", (second.root / "shared.py").read_text())

    def test_refuses_credential_like_files(self):
        workspace = self.manager.workspace_for_group("secrets-test")
        (workspace.root / ".env").write_text("API_KEY=do-not-commit\n", encoding="utf-8")
        with self.assertRaisesRegex(GitWorktreeError, "credential-like"):
            self.coordinator.integrate(
                integration_id="blocked",
                workspace_group_ids=["secrets-test"],
            )

    def test_requires_nonempty_unique_groups(self):
        with self.assertRaises(ValueError):
            self.coordinator.integrate(integration_id="empty", workspace_group_ids=[])
        with self.assertRaises(ValueError):
            self.coordinator.integrate(integration_id="dupe", workspace_group_ids=["x", "x"])

    def test_integration_branch_is_not_development_or_main(self):
        workspace = self.manager.workspace_for_group("simple")
        (workspace.root / "new.txt").write_text("hello\n", encoding="utf-8")
        result = self.coordinator.integrate(
            integration_id="safe-branch",
            workspace_group_ids=["simple"],
        )
        self.assertTrue(result.branch.startswith("bcs/integration/"))
        self.assertNotIn(result.branch, {"main", "development"})
        self.assertEqual("development", git(self.repo, "branch", "--show-current"))


if __name__ == "__main__":
    unittest.main()
