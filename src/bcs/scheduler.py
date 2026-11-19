"""Bounded parallel dispatch for independent BCS tasks.

This scheduler bounds concurrent model/worker calls. Callers must only submit
tasks that are independent at this stage; dependency-graph scheduling is a later
layer. Workspace isolation is supplied by each worker's trusted workspace factory.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading

from .orchestrator import DispatchOutcome, Orchestrator


@dataclass(frozen=True)
class SchedulerResult:
    outcomes: tuple[DispatchOutcome, ...]
    requested: int
    concurrency_limit: int


class TaskScheduler:
    """Dispatch an explicitly selected batch with a strict concurrency ceiling."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        *,
        max_concurrency: int = 2,
        max_batch_size: int = 100,
    ) -> None:
        if not isinstance(orchestrator, Orchestrator):
            raise TypeError("A trusted Orchestrator instance is required.")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 1 <= max_concurrency <= 64
        ):
            raise ValueError("max_concurrency must be between 1 and 64.")
        if (
            isinstance(max_batch_size, bool)
            or not isinstance(max_batch_size, int)
            or not 1 <= max_batch_size <= 1000
        ):
            raise ValueError("max_batch_size must be between 1 and 1000.")
        self.orchestrator = orchestrator
        self.max_concurrency = max_concurrency
        self.max_batch_size = max_batch_size
        self._dispatch_lock = threading.Lock()

    def dispatch_independent(self, task_ids) -> SchedulerResult:
        """Dispatch a caller-verified independent batch, preserving input order."""
        if isinstance(task_ids, (str, bytes)):
            raise ValueError("Task IDs must be a sequence, not a string.")
        try:
            selected = tuple(task_ids)
        except TypeError as exc:
            raise ValueError("Task IDs must be an iterable of strings.") from exc
        if len(selected) > self.max_batch_size:
            raise ValueError("Dispatch batch exceeds the configured queue limit.")
        if any(not isinstance(item, str) or not item.strip() for item in selected):
            raise ValueError("Every task ID must be a non-empty string.")
        if len(set(selected)) != len(selected):
            raise ValueError("A task may appear only once in a dispatch batch.")
        if not selected:
            return SchedulerResult((), 0, self.max_concurrency)

        # Prevent overlapping batches through the same scheduler instance. A
        # second caller can queue separately after the active batch completes.
        with self._dispatch_lock:
            with ThreadPoolExecutor(
                max_workers=min(self.max_concurrency, len(selected)),
                thread_name_prefix="bcs-worker",
            ) as pool:
                futures = [pool.submit(self.orchestrator.dispatch, task_id) for task_id in selected]
                outcomes = tuple(future.result() for future in futures)
        return SchedulerResult(outcomes, len(selected), self.max_concurrency)


__all__ = ["SchedulerResult", "TaskScheduler"]
