"""Durable, lease-based background execution for BCS tasks.

SQLite provides the development queue adapter. Jobs are at-least-once: a process
can die after a worker changes files but before the job result is committed.
Task/workspace idempotency and operator review remain important.
"""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time
from uuid import uuid4


class ExecutionJobService:
    def __init__(self, store, scheduler, *, execution_handler=None, worker_id=None, lease_seconds=300,
                 poll_seconds=0.5):
        if not callable(getattr(store, "enqueue_execution_job", None)):
            raise TypeError("A durable execution-job store is required.")
        if not callable(getattr(scheduler, "dispatch_plan", None)):
            raise TypeError("A trusted task scheduler is required.")
        if not isinstance(lease_seconds, int) or not 15 <= lease_seconds <= 86400:
            raise ValueError("lease_seconds must be between 15 and 86400.")
        if not isinstance(poll_seconds, (int, float)) or not 0.05 <= poll_seconds <= 30:
            raise ValueError("poll_seconds must be between 0.05 and 30.")
        self.store = store
        self.scheduler = scheduler
        if execution_handler is not None and not callable(execution_handler):
            raise TypeError("execution_handler must be callable when provided.")
        self.execution_handler = execution_handler
        self.worker_id = worker_id or "bcs-job-worker-" + uuid4().hex
        self.lease_seconds = lease_seconds
        self.poll_seconds = float(poll_seconds)
        self._stop = threading.Event()
        self._thread = None

    def enqueue(self, task_ids, requested_by):
        job_id = "job_" + uuid4().hex
        job = self.store.enqueue_execution_job(
            job_id=job_id, task_ids=tuple(task_ids), requested_by=requested_by,
            now=datetime.now(timezone.utc),
        )
        self.start()
        return job

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="bcs-durable-job-runner", daemon=True
        )
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout))
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
        return stopped

    def run_once(self):
        now = datetime.now(timezone.utc)
        job = self.store.claim_next_execution_job(
            worker_id=self.worker_id, now=now, lease_seconds=self.lease_seconds
        )
        if job is None:
            return None
        heartbeat_stop = threading.Event()

        def heartbeat():
            interval = max(5.0, self.lease_seconds / 3)
            while not heartbeat_stop.wait(interval):
                try:
                    renewed = self.store.heartbeat_execution_job(
                        job_id=job["job_id"], worker_id=self.worker_id,
                        attempt=job["attempt"], now=datetime.now(timezone.utc),
                        lease_seconds=self.lease_seconds,
                    )
                    if not renewed:
                        return
                except Exception:
                    return

        beat = threading.Thread(target=heartbeat, name="bcs-job-heartbeat", daemon=True)
        beat.start()
        try:
            if self.execution_handler is not None:
                outcomes = self.execution_handler(job)
            else:
                result = self.scheduler.dispatch_plan(job["task_ids"])
                outcomes = [{
                    "task_id": item.task_id,
                    "state": item.state.value,
                    "worker_id": item.worker_id,
                    "reason": item.reason[:2000],
                    "result": None if item.result is None else {
                        "status": item.result.status.value,
                        "summary": item.result.summary[:12000],
                        "changed_files": list(item.result.changed_files)[:200],
                        "evidence_refs": list(item.result.evidence_refs)[:200],
                        "blockers": list(item.result.blockers)[:100],
                    },
                } for item in result.outcomes]
            if not isinstance(outcomes, list) or not outcomes or len(outcomes) > 100:
                raise ValueError("Execution handler returned an invalid outcome list.")
            completed = sum(item.get("state") == "completed" and item.get("qa", {}).get("status", "not_configured") not in {"blocked", "repair_required", "failed"} for item in outcomes)
            state = "completed" if completed == len(outcomes) else (
                "partial" if completed else "failed"
            )
            self.store.complete_execution_job(
                job_id=job["job_id"], worker_id=self.worker_id,
                attempt=job["attempt"], state=state, outcomes=outcomes,
                now=datetime.now(timezone.utc),
            )
        except Exception as exc:
            self.store.fail_execution_job(
                job_id=job["job_id"], worker_id=self.worker_id,
                attempt=job["attempt"],
                error=f"{type(exc).__name__}: execution failed; inspect task history.",
                now=datetime.now(timezone.utc),
            )
        finally:
            heartbeat_stop.set()
            beat.join(timeout=1)
        return job["job_id"]

    def _run(self):
        while not self._stop.is_set():
            try:
                job_id = self.run_once()
            except Exception:
                job_id = None
            if job_id is None:
                self._stop.wait(self.poll_seconds)


__all__ = ["ExecutionJobService"]
