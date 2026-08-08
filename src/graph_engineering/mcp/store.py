"""SQLite-backed graph task claims with lease and generation fencing."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MAX_IDENTIFIER = 128
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_RESULT_BYTES = 256 * 1024
MIN_LEASE_MS = 1_000
MAX_LEASE_MS = 15 * 60 * 1_000
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class TaskStoreError(ValueError):
    """A caller-visible task state or input error."""


class ClaimConflict(TaskStoreError):
    """A task is not currently claimable."""


class StaleClaim(TaskStoreError):
    """The claim lease or fencing generation is no longer valid."""


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    workflow_id: str
    node_id: str
    payload: dict[str, Any]
    state: str
    owner: str | None
    generation: int
    lease_expires_ms: int | None
    result: Any | None
    error: str | None
    created_ms: int
    updated_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Claim:
    task: TaskRecord
    owner: str
    generation: int
    lease_expires_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task.to_dict(),
            "owner": self.owner,
            "generation": self.generation,
            "lease_expires_ms": self.lease_expires_ms,
        }


class GraphTaskStore:
    """Durable task store whose writes are fenced by owner, generation, and lease."""

    def __init__(
        self, database: Path | str, *, clock_ms: Callable[[], int] | None = None
    ):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS graph_tasks (
                    task_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending','claimed','completed','failed','cancelled')
                    ),
                    owner TEXT,
                    generation INTEGER NOT NULL DEFAULT 0,
                    lease_expires_ms INTEGER,
                    result_json TEXT,
                    error TEXT,
                    created_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS graph_tasks_state_idx
                    ON graph_tasks(state, created_ms, task_id);
                """
            )

    def create(
        self, workflow_id: str, node_id: str, payload: Mapping[str, Any]
    ) -> TaskRecord:
        _check_identifier("workflow_id", workflow_id)
        _check_identifier("node_id", node_id)
        payload_json = _bounded_json("payload", dict(payload), MAX_PAYLOAD_BYTES)
        task_id = str(uuid.uuid4())
        now = self._clock_ms()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO graph_tasks
                   (task_id, workflow_id, node_id, payload_json, state, created_ms, updated_ms)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (task_id, workflow_id, node_id, payload_json, now, now),
            )
        return self.inspect(task_id)

    def inspect(self, task_id: str) -> TaskRecord:
        _check_task_id(task_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM graph_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise TaskStoreError("unknown task_id")
        return _record(row)

    def claim(self, task_id: str, owner: str, lease_ms: int) -> Claim:
        _check_task_id(task_id)
        _check_identifier("owner", owner)
        if not isinstance(lease_ms, int) or isinstance(lease_ms, bool):
            raise TaskStoreError("lease_ms must be an integer")
        if not MIN_LEASE_MS <= lease_ms <= MAX_LEASE_MS:
            raise TaskStoreError(
                f"lease_ms must be between {MIN_LEASE_MS} and {MAX_LEASE_MS}"
            )
        now = self._clock_ms()
        expires = now + lease_ms
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state, lease_expires_ms, generation FROM graph_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise TaskStoreError("unknown task_id")
            claimable = row["state"] == "pending" or (
                row["state"] == "claimed"
                and row["lease_expires_ms"] is not None
                and row["lease_expires_ms"] <= now
            )
            if not claimable:
                connection.rollback()
                raise ClaimConflict("task is not claimable")
            generation = int(row["generation"]) + 1
            connection.execute(
                """UPDATE graph_tasks SET state='claimed', owner=?, generation=?,
                   lease_expires_ms=?, updated_ms=? WHERE task_id=?""",
                (owner, generation, expires, now, task_id),
            )
            connection.commit()
        return Claim(self.inspect(task_id), owner, generation, expires)

    def heartbeat(
        self, task_id: str, owner: str, generation: int, lease_ms: int
    ) -> TaskRecord:
        if (
            not isinstance(lease_ms, int)
            or not MIN_LEASE_MS <= lease_ms <= MAX_LEASE_MS
        ):
            raise TaskStoreError(
                f"lease_ms must be between {MIN_LEASE_MS} and {MAX_LEASE_MS}"
            )
        now = self._clock_ms()
        return self._fenced_update(
            task_id,
            owner,
            generation,
            now,
            "UPDATE graph_tasks SET lease_expires_ms=?, updated_ms=? WHERE task_id=?",
            (now + lease_ms, now, task_id),
        )

    def complete(
        self, task_id: str, owner: str, generation: int, result: Any
    ) -> TaskRecord:
        result_json = _bounded_json("result", result, MAX_RESULT_BYTES)
        now = self._clock_ms()
        return self._fenced_update(
            task_id,
            owner,
            generation,
            now,
            """UPDATE graph_tasks SET state='completed', result_json=?, owner=NULL,
               lease_expires_ms=NULL, updated_ms=? WHERE task_id=?""",
            (result_json, now, task_id),
        )

    def fail(self, task_id: str, owner: str, generation: int, error: str) -> TaskRecord:
        if (
            not isinstance(error, str)
            or not error
            or len(error.encode()) > MAX_PAYLOAD_BYTES
        ):
            raise TaskStoreError(
                "error must be a non-empty string no larger than 65536 bytes"
            )
        now = self._clock_ms()
        return self._fenced_update(
            task_id,
            owner,
            generation,
            now,
            """UPDATE graph_tasks SET state='failed', error=?, owner=NULL,
               lease_expires_ms=NULL, updated_ms=? WHERE task_id=?""",
            (error, now, task_id),
        )

    def cancel(self, task_id: str, owner: str, generation: int) -> TaskRecord:
        now = self._clock_ms()
        return self._fenced_update(
            task_id,
            owner,
            generation,
            now,
            """UPDATE graph_tasks SET state='cancelled', owner=NULL,
               lease_expires_ms=NULL, updated_ms=? WHERE task_id=?""",
            (now, task_id),
        )

    def request_protocol_cancel(self, task_id: str) -> TaskRecord:
        """Cancel an MCP Task handle; does not bypass completed terminal state."""
        _check_task_id(task_id)
        now = self._clock_ms()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM graph_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise TaskStoreError("unknown task_id")
            if row["state"] not in {"completed", "failed", "cancelled"}:
                connection.execute(
                    """UPDATE graph_tasks SET state='cancelled', owner=NULL,
                       lease_expires_ms=NULL, updated_ms=? WHERE task_id=?""",
                    (now, task_id),
                )
            connection.commit()
        return self.inspect(task_id)

    def _fenced_update(
        self,
        task_id: str,
        owner: str,
        generation: int,
        now: int,
        sql: str,
        params: tuple[Any, ...],
    ) -> TaskRecord:
        _check_task_id(task_id)
        _check_identifier("owner", owner)
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
        ):
            raise TaskStoreError("generation must be a positive integer")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT state, owner, generation, lease_expires_ms
                   FROM graph_tasks WHERE task_id=?""",
                (task_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise TaskStoreError("unknown task_id")
            valid = (
                row["state"] == "claimed"
                and row["owner"] == owner
                and row["generation"] == generation
                and row["lease_expires_ms"] is not None
                and row["lease_expires_ms"] > now
            )
            if not valid:
                connection.rollback()
                raise StaleClaim("claim owner, generation, state, or lease is stale")
            connection.execute(sql, params)
            connection.commit()
        return self.inspect(task_id)


def _check_identifier(label: str, value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise TaskStoreError(
            f"{label} must match {_IDENTIFIER.pattern} and be at most {MAX_IDENTIFIER} characters"
        )


def _check_task_id(value: str) -> None:
    try:
        uuid.UUID(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TaskStoreError("task_id must be a UUID") from exc


def _bounded_json(label: str, value: Any, limit: int) -> str:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise TaskStoreError(f"{label} must be JSON serializable") from exc
    if len(encoded.encode()) > limit:
        raise TaskStoreError(f"{label} exceeds {limit} bytes")
    return encoded


def _record(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"],
        workflow_id=row["workflow_id"],
        node_id=row["node_id"],
        payload=json.loads(row["payload_json"]),
        state=row["state"],
        owner=row["owner"],
        generation=row["generation"],
        lease_expires_ms=row["lease_expires_ms"],
        result=json.loads(row["result_json"])
        if row["result_json"] is not None
        else None,
        error=row["error"],
        created_ms=row["created_ms"],
        updated_ms=row["updated_ms"],
    )
