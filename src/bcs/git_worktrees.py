"""Controller-owned Git worktree provisioning for isolated BCS project work.

The manager creates one worktree per stable workspace_group_id. Repair tasks
therefore return to the same checkout, while unrelated groups receive separate
branches and directories. It never commits, merges, pushes, or touches main.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import subprocess
import threading

from .workspace import Workspace


class GitWorktreeError(RuntimeError):
    """Raised when safe worktree provisioning or inspection cannot proceed."""


@dataclass(frozen=True)
class WorktreeRecord:
    workspace_group_id: str
    branch: str
    path: Path
    base_ref: str


class GitWorktreeManager:
    """Provision and inspect isolated worktrees under controller-owned roots."""

    def __init__(
        self,
        *,
        repository_root: str | Path,
        worktree_root: str | Path,
        base_ref: str = "development",
        max_active: int = 16,
        git_executable: str = "git",
        command_timeout: int = 20,
    ) -> None:
        if not isinstance(base_ref, str) or base_ref != "development":
            raise GitWorktreeError("Only the configured development base branch is allowed.")
        if not isinstance(max_active, int) or isinstance(max_active, bool) or not 1 <= max_active <= 256:
            raise ValueError("max_active must be between 1 and 256.")
        if not isinstance(command_timeout, int) or isinstance(command_timeout, bool) or not 1 <= command_timeout <= 300:
            raise ValueError("command_timeout must be between 1 and 300 seconds.")
        if not isinstance(git_executable, str) or not git_executable.strip():
            raise ValueError("A Git executable is required.")

        try:
            self.repository_root = Path(repository_root).expanduser().resolve(strict=True)
            if not self.repository_root.is_dir():
                raise GitWorktreeError("Repository root must be a directory.")
            self.worktree_root = Path(worktree_root).expanduser().resolve(strict=False)
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            self.worktree_root = self.worktree_root.resolve(strict=True)
            self.worktree_root.relative_to(self.repository_root)
            raise GitWorktreeError("Worktree storage must be outside the source repository.")
        except ValueError:
            # relative_to raises ValueError when worktree_root is outside repo.
            pass
        except OSError as exc:
            raise GitWorktreeError("Repository or worktree storage is inaccessible.") from exc

        self.base_ref = base_ref
        self.max_active = max_active
        self.git_executable = git_executable
        self.command_timeout = command_timeout
        self._records: dict[str, WorktreeRecord] = {}
        self._lock = threading.RLock()

        top = self._run(["rev-parse", "--show-toplevel"]).stdout.strip()
        if Path(top).resolve() != self.repository_root:
            raise GitWorktreeError("Configured repository root is not the Git working-tree root.")
        self._run(["check-ref-format", "--branch", self.base_ref])
        self._run(["show-ref", "--verify", "--quiet", f"refs/heads/{self.base_ref}"])

    def _run(self, args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [self.git_executable, *args],
                cwd=str(cwd or self.repository_root),
                check=True,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            stderr = getattr(exc, "stderr", None)
            detail = stderr.strip()[:500] if isinstance(stderr, str) and stderr.strip() else type(exc).__name__
            raise GitWorktreeError(f"Git operation failed: {detail}") from exc

    @staticmethod
    def _identity(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise GitWorktreeError("Workspace group ID must contain 1 to 256 characters.")
        if "\x00" in value:
            raise GitWorktreeError("Workspace group ID contains an invalid character.")
        return value.strip()

    def _names(self, group_id: str) -> tuple[str, str]:
        digest = hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:12]
        slug = re.sub(r"[^a-z0-9-]+", "-", group_id.lower()).strip("-")[:32] or "group"
        return f"bcs/work/{slug}-{digest}", f"{slug}-{digest}"

    def workspace_for_group(self, workspace_group_id: str) -> Workspace:
        group_id = self._identity(workspace_group_id)
        with self._lock:
            existing = self._records.get(group_id)
            if existing is None:
                if len(self._records) >= self.max_active:
                    raise GitWorktreeError("Active worktree limit reached.")
                branch, dirname = self._names(group_id)
                path = (self.worktree_root / dirname).resolve(strict=False)
                try:
                    path.relative_to(self.worktree_root)
                except ValueError as exc:
                    raise GitWorktreeError("Generated worktree path escaped its trusted root.") from exc
                if path.exists():
                    raise GitWorktreeError("Worktree destination already exists; refusing to reuse it.")
                self._run(["worktree", "add", "-b", branch, str(path), self.base_ref])
                existing = WorktreeRecord(group_id, branch, path.resolve(strict=True), self.base_ref)
                self._records[group_id] = existing
            return Workspace(
                workspace_id="workspace-" + hashlib.sha256(group_id.encode("utf-8")).hexdigest()[:20],
                root=existing.path,
            )

    def workspace_for_task(self, task) -> Workspace:
        """Trusted factory adapter for BCS WorkerTask instances."""
        group_id = getattr(task, "workspace_group_id", "") or getattr(task, "task_id", "")
        return self.workspace_for_group(group_id)

    def record_for_group(self, workspace_group_id: str) -> WorktreeRecord:
        group_id = self._identity(workspace_group_id)
        with self._lock:
            try:
                return self._records[group_id]
            except KeyError as exc:
                raise GitWorktreeError("Workspace group has no manager-owned worktree.") from exc

    def status(self, workspace_group_id: str) -> tuple[str, ...]:
        record = self.record_for_group(workspace_group_id)
        result = self._run(["status", "--porcelain", "--untracked-files=all"], cwd=record.path)
        return tuple(line for line in result.stdout.splitlines() if line.strip())

    def changed_files(self, workspace_group_id: str) -> tuple[str, ...]:
        record = self.record_for_group(workspace_group_id)
        result = self._run(["status", "--porcelain", "--untracked-files=all"], cwd=record.path)
        paths = set()
        for line in result.stdout.splitlines():
            value = line[3:].strip()
            if " -> " in value:
                value = value.split(" -> ", 1)[1]
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            if value:
                paths.add(value)
        return tuple(sorted(paths))

    def diff(self, workspace_group_id: str) -> str:
        record = self.record_for_group(workspace_group_id)
        tracked = self._run(["diff", "--no-ext-diff", "--"], cwd=record.path).stdout
        staged = self._run(["diff", "--cached", "--no-ext-diff", "--"], cwd=record.path).stdout
        return staged + tracked

    def remove_clean_worktree(self, workspace_group_id: str) -> None:
        """Remove only a manager-owned, clean worktree; preserve its branch."""
        group_id = self._identity(workspace_group_id)
        with self._lock:
            record = self.record_for_group(group_id)
            if self.status(group_id):
                raise GitWorktreeError("Refusing to remove a dirty worktree; preserve its changes for review.")
            self._run(["worktree", "remove", str(record.path)])
            self._records.pop(group_id, None)


__all__ = ["GitWorktreeError", "WorktreeRecord", "GitWorktreeManager"]
