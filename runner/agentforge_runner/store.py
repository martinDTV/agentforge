"""Persistent registry of executions (SQLite): idempotency and audit.

One row per execution id. ``claim`` is the atomic point of no duplication:
an ``INSERT`` that loses the race finds an existing row and the service
answers from it (result, in progress, conflict or uncertain). After a crash
every row still open is marked ``uncertain`` on startup and is never rerun
automatically. Rows hold the bounded result the API already received, never
the archive nor anything beyond it.
"""

import json
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .protocol import ExecutionState, ExecutionView, ProgramId, SandboxRequest, SandboxResult

Clock = Callable[[], datetime]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    step TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    program TEXT NOT NULL,
    actor_subject TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    workspace_sha256 TEXT NOT NULL,
    limits_json TEXT NOT NULL,
    state TEXT NOT NULL,
    container_name TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    result_json TEXT,
    resolution TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_executions_state ON executions(state);
CREATE INDEX IF NOT EXISTS ix_executions_run ON executions(run_id, created_at);
"""


class ExecutionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_id: UUID
    run_id: UUID
    project_id: str
    step: str
    attempt: int
    program: ProgramId
    actor_subject: str
    request_digest: str
    workspace_sha256: str
    limits_json: str
    state: ExecutionState
    container_name: str | None = None
    cancel_requested: bool = False
    error_code: str | None = None
    result: SandboxResult | None = None
    resolution: str | None = None  # how an uncertain row was closed and by what evidence
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def view(self) -> ExecutionView:
        return ExecutionView(
            execution_id=self.execution_id,
            state=self.state,
            program=self.program,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            cancel_requested=self.cancel_requested,
            error_code=self.error_code,
            resolution=self.resolution,
            container_name=self.container_name,
            result=self.result,
        )


def request_digest(request: SandboxRequest) -> str:
    import hashlib

    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ExecutionStore:
    def __init__(self, path: Path, *, clock: Clock | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._clock: Clock = clock or (lambda: datetime.now(UTC))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- lifecycle ------------------------------------------------------------------------

    def claim(self, request: SandboxRequest, digest: str) -> tuple[bool, ExecutionRecord]:
        """Insert the row as ``pending``; False with the existing row when it already exists."""
        now = self._clock().isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO executions (execution_id, run_id, project_id, step, "
                "attempt, program, actor_subject, request_digest, workspace_sha256, limits_json, "
                "state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
                (
                    str(request.execution_id),
                    str(request.run_id),
                    request.project_id,
                    request.step,
                    request.attempt,
                    request.program.value,
                    request.actor_subject,
                    digest,
                    request.workspace_sha256,
                    request.limits.model_dump_json(),
                    now,
                ),
            )
            claimed = cursor.rowcount == 1
            record = self.get(request.execution_id)
            assert record is not None
            return claimed, record

    def mark_running(self, execution_id: UUID, container_name: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE executions SET state='running', container_name=?, started_at=? "
                "WHERE execution_id=? AND state='pending'",
                (container_name, self._clock().isoformat(), str(execution_id)),
            )
            return cursor.rowcount == 1

    def finish(
        self, execution_id: UUID, result: SandboxResult, *, error_code: str | None = None
    ) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE executions SET state='finished', result_json=?, error_code=?, "
                "finished_at=? WHERE execution_id=? AND state IN ('pending', 'running')",
                (
                    result.model_dump_json(),
                    error_code,
                    self._clock().isoformat(),
                    str(execution_id),
                ),
            )
            return cursor.rowcount == 1

    def request_cancel(self, execution_id: UUID) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE executions SET cancel_requested=1 "
                "WHERE execution_id=? AND state IN ('pending', 'running')",
                (str(execution_id),),
            )
            return cursor.rowcount == 1

    def mark_uncertain(self, execution_id: UUID, reason: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE executions SET state='uncertain', error_code=?, finished_at=? "
                "WHERE execution_id=? AND state IN ('pending', 'running')",
                (reason, self._clock().isoformat(), str(execution_id)),
            )
            return cursor.rowcount == 1

    def resolve(
        self,
        execution_id: UUID,
        result: SandboxResult,
        *,
        resolution: str,
        error_code: str | None,
    ) -> bool:
        """Close an ``uncertain`` row with engine evidence or an operator's failure verdict."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE executions SET state='finished', result_json=?, error_code=?, "
                "resolution=?, finished_at=? WHERE execution_id=? AND state='uncertain'",
                (
                    result.model_dump_json(),
                    error_code,
                    resolution,
                    self._clock().isoformat(),
                    str(execution_id),
                ),
            )
            return cursor.rowcount == 1

    def list_by_state(self, state: ExecutionState, *, limit: int = 100) -> list[ExecutionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM executions WHERE state=? ORDER BY created_at DESC LIMIT ?",
                (state, limit),
            ).fetchall()
        return [_to_record(row) for row in rows]

    # -- reads ----------------------------------------------------------------------------

    def get(self, execution_id: UUID) -> ExecutionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM executions WHERE execution_id=?", (str(execution_id),)
            ).fetchone()
        return _to_record(row) if row is not None else None

    def open_executions(self) -> list[ExecutionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM executions WHERE state IN ('pending', 'running') ORDER BY created_at"
            ).fetchall()
        return [_to_record(row) for row in rows]


def _to_record(row: sqlite3.Row) -> ExecutionRecord:
    result_json = row["result_json"]
    return ExecutionRecord(
        execution_id=UUID(row["execution_id"]),
        run_id=UUID(row["run_id"]),
        project_id=row["project_id"],
        step=row["step"],
        attempt=row["attempt"],
        program=ProgramId(row["program"]),
        actor_subject=row["actor_subject"],
        request_digest=row["request_digest"],
        workspace_sha256=row["workspace_sha256"],
        limits_json=row["limits_json"],
        state=row["state"],
        container_name=row["container_name"],
        cancel_requested=bool(row["cancel_requested"]),
        error_code=row["error_code"],
        result=SandboxResult.model_validate_json(result_json) if result_json else None,
        resolution=row["resolution"],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
    )
