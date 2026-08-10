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

from .lifecycle import append_event_connection
from .supervision import (
    artifact_set_digest,
    next_progress_observation,
    progress_budget,
)

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
    """
    CREATE TABLE join_states (
      run_id TEXT NOT NULL REFERENCES runs(id), node_id TEXT NOT NULL,
      policy TEXT NOT NULL, threshold INTEGER NOT NULL,
      expected INTEGER NOT NULL, received INTEGER NOT NULL,
      passed INTEGER NOT NULL, failed INTEGER NOT NULL,
      cancelled INTEGER NOT NULL, missing INTEGER NOT NULL,
      decision TEXT NOT NULL, settlements_json TEXT NOT NULL,
      updated_at REAL NOT NULL,
      PRIMARY KEY (run_id, node_id)
    );
    """,
    """
    CREATE TABLE remote_tasks (
      run_id TEXT NOT NULL REFERENCES runs(id), node_id TEXT NOT NULL,
      task_id TEXT NOT NULL, attempt_number INTEGER NOT NULL,
      profile TEXT NOT NULL, protocol_version TEXT NOT NULL,
      interface_url TEXT NOT NULL, card_digest TEXT NOT NULL,
      capability_digest TEXT NOT NULL, created_at REAL NOT NULL,
      PRIMARY KEY (run_id, node_id)
    );
    """,
    """
    ALTER TABLE runs ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'legacy';
    """,
    """
    CREATE TABLE node_progress (
      run_id TEXT NOT NULL REFERENCES runs(id), node_id TEXT NOT NULL,
      max_elapsed_seconds INTEGER NOT NULL, max_commands INTEGER,
      no_progress_limit INTEGER NOT NULL, started_at REAL, deadline_at REAL,
      observations INTEGER NOT NULL DEFAULT 0, command_count INTEGER,
      artifact_digest TEXT, artifact_delta INTEGER NOT NULL DEFAULT 0,
      failure_digest TEXT, repeated_failure INTEGER NOT NULL DEFAULT 0,
      no_progress_count INTEGER NOT NULL DEFAULT 0,
      last_meaningful_progress_at REAL, last_observed_at REAL,
      elapsed_seconds REAL NOT NULL DEFAULT 0,
      decision TEXT NOT NULL DEFAULT 'continue', reason TEXT NOT NULL DEFAULT 'not started',
      PRIMARY KEY (run_id, node_id)
    );
    """,
)

_MIGRATION_LOCK = threading.Lock()


class RunLeaseError(RuntimeError):
    """A run has another live owner or the caller lost ownership."""


class StaleAttemptError(RuntimeError):
    """An attempt is no longer the fenced active attempt for its node."""


class ProgressBudgetExpiredError(RuntimeError):
    """A pending attempt reached its persisted budget before dispatch."""


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

    def create_run(
        self,
        workflow: dict[str, Any],
        run_id: str | None = None,
        *,
        lifecycle: bool = False,
    ) -> str:
        run_id = run_id or uuid.uuid4().hex
        now = time.time()
        encoded = json.dumps(workflow, sort_keys=True, separators=(",", ":"))
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO runs(id,workflow_id,workflow_json,status,created_at,updated_at,lifecycle_state) "
                "VALUES (?, ?, ?, 'running', ?, ?, ?)",
                (
                    run_id,
                    workflow["id"],
                    encoded,
                    now,
                    now,
                    "pending" if lifecycle else "legacy",
                ),
            )
            connection.executemany(
                "INSERT INTO nodes(run_id,node_id,status,required) VALUES (?,?,'pending',?)",
                [
                    (run_id, node["id"], int(node["required"]))
                    for node in workflow["nodes"]
                ],
            )
            connection.executemany(
                "INSERT INTO node_progress(run_id,node_id,max_elapsed_seconds,max_commands,"
                "no_progress_limit) VALUES (?,?,?,?,?)",
                [
                    (
                        run_id,
                        node["id"],
                        budget["max_elapsed_seconds"],
                        budget["max_commands"],
                        budget["no_progress_limit"],
                    )
                    for node in workflow["nodes"]
                    for budget in (progress_budget(workflow, node),)
                ],
            )
            connection.commit()
        return run_id

    def acquire_lease(
        self,
        run_id: str,
        *,
        token: str | None = None,
        ttl_seconds: float = 30.0,
        lifecycle_resume: bool = False,
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
            if lifecycle_resume:
                append_event_connection(
                    connection,
                    run_id,
                    f"run:resumed:{generation}",
                    "run.resumed",
                    payload={"owner_generation": generation},
                    created_at=now,
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

    def _ensure_progress_connection(
        self, connection: sqlite3.Connection, run_id: str, node_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM node_progress WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()
        if row is not None:
            return row
        stored = connection.execute(
            "SELECT workflow_json FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        if stored is None:
            raise KeyError(f"unknown run {run_id}")
        workflow = json.loads(stored["workflow_json"])
        try:
            node = next(item for item in workflow["nodes"] if item["id"] == node_id)
        except (KeyError, StopIteration, TypeError) as exc:
            raise KeyError(f"unknown node {node_id!r}") from exc
        budget = progress_budget(workflow, node)
        first_attempt = connection.execute(
            "SELECT MIN(started_at) FROM attempts WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()[0]
        deadline = (
            float(first_attempt) + int(budget["max_elapsed_seconds"])
            if first_attempt is not None
            else None
        )
        connection.execute(
            "INSERT INTO node_progress(run_id,node_id,max_elapsed_seconds,max_commands,"
            "no_progress_limit,started_at,deadline_at,last_meaningful_progress_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                node_id,
                budget["max_elapsed_seconds"],
                budget["max_commands"],
                budget["no_progress_limit"],
                first_attempt,
                deadline,
                first_attempt,
            ),
        )
        return connection.execute(
            "SELECT * FROM node_progress WHERE run_id=? AND node_id=?",
            (run_id, node_id),
        ).fetchone()

    def progress_rows(self, run_id: str) -> dict[str, dict[str, Any]]:
        """Return durable budgets and the latest observation for every node."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM node_progress WHERE run_id=? ORDER BY node_id", (run_id,)
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            value = dict(row)
            # Keep the SQLite columns stable for existing databases while exposing
            # what these counters actually measure: deterministic acceptance checks,
            # not provider/tool commands executed inside a worker.
            value["max_deterministic_checks"] = value.pop("max_commands")
            value["deterministic_check_count"] = value.pop("command_count")
            result[row["node_id"]] = value
        return result

    def progress_deadline(self, run_id: str, node_id: str) -> float | None:
        """Return the original persisted wall-clock deadline for one node."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT deadline_at FROM node_progress WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown node {node_id!r}")
        return float(row["deadline_at"]) if row["deadline_at"] is not None else None

    def admit_attempt(
        self,
        run_id: str,
        node_id: str,
        lease: RunLease,
        *,
        resumed: bool = False,
    ) -> bool:
        """Fail a pending node before dispatch when its durable budget is exhausted."""

        now = time.time()
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            progress = self._ensure_progress_connection(connection, run_id, node_id)
            expired = (
                progress["deadline_at"] is not None
                and float(progress["deadline_at"]) <= now
            )
            stopped = progress["decision"] == "stop"
            if not expired and not stopped:
                connection.commit()
                return True
            reason = (
                "elapsed budget exhausted on resume"
                if expired and resumed
                else "elapsed progress budget exhausted before dispatch"
                if expired
                else progress["reason"]
            )
            elapsed = (
                max(0.0, now - float(progress["started_at"]))
                if progress["started_at"] is not None
                else float(progress["elapsed_seconds"])
            )
            connection.execute(
                "UPDATE node_progress SET observations=observations+1,last_observed_at=?,"
                "elapsed_seconds=?,decision='stop',reason=? WHERE run_id=? AND node_id=?",
                (now, elapsed, reason, run_id, node_id),
            )
            node = connection.execute(
                "SELECT status,attempt_count FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            if node is not None and node["status"] == "pending":
                connection.execute(
                    "UPDATE nodes SET status='failed',error=? WHERE run_id=? AND node_id=?",
                    (reason, run_id, node_id),
                )
                append_event_connection(
                    connection,
                    run_id,
                    f"progress:{node_id}:resume-expired",
                    "progress.observed",
                    node_id=node_id,
                    payload={
                        "decision": "stop",
                        "reason": reason,
                        "elapsed_seconds": elapsed,
                        "deadline_at": progress["deadline_at"],
                    },
                    created_at=now,
                )
                append_event_connection(
                    connection,
                    run_id,
                    f"node:{node_id}:failed:progress-budget",
                    "node.failed",
                    node_id=node_id,
                    attempt=int(node["attempt_count"]) or None,
                    payload={"error": reason},
                    created_at=now,
                )
            connection.commit()
        return False

    def admit_resumed_attempt(self, run_id: str, node_id: str, lease: RunLease) -> bool:
        """Compatibility wrapper for callers explicitly admitting a resumed node."""

        return self.admit_attempt(run_id, node_id, lease, resumed=True)

    def _record_progress_connection(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        node_id: str,
        number: int,
        *,
        artifacts: Mapping[str, tuple[str, dict[str, Any]]],
        failure_digest: str | None,
        deterministic_check_count: int | None,
        succeeded: bool,
        observed_at: float,
    ) -> dict[str, Any]:
        prior = self._ensure_progress_connection(connection, run_id, node_id)
        if prior["started_at"] is None:
            raise RuntimeError(f"progress budget for {node_id!r} has not started")
        observation = next_progress_observation(
            dict(prior),
            observed_at=observed_at,
            artifact_digest=artifact_set_digest(artifacts),
            failure_digest=failure_digest,
            deterministic_check_delta=deterministic_check_count,
            succeeded=succeeded,
        )
        connection.execute(
            "UPDATE node_progress SET observations=?,command_count=?,artifact_digest=?,"
            "artifact_delta=?,failure_digest=?,repeated_failure=?,no_progress_count=?,"
            "last_meaningful_progress_at=?,last_observed_at=?,elapsed_seconds=?,decision=?,reason=? "
            "WHERE run_id=? AND node_id=?",
            (
                observation["observations"],
                observation["deterministic_check_count"],
                observation["artifact_digest"],
                int(observation["artifact_delta"]),
                observation["failure_digest"],
                int(observation["repeated_failure"]),
                observation["no_progress_count"],
                observation["last_meaningful_progress_at"],
                observation["last_observed_at"],
                observation["elapsed_seconds"],
                observation["decision"],
                observation["reason"],
                run_id,
                node_id,
            ),
        )
        append_event_connection(
            connection,
            run_id,
            f"progress:{node_id}:{number}",
            "progress.observed",
            node_id=node_id,
            attempt=number,
            payload=observation,
            created_at=observed_at,
        )
        return observation

    def record_join_state(
        self,
        run_id: str,
        node_id: str,
        snapshot: Mapping[str, Any],
        lease: RunLease,
    ) -> dict[str, Any]:
        """Persist settlement progress and freeze the first terminal decision."""

        now = time.time()
        settlements = json.dumps(
            snapshot["settlements"], sort_keys=True, separators=(",", ":")
        )
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            previous = connection.execute(
                "SELECT decision FROM join_states WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO join_states(run_id,node_id,policy,threshold,expected,received,"
                "passed,failed,cancelled,missing,decision,settlements_json,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id,node_id) DO UPDATE SET "
                "policy=excluded.policy,threshold=excluded.threshold,"
                "expected=excluded.expected,received=excluded.received,"
                "passed=excluded.passed,failed=excluded.failed,"
                "cancelled=excluded.cancelled,missing=excluded.missing,"
                "decision=excluded.decision,settlements_json=excluded.settlements_json,"
                "updated_at=excluded.updated_at "
                "WHERE join_states.decision='waiting'",
                (
                    run_id,
                    node_id,
                    snapshot["policy"],
                    snapshot["threshold"],
                    snapshot["expected"],
                    snapshot["received"],
                    snapshot["passed"],
                    snapshot["failed"],
                    snapshot["cancelled"],
                    snapshot["missing"],
                    snapshot["decision"],
                    settlements,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM join_states WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            assert row is not None
            first_terminal = row["decision"] != "waiting" and (
                previous is None or previous["decision"] == "waiting"
            )
            if first_terminal:
                append_event_connection(
                    connection,
                    run_id,
                    f"join:{node_id}:decided",
                    "join.decided",
                    node_id=node_id,
                    payload={
                        "policy": row["policy"],
                        "threshold": row["threshold"],
                        "expected": row["expected"],
                        "received": row["received"],
                        "passed": row["passed"],
                        "failed": row["failed"],
                        "cancelled": row["cancelled"],
                        "missing": row["missing"],
                        "decision": row["decision"],
                        "settlements": json.loads(row["settlements_json"]),
                    },
                    created_at=now,
                )
            connection.commit()
        value = dict(row)
        value["settlements"] = json.loads(value.pop("settlements_json"))
        return value

    def join_state(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM join_states WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["settlements"] = json.loads(value.pop("settlements_json"))
        return value

    def remote_task(self, run_id: str, node_id: str) -> dict[str, Any] | None:
        """Return the pinned remote task identity used to resume an A2A node."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM remote_tasks WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def bind_remote_task(
        self,
        run_id: str,
        node_id: str,
        *,
        task_id: str,
        attempt_number: int,
        profile: str,
        protocol_version: str,
        interface_url: str,
        card_digest: str,
        capability_digest: str,
    ) -> None:
        """Bind a node to one remote task; a retry may never change its identity."""

        values = (
            run_id,
            node_id,
            task_id,
            attempt_number,
            profile,
            protocol_version,
            interface_url,
            card_digest,
            capability_digest,
        )
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT status,active_attempt FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            if (
                active is None
                or active["status"] != "running"
                or int(active["active_attempt"] or 0) != attempt_number
            ):
                connection.rollback()
                raise StaleAttemptError(
                    f"attempt {node_id}#{attempt_number} cannot bind a remote task"
                )
            prior = connection.execute(
                "SELECT task_id,attempt_number,profile,protocol_version,interface_url,card_digest,"
                "capability_digest FROM remote_tasks WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            if prior is not None:
                if tuple(prior) != values[2:]:
                    connection.rollback()
                    raise StaleAttemptError(
                        f"remote task binding for {node_id!r} changed after recording"
                    )
                connection.commit()
                return
            connection.execute(
                "INSERT INTO remote_tasks(run_id,node_id,task_id,attempt_number,profile,protocol_version,"
                "interface_url,card_digest,capability_digest,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*values, time.time()),
            )
            connection.commit()

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
            progress = self._ensure_progress_connection(connection, run_id, node_id)
            if (
                progress["deadline_at"] is not None
                and float(progress["deadline_at"]) <= now
            ):
                reason = "elapsed progress budget exhausted before dispatch"
                elapsed = max(0.0, now - float(progress["started_at"]))
                connection.execute(
                    "UPDATE node_progress SET observations=observations+1,"
                    "last_observed_at=?,elapsed_seconds=?,decision='stop',reason=? "
                    "WHERE run_id=? AND node_id=?",
                    (now, elapsed, reason, run_id, node_id),
                )
                connection.execute(
                    "UPDATE nodes SET status='failed',error=? "
                    "WHERE run_id=? AND node_id=? AND status='pending'",
                    (reason, run_id, node_id),
                )
                append_event_connection(
                    connection,
                    run_id,
                    f"progress:{node_id}:dispatch-expired",
                    "progress.observed",
                    node_id=node_id,
                    payload={
                        "decision": "stop",
                        "reason": reason,
                        "elapsed_seconds": elapsed,
                        "deadline_at": progress["deadline_at"],
                    },
                    created_at=now,
                )
                connection.commit()
                raise ProgressBudgetExpiredError(reason)
            if progress["started_at"] is None:
                connection.execute(
                    "UPDATE node_progress SET started_at=?,deadline_at=?,"
                    "last_meaningful_progress_at=? WHERE run_id=? AND node_id=?",
                    (
                        now,
                        now + int(progress["max_elapsed_seconds"]),
                        now,
                        run_id,
                        node_id,
                    ),
                )
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
            if number > 1:
                append_event_connection(
                    connection,
                    run_id,
                    f"retry:{node_id}:{number}",
                    "retry.scheduled",
                    node_id=node_id,
                    attempt=number,
                    payload={"prior_attempt": number - 1},
                    created_at=now,
                )
            append_event_connection(
                connection,
                run_id,
                f"node:{node_id}:running:{number}",
                "node.running",
                node_id=node_id,
                attempt=number,
                created_at=now,
            )
            append_event_connection(
                connection,
                run_id,
                f"attempt:{node_id}:{number}:started",
                "attempt.started",
                node_id=node_id,
                attempt=number,
                payload={"owner_generation": lease.generation},
                created_at=now,
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
        artifacts: Mapping[str, tuple[str, dict[str, Any]]] | None = None,
        deterministic_check_count: int | None = None,
        command_count: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if deterministic_check_count is not None and command_count is not None:
            raise ValueError(
                "provide deterministic_check_count or legacy command_count, not both"
            )
        check_count = (
            deterministic_check_count
            if deterministic_check_count is not None
            else command_count
        )
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
            observation = self._record_progress_connection(
                connection,
                run_id,
                node_id,
                number,
                artifacts=artifacts or {},
                failure_digest=digest,
                deterministic_check_count=check_count,
                succeeded=False,
                observed_at=now,
            )
            append_event_connection(
                connection,
                run_id,
                f"attempt:{node_id}:{number}:{status}",
                f"attempt.{status}",
                node_id=node_id,
                attempt=number,
                payload={"digest": digest, "error": error},
                created_at=now,
            )
            append_event_connection(
                connection,
                run_id,
                f"node:{node_id}:{status}:{number}",
                f"node.{status}",
                node_id=node_id,
                attempt=number,
                payload={"error": error},
                created_at=now,
            )
            connection.commit()
        return no_progress, observation

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
            append_event_connection(
                connection,
                run_id,
                f"repair:{integration_node}:{route_id}:{rounds + 1}",
                "repair.routed",
                node_id=integration_node,
                payload={
                    "route_id": route_id,
                    "round": rounds + 1,
                    "failure_digest": failure_digest,
                    "targets": sorted(targets),
                },
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
        *,
        deterministic_check_count: int | None = None,
        command_count: int | None = None,
    ) -> None:
        """Atomically accept the fenced attempt and publish all artifact bindings."""

        if deterministic_check_count is not None and command_count is not None:
            raise ValueError(
                "provide deterministic_check_count or legacy command_count, not both"
            )
        check_count = (
            deterministic_check_count
            if deterministic_check_count is not None
            else command_count
        )

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
            self._record_progress_connection(
                connection,
                run_id,
                node_id,
                number,
                artifacts=artifacts,
                failure_digest=None,
                deterministic_check_count=check_count,
                succeeded=True,
                observed_at=now,
            )
            for name, (artifact_digest, _schema) in sorted(artifacts.items()):
                append_event_connection(
                    connection,
                    run_id,
                    f"artifact:{node_id}:{number}:{name}:{artifact_digest}",
                    "artifact.accepted",
                    node_id=node_id,
                    attempt=number,
                    payload={"output_name": name, "digest": artifact_digest},
                    created_at=now,
                )
            append_event_connection(
                connection,
                run_id,
                f"attempt:{node_id}:{number}:succeeded",
                "attempt.succeeded",
                node_id=node_id,
                attempt=number,
                payload={"digest": digest},
                created_at=now,
            )
            append_event_connection(
                connection,
                run_id,
                f"node:{node_id}:succeeded:{number}",
                "node.succeeded",
                node_id=node_id,
                attempt=number,
                created_at=now,
            )
            workflow_row = connection.execute(
                "SELECT workflow_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            try:
                workflow = json.loads(workflow_row[0])
                kind = next(
                    item["kind"] for item in workflow["nodes"] if item["id"] == node_id
                )
            except (KeyError, StopIteration, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    "persisted workflow cannot identify node kind"
                ) from exc
            if kind == "integration":
                append_event_connection(
                    connection,
                    run_id,
                    f"integration:{node_id}:{number}",
                    "integration.completed",
                    node_id=node_id,
                    attempt=number,
                    created_at=now,
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
            row = connection.execute(
                "SELECT attempt_count FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            append_event_connection(
                connection,
                run_id,
                f"node:{node_id}:{status}:{row['attempt_count']}",
                f"node.{status}",
                node_id=node_id,
                attempt=int(row["attempt_count"]) or None,
                payload={"error": error},
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
                "SELECT node_id,active_attempt FROM nodes WHERE run_id=? AND status='running'",
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
                attempt = int(row["active_attempt"])
                append_event_connection(
                    connection,
                    run_id,
                    f"attempt:{node_id}:{attempt}:interrupted",
                    "attempt.interrupted",
                    node_id=node_id,
                    attempt=attempt,
                    payload={"error": "runtime interrupted"},
                    created_at=now,
                )
                append_event_connection(
                    connection,
                    run_id,
                    f"node:{node_id}:{status}:{attempt}",
                    f"node.{status}",
                    node_id=node_id,
                    attempt=attempt,
                    payload={"error": "runtime interrupted"},
                    created_at=now,
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
            attempt_row = connection.execute(
                "SELECT attempt_count FROM nodes WHERE run_id=? AND node_id=?",
                (run_id, node_id),
            ).fetchone()
            attempt_count = int(attempt_row["attempt_count"])
            connection.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            append_event_connection(
                connection,
                run_id,
                f"reconciliation:{node_id}:{attempt_count}:{decision}",
                "reconciliation.decided",
                node_id=node_id,
                attempt=attempt_count or None,
                payload={"decision": decision},
            )
            append_event_connection(
                connection,
                run_id,
                f"node:{node_id}:{target}:reconciliation:{attempt_count}",
                f"node.{target}",
                node_id=node_id,
                attempt=attempt_count or None,
                payload={"decision": decision},
            )
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
            append_event_connection(
                connection,
                run_id,
                f"node:{node_id}:failed:invalidated",
                "node.failed",
                node_id=node_id,
                payload={"error": error, "reason": "accepted artifact invalidated"},
            )
            connection.commit()

    def request_cancel(self, run_id: str) -> None:
        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,)
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise KeyError(f"unknown run {run_id}")
            append_event_connection(
                connection, run_id, "cancel:requested", "cancel.requested"
            )
            connection.commit()

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
                active_attempts = connection.execute(
                    "SELECT node_id,number FROM attempts WHERE run_id=? AND status='running'",
                    (run_id,),
                ).fetchall()
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
                for row in active_attempts:
                    append_event_connection(
                        connection,
                        run_id,
                        f"attempt:{row['node_id']}:{row['number']}:cancelled",
                        "attempt.cancelled",
                        node_id=row["node_id"],
                        attempt=int(row["number"]),
                        created_at=now,
                    )
                cancelled = connection.execute(
                    "SELECT node_id,attempt_count FROM nodes WHERE run_id=? AND status='cancelled'",
                    (run_id,),
                ).fetchall()
                for row in cancelled:
                    append_event_connection(
                        connection,
                        run_id,
                        f"node:{row['node_id']}:cancelled:{row['attempt_count']}",
                        "node.cancelled",
                        node_id=row["node_id"],
                        attempt=int(row["attempt_count"]) or None,
                        created_at=now,
                    )
            connection.execute(
                "UPDATE runs SET status=?,updated_at=? WHERE id=?",
                (status, now, run_id),
            )
            append_event_connection(
                connection,
                run_id,
                f"run:{status}",
                f"run.{status}",
                created_at=now,
            )
            connection.commit()

    def record_join_decision(
        self,
        run_id: str,
        node_id: str,
        decision_key: str,
        decision: Mapping[str, Any],
        lease: RunLease,
    ) -> None:
        """Record a deterministic join decision without owning join semantics."""

        with self._write_lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            append_event_connection(
                connection,
                run_id,
                f"join:{node_id}:{decision_key}",
                "join.decided",
                node_id=node_id,
                payload=decision,
            )
            connection.commit()
