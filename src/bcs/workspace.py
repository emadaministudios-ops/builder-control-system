"""Controller-owned project workspace path confinement.

This module provides filesystem path resolution only. It does not execute
commands, grant worker permissions, or create an OS-level sandbox. Callers
must authorize requests through the policy gateway before using workspace
operations, and must prevent untrusted concurrent processes from mutating the
workspace until a hardened executor is introduced.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re


class WorkspaceError(ValueError):
    """Raised when a workspace path is invalid or escapes its trusted root."""


@dataclass(frozen=True)
class Workspace:
    """A project workspace whose root is supplied by the trusted controller."""

    workspace_id: str
    root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise WorkspaceError("workspace_id is required.")
        try:
            canonical_root = Path(self.root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError("Workspace root must exist and be accessible.") from exc
        if not canonical_root.is_dir():
            raise WorkspaceError("Workspace root must be a directory.")
        object.__setattr__(self, "root", canonical_root)

    @staticmethod
    def _validate_relative_path(relative_path: str) -> PurePosixPath:
        if not isinstance(relative_path, str) or not relative_path:
            raise WorkspaceError("A non-empty workspace-relative path is required.")
        if "\x00" in relative_path:
            raise WorkspaceError("NUL bytes are not allowed in paths.")

        normalized = relative_path.replace("\\\\", "/")
        if normalized.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", normalized):
            raise WorkspaceError("Absolute paths are not allowed.")
        parts = normalized.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise WorkspaceError("Empty, current-directory, and traversal segments are not allowed.")
        return PurePosixPath(*parts)

    def resolve_path(self, relative_path: str, *, must_exist: bool = False) -> Path:
        """Resolve a path and reject symlink or traversal escapes from this root."""
        safe_relative = self._validate_relative_path(relative_path)
        candidate = self.root.joinpath(*safe_relative.parts)
        try:
            resolved = candidate.resolve(strict=must_exist)
            resolved.relative_to(self.root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspaceError("Path is inaccessible or escapes the workspace.") from exc
        return resolved

    def read_text(self, relative_path: str, *, encoding: str = "utf-8") -> str:
        path = self.resolve_path(relative_path, must_exist=True)
        if not path.is_file():
            raise WorkspaceError("Requested workspace path is not a file.")
        return path.read_text(encoding=encoding)

    def list_entries(self, relative_path: str = ".") -> tuple[str, ...]:
        """List names in a directory; the root is selected only by the controller."""
        if relative_path == ".":
            directory = self.root
        else:
            directory = self.resolve_path(relative_path, must_exist=True)
        if not directory.is_dir():
            raise WorkspaceError("Requested workspace path is not a directory.")
        return tuple(sorted(entry.name for entry in directory.iterdir()))
