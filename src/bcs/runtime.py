"""Application composition root for the BCS core services.

This module wires durable storage, orchestration, and human approvals into one
lifecycle. It does not start an HTTP server or connect model providers yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import os
from pathlib import Path

from .approvals import ApprovalService
from .orchestrator import Orchestrator
from .storage import SQLiteOrchestrationStore


@dataclass
class BCSRuntime:
    """Owned application services and their shared persistence connection."""

    store: SQLiteOrchestrationStore
    orchestrator: Orchestrator
    approvals: ApprovalService

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "BCSRuntime":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def create_runtime(
    database_path: str | Path | None = None,
    *,
    approval_ttl_seconds: int = 900,
    max_attempts: int = 2,
) -> BCSRuntime:
    """Create a configured runtime, sharing one durable store across services.

    BCS_DATABASE_PATH may override the default ./data/bcs.sqlite3 location.
    Explicit arguments take precedence over environment configuration.
    """
    if (
        isinstance(approval_ttl_seconds, bool)
        or not isinstance(approval_ttl_seconds, int)
        or approval_ttl_seconds <= 0
    ):
        raise ValueError("approval_ttl_seconds must be a positive integer.")

    configured_path = database_path or os.environ.get("BCS_DATABASE_PATH")
    resolved_path = Path(configured_path or "./data/bcs.sqlite3")
    store = SQLiteOrchestrationStore(resolved_path)
    try:
        orchestrator = Orchestrator(max_attempts=max_attempts, store=store)
        approvals = ApprovalService(
            ttl=timedelta(seconds=approval_ttl_seconds), store=store
        )
        return BCSRuntime(
            store=store,
            orchestrator=orchestrator,
            approvals=approvals,
        )
    except Exception:
        store.close()
        raise


__all__ = ["BCSRuntime", "create_runtime"]
