"""Safe integration of completed BCS worktree branches.

This coordinator snapshots completed worker changes on their own branches and
cherry-picks those commits into a separate integration worktree. It never
updates development/main, pushes, or treats integration as QA approval.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from pathlib import Path

from .git_worktrees import GitWorktreeError, GitWorktreeManager


@dataclass(frozen=True)
class IntegrationResult:
    integration_id: str
    branch: str
    path: Path
    status: str
    included_groups: tuple[str, ...]
    commits: tuple[str, ...]
    conflicts: tuple[str, ...] = ()
    reason: str = ""


class GitIntegrationCoordinator:
    """Create an isolated integration branch from completed worker worktrees."""

    _SENSITIVE_PATH_PARTS = {
        ".env", ".env.local", ".env.production", ".npmrc", ".pypirc",
        "id_rsa", "id_ed25519", "credentials", "secrets.json",
        "service-account.json",
    }

    def __init__(self, manager: GitWorktreeManager, *, max_groups: int = 32) -> None:
        if not isinstance(manager, GitWorktreeManager):
            raise TypeError("manager must be a GitWorktreeManager")
        if not isinstance(max_groups, int) or isinstance(max_groups, bool) or not 1 <= max_groups <= 128:
            raise ValueError("max_groups must be between 1 and 128")
        self.manager = manager
        self.max_groups = max_groups

    @staticmethod
    def _integration_names(integration_id: str) -> tuple[str, str]:
        if (
            not isinstance(integration_id, str)
            or not integration_id.strip()
            or len(integration_id) > 200
            or "\x00" in integration_id
        ):
            raise GitWorktreeError("Integration ID must contain 1 to 200 valid characters.")
        value = integration_id.strip()
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        slug = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")[:32] or "integration"
        return f"bcs/integration/{slug}-{digest}", f"integration-{slug}-{digest}"

    def _snapshot_group(self, group_id: str) -> tuple[str, ...]:
        record = self.manager.record_for_group(group_id)
        changed = self.manager.changed_files(group_id)
        if not changed:
            commits = self.manager._run(
                ["rev-list", "--reverse", f"{self.manager.base_ref}..{record.branch}"]
            ).stdout.splitlines()
            return tuple(commit for commit in commits if commit)
        blocked = [
            name for name in changed
            if Path(name).name.lower() in self._SENSITIVE_PATH_PARTS
            or Path(name).name.lower().startswith(".env.")
        ]
        if blocked:
            raise GitWorktreeError(
                "Refusing to snapshot files with credential-like names: "
                + ", ".join(sorted(blocked))
            )
        self.manager._run(["add", "--all", "--", "."], cwd=record.path)
        staged = self.manager._run(
            ["diff", "--cached", "--name-only", "--"], cwd=record.path
        ).stdout.splitlines()
        if not staged:
            return ()
        # Disable repository hooks for this controller-generated snapshot commit.
        self.manager._run(
            ["-c", "core.hooksPath=/dev/null", "commit", "-m",
             f"BCS completed work: {group_id[:120]}"],
            cwd=record.path,
        )
        commits = self.manager._run(
            ["rev-list", "--reverse", f"{self.manager.base_ref}..{record.branch}"]
        ).stdout.splitlines()
        return tuple(commit for commit in commits if commit)

    def integrate(
        self,
        *,
        integration_id: str,
        workspace_group_ids: tuple[str, ...] | list[str],
    ) -> IntegrationResult:
        branch, dirname = self._integration_names(integration_id)
        if (
            isinstance(workspace_group_ids, (str, bytes))
            or not isinstance(workspace_group_ids, (tuple, list))
            or not workspace_group_ids
            or len(workspace_group_ids) > self.max_groups
        ):
            raise ValueError("Provide 1 to max_groups workspace group IDs.")
        if len(set(workspace_group_ids)) != len(workspace_group_ids):
            raise ValueError("Workspace group IDs must be unique.")

        groups = tuple(self.manager._identity(item) for item in workspace_group_ids)
        with self.manager._lock:
            base_sha = self.manager._run(
                ["rev-parse", f"refs/heads/{self.manager.base_ref}"]
            ).stdout.strip()
            commits: list[str] = []
            included: list[str] = []
            for group in groups:
                record = self.manager.record_for_group(group)
                if record.base_ref != self.manager.base_ref:
                    raise GitWorktreeError("Worker branch does not use the configured development base.")
                group_commits = self._snapshot_group(group)
                if group_commits:
                    included.append(group)
                    commits.extend(group_commits)

            if not commits:
                raise GitWorktreeError("No completed worker changes are available to integrate.")
            integration_path = (self.manager.worktree_root / dirname).resolve(strict=False)
            try:
                integration_path.relative_to(self.manager.worktree_root)
            except ValueError as exc:
                raise GitWorktreeError("Integration path escaped its trusted root.") from exc
            if integration_path.exists():
                raise GitWorktreeError("Integration destination already exists; refusing to overwrite it.")
            self.manager._run([
                "worktree", "add", "-b", branch, str(integration_path), base_sha
            ])

            applied: list[str] = []
            for commit in commits:
                try:
                    self.manager._run(["cherry-pick", "--no-edit", commit], cwd=integration_path)
                    applied.append(commit)
                except GitWorktreeError:
                    conflicts = self.manager._run(
                        ["diff", "--name-only", "--diff-filter=U"]
                        , cwd=integration_path
                    ).stdout.splitlines()
                    self.manager._run(["cherry-pick", "--abort"], cwd=integration_path)
                    return IntegrationResult(
                        integration_id=integration_id,
                        branch=branch,
                        path=integration_path,
                        status="conflict",
                        included_groups=tuple(included),
                        commits=tuple(applied),
                        conflicts=tuple(sorted(set(conflicts))),
                        reason="Git reported a content conflict; integration stopped and no worker branch was changed.",
                    )

            return IntegrationResult(
                integration_id=integration_id,
                branch=branch,
                path=integration_path,
                status="awaiting_qa",
                included_groups=tuple(included),
                commits=tuple(applied),
                reason="Changes were combined on an isolated integration branch. QA and human review are still required.",
            )


__all__ = ["IntegrationResult", "GitIntegrationCoordinator"]
