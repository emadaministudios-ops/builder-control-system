"""Evidence-driven QA feedback-cycle policy.

The controller decides whether work needs repair, another verification pass,
human attention, or can be considered verified. It does not execute workers.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading

from .qa import QAReport, QACheckStatus


class FeedbackState(str, Enum):
    VERIFY_AGAIN = "verify_again"
    REPAIR_REQUIRED = "repair_required"
    VERIFIED = "verified"
    HUMAN_ATTENTION = "human_attention"
    CYCLE_BUDGET_EXHAUSTED = "cycle_budget_exhausted"


@dataclass(frozen=True)
class FeedbackCycle:
    task_id: str
    qa_run_id: str
    cycle_number: int
    report_status: QACheckStatus
    state: FeedbackState
    instruction: str


@dataclass(frozen=True)
class FeedbackSnapshot:
    task_id: str
    cycles: tuple[FeedbackCycle, ...]
    state: FeedbackState
    cycles_remaining: int


class FeedbackController:
    """Apply minimum verification and maximum repair-cycle rules per task.

    A cycle is one distinct QA report. Replaying the same QA run is idempotent.
    Passing before the minimum cycle count schedules another independent review.
    Blocked/error reports require human/infrastructure attention immediately.
    Failed reports request repair until the configured cycle budget is exhausted.
    """

    def __init__(self, *, minimum_cycles: int = 2, maximum_cycles: int = 5, store=None):
        if (
            isinstance(minimum_cycles, bool) or not isinstance(minimum_cycles, int)
            or minimum_cycles < 1
        ):
            raise ValueError("minimum_cycles must be a positive integer.")
        if (
            isinstance(maximum_cycles, bool) or not isinstance(maximum_cycles, int)
            or maximum_cycles < minimum_cycles or maximum_cycles > 20
        ):
            raise ValueError("maximum_cycles must be between minimum_cycles and 20.")
        if store is not None and not all(
            callable(getattr(store, name, None))
            for name in ("list_feedback_cycles", "save_feedback_cycle")
        ):
            raise TypeError("store must implement the feedback-cycle repository contract.")
        self.minimum_cycles = minimum_cycles
        self.maximum_cycles = maximum_cycles
        self._store = store
        self._cycles: dict[str, list[FeedbackCycle]] = {}
        self._lock = threading.RLock()
        if store is not None:
            for cycle in store.list_feedback_cycles():
                self._cycles.setdefault(cycle.task_id, []).append(cycle)
            for task_id in self._cycles:
                self._cycles[task_id].sort(key=lambda item: item.cycle_number)

    def record_qa_report(self, report: QAReport) -> FeedbackSnapshot:
        if not isinstance(report, QAReport):
            raise TypeError("A validated QAReport is required.")
        if not isinstance(report.task_id, str) or not report.task_id.strip():
            raise ValueError("QA report task ID is required.")
        if not isinstance(report.qa_run_id, str) or not report.qa_run_id.strip():
            raise ValueError("QA report run ID is required.")

        with self._lock:
            existing = self._cycles.get(report.task_id, [])
            for item in existing:
                if item.qa_run_id == report.qa_run_id:
                    return self._snapshot(report.task_id, existing)

            number = len(existing) + 1
            if report.status in {QACheckStatus.BLOCKED, QACheckStatus.ERROR}:
                state = FeedbackState.HUMAN_ATTENTION
                instruction = (
                    "QA could not establish a reliable result. Resolve the approval, "
                    "infrastructure, or execution blocker before claiming completion."
                )
            elif report.status == QACheckStatus.FAILED:
                if number >= self.maximum_cycles:
                    state = FeedbackState.CYCLE_BUDGET_EXHAUSTED
                    instruction = (
                        "The configured feedback-cycle budget is exhausted. Preserve "
                        "the remaining findings and request human direction; do not "
                        "mark the task complete."
                    )
                else:
                    state = FeedbackState.REPAIR_REQUIRED
                    instruction = (
                        "Repair the failed acceptance checks using QA evidence. "
                        "Address root causes, add or update coverage, then request a "
                        "fresh QA run. Do not claim the failed checks passed."
                    )
            elif number < self.minimum_cycles:
                state = FeedbackState.VERIFY_AGAIN
                instruction = (
                    "This QA pass found no reported failures, but the minimum number "
                    "of independent verification cycles has not been reached. Run "
                    "another meaningful review with fresh evidence."
                )
            else:
                state = FeedbackState.VERIFIED
                instruction = (
                    "Minimum verification cycles are complete and this QA report "
                    "contains no failed, blocked, or errored checks. Preserve the "
                    "evidence and proceed to independent release review."
                )

            cycle = FeedbackCycle(
                task_id=report.task_id,
                qa_run_id=report.qa_run_id,
                cycle_number=number,
                report_status=report.status,
                state=state,
                instruction=instruction,
            )
            if self._store is not None:
                self._store.save_feedback_cycle(cycle)
            updated = [*existing, cycle]
            self._cycles[report.task_id] = updated
            return self._snapshot(report.task_id, updated)

    def get_snapshot(self, task_id: str) -> FeedbackSnapshot | None:
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("Task ID is required.")
        with self._lock:
            cycles = self._cycles.get(task_id)
            return None if cycles is None else self._snapshot(task_id, cycles)

    def _snapshot(self, task_id: str, cycles: list[FeedbackCycle]) -> FeedbackSnapshot:
        latest = cycles[-1]
        return FeedbackSnapshot(
            task_id=task_id,
            cycles=tuple(cycles),
            state=latest.state,
            cycles_remaining=max(0, self.maximum_cycles - len(cycles)),
        )


__all__ = [
    "FeedbackState", "FeedbackCycle", "FeedbackSnapshot", "FeedbackController",
]
