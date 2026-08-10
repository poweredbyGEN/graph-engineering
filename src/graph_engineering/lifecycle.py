"""Immutable run context and an append-only lifecycle transition journal."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from .artifacts import canonical_json

EVENT_SCHEMA_VERSION = "graph-engineering.lifecycle/v1"
CONTEXT_SCHEMA_VERSION = "graph-engineering.run-context/v1"
GENESIS_DIGEST = "0" * 64
MAX_DEPTH = 8
MAX_ITEMS = 256
MAX_STRING_BYTES = 16_384
DEFAULT_TRACE_LIMIT = 500
MAX_TRACE_LIMIT = 5_000
EVENT_STREAM_VERSION = "graph-engineering.event-stream/v1"
DEFAULT_STREAM_LIMIT = 100
MAX_STREAM_LIMIT = 256
MAX_STREAM_WAIT_SECONDS = 30.0

EVENT_TYPES = frozenset(
    {
        "run.started",
        "run.forked",
        "run.legacy_bootstrapped",
        "run.resumed",
        "run.succeeded",
        "run.failed",
        "run.cancelled",
        "run.needs_reconciliation",
        "node.pending",
        "node.running",
        "node.succeeded",
        "node.failed",
        "node.optional_failed",
        "node.cancelled",
        "node.uncertain",
        "node.blocked",
        "attempt.started",
        "attempt.succeeded",
        "attempt.failed",
        "attempt.interrupted",
        "attempt.cancelled",
        "artifact.accepted",
        "check.completed",
        "retry.scheduled",
        "progress.observed",
        "repair.routed",
        "reconciliation.decided",
        "integration.completed",
        "join.decided",
        "cancel.requested",
    }
)

_TERMINAL_RUN_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "needs_reconciliation"}
)

_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(authorization|cookie|credential|password|passwd|secret|token|api[_-]?key|private[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:bearer\s+[a-z0-9._~+/=-]{8,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)


class LifecycleError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class RunContextProvider(Protocol):
    """Trusted host-side provider; workflows cannot select or execute it."""

    def provide(self) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class StaticRunContextProvider:
    values: Mapping[str, Any]

    def provide(self) -> Mapping[str, Any]:
        return dict(self.values)


@dataclass(frozen=True)
class RunContext:
    version: str
    run_id: str
    values: Mapping[str, Any]
    digest: str
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "values": _thaw(self.values),
            "digest": self.digest,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class LifecycleEvent:
    version: str
    run_id: str
    sequence: int
    event_key: str
    event_type: str
    node_id: str | None
    attempt: int | None
    payload: Mapping[str, Any]
    previous_digest: str
    digest: str
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "event_key": self.event_key,
            "event_type": self.event_type,
            "node_id": self.node_id,
            "attempt": self.attempt,
            "payload": _thaw(self.payload),
            "previous_digest": self.previous_digest,
            "digest": self.digest,
            "created_at": self.created_at,
        }


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _bounded(value: Any, *, redact: bool, depth: int = 0) -> Any:
    if depth > MAX_DEPTH:
        raise LifecycleError("VALUE_TOO_DEEP", "lifecycle value exceeds nesting limit")
    if value is None or isinstance(value, (bool, int, float)):
        try:
            canonical_json(value)
        except Exception as exc:
            raise LifecycleError("VALUE_INVALID", str(exc)) from exc
        return value
    if isinstance(value, str):
        if len(value.encode()) > MAX_STRING_BYTES:
            if not redact:
                raise LifecycleError(
                    "VALUE_TOO_LARGE", "lifecycle string exceeds limit"
                )
            value = (
                value.encode()[: MAX_STRING_BYTES - 32].decode(errors="ignore")
                + "...[TRUNCATED]"
            )
        if not redact and _SENSITIVE_VALUE.search(value):
            raise LifecycleError("CONTEXT_SECRET", "context contains credential data")
        return _SENSITIVE_VALUE.sub("[REDACTED]", value) if redact else value
    if isinstance(value, Mapping):
        if len(value) > MAX_ITEMS:
            raise LifecycleError("VALUE_TOO_LARGE", "lifecycle object exceeds limit")
        result: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if not isinstance(key, str):
                raise LifecycleError("VALUE_INVALID", "object keys must be strings")
            if _SENSITIVE_KEY.search(key):
                if not redact:
                    raise LifecycleError("CONTEXT_SECRET", f"sensitive key {key!r}")
                result[key] = "[REDACTED]"
            else:
                result[key] = _bounded(item, redact=redact, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_ITEMS:
            raise LifecycleError("VALUE_TOO_LARGE", "lifecycle array exceeds limit")
        return [_bounded(item, redact=redact, depth=depth + 1) for item in value]
    raise LifecycleError("VALUE_INVALID", f"unsupported value {type(value).__name__}")


def _digest(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _decode_json(raw: str, *, code: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LifecycleError(code, "persisted lifecycle JSON is invalid") from exc


def _encode_cursor(run_id: str, sequence: int, digest: str) -> str:
    payload = canonical_json(
        {
            "version": EVENT_STREAM_VERSION,
            "run_id": run_id,
            "sequence": sequence,
            "digest": digest,
        }
    )
    return urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, run_id: str) -> tuple[int, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        value = json.loads(urlsafe_b64decode(cursor + padding))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifecycleError("STREAM_CURSOR_INVALID", "cursor is malformed") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "run_id", "sequence", "digest"}
        or value["version"] != EVENT_STREAM_VERSION
        or value["run_id"] != run_id
        or not isinstance(value["sequence"], int)
        or value["sequence"] < 0
        or not isinstance(value["digest"], str)
    ):
        raise LifecycleError("STREAM_CURSOR_INVALID", "cursor identity is invalid")
    return value["sequence"], value["digest"]


def _event_from_row(row: sqlite3.Row) -> LifecycleEvent:
    try:
        payload = _decode_json(row["payload_json"], code="EVENT_LEDGER_CORRUPT")
        if not isinstance(payload, dict):
            raise LifecycleError(
                "EVENT_LEDGER_CORRUPT", "event payload is not an object"
            )
        return LifecycleEvent(
            row["version"],
            row["run_id"],
            int(row["sequence"]),
            row["event_key"],
            row["event_type"],
            row["node_id"],
            row["attempt"],
            _freeze(payload),
            row["previous_digest"],
            row["digest"],
            float(row["created_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LifecycleError("EVENT_LEDGER_CORRUPT", "invalid event row") from exc


def _verify_rows(
    rows: list[sqlite3.Row], head: sqlite3.Row | None
) -> tuple[LifecycleEvent, ...]:
    events = tuple(_event_from_row(row) for row in rows)
    previous = GENESIS_DIGEST
    for sequence, event in enumerate(events, start=1):
        body = event.as_dict()
        digest = body.pop("digest")
        if (
            event.version != EVENT_SCHEMA_VERSION
            or event.sequence != sequence
            or event.previous_digest != previous
            or event.event_type not in EVENT_TYPES
            or _digest(body) != digest
        ):
            raise LifecycleError("EVENT_LEDGER_CORRUPT", "event hash chain is invalid")
        previous = event.digest
    if head is None:
        if events:
            raise LifecycleError("EVENT_LEDGER_CORRUPT", "event head is missing")
        return events
    if int(head["event_count"]) != len(events) or head["head_digest"] != previous:
        raise LifecycleError("EVENT_LEDGER_CORRUPT", "event head is invalid")
    return events


def _read_active_snapshot(
    connection: sqlite3.Connection, run_id: str
) -> tuple[RunContext, tuple[LifecycleEvent, ...]]:
    run = connection.execute(
        "SELECT lifecycle_state FROM runs WHERE id=?", (run_id,)
    ).fetchone()
    if run is None:
        raise KeyError(f"unknown run {run_id}")
    if run["lifecycle_state"] != "active":
        raise LifecycleError("LIFECYCLE_NOT_ACTIVE", "run lifecycle is not active")
    context_row = connection.execute(
        "SELECT * FROM lifecycle_contexts WHERE run_id=?", (run_id,)
    ).fetchone()
    head = connection.execute(
        "SELECT * FROM lifecycle_heads WHERE run_id=?", (run_id,)
    ).fetchone()
    rows = connection.execute(
        "SELECT * FROM lifecycle_events WHERE run_id=? ORDER BY sequence", (run_id,)
    ).fetchall()
    if context_row is None or head is None:
        raise LifecycleError(
            "LIFECYCLE_DELETED", "active lifecycle evidence is missing"
        )
    values = _decode_json(context_row["values_json"], code="CONTEXT_CORRUPT")
    if not isinstance(values, dict):
        raise LifecycleError("CONTEXT_CORRUPT", "context is not an object")
    body = {"version": context_row["version"], "run_id": run_id, "values": values}
    if (
        context_row["version"] != CONTEXT_SCHEMA_VERSION
        or _digest(body) != context_row["digest"]
    ):
        raise LifecycleError("CONTEXT_CORRUPT", "context digest mismatch")
    context = RunContext(
        context_row["version"],
        run_id,
        _freeze(values),
        context_row["digest"],
        float(context_row["created_at"]),
    )
    return context, _verify_rows(rows, head)


def append_event_connection(
    connection: sqlite3.Connection,
    run_id: str,
    event_key: str,
    event_type: str,
    *,
    node_id: str | None = None,
    attempt: int | None = None,
    payload: Mapping[str, Any] | None = None,
    created_at: float | None = None,
) -> LifecycleEvent | None:
    """Append inside the caller's state transaction; legacy runs are explicit no-ops."""

    try:
        run = connection.execute(
            "SELECT lifecycle_state FROM runs WHERE id=?", (run_id,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None
    if run is None:
        return None
    if run["lifecycle_state"] == "legacy":
        return None
    if run["lifecycle_state"] != "active":
        raise LifecycleError(
            "LIFECYCLE_INCOMPLETE", "lifecycle bootstrap is incomplete"
        )
    _context, events = _read_active_snapshot(connection, run_id)
    if event_type not in EVENT_TYPES:
        raise LifecycleError("EVENT_TYPE_INVALID", f"unknown event type {event_type!r}")
    clean = _bounded(payload or {}, redact=True)
    existing = connection.execute(
        "SELECT * FROM lifecycle_events WHERE run_id=? AND event_key=?",
        (run_id, event_key),
    ).fetchone()
    if existing is not None:
        event = _event_from_row(existing)
        if (event.event_type, event.node_id, event.attempt, _thaw(event.payload)) != (
            event_type,
            node_id,
            attempt,
            clean,
        ):
            raise LifecycleError("EVENT_CONFLICT", f"event {event_key!r} changed")
        return event
    sequence = len(events) + 1
    previous = events[-1].digest if events else GENESIS_DIGEST
    timestamp = time.time() if created_at is None else float(created_at)
    body = {
        "version": EVENT_SCHEMA_VERSION,
        "run_id": run_id,
        "sequence": sequence,
        "event_key": event_key,
        "event_type": event_type,
        "node_id": node_id,
        "attempt": attempt,
        "payload": clean,
        "previous_digest": previous,
        "created_at": timestamp,
    }
    digest = _digest(body)
    connection.execute(
        "INSERT INTO lifecycle_events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            sequence,
            event_key,
            EVENT_SCHEMA_VERSION,
            event_type,
            node_id,
            attempt,
            canonical_json(clean).decode(),
            previous,
            digest,
            timestamp,
        ),
    )
    connection.execute(
        "UPDATE lifecycle_heads SET event_count=?,head_digest=? WHERE run_id=?",
        (sequence, digest, run_id),
    )
    return _event_from_row(
        connection.execute(
            "SELECT * FROM lifecycle_events WHERE run_id=? AND sequence=?",
            (run_id, sequence),
        ).fetchone()
    )


class LifecycleStore:
    def __init__(self, state_path: str | Path):
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._migrate()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _migrate(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_contexts (
                  run_id TEXT PRIMARY KEY, version TEXT NOT NULL,
                  values_json TEXT NOT NULL, digest TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                  run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                  event_key TEXT NOT NULL, version TEXT NOT NULL, event_type TEXT NOT NULL,
                  node_id TEXT, attempt INTEGER, payload_json TEXT NOT NULL,
                  previous_digest TEXT NOT NULL, digest TEXT NOT NULL, created_at REAL NOT NULL,
                  PRIMARY KEY (run_id, sequence), UNIQUE (run_id, event_key)
                );
                CREATE TABLE IF NOT EXISTS lifecycle_heads (
                  run_id TEXT PRIMARY KEY, event_count INTEGER NOT NULL, head_digest TEXT NOT NULL
                );
                """
            )

    def initialize_context(
        self,
        run_id: str,
        provider: RunContextProvider,
        *,
        allow_legacy_bootstrap: bool = False,
    ) -> RunContext:
        try:
            supplied = provider.provide()
        except Exception as exc:
            raise LifecycleError("CONTEXT_PROVIDER_FAILED", str(exc)) from exc
        if not isinstance(supplied, Mapping):
            raise LifecycleError("CONTEXT_INVALID", "provider must return a mapping")
        values = _bounded(supplied, redact=False)
        body = {"version": CONTEXT_SCHEMA_VERSION, "run_id": run_id, "values": values}
        digest = _digest(body)
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT lifecycle_state,workflow_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if run is None:
                connection.rollback()
                raise KeyError(f"unknown run {run_id}")
            if run["lifecycle_state"] == "active":
                context, _events = _read_active_snapshot(connection, run_id)
                if context.digest != digest:
                    connection.rollback()
                    raise LifecycleError("CONTEXT_IMMUTABLE", "run context changed")
                connection.commit()
                return context
            if run["lifecycle_state"] == "legacy" and not allow_legacy_bootstrap:
                connection.rollback()
                raise LifecycleError(
                    "LEGACY_BOOTSTRAP_REQUIRED",
                    "legacy run requires explicit bootstrap",
                )
            if run["lifecycle_state"] not in {"pending", "legacy"}:
                connection.rollback()
                raise LifecycleError("LIFECYCLE_INCOMPLETE", "invalid lifecycle state")
            existing = connection.execute(
                "SELECT 1 FROM lifecycle_contexts WHERE run_id=? UNION ALL "
                "SELECT 1 FROM lifecycle_events WHERE run_id=? UNION ALL "
                "SELECT 1 FROM lifecycle_heads WHERE run_id=? LIMIT 1",
                (run_id, run_id, run_id),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                raise LifecycleError(
                    "LIFECYCLE_PARTIAL", "partial bootstrap evidence exists"
                )
            connection.execute(
                "INSERT INTO lifecycle_contexts VALUES(?,?,?,?,?)",
                (
                    run_id,
                    CONTEXT_SCHEMA_VERSION,
                    canonical_json(values).decode(),
                    digest,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO lifecycle_heads VALUES(?,?,?)", (run_id, 0, GENESIS_DIGEST)
            )
            connection.execute(
                "UPDATE runs SET lifecycle_state='active' WHERE id=?", (run_id,)
            )
            legacy = run["lifecycle_state"] == "legacy"
            fork = values.get("fork_lineage")
            forked = isinstance(fork, Mapping)
            append_event_connection(
                connection,
                run_id,
                (
                    "run:legacy-bootstrap"
                    if legacy
                    else "run:forked"
                    if forked
                    else "run:started"
                ),
                (
                    "run.legacy_bootstrapped"
                    if legacy
                    else "run.forked"
                    if forked
                    else "run.started"
                ),
                payload={
                    "workflow_id": run["workflow_id"],
                    **(
                        {
                            "parent_run_id": fork.get("parent_run_id"),
                            "parent_event_sequence": fork.get("parent_event", {}).get(
                                "sequence"
                            ),
                            "parent_event_digest": fork.get("parent_event", {}).get(
                                "digest"
                            ),
                        }
                        if forked
                        else {}
                    ),
                },
                created_at=now,
            )
            context, _events = _read_active_snapshot(connection, run_id)
            connection.commit()
            return context

    def context(self, run_id: str) -> RunContext:
        return self.snapshot(run_id)[0]

    def append(
        self, run_id: str, event_key: str, event_type: str, **kwargs: Any
    ) -> LifecycleEvent:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = append_event_connection(
                connection, run_id, event_key, event_type, **kwargs
            )
            if event is None:
                connection.rollback()
                raise LifecycleError(
                    "LIFECYCLE_NOT_ACTIVE", "legacy lifecycle is inactive"
                )
            connection.commit()
            return event

    def append_if_active(
        self, run_id: str, event_key: str, event_type: str, **kwargs: Any
    ) -> LifecycleEvent | None:
        """Append for a real lifecycle run; receipt-only legacy tests remain compatible."""

        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = append_event_connection(
                connection, run_id, event_key, event_type, **kwargs
            )
            connection.commit()
            return event

    def snapshot(self, run_id: str) -> tuple[RunContext, tuple[LifecycleEvent, ...]]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            try:
                result = _read_active_snapshot(connection, run_id)
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def events(self, run_id: str) -> tuple[LifecycleEvent, ...]:
        return self.snapshot(run_id)[1]

    def trace(self, run_id: str, *, limit: int = DEFAULT_TRACE_LIMIT) -> dict[str, Any]:
        if not 1 <= limit <= MAX_TRACE_LIMIT:
            raise LifecycleError(
                "TRACE_LIMIT_INVALID", f"limit must be 1..{MAX_TRACE_LIMIT}"
            )
        context, events = self.snapshot(run_id)
        selected = events[-limit:]
        return {
            "version": EVENT_SCHEMA_VERSION,
            "run_id": run_id,
            "context": context.as_dict(),
            "event_count": len(events),
            "truncated": len(selected) < len(events),
            "events": [event.as_dict() for event in selected],
        }

    def stream(
        self,
        run_id: str,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_STREAM_LIMIT,
        wait_seconds: float = 0,
    ) -> dict[str, Any]:
        """Return one bounded, reconnectable batch for neutral event consumers."""

        if not 1 <= limit <= MAX_STREAM_LIMIT:
            raise LifecycleError(
                "STREAM_LIMIT_INVALID", f"limit must be 1..{MAX_STREAM_LIMIT}"
            )
        if not 0 <= wait_seconds <= MAX_STREAM_WAIT_SECONDS:
            raise LifecycleError(
                "STREAM_WAIT_INVALID",
                f"wait must be 0..{MAX_STREAM_WAIT_SECONDS:g} seconds",
            )
        sequence, digest = (
            _decode_cursor(cursor, run_id)
            if cursor is not None
            else (0, GENESIS_DIGEST)
        )
        deadline = time.monotonic() + wait_seconds
        timed_out = False
        while True:
            _context, events = self.snapshot(run_id)
            if sequence > len(events):
                raise LifecycleError(
                    "STREAM_CURSOR_INVALID", "cursor is ahead of the lifecycle head"
                )
            expected = GENESIS_DIGEST if sequence == 0 else events[sequence - 1].digest
            if digest != expected:
                raise LifecycleError(
                    "STREAM_CURSOR_STALE",
                    "cursor digest does not match lifecycle history",
                )
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT status FROM runs WHERE id=?", (run_id,)
                ).fetchone()
            if row is None:
                raise KeyError(f"unknown run {run_id}")
            available = events[sequence:]
            terminal_status = row["status"] in _TERMINAL_RUN_STATUSES
            if available or terminal_status or time.monotonic() >= deadline:
                timed_out = not available and not terminal_status and wait_seconds > 0
                break
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        selected = available[:limit]
        next_sequence = selected[-1].sequence if selected else sequence
        next_digest = selected[-1].digest if selected else digest
        has_more = len(available) > len(selected)
        return {
            "version": EVENT_STREAM_VERSION,
            "run_id": run_id,
            "events": [event.as_dict() for event in selected],
            "next_cursor": _encode_cursor(run_id, next_sequence, next_digest),
            "has_more": has_more,
            "terminal": terminal_status and not has_more,
            "timed_out": timed_out,
        }


__all__ = [
    "CONTEXT_SCHEMA_VERSION",
    "EVENT_SCHEMA_VERSION",
    "EVENT_STREAM_VERSION",
    "EVENT_TYPES",
    "LifecycleError",
    "LifecycleEvent",
    "LifecycleStore",
    "RunContext",
    "RunContextProvider",
    "StaticRunContextProvider",
    "append_event_connection",
]
