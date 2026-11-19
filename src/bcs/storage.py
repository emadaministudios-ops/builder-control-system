"""SQLite persistence adapter for BCS orchestration state.

SQLite is the initial local/development adapter. Production deployments should
use a PostgreSQL implementation of the same repository contract. Only explicit,
versioned JSON fields are stored; Python pickle and executable object loading
are deliberately not used.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import threading
from typing import Iterable, TYPE_CHECKING

if TYPE_CHECKING:
    from .orchestrator import TaskRecord
    from .qa_handoff import QAEscalation
    from .approvals import ApprovalRequest
    from .feedback import FeedbackCycle

_SCHEMA_VERSION = 10


class StorageError(RuntimeError):
    """Raised when persisted orchestration data cannot be safely stored/read."""


class SQLiteOrchestrationStore:
    """Thread-safe SQLite repository for task records and QA escalations."""

    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        if not self.database:
            raise ValueError("A database path is required.")
        if self.database != ":memory:":
            Path(self.database).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database, timeout=10, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 10000")
        if self.database != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            version = self._connection.execute("PRAGMA user_version").fetchone()[0]
            if version > _SCHEMA_VERSION:
                raise StorageError(
                    f"Database schema version {version} is newer than supported "
                    f"version {_SCHEMA_VERSION}."
                )
            try:
                if version == 0:
                    with self._connection:
                        self._connection.execute("""
                            CREATE TABLE IF NOT EXISTS orchestration_tasks (
                                task_id TEXT PRIMARY KEY,
                                state TEXT NOT NULL,
                                created_at TEXT NOT NULL,
                                updated_at TEXT NOT NULL,
                                payload TEXT NOT NULL CHECK (json_valid(payload))
                            )
                        """)
                        self._connection.execute("""
                            CREATE TABLE IF NOT EXISTS qa_escalations (
                                escalation_id TEXT PRIMARY KEY,
                                parent_task_id TEXT NOT NULL,
                                payload TEXT NOT NULL CHECK (json_valid(payload))
                            )
                        """)
                        self._connection.execute("""
                            CREATE TABLE IF NOT EXISTS approval_requests (
                                request_id TEXT PRIMARY KEY,
                                status TEXT NOT NULL,
                                expires_at TEXT NOT NULL,
                                payload TEXT NOT NULL CHECK (json_valid(payload))
                            )
                        """)
                        self._connection.execute("""
                            CREATE TABLE IF NOT EXISTS feedback_cycles (
                                task_id TEXT NOT NULL,
                                cycle_number INTEGER NOT NULL,
                                qa_run_id TEXT NOT NULL UNIQUE,
                                payload TEXT NOT NULL CHECK (json_valid(payload)),
                                PRIMARY KEY (task_id, cycle_number)
                            )
                        """)
                        self._connection.execute(
                            f"PRAGMA user_version = {_SCHEMA_VERSION}"
                        )
                elif version == 1:
                    with self._connection:
                        self._connection.execute("""
                            CREATE TABLE IF NOT EXISTS approval_requests (
                                request_id TEXT PRIMARY KEY,
                                status TEXT NOT NULL,
                                expires_at TEXT NOT NULL,
                                payload TEXT NOT NULL CHECK (json_valid(payload))
                            )
                        """)
                        self._connection.execute("""
                            CREATE TABLE IF NOT EXISTS feedback_cycles (
                                task_id TEXT NOT NULL,
                                cycle_number INTEGER NOT NULL,
                                qa_run_id TEXT NOT NULL UNIQUE,
                                payload TEXT NOT NULL CHECK (json_valid(payload)),
                                PRIMARY KEY (task_id, cycle_number)
                            )
                        """)
                        self._connection.execute(
                            f"PRAGMA user_version = {_SCHEMA_VERSION}"
                        )
                elif version == 2:
                    with self._connection:
                        self._connection.execute("""
                            CREATE TABLE IF NOT EXISTS feedback_cycles (
                                task_id TEXT NOT NULL,
                                cycle_number INTEGER NOT NULL,
                                qa_run_id TEXT NOT NULL UNIQUE,
                                payload TEXT NOT NULL CHECK (json_valid(payload)),
                                PRIMARY KEY (task_id, cycle_number)
                            )
                        """)
                        self._connection.execute(
                            f"PRAGMA user_version = {_SCHEMA_VERSION}"
                        )
                with self._connection:
                    self._connection.execute("""
                        CREATE TABLE IF NOT EXISTS specialist_consultations (
                            consultation_id TEXT PRIMARY KEY,
                            task_id TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            payload TEXT NOT NULL CHECK (json_valid(payload))
                        )
                    """)
                    self._connection.execute("""
                        CREATE TABLE IF NOT EXISTS consultation_outcomes (
                            outcome_id TEXT PRIMARY KEY,
                            consultation_id TEXT NOT NULL,
                            task_id TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            payload TEXT NOT NULL CHECK (json_valid(payload)),
                            FOREIGN KEY (consultation_id)
                                REFERENCES specialist_consultations(consultation_id)
                        )
                    """)
                    self._connection.execute("""
                        CREATE TABLE IF NOT EXISTS integration_runs (
                            integration_id TEXT PRIMARY KEY,
                            status TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            payload TEXT NOT NULL CHECK (json_valid(payload))
                        )
                    """)
                    self._connection.execute("""
                        CREATE TABLE IF NOT EXISTS integration_qa_assessments (
                            integration_id TEXT NOT NULL,
                            qa_run_id TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            payload TEXT NOT NULL CHECK (json_valid(payload)),
                            PRIMARY KEY (integration_id, qa_run_id),
                            FOREIGN KEY (integration_id)
                                REFERENCES integration_runs(integration_id)
                        )
                    """)
                    self._connection.execute("""
                        CREATE TABLE IF NOT EXISTS integration_promotion_requests (
                            request_id TEXT PRIMARY KEY,
                            integration_id TEXT NOT NULL,
                            status TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            payload TEXT NOT NULL CHECK (json_valid(payload)),
                            FOREIGN KEY (integration_id)
                                REFERENCES integration_runs(integration_id)
                        )
                    """)
                    self._connection.execute("""
                        CREATE TABLE IF NOT EXISTS execution_jobs (
                            job_id TEXT PRIMARY KEY,
                            requested_by TEXT NOT NULL,
                            state TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            lease_owner TEXT,
                            lease_expires_at TEXT,
                            attempt INTEGER NOT NULL DEFAULT 0,
                            payload TEXT NOT NULL CHECK (json_valid(payload))
                        )
                    """)
                    self._connection.execute(
                        f"PRAGMA user_version = {_SCHEMA_VERSION}"
                    )
            except sqlite3.Error as exc:
                raise StorageError("Could not initialize the BCS database.") from exc

    @staticmethod
    def _timestamp(value: datetime) -> str:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise StorageError("Persisted timestamps must be timezone-aware.")
        return value.isoformat()

    @staticmethod
    def _json(payload: dict) -> str:
        try:
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                              allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise StorageError("Record contains values that are not safe JSON.") from exc

    @staticmethod
    def _task_payload(record: "TaskRecord") -> dict:
        task = record.task
        result = record.result
        return {
            "version": 1,
            "task": {
                "task_id": task.task_id,
                "parent_task_id": task.parent_task_id,
                "role": task.role,
                "objective": task.objective,
                "acceptance_criteria": list(task.acceptance_criteria),
                "evidence_refs": list(task.evidence_refs),
                "attempt": task.attempt,
                "dependency_ids": list(task.dependency_ids),
                "workspace_group_id": task.workspace_group_id,
            },
            "state": record.state.value,
            "assigned_worker_id": record.assigned_worker_id,
            "result": None if result is None else {
                "status": result.status.value,
                "summary": result.summary,
                "changed_files": list(result.changed_files),
                "evidence_refs": list(result.evidence_refs),
                "blockers": list(result.blockers),
                "consultation_disposition": result.consultation_disposition,
                "consultation_reason": result.consultation_reason,
            },
            "created_at": SQLiteOrchestrationStore._timestamp(record.created_at),
            "updated_at": SQLiteOrchestrationStore._timestamp(record.updated_at),
        }

    @staticmethod
    def _escalation_payload(item: "QAEscalation") -> dict:
        return {
            "version": 1,
            "escalation_id": item.escalation_id,
            "parent_task_id": item.parent_task_id,
            "source_qa_run_id": item.source_qa_run_id,
            "source_check_id": item.source_check_id,
            "reason": item.reason,
            "evidence_refs": list(item.evidence_refs),
        }

    @staticmethod
    def _decode_task(payload: str):
        from .orchestrator import (
            DispatchOutcome, TaskRecord, TaskState, WorkerResult,
            WorkerResultStatus, WorkerTask,
        )
        del DispatchOutcome  # The persisted contract intentionally excludes outcomes.
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported task record version.")
            raw = data["task"]
            task = WorkerTask(
                task_id=raw["task_id"],
                parent_task_id=raw["parent_task_id"],
                role=raw["role"],
                objective=raw["objective"],
                acceptance_criteria=tuple(raw["acceptance_criteria"]),
                evidence_refs=tuple(raw["evidence_refs"]),
                attempt=raw["attempt"],
                dependency_ids=tuple(raw.get("dependency_ids", ())),
                workspace_group_id=raw.get("workspace_group_id", ""),
            )
            raw_result = data["result"]
            result = None if raw_result is None else WorkerResult(
                status=WorkerResultStatus(raw_result["status"]),
                summary=raw_result["summary"],
                changed_files=tuple(raw_result["changed_files"]),
                evidence_refs=tuple(raw_result["evidence_refs"]),
                blockers=tuple(raw_result["blockers"]),
                consultation_disposition=raw_result.get("consultation_disposition", "unknown"),
                consultation_reason=raw_result.get("consultation_reason", ""),
            )
            created = datetime.fromisoformat(data["created_at"])
            updated = datetime.fromisoformat(data["updated_at"])
            if created.tzinfo is None or updated.tzinfo is None:
                raise ValueError("Naive timestamps are not accepted.")
            return TaskRecord(
                task=task,
                state=TaskState(data["state"]),
                assigned_worker_id=data["assigned_worker_id"],
                result=result,
                created_at=created,
                updated_at=updated,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError("Stored task record is invalid or unsupported.") from exc

    @staticmethod
    def _decode_escalation(payload: str):
        from .qa_handoff import QAEscalation
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported escalation record version.")
            return QAEscalation(
                escalation_id=data["escalation_id"],
                parent_task_id=data["parent_task_id"],
                source_qa_run_id=data["source_qa_run_id"],
                source_check_id=data["source_check_id"],
                reason=data["reason"],
                evidence_refs=tuple(data["evidence_refs"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError("Stored QA escalation is invalid or unsupported.") from exc

    def save_state(
        self,
        tasks: Iterable["TaskRecord"],
        escalations: Iterable["QAEscalation"],
    ) -> None:
        """Atomically persist a snapshot; a failed write leaves the prior state intact."""
        task_rows = []
        for record in tasks:
            payload = self._task_payload(record)
            task_rows.append((
                record.task.task_id, record.state.value,
                payload["created_at"], payload["updated_at"], self._json(payload),
            ))
        escalation_rows = []
        for item in escalations:
            payload = self._escalation_payload(item)
            escalation_rows.append((
                item.escalation_id, item.parent_task_id, self._json(payload)
            ))
        with self._lock:
            try:
                with self._connection:
                    self._connection.executemany("""
                        INSERT INTO orchestration_tasks
                            (task_id, state, created_at, updated_at, payload)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(task_id) DO UPDATE SET
                            state=excluded.state,
                            updated_at=excluded.updated_at,
                            payload=excluded.payload
                    """, task_rows)
                    self._connection.executemany("""
                        INSERT INTO qa_escalations
                            (escalation_id, parent_task_id, payload)
                        VALUES (?, ?, ?)
                        ON CONFLICT(escalation_id) DO UPDATE SET
                            parent_task_id=excluded.parent_task_id,
                            payload=excluded.payload
                    """, escalation_rows)
            except sqlite3.Error as exc:
                raise StorageError("Could not commit orchestration state.") from exc

    def save_integration_run(self, record: dict) -> None:
        """Persist an immutable integration result; duplicate IDs are rejected."""
        required = {"integration_id", "branch", "path", "status", "included_groups", "commits", "conflicts", "reason", "commit_sha"}
        if not isinstance(record, dict) or set(record) != required:
            raise StorageError("Integration record has an invalid shape.")
        if any(not isinstance(record[key], str) or not record[key] for key in ("integration_id", "branch", "path", "status", "commit_sha")):
            raise StorageError("Integration identity and status fields are required.")
        for key in ("included_groups", "commits", "conflicts"):
            value = record[key]
            if not isinstance(value, (tuple, list)) or any(not isinstance(item, str) or not item for item in value):
                raise StorageError("Integration collections must contain non-empty strings.")
        if not isinstance(record["reason"], str) or len(record["reason"]) > 4000:
            raise StorageError("Integration reason is invalid.")
        payload = {"version": 1, **record,
                   "included_groups": list(record["included_groups"]),
                   "commits": list(record["commits"]),
                   "conflicts": list(record["conflicts"])}
        encoded = self._json(payload)
        created_at = datetime.now().astimezone().isoformat()
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute(
                        "INSERT INTO integration_runs (integration_id,status,created_at,payload) VALUES (?,?,?,?)",
                        (record["integration_id"], record["status"], created_at, encoded),
                    )
            except sqlite3.IntegrityError as exc:
                raise StorageError("Integration ID already exists; refusing duplicate integration record.") from exc
            except sqlite3.Error as exc:
                raise StorageError("Could not persist integration result.") from exc

    def list_integration_runs(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM integration_runs ORDER BY created_at, integration_id"
            ).fetchall()
        records = []
        for row in rows:
            try:
                data = json.loads(row["payload"])
                if not isinstance(data, dict) or data.get("version") != 1:
                    raise ValueError("Unsupported integration record.")
                data.pop("version")
                data["included_groups"] = tuple(data["included_groups"])
                data["commits"] = tuple(data["commits"])
                data["conflicts"] = tuple(data["conflicts"])
                records.append(data)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StorageError("Stored integration record is invalid.") from exc
        return tuple(records)


    def save_integration_qa_assessment(self, record: dict) -> None:
        """Append an immutable QA assessment bound to an integration commit."""
        required = {
            "integration_id", "commit_sha", "qa_run_id", "task_id", "status",
            "executed_check_count", "evidence_refs", "checks", "started_at",
            "finished_at",
        }
        if not isinstance(record, dict) or set(record) != required:
            raise StorageError("Integration QA assessment has an invalid shape.")
        for key in ("integration_id", "commit_sha", "qa_run_id", "task_id", "status"):
            if not isinstance(record[key], str) or not record[key]:
                raise StorageError("Integration QA identity fields are required.")
        if record["status"] not in {"passed", "failed", "blocked", "error", "not_assessed"}:
            raise StorageError("Unsupported integration QA status.")
        count = record["executed_check_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise StorageError("Executed check count must be a non-negative integer.")
        if record["status"] == "passed" and count < 1:
            raise StorageError("A QA pass requires at least one executed check.")
        for key in ("evidence_refs", "checks"):
            if not isinstance(record[key], (tuple, list)):
                raise StorageError("QA evidence and checks must be arrays.")
        if any(not isinstance(item, str) or not item for item in record["evidence_refs"]):
            raise StorageError("QA evidence references must be non-empty strings.")
        if not isinstance(record["started_at"], str) or not isinstance(record["finished_at"], str):
            raise StorageError("QA timestamps must be serialized strings.")
        payload = {"version": 1, **record,
                   "evidence_refs": list(record["evidence_refs"]),
                   "checks": list(record["checks"])}
        encoded = self._json(payload)
        created_at = datetime.now().astimezone().isoformat()
        with self._lock:
            try:
                with self._connection:
                    existing = self._connection.execute(
                        "SELECT payload FROM integration_qa_assessments "
                        "WHERE integration_id = ? AND qa_run_id = ?",
                        (record["integration_id"], record["qa_run_id"]),
                    ).fetchone()
                    if existing is not None:
                        if existing["payload"] != encoded:
                            raise StorageError("QA run ID is already bound to different assessment data.")
                        return
                    integration = self._connection.execute(
                        "SELECT payload FROM integration_runs WHERE integration_id = ?",
                        (record["integration_id"],),
                    ).fetchone()
                    if integration is None:
                        raise StorageError("Cannot attach QA to an unknown integration.")
                    integration_data = json.loads(integration["payload"])
                    if integration_data.get("commit_sha") != record["commit_sha"]:
                        raise StorageError("QA assessment commit does not match the integration commit.")
                    self._connection.execute(
                        "INSERT INTO integration_qa_assessments "
                        "(integration_id, qa_run_id, created_at, payload) VALUES (?, ?, ?, ?)",
                        (record["integration_id"], record["qa_run_id"], created_at, encoded),
                    )
            except sqlite3.IntegrityError as exc:
                raise StorageError("QA assessment references an unknown integration or duplicate run.") from exc
            except sqlite3.Error as exc:
                raise StorageError("Could not persist integration QA assessment.") from exc

    def list_integration_qa_assessments(self, integration_id: str):
        if not isinstance(integration_id, str) or not integration_id:
            raise StorageError("Integration ID is required.")
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM integration_qa_assessments "
                "WHERE integration_id = ? ORDER BY created_at, qa_run_id",
                (integration_id,),
            ).fetchall()
        records = []
        for row in rows:
            try:
                data = json.loads(row["payload"])
                if not isinstance(data, dict) or data.get("version") != 1:
                    raise ValueError("Unsupported integration QA assessment.")
                data.pop("version")
                data["evidence_refs"] = tuple(data["evidence_refs"])
                data["checks"] = tuple(data["checks"])
                records.append(data)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StorageError("Stored integration QA assessment is invalid.") from exc
        return tuple(records)


    def save_integration_promotion_request(self, record: dict) -> None:
        required = {"request_id", "integration_id", "commit_sha", "target_branch", "target_base_sha", "qa_run_id", "task_id"}
        if not isinstance(record, dict) or set(record) != required:
            raise StorageError("Promotion request has an invalid shape.")
        if any(not isinstance(record[key], str) or not record[key] for key in required):
            raise StorageError("Promotion request identity fields are required.")
        if record["target_branch"] != "development":
            raise StorageError("Only development may be a promotion target.")
        payload = {"version": 1, **record, "status": "pending", "promoted_at": None, "promoted_by": None}
        encoded = self._json(payload)
        created_at = datetime.now().astimezone().isoformat()
        with self._lock:
            try:
                with self._connection:
                    integration = self._connection.execute(
                        "SELECT payload FROM integration_runs WHERE integration_id = ?",
                        (record["integration_id"],),
                    ).fetchone()
                    if integration is None:
                        raise StorageError("Promotion request references an unknown integration.")
                    data = json.loads(integration["payload"])
                    if data.get("commit_sha") != record["commit_sha"]:
                        raise StorageError("Promotion request commit does not match integration.")
                    self._connection.execute(
                        "INSERT INTO integration_promotion_requests (request_id,integration_id,status,created_at,payload) VALUES (?,?,?,?,?)",
                        (record["request_id"], record["integration_id"], "pending", created_at, encoded),
                    )
            except sqlite3.IntegrityError as exc:
                raise StorageError("Promotion request ID already exists.") from exc
            except sqlite3.Error as exc:
                raise StorageError("Could not persist promotion request.") from exc

    def get_integration_promotion_request(self, request_id: str):
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM integration_promotion_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row["payload"])
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported promotion request.")
            data.pop("version")
            return data
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError("Stored promotion request is invalid.") from exc

    def list_integration_promotion_requests(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM integration_promotion_requests ORDER BY created_at, request_id"
            ).fetchall()
        records = []
        for row in rows:
            try:
                data = json.loads(row["payload"])
                if not isinstance(data, dict) or data.get("version") != 1:
                    raise ValueError("Unsupported promotion request.")
                data.pop("version")
                records.append(data)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StorageError("Stored promotion request is invalid.") from exc
        return tuple(records)

    def mark_integration_promotion_completed(self, request_id: str, *, promoted_by: str) -> dict:
        if not isinstance(promoted_by, str) or not promoted_by.strip():
            raise StorageError("Human promotion actor is required.")
        with self._lock:
            try:
                with self._connection:
                    row = self._connection.execute(
                        "SELECT payload FROM integration_promotion_requests WHERE request_id = ?",
                        (request_id,),
                    ).fetchone()
                    if row is None:
                        raise StorageError("Promotion request does not exist.")
                    data = json.loads(row["payload"])
                    if data.get("status") == "promoted":
                        return {key: value for key, value in data.items() if key != "version"}
                    if data.get("status") != "pending":
                        raise StorageError("Promotion request is not pending.")
                    data["status"] = "promoted"
                    data["promoted_at"] = datetime.now().astimezone().isoformat()
                    data["promoted_by"] = promoted_by
                    self._connection.execute(
                        "UPDATE integration_promotion_requests SET status = ?, payload = ? WHERE request_id = ?",
                        ("promoted", self._json(data), request_id),
                    )
                    return {key: value for key, value in data.items() if key != "version"}
            except sqlite3.Error as exc:
                raise StorageError("Could not record promotion completion.") from exc

    @staticmethod
    def _feedback_payload(record: "FeedbackCycle") -> dict:
        return {
            "version": 1,
            "task_id": record.task_id,
            "qa_run_id": record.qa_run_id,
            "cycle_number": record.cycle_number,
            "report_status": record.report_status.value,
            "state": record.state.value,
            "instruction": record.instruction,
        }

    @staticmethod
    def _decode_feedback(payload: str):
        from .feedback import FeedbackCycle, FeedbackState
        from .qa import QACheckStatus
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported feedback-cycle record version.")
            number = data["cycle_number"]
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise ValueError("Invalid feedback-cycle number.")
            return FeedbackCycle(
                task_id=data["task_id"],
                qa_run_id=data["qa_run_id"],
                cycle_number=number,
                report_status=QACheckStatus(data["report_status"]),
                state=FeedbackState(data["state"]),
                instruction=data["instruction"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError("Stored feedback-cycle record is invalid or unsupported.") from exc

    def save_feedback_cycle(self, record: "FeedbackCycle") -> None:
        payload = self._feedback_payload(record)
        encoded = self._json(payload)
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute("""
                        INSERT INTO feedback_cycles
                            (task_id, cycle_number, qa_run_id, payload)
                        VALUES (?, ?, ?, ?)
                    """, (
                        record.task_id, record.cycle_number,
                        record.qa_run_id, encoded,
                    ))
            except sqlite3.IntegrityError as exc:
                raise StorageError("Feedback cycle already exists; reload task history.") from exc
            except sqlite3.Error as exc:
                raise StorageError("Could not commit feedback-cycle record.") from exc

    def list_feedback_cycles(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM feedback_cycles ORDER BY task_id, cycle_number"
            ).fetchall()
        return tuple(self._decode_feedback(row["payload"]) for row in rows)

    @staticmethod
    def _approval_payload(record: "ApprovalRequest") -> dict:
        return {
            "version": 1,
            "request_id": record.request_id,
            "task_id": record.task_id,
            "worker_id": record.worker_id,
            "capability": record.capability,
            "request_fingerprint": record.request_fingerprint,
            "status": record.status.value,
            "created_at": SQLiteOrchestrationStore._timestamp(record.created_at),
            "expires_at": SQLiteOrchestrationStore._timestamp(record.expires_at),
            "decided_at": (
                None if record.decided_at is None
                else SQLiteOrchestrationStore._timestamp(record.decided_at)
            ),
            "decided_by": record.decided_by,
            "reason": record.reason,
        }

    @staticmethod
    def _decode_approval(payload: str):
        from .approvals import ApprovalRequest, ApprovalStatus
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported approval record version.")
            created = datetime.fromisoformat(data["created_at"])
            expires = datetime.fromisoformat(data["expires_at"])
            decided = (
                None if data["decided_at"] is None
                else datetime.fromisoformat(data["decided_at"])
            )
            if any(value.tzinfo is None for value in (created, expires)):
                raise ValueError("Naive approval timestamps are not accepted.")
            if decided is not None and decided.tzinfo is None:
                raise ValueError("Naive decision timestamp is not accepted.")
            return ApprovalRequest(
                request_id=data["request_id"],
                task_id=data["task_id"],
                worker_id=data["worker_id"],
                capability=data["capability"],
                request_fingerprint=data["request_fingerprint"],
                status=ApprovalStatus(data["status"]),
                created_at=created,
                expires_at=expires,
                decided_at=decided,
                decided_by=data["decided_by"],
                reason=data["reason"],
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError("Stored approval record is invalid or unsupported.") from exc

    def save_approval(self, record: "ApprovalRequest") -> None:
        payload = self._approval_payload(record)
        encoded = self._json(payload)
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute("""
                        INSERT INTO approval_requests
                            (request_id, status, expires_at, payload)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(request_id) DO UPDATE SET
                            status=excluded.status,
                            expires_at=excluded.expires_at,
                            payload=excluded.payload
                    """, (
                        record.request_id, record.status.value,
                        payload["expires_at"], encoded,
                    ))
            except sqlite3.Error as exc:
                raise StorageError("Could not commit approval record.") from exc

    def get_approval(self, request_id: str):
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM approval_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return None if row is None else self._decode_approval(row["payload"])

    def list_approvals(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM approval_requests ORDER BY expires_at, request_id"
            ).fetchall()
        return tuple(self._decode_approval(row["payload"]) for row in rows)


    @staticmethod
    def _consultation_payload(record) -> dict:
        return {
            "version": 1, "consultation_id": record.consultation_id,
            "task_id": record.task_id, "requesting_role": record.requesting_role,
            "should_consult": record.should_consult, "specialist_role": record.specialist_role,
            "question": record.question, "reason": record.reason, "advice": record.advice,
            "risks": list(record.risks), "next_steps": list(record.next_steps),
            "confidence": record.confidence,
            "created_at": SQLiteOrchestrationStore._timestamp(record.created_at),
        }

    @staticmethod
    def _decode_consultation(payload: str):
        from .consultation import ConsultationRecord
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported consultation record version.")
            created = datetime.fromisoformat(data["created_at"])
            if created.tzinfo is None:
                raise ValueError("Naive consultation timestamp is not accepted.")
            return ConsultationRecord(
                consultation_id=data["consultation_id"], task_id=data["task_id"],
                requesting_role=data["requesting_role"], should_consult=data["should_consult"],
                specialist_role=data["specialist_role"], question=data["question"],
                reason=data["reason"], advice=data["advice"], risks=tuple(data["risks"]),
                next_steps=tuple(data["next_steps"]), confidence=data["confidence"],
                created_at=created,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError("Stored consultation record is invalid or unsupported.") from exc

    def save_consultation(self, record) -> None:
        from .consultation import ConsultationRecord
        if not isinstance(record, ConsultationRecord):
            raise TypeError("A validated ConsultationRecord is required.")
        payload = self._consultation_payload(record)
        encoded = self._json(payload)
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute("""
                        INSERT INTO specialist_consultations
                            (consultation_id, task_id, created_at, payload)
                        VALUES (?, ?, ?, ?)
                    """, (record.consultation_id, record.task_id, payload["created_at"], encoded))
            except sqlite3.IntegrityError as exc:
                raise StorageError("Consultation record already exists.") from exc
            except sqlite3.Error as exc:
                raise StorageError("Could not commit consultation record.") from exc

    def list_consultations(self, task_id: str | None = None):
        with self._lock:
            if task_id is None:
                rows = self._connection.execute(
                    "SELECT payload FROM specialist_consultations ORDER BY created_at, consultation_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload FROM specialist_consultations WHERE task_id = ? ORDER BY created_at, consultation_id",
                    (task_id,),
                ).fetchall()
        return tuple(self._decode_consultation(row["payload"]) for row in rows)


    @staticmethod
    def _consultation_outcome_payload(record) -> dict:
        return {
            "version": 1, "outcome_id": record.outcome_id,
            "consultation_id": record.consultation_id, "task_id": record.task_id,
            "disposition": record.disposition, "worker_reason": record.worker_reason,
            "qa_status": record.qa_status, "qa_run_id": record.qa_run_id,
            "evidence_refs": list(record.evidence_refs),
            "created_at": SQLiteOrchestrationStore._timestamp(record.created_at),
        }

    @staticmethod
    def _decode_consultation_outcome(payload: str):
        from .consultation_feedback import ConsultationOutcome
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or data.get("version") != 1:
                raise ValueError("Unsupported consultation outcome version.")
            created = datetime.fromisoformat(data["created_at"])
            if created.tzinfo is None:
                raise ValueError("Naive consultation outcome timestamp is not accepted.")
            return ConsultationOutcome(
                outcome_id=data["outcome_id"], consultation_id=data["consultation_id"],
                task_id=data["task_id"], disposition=data["disposition"],
                worker_reason=data["worker_reason"], qa_status=data["qa_status"],
                qa_run_id=data["qa_run_id"], evidence_refs=tuple(data["evidence_refs"]),
                created_at=created,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StorageError("Stored consultation outcome is invalid or unsupported.") from exc

    def save_consultation_outcome(self, record) -> None:
        from .consultation_feedback import ConsultationOutcome
        if not isinstance(record, ConsultationOutcome):
            raise TypeError("A validated ConsultationOutcome is required.")
        payload = self._consultation_outcome_payload(record)
        encoded = self._json(payload)
        with self._lock:
            try:
                with self._connection:
                    self._connection.execute("""
                        INSERT INTO consultation_outcomes
                            (outcome_id, consultation_id, task_id, created_at, payload)
                        VALUES (?, ?, ?, ?, ?)
                    """, (record.outcome_id, record.consultation_id, record.task_id,
                          payload["created_at"], encoded))
            except sqlite3.IntegrityError as exc:
                raise StorageError("Consultation outcome already exists or references an unknown consultation.") from exc
            except sqlite3.Error as exc:
                raise StorageError("Could not commit consultation outcome.") from exc

    def list_consultation_outcomes(self, task_id: str | None = None):
        with self._lock:
            if task_id is None:
                rows = self._connection.execute(
                    "SELECT payload FROM consultation_outcomes ORDER BY created_at, outcome_id"
                ).fetchall()
            else:
                rows = self._connection.execute(
                    "SELECT payload FROM consultation_outcomes WHERE task_id = ? ORDER BY created_at, outcome_id",
                    (task_id,),
                ).fetchall()
        return tuple(self._decode_consultation_outcome(row["payload"]) for row in rows)

    def save_task(self, record: "TaskRecord") -> None:
        self.save_state((record,), ())

    def save_escalation(self, item: "QAEscalation") -> None:
        self.save_state((), (item,))

    def get_task(self, task_id: str):
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM orchestration_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return None if row is None else self._decode_task(row["payload"])

    def list_tasks(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM orchestration_tasks ORDER BY created_at, task_id"
            ).fetchall()
        return tuple(self._decode_task(row["payload"]) for row in rows)

    def list_escalations(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM qa_escalations ORDER BY escalation_id"
            ).fetchall()
        return tuple(self._decode_escalation(row["payload"]) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


__all__ = ["StorageError", "SQLiteOrchestrationStore"]
