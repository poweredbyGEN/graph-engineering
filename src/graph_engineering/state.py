"""SQLite state, run-owner leases, and attempt fencing."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIGRATIONS = (
    """
    CREATE TABLE runs (
      id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, workflow_json TEXT NOT NULL,
      status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
      created_at REAL NOT NULL, updated_at REAL NOT NULL
    );
    CREATE TABLE nodes (
      run_id TEXT NOT NULL REFERENCES runs(id), node_id TEXT NOT NULL,
      status TEXT NOT NULL, required INTEGER NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
      last_digest TEXT, no_progress_count INTEGER NOT NULL DEFAULT 0, error TEXT,
      PRIMARY KEY (run_id, node_id)
    );
    CREATE TABLE attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, node_id TEXT NOT NULL,
      number INTEGER NOT NULL, status TEXT NOT NULL, digest TEXT, error TEXT,
      started_at REAL NOT NULL, finished_at REAL,
      UNIQUE (run_id, node_id, number)
    );
    CREATE TABLE artifacts (
      run_id TEXT NOT NULL, node_id TEXT NOT NULL, output_name TEXT NOT NULL,
      digest TEXT NOT NULL, schema_json TEXT NOT NULL,
      PRIMARY KEY (run_id, node_id, output_name)
    );
    """,
    """
    ALTER TABLE runs ADD COLUMN owner_token TEXT;
    ALTER TABLE runs ADD COLUMN owner_generation INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE runs ADD COLUMN lease_expires_at REAL;
    ALTER TABLE nodes ADD COLUMN active_attempt INTEGER;
    ALTER TABLE nodes ADD COLUMN active_generation INTEGER;
    ALTER TABLE attempts ADD COLUMN owner_generation INTEGER NOT NULL DEFAULT 0;
    """,
    """
    ALTER TABLE attempts ADD COLUMN failure_json TEXT;
    CREATE TABLE repair_routes (
      run_id TEXT NOT NULL, integration_node TEXT NOT NULL, route_id TEXT NOT NULL,
      rounds INTEGER NOT NULL DEFAULT 0, last_failure_digest TEXT,
      no_progress_count INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (run_id, integration_node, route_id)
    );
    CREATE TABLE repair_inputs (
      run_id TEXT NOT NULL, node_id TEXT NOT NULL, input_name TEXT NOT NULL,
      digest TEXT NOT NULL, schema_json TEXT NOT NULL,
      PRIMARY KEY (run_id, node_id, input_name)
    );
    """,
)

_MIGRATION_LOCK = threading.Lock()


class RunLeaseError(RuntimeError):
    """A run has another live owner or the caller lost ownership."""


class StaleAttemptError(RuntimeError):
    """An attempt is no longer the fenced active attempt for its node."""


@dataclass(frozen=True)
class RunLease:
    run_id: str
    token: str
    generation: int
    ttl_seconds: float


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        # WAL + NORMAL remains transactionally consistent across process crashes while avoiding
        # a full storage flush on every scheduler state transition.
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with _MIGRATION_LOCK, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > len(MIGRATIONS):
                    raise RuntimeError(
                        f"state database version {version} is newer than this runtime"
                    )
                for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
                    for statement in script.split(";"):
                        if statement.strip():
                            connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {index}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def create_run(self, workflow: dict[str, Any], run_id: str | None = None) -> str:
        run_id = run_id or uuid.uuid4().hex
        now = time.time()
        encoded = json.dumps(workflow, sort_keys=True, separators=(",", ":"))
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO runs(id,workflow_id,workflow_json,status,created_at,updated_at) "
                "VALUES (?, ?, ?, 'running', ?, ?)",
                (run_id, workflow["id"], encoded, now, now),
            )
            connection.executemany(
                "INSERT INTO nodes(run_id,node_id,status,required) VALUES (?,?,'pending',?)",
                [
                    (run_id, node["id"], int(node["required"]))
                    for node in workflow["nodes"]
                ],
            )
            connection.commit()
        return run_id

    def acquire_lease(
        self, run_id: str, *, token: str | None = None, ttl_seconds: float = 30.0
    ) -> RunLease:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        token = token or uuid.uuid4().hex
        now = time.time()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_token,owner_generation,lease_expires_at FROM runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise KeyError(f"unknown run {run_id}")
            live_other = (
                row["owner_token"] not in (None, token)
                and (row["lease_expires_at"] or 0) > now
            )
            if live_other:
                connection.rollback()
                raise RunLeaseError(f"run {run_id} already has a live owner")
            same_live_owner = (
                row["owner_token"] == token and (row["lease_expires_at"] or 0) > now
            )
            generation = (
                int(row["owner_generation"])
                if same_live_owner
                else int(row["owner_generation"]) + 1
            )
            connection.execute(
                "UPDATE runs SET owner_token=?,owner_generation=?,lease_expires_at=?,updated_at=? WHERE id=?",
                (token, generation, now + ttl_seconds, now, run_id),
            )
            connection.commit()
        return RunLease(run_id, token, generation, ttl_seconds)

    def renew_lease(self, lease: RunLease) -> None:
        now = time.time()
        with self._write_lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE runs SET lease_expires_at=?,updated_at=? "
                "WHERE id=? AND owner_token=? AND owner_generation=? AND lease_expires_at>?",
                (
                    now + lease.ttl_seconds,
                    now,
                    lease.run_id,
                    lease.token,
                    lease.generation,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise RunLeaseError(
                    f"lease generation {lease.generation} is no longer live"
                )

    def release_lease(self, lease: RunLease) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "UPDATE runs SET owner_token=NULL,lease_expires_at=NULL "
                "WHERE id=? AND owner_token=? AND owner_generation=?",
                (lease.run_id, lease.token, lease.generation),
            )

    def _assert_lease(self, connection: sqlite3.Connection, lease: RunLease) -> None:
        row = connection.execute(
            "SELECT owner_token,owner_generation,lease_expires_at FROM runs WHERE id=?",
            (lease.run_id,),
        ).fetchone()
        if (
            row is None
            or row["owner_token"] != lease.token
            or int(row["owner_generation"]) != lease.generation
            or (row["lease_expires_at"] or 0) <= time.time()
        ):
            raise RunLeaseError(
                f"lease generation {lease.generation} is no longer live"
            )

    def run(self, run_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run {run_id}")
        value = dict(row)
        value["workflow"] = json.loads(value.pop("workflow_json"))
        return value

    def node_rows(self, run_id: str) -> dict[str, dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM nodes WHERE run_id = ? ORDER BY node_id", (run_id,)
            ).fetchall()
        return {row["node_id"]: dict(row) for row in rows}

    def attempt_rows(self, run_id: str) -> tuple[dict[str, Any], ...]:
        """Return stable, read-only attempt history for operator status surfaces."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM attempts WHERE run_id = ? ORDER BY node_id, number",
                (run_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def artifact_rows(self, run_id: str) -> tuple[dict[str, Any], ...]:
        """Return accepted artifact receipts without loading artifact payloads."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT run_id,node_id,output_name,digest FROM artifacts "
                "WHERE run_id = ? ORDER BY node_id,output_name",
                (run_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def start_attempt(self, run_id: str, node_id: str, lease: RunLease) -> int:
        now = time.time()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            row = connection.execute(
                "SELECT status,attempt_count FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            if row is None or row["status"] != "pending":
                connection.rollback()
                raise StaleAttemptError(f"node {node_id} is not pending")
            number = int(row["attempt_count"]) + 1
            cursor = connection.execute(
                "UPDATE nodes SET status='running',attempt_count=?,active_attempt=?,active_generation=?,error=NULL "
                "WHERE run_id=? AND node_id=? AND status='pending'",
                (number, number, lease.generation, run_id, node_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise StaleAttemptError(f"node {node_id} lost its start race")
            connection.execute(
                "INSERT INTO attempts(run_id,node_id,number,status,started_at,owner_generation) "
                "VALUES (?,?,?,'running',?,?)",
                (run_id, node_id, number, now, lease.generation),
            )
            connection.commit()
        return number

    def _assert_active_attempt(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        node_id: str,
        number: int,
        lease: RunLease,
    ) -> None:
        self._assert_lease(connection, lease)
        row = connection.execute(
            "SELECT status,active_attempt,active_generation FROM nodes WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()
        if (
            row is None
            or row["status"] != "running"
            or row["active_attempt"] != number
            or row["active_generation"] != lease.generation
        ):
            raise StaleAttemptError(f"attempt {node_id}#{number} is no longer active")

    def finish_attempt(
        self,
        run_id: str,
        node_id: str,
        number: int,
        status: str,
        digest: str,
        error: str | None,
        lease: RunLease,
        *,
        failure: Mapping[str, Any] | None = None,
    ) -> int:
        now = time.time()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_active_attempt(connection, run_id, node_id, number, lease)
            previous = connection.execute(
                "SELECT last_digest,no_progress_count FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            no_progress = int(previous[1]) + 1 if previous[0] == digest else 0
            failure_json = (
                json.dumps(failure, sort_keys=True, separators=(",", ":"))
                if failure is not None
                else None
            )
            connection.execute(
                "UPDATE attempts SET status=?,digest=?,error=?,failure_json=?,finished_at=? "
                "WHERE run_id=? AND node_id=? AND number=? AND status='running' AND owner_generation=?",
                (
                    status,
                    digest,
                    error,
                    failure_json,
                    now,
                    run_id,
                    node_id,
                    number,
                    lease.generation,
                ),
            )
            connection.execute(
                "UPDATE nodes SET status=?,last_digest=?,no_progress_count=?,error=?,"
                "active_attempt=NULL,active_generation=NULL WHERE run_id=? AND node_id=?",
                (status, digest, no_progress, error, run_id, node_id),
            )
            connection.commit()
        return no_progress

    def last_attempt_failure(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        """Return the last durable structured failure, never an inferred error string."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT failure_json FROM attempts WHERE run_id=? AND node_id=? "
                "ORDER BY number DESC LIMIT 1",
                (run_id, node_id),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise TypeError("persisted attempt failure must be an object")
        return value

    def repair_inputs(self, run_id: str, node_id: str) -> tuple[dict[str, Any], ...]:
        """Return typed dynamic inputs attached by an accepted repair route."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT input_name,digest,schema_json FROM repair_inputs "
                "WHERE run_id=? AND node_id=? ORDER BY input_name",
                (run_id, node_id),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def route_repair(
        self,
        run_id: str,
        integration_node: str,
        route_id: str,
        failure_digest: str,
        *,
        max_rounds: int,
        no_progress_limit: int,
        targets: Mapping[str, tuple[str, str, dict[str, Any], int]],
        integration_attempt_limit: int,
        lease: RunLease,
    ) -> bool:
        """Atomically invalidate only named producers and reopen their integration."""

        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            integration = connection.execute(
                "SELECT status,attempt_count FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, integration_node),
            ).fetchone()
            if integration is None or integration["status"] != "failed":
                connection.rollback()
                return False
            if int(integration["attempt_count"]) >= integration_attempt_limit:
                connection.rollback()
                return False

            prior = connection.execute(
                "SELECT rounds,last_failure_digest,no_progress_count FROM repair_routes "
                "WHERE run_id=? AND integration_node=? AND route_id=?",
                (run_id, integration_node, route_id),
            ).fetchone()
            rounds = 0 if prior is None else int(prior["rounds"])
            no_progress = (
                0
                if prior is None or prior["last_failure_digest"] != failure_digest
                else int(prior["no_progress_count"]) + 1
            )
            if rounds >= max_rounds or no_progress >= no_progress_limit:
                connection.rollback()
                return False

            for target_id, (_input, _digest, _schema, attempt_limit) in targets.items():
                target = connection.execute(
                    "SELECT status,attempt_count FROM nodes WHERE run_id=? AND node_id=?",
                    (run_id, target_id),
                ).fetchone()
                if (
                    target is None
                    or target["status"] != "succeeded"
                    or int(target["attempt_count"]) >= attempt_limit
                ):
                    connection.rollback()
                    return False

            connection.execute(
                "INSERT INTO repair_routes VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(run_id,integration_node,route_id) DO UPDATE SET "
                "rounds=excluded.rounds,last_failure_digest=excluded.last_failure_digest,"
                "no_progress_count=excluded.no_progress_count",
                (
                    run_id,
                    integration_node,
                    route_id,
                    rounds + 1,
                    failure_digest,
                    no_progress,
                ),
            )
            for target_id, (
                input_name,
                digest,
                schema,
                _attempt_limit,
            ) in targets.items():
                connection.execute(
                    "DELETE FROM artifacts WHERE run_id=? AND node_id=?",
                    (run_id, target_id),
                )
                connection.execute(
                    "DELETE FROM repair_inputs WHERE run_id=? AND node_id=?",
                    (run_id, target_id),
                )
                connection.execute(
                    "INSERT INTO repair_inputs VALUES(?,?,?,?,?)",
                    (
                        run_id,
                        target_id,
                        input_name,
                        digest,
                        json.dumps(schema, sort_keys=True),
                    ),
                )
                connection.execute(
                    "UPDATE nodes SET status='pending',error=? "
                    "WHERE run_id=? AND node_id=?",
                    (
                        f"repair route {integration_node}.{route_id}",
                        run_id,
                        target_id,
                    ),
                )
            connection.execute(
                "DELETE FROM artifacts WHERE run_id=? AND node_id=?",
                (run_id, integration_node),
            )
            connection.execute(
                "UPDATE nodes SET status='pending',error=? WHERE run_id=? AND node_id=?",
                (f"awaiting repair route {route_id}", run_id, integration_node),
            )
            connection.execute(
                "UPDATE runs SET status='running',updated_at=? WHERE id=?",
                (time.time(), run_id),
            )
            connection.commit()
        return True

    def succeed_attempt(
        self,
        run_id: str,
        node_id: str,
        number: int,
        digest: str,
        artifacts: Mapping[str, tuple[str, dict[str, Any]]],
        lease: RunLease,
    ) -> None:
        """Atomically accept the fenced attempt and publish all artifact bindings."""

        now = time.time()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_active_attempt(connection, run_id, node_id, number, lease)
            connection.execute(
                "DELETE FROM artifacts WHERE run_id=? AND node_id=?", (run_id, node_id)
            )
            connection.executemany(
                "INSERT INTO artifacts VALUES (?,?,?,?,?)",
                [
                    (
                        run_id,
                        node_id,
                        name,
                        artifact_digest,
                        json.dumps(schema, sort_keys=True),
                    )
                    for name, (artifact_digest, schema) in sorted(artifacts.items())
                ],
            )
            connection.execute(
                "UPDATE attempts SET status='succeeded',digest=?,error=NULL,finished_at=? "
                "WHERE run_id=? AND node_id=? AND number=? AND status='running' AND owner_generation=?",
                (digest, now, run_id, node_id, number, lease.generation),
            )
            connection.execute(
                "UPDATE nodes SET status='succeeded',last_digest=?,no_progress_count=0,error=NULL,"
                "active_attempt=NULL,active_generation=NULL WHERE run_id=? AND node_id=?",
                (digest, run_id, node_id),
            )
            connection.commit()

    def set_node_status(
        self, run_id: str, node_id: str, status: str, error: str | None, lease: RunLease
    ) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            connection.execute(
                "UPDATE nodes SET status=?,error=? WHERE run_id=? AND node_id=?",
                (status, error, run_id, node_id),
            )
            connection.commit()

    def artifact(
        self, run_id: str, node_id: str, output_name: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE run_id=? AND node_id=? AND output_name=?",
                (run_id, node_id, output_name),
            ).fetchone()
        return dict(row) if row else None

    def recover_interrupted(
        self, run_id: str, lease: RunLease, replay_safe: Mapping[str, bool]
    ) -> tuple[str, ...]:
        now = time.time()
        uncertain: list[str] = []
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            running = connection.execute(
                "SELECT node_id FROM nodes WHERE run_id=? AND status='running'",
                (run_id,),
            ).fetchall()
            connection.execute(
                "UPDATE attempts SET status='interrupted',error='runtime interrupted',finished_at=? "
                "WHERE run_id=? AND status='running'",
                (now, run_id),
            )
            for row in running:
                node_id = row["node_id"]
                status = "pending" if replay_safe.get(node_id, False) else "uncertain"
                if status == "uncertain":
                    uncertain.append(node_id)
                connection.execute(
                    "UPDATE nodes SET status=?,error='runtime interrupted',active_attempt=NULL,"
                    "active_generation=NULL WHERE run_id=? AND node_id=?",
                    (status, run_id, node_id),
                )
            connection.execute(
                "UPDATE runs SET status='running',updated_at=? WHERE id=?",
                (now, run_id),
            )
            connection.commit()
        return tuple(sorted(uncertain))

    def reconcile_node(
        self, run_id: str, node_id: str, decision: str, lease: RunLease
    ) -> None:
        if decision not in {"retry", "fail"}:
            raise ValueError("reconciliation decision must be 'retry' or 'fail'")
        target = "pending" if decision == "retry" else "failed"
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            cursor = connection.execute(
                "UPDATE nodes SET status=?,error=? WHERE run_id=? AND node_id=? AND status='uncertain'",
                (target, f"explicit reconciliation: {decision}", run_id, node_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise ValueError(f"node {node_id} is not awaiting reconciliation")
            connection.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            connection.commit()

    def invalidate_node(
        self, run_id: str, node_id: str, error: str, lease: RunLease
    ) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            connection.execute(
                "UPDATE nodes SET status='failed',error=? WHERE run_id=? AND node_id=?",
                (error, run_id, node_id),
            )
            connection.commit()

    def request_cancel(self, run_id: str) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute(
                "UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,)
            )

    def cancel_requested(self, run_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        return bool(row and row[0])

    def finish_run(self, run_id: str, status: str, lease: RunLease) -> None:
        now = time.time()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            if status == "cancelled":
                connection.execute(
                    "UPDATE nodes SET status='cancelled',error='run cancelled',active_attempt=NULL,"
                    "active_generation=NULL WHERE run_id=? AND status NOT IN ('succeeded','optional_failed')",
                    (run_id,),
                )
                connection.execute(
                    "UPDATE attempts SET status='cancelled',error='run cancelled',finished_at=? "
                    "WHERE run_id=? AND status='running'",
                    (now, run_id),
                )
            connection.execute(
                "UPDATE runs SET status=?,updated_at=? WHERE id=?",
                (status, now, run_id),
            )
            connection.commit()
