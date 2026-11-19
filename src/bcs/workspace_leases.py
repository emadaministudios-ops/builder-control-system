"""In-process exclusive leases for shared workspace roots."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading


class WorkspaceLeaseManager:
    """Serialize worker mutations when trusted factories resolve to the same root.

    Separate Git worktrees have separate canonical roots and remain parallel.
    The manager is process-local; multi-process deployments need a distributed
    lease provider before sharing mutable workspace roots across processes.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._leases: dict[Path, threading.RLock] = {}

    def _lock_for(self, root: str | Path) -> threading.RLock:
        try:
            canonical = Path(root).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("Workspace lease root must exist.") from exc
        if not canonical.is_dir():
            raise ValueError("Workspace lease root must be a directory.")
        with self._guard:
            return self._leases.setdefault(canonical, threading.RLock())

    @contextmanager
    def lease(self, root: str | Path):
        lock = self._lock_for(root)
        lock.acquire()
        try:
            yield
        finally:
            lock.release()


__all__ = ["WorkspaceLeaseManager"]
