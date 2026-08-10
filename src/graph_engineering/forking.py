"""Immutable run lineage built from verified lifecycle checkpoints."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .artifacts import canonical_json
from .lifecycle import LifecycleStore
from .state import StateStore

FORK_VERSION = "graph-engineering.run-fork/v1"
FORK_CHECKPOINT_TYPES = frozenset(
    {
        "run.started",
        "run.forked",
        "run.resumed",
        "run.succeeded",
        "run.failed",
        "run.cancelled",
        "run.needs_reconciliation",
        "node.succeeded",
        "node.failed",
        "node.optional_failed",
        "node.cancelled",
        "node.uncertain",
        "node.blocked",
        "artifact.accepted",
        "check.completed",
        "reconciliation.decided",
        "integration.completed",
        "join.decided",
    }
)
_ATTEMPT_TERMINAL = frozenset(
    {
        "attempt.succeeded",
        "attempt.failed",
        "attempt.interrupted",
        "attempt.cancelled",
    }
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ForkError(RuntimeError):
    """Stable fail-closed error for immutable run forks."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _manifest(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT base_sha,profile_manifest_sha256,profile_manifest_json,"
        "project_policy_sha256,private_execution_sha256,repository_sha256,"
        "workflow_sha256,product_contract_sha256,product_contract_generation "
        "FROM runtime_manifests WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ForkError("FORK_MANIFEST_MISSING", "parent runtime manifest is missing")
    keys = (
        "base_sha",
        "profile_manifest_digest",
        "profile_manifest_json",
        "project_policy_digest",
        "private_execution_digest",
        "repository_digest",
        "workflow_digest",
        "product_contract_digest",
        "product_contract_generation",
    )
    result = dict(zip(keys, row, strict=True))
    try:
        profiles = json.loads(str(result.pop("profile_manifest_json")))
    except json.JSONDecodeError as exc:
        raise ForkError("FORK_MANIFEST_CORRUPT", "profile manifest is invalid") from exc
    if _sha(profiles) != result["profile_manifest_digest"]:
        raise ForkError(
            "FORK_MANIFEST_CORRUPT", "profile manifest digest does not match"
        )
    required = ("profile_manifest_digest",)
    if any(
        not isinstance(result[name], str) or not _DIGEST.fullmatch(result[name])
        for name in required
    ):
        raise ForkError("FORK_MANIFEST_CORRUPT", "runtime identity digest is missing")
    optional = (
        "project_policy_digest",
        "private_execution_digest",
        "repository_digest",
        "product_contract_digest",
    )
    if any(
        result[name] is not None
        and (not isinstance(result[name], str) or not _DIGEST.fullmatch(result[name]))
        for name in optional
    ):
        raise ForkError("FORK_MANIFEST_CORRUPT", "optional runtime identity is invalid")
    if not isinstance(result["base_sha"], str) or not result["base_sha"]:
        raise ForkError("FORK_MANIFEST_CORRUPT", "base SHA is missing")
    result["profiles"] = profiles
    return result


def _artifact_snapshot(
    state_path: Path, events: tuple[Any, ...], sequence: int
) -> list[dict[str, Any]]:
    accepted: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events[:sequence]:
        if event.event_type != "artifact.accepted":
            continue
        output_name = event.payload.get("output_name")
        digest = event.payload.get("digest")
        if (
            not isinstance(event.node_id, str)
            or not isinstance(output_name, str)
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
        ):
            raise ForkError("FORK_ARTIFACT_CORRUPT", "artifact event is invalid")
        path = state_path.parent / "artifacts" / digest[:2] / f"{digest}.json"
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ForkError(
                "FORK_ARTIFACT_MISSING", "checkpoint artifact is missing"
            ) from exc
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ForkError(
                "FORK_ARTIFACT_CORRUPT", "checkpoint artifact digest does not match"
            )
        accepted[(event.node_id, output_name)] = {
            "node_id": event.node_id,
            "output_name": output_name,
            "digest": digest,
        }
    return [accepted[key] for key in sorted(accepted)]


def _settled_attempts(events: tuple[Any, ...], sequence: int) -> set[tuple[str, int]]:
    return {
        (event.node_id, event.attempt)
        for event in events[:sequence]
        if event.event_type in _ATTEMPT_TERMINAL
        and isinstance(event.node_id, str)
        and isinstance(event.attempt, int)
    }


def _receipt_snapshot(
    state_path: Path, run_id: str, events: tuple[Any, ...], sequence: int
) -> dict[str, Any]:
    try:
        with _connect(state_path) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_ledgers'"
            ).fetchone()
            row = (
                connection.execute(
                    "SELECT digest FROM receipt_ledgers WHERE run_id=?", (run_id,)
                ).fetchone()
                if table is not None
                else None
            )
    except sqlite3.Error as exc:
        raise ForkError(
            "FORK_RECEIPTS_CORRUPT", "receipt binding is unreadable"
        ) from exc
    settled = _settled_attempts(events, sequence)
    completed_checks = {
        (event.node_id, event.attempt, event.payload.get("check_id"))
        for event in events[:sequence]
        if event.event_type == "check.completed"
    }
    snapshot: dict[str, Any] = {"agent_receipts": {}, "check_receipts": []}
    if row is None:
        return snapshot
    path = (
        state_path.parent
        / "artifacts"
        / "receipts"
        / f"{hashlib.sha256(run_id.encode()).hexdigest()}.json"
    )
    try:
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != row[0]:
            raise ValueError("receipt file binding mismatch")
        envelope = json.loads(payload)
        body = envelope["body"]
        if (
            envelope["digest"] != _sha(body)
            or body["run_id"] != run_id
            or body["version"] != 1
        ):
            raise ValueError("receipt envelope mismatch")
        agents = body["agent_receipts"]
        checks = body["check_receipts"]
        if not isinstance(agents, dict) or not isinstance(checks, list):
            raise TypeError("receipt collections are invalid")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ForkError(
            "FORK_RECEIPTS_CORRUPT", "receipt ledger is not trustworthy"
        ) from exc
    for key, value in sorted(agents.items()):
        node_id, separator, raw_attempt = key.rpartition("#")
        if (
            separator
            and raw_attempt.isdigit()
            and (node_id, int(raw_attempt)) in settled
        ):
            snapshot["agent_receipts"][key] = value
    snapshot["check_receipts"] = [
        value
        for value in checks
        if isinstance(value, dict)
        and (value.get("node_id"), value.get("attempt"), value.get("check_id"))
        in completed_checks
    ]
    return snapshot


def build_lineage(state_path: str | Path, run_id: str, sequence: int) -> dict[str, Any]:
    """Validate a quiescent parent checkpoint and bind all reusable identities."""

    state_path = Path(state_path).expanduser().resolve(strict=True)
    store = StateStore(state_path)
    run = store.run(run_id)
    context, events = LifecycleStore(state_path).snapshot(run_id)
    if not 1 <= sequence <= len(events):
        raise ForkError("FORK_EVENT_UNKNOWN", "checkpoint sequence does not exist")
    checkpoint = events[sequence - 1]
    if checkpoint.event_type not in FORK_CHECKPOINT_TYPES:
        raise ForkError(
            "FORK_EVENT_UNSAFE", "checkpoint is not an accepted settlement event"
        )
    started = {
        (event.node_id, event.attempt)
        for event in events[:sequence]
        if event.event_type == "attempt.started"
    }
    settled = _settled_attempts(events, sequence)
    if started - settled:
        raise ForkError("FORK_EVENT_IN_FLIGHT", "checkpoint has an unsettled attempt")
    nodes = {node["id"]: node for node in run["workflow"]["nodes"]}
    unsafe = sorted(
        node_id
        for node_id, _attempt in started
        if nodes[node_id].get("effect") not in {"none", "read"}
        and not (
            nodes[node_id].get("effect") == "idempotent_write"
            and nodes[node_id].get("idempotency_key")
        )
    )
    if unsafe:
        raise ForkError(
            "FORK_EFFECT_REPLAY_UNSAFE",
            "checkpoint includes non-replay-safe effects: " + ", ".join(unsafe),
        )
    try:
        with _connect(state_path) as connection:
            manifest = _manifest(connection, run_id)
    except sqlite3.Error as exc:
        raise ForkError(
            "FORK_MANIFEST_CORRUPT", "parent runtime manifest is unreadable"
        ) from exc
    workflow_digest = _sha(run["workflow"])
    if manifest["workflow_digest"] not in {None, workflow_digest}:
        raise ForkError("FORK_WORKFLOW_DRIFT", "parent workflow digest does not match")
    manifest["workflow_digest"] = workflow_digest
    if context.values.get("base_sha") != manifest["base_sha"]:
        raise ForkError("FORK_BASE_DRIFT", "parent context and manifest base differ")
    if context.values.get("workflow_digest") != manifest["workflow_digest"]:
        raise ForkError(
            "FORK_WORKFLOW_DRIFT", "parent context and manifest workflow differ"
        )
    artifacts = _artifact_snapshot(state_path, events, sequence)
    receipts = _receipt_snapshot(state_path, run_id, events, sequence)
    return {
        "version": FORK_VERSION,
        "parent_run_id": run_id,
        "parent_event": {
            "sequence": checkpoint.sequence,
            "digest": checkpoint.digest,
            "event_type": checkpoint.event_type,
        },
        "parent_context_digest": context.digest,
        "base_sha": manifest["base_sha"],
        "workflow_digest": manifest["workflow_digest"],
        "profile_manifest_digest": manifest["profile_manifest_digest"],
        "artifact_snapshot_digest": _sha(artifacts),
        "receipt_snapshot_digest": _sha(receipts),
        "artifact_count": len(artifacts),
        "receipt_count": len(receipts["agent_receipts"])
        + len(receipts["check_receipts"]),
    }


def verify_lineage(state_path: str | Path, run_id: str) -> dict[str, Any] | None:
    """Rebuild the parent binding and reject a child whose lineage drifted."""

    state_path = Path(state_path).expanduser().resolve(strict=True)
    lineage = StateStore(state_path).fork_lineage(run_id)
    if lineage is None:
        return None
    parent = lineage.get("parent_run_id")
    event = lineage.get("parent_event")
    if not isinstance(parent, str) or not isinstance(event, Mapping):
        raise ForkError("FORK_LINEAGE_CORRUPT", "stored lineage is incomplete")
    try:
        current = build_lineage(state_path, parent, int(event["sequence"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ForkError("FORK_LINEAGE_CORRUPT", "stored checkpoint is invalid") from exc
    if current != lineage:
        raise ForkError("FORK_LINEAGE_DRIFT", "parent checkpoint binding changed")
    return lineage


def create_fork(
    state_path: str | Path,
    parent_run_id: str,
    sequence: int,
    run_id: str,
) -> dict[str, Any]:
    """Create a fresh pending child without copying mutable execution state."""

    state_path = Path(state_path).expanduser().resolve(strict=True)
    lineage = build_lineage(state_path, parent_run_id, sequence)
    try:
        StateStore(state_path).create_fork_run(parent_run_id, run_id, lineage)
    except sqlite3.IntegrityError as exc:
        raise ForkError("FORK_RUN_EXISTS", "child run id already exists") from exc
    except (sqlite3.Error, ValueError) as exc:
        raise ForkError(
            "FORK_CREATE_CONFLICT", "parent evidence changed before child creation"
        ) from exc
    return lineage


__all__ = [
    "FORK_CHECKPOINT_TYPES",
    "FORK_VERSION",
    "ForkError",
    "build_lineage",
    "create_fork",
    "verify_lineage",
]
