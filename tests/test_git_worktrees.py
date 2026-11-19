"""Integration tests for controller-owned Git worktree provisioning."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.git_worktrees import GitWorktreeError, GitWorktreeManager


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class GitWorktreeManagerTests(unittest.TestCase):
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

    def tearDown(self):
        self.temp.cleanup()

    def test_independent_groups_receive_separate_branches_and_directories(self):
        first = self.manager.workspace_for_group("project-alpha")
        second = self.manager.workspace_for_group("project-beta")
        self.assertNotEqual(first.root, second.root)
        self.assertNotEqual(
            self.manager.record_for_group("project-alpha").branch,
            self.manager.record_for_group("project-beta").branch,
        )
        (first.root / "alpha.txt").write_text("alpha", encoding="utf-8")
        self.assertFalse((second.root / "alpha.txt").exists())
        self.assertEqual(("alpha.txt",), self.manager.changed_files("project-alpha"))

    def test_same_workspace_group_reuses_worktree_for_repair(self):
        first = self.manager.workspace_for_group("same-project")
        second = self.manager.workspace_for_group("same-project")
        self.assertEqual(first.root, second.root)
        self.assertEqual(first.workspace_id, second.workspace_id)

    def test_workspace_for_task_uses_stable_group_not_task_id(self):
        class Task:
            task_id = "repair-2"
            workspace_group_id = "original-project"
        workspace = self.manager.workspace_for_task(Task())
        self.assertEqual(
            workspace.root,
            self.manager.workspace_for_group("original-project").root,
        )

    def test_dirty_worktree_cannot_be_removed(self):
        workspace = self.manager.workspace_for_group("keep-me")
        (workspace.root / "unfinished.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(GitWorktreeError, "dirty worktree"):
            self.manager.remove_clean_worktree("keep-me")
        self.assertTrue((workspace.root / "unfinished.txt").exists())

    def test_clean_worktree_can_be_removed_but_branch_is_preserved(self):
        workspace = self.manager.workspace_for_group("clean-me")
        branch = self.manager.record_for_group("clean-me").branch
        self.manager.remove_clean_worktree("clean-me")
        self.assertFalse(workspace.root.exists())
        self.assertEqual(branch, git(self.repo, "branch", "--list", branch).strip("* ").strip())

    def test_rejects_main_as_base_branch(self):
        with self.assertRaisesRegex(GitWorktreeError, "development"):
            GitWorktreeManager(
                repository_root=self.repo,
                worktree_root=self.base / "other",
                base_ref="main",
            )

    def test_rejects_worktree_storage_inside_source_repository(self):
        with self.assertRaisesRegex(GitWorktreeError, "outside"):
            GitWorktreeManager(
                repository_root=self.repo,
                worktree_root=self.repo / ".bcs-worktrees",
            )

    def test_enforces_active_worktree_limit(self):
        manager = GitWorktreeManager(
            repository_root=self.repo,
            worktree_root=self.base / "limited",
            max_active=1,
        )
        manager.workspace_for_group("one")
        with self.assertRaisesRegex(GitWorktreeError, "limit"):
            manager.workspace_for_group("two")


if __name__ == "__main__":
    unittest.main()
