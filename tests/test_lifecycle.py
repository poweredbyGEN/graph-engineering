from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from graph_engineering.lifecycle import (
    CONTEXT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    LifecycleError,
    LifecycleStore,
    StaticRunContextProvider,
)
from graph_engineering.state import RunLeaseError, StateStore

WORKFLOW = {
    "id": "wf",
    "nodes": [{"id": "worker", "kind": "agent", "required": True}],
}


class BrokenProvider:
    def provide(self):
        raise RuntimeError("provider is unavailable")


def pending_run(
    tmp_path: Path, run_id: str = "run"
) -> tuple[StateStore, LifecycleStore]:
    state = StateStore(tmp_path / "state.db")
    state.create_run(WORKFLOW, run_id, lifecycle=True)
    return state, LifecycleStore(state.path)


def initialize(ledger: LifecycleStore, run_id: str = "run"):
    return ledger.initialize_context(
        run_id,
        StaticRunContextProvider({"base_sha": "abc", "nested": {"labels": ["pilot"]}}),
    )


def test_bootstrap_atomically_creates_deeply_immutable_context_and_start_fact(
    tmp_path: Path,
):
    # intent: run context and the first lifecycle fact appear together and cannot mutate.
    _state, ledger = pending_run(tmp_path)
    context = initialize(ledger)
    snapshot_context, events = ledger.snapshot("run")

    assert context == snapshot_context
    assert context.version == CONTEXT_SCHEMA_VERSION
    assert [event.event_type for event in events] == ["run.started"]
    assert events[0].version == EVENT_SCHEMA_VERSION
    with pytest.raises(TypeError):
        context.values["base_sha"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        context.values["nested"]["labels"][0] = "changed"  # type: ignore[index]


def test_initialize_rehashes_stored_context_before_accepting_same_provider(
    tmp_path: Path,
):
    # intent: matching provider input cannot bless a tampered stored JSON document.
    _state, ledger = pending_run(tmp_path)
    initialize(ledger)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "UPDATE lifecycle_contexts SET values_json=? WHERE run_id='run'",
            ('{"base_sha":"abc","nested":{"labels":["tampered"]}}',),
        )
        connection.commit()
    with pytest.raises(LifecycleError, match="CONTEXT_CORRUPT"):
        initialize(ledger)


def test_failed_or_secret_provider_fails_closed_without_partial_bootstrap(
    tmp_path: Path,
):
    # intent: bootstrap failure leaves a recognizable pending run, never partial evidence.
    state, ledger = pending_run(tmp_path)
    with pytest.raises(LifecycleError, match="CONTEXT_PROVIDER_FAILED"):
        ledger.initialize_context("run", BrokenProvider())
    assert state.run("run")["lifecycle_state"] == "pending"
    with sqlite3.connect(ledger.path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM lifecycle_contexts").fetchone()[0]
            == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM lifecycle_events").fetchone()[0]
            == 0
        )

    _state2, ledger2 = pending_run(tmp_path, "secret")
    with pytest.raises(LifecycleError, match="CONTEXT_SECRET"):
        ledger2.initialize_context(
            "secret", StaticRunContextProvider({"header": "Bearer abcdefghijklmnop"})
        )


@pytest.mark.parametrize(
    "table", ["lifecycle_contexts", "lifecycle_events", "lifecycle_heads"]
)
def test_active_run_fails_closed_when_lifecycle_evidence_is_deleted(
    tmp_path: Path, table: str
):
    # intent: resume must never silently recreate deleted lifecycle history.
    state, ledger = pending_run(tmp_path)
    initialize(ledger)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(f"DELETE FROM {table} WHERE run_id='run'")
        connection.commit()
    with pytest.raises(LifecycleError, match="LIFECYCLE_DELETED|EVENT_LEDGER_CORRUPT"):
        initialize(ledger)
    assert state.run("run")["lifecycle_state"] == "active"


def test_invalid_persisted_json_is_normalized_to_lifecycle_error(tmp_path: Path):
    # intent: corrupt storage never leaks decoder-specific exceptions to operators.
    _state, ledger = pending_run(tmp_path)
    initialize(ledger)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute(
            "UPDATE lifecycle_events SET payload_json='{' WHERE run_id='run'"
        )
        connection.commit()
    with pytest.raises(LifecycleError, match="EVENT_LEDGER_CORRUPT"):
        ledger.events("run")


def test_rejected_lease_does_not_emit_false_resume(tmp_path: Path):
    # intent: a competing process records resume only after it owns the run lease.
    state, ledger = pending_run(tmp_path)
    initialize(ledger)
    owner = state.acquire_lease("run", token="owner", ttl_seconds=30)
    with pytest.raises(RunLeaseError):
        state.acquire_lease(
            "run", token="competitor", ttl_seconds=30, lifecycle_resume=True
        )
    assert [event.event_type for event in ledger.events("run")] == ["run.started"]
    state.release_lease(owner)


def test_state_transitions_are_journaled_in_commit_order_before_run_finishes(
    tmp_path: Path,
):
    # intent: a crash after node start still leaves the accepted start transition.
    state, ledger = pending_run(tmp_path)
    initialize(ledger)
    lease = state.acquire_lease("run", ttl_seconds=30)
    number = state.start_attempt("run", "worker", lease)
    assert [event.event_type for event in ledger.events("run")] == [
        "run.started",
        "node.running",
        "attempt.started",
    ]
    state.finish_attempt("run", "worker", number, "failed", "d" * 64, "boom", lease)
    state.set_node_status("run", "worker", "pending", "retry", lease)
    event_types = [event.event_type for event in ledger.events("run")]
    assert event_types[-3:] == ["attempt.failed", "node.failed", "node.pending"]
    assert [event.sequence for event in ledger.events("run")] == list(
        range(1, len(event_types) + 1)
    )
    state.release_lease(lease)


def test_accepted_resume_reconciliation_and_join_decisions_are_first_class(
    tmp_path: Path,
):
    # intent: recovery and quorum integration have durable decision evidence.
    state, ledger = pending_run(tmp_path)
    initialize(ledger)
    lease = state.acquire_lease("run", ttl_seconds=30)
    state.start_attempt("run", "worker", lease)
    state.release_lease(lease)

    resumed = state.acquire_lease("run", ttl_seconds=30, lifecycle_resume=True)
    uncertain = state.recover_interrupted("run", resumed, {"worker": False})
    assert uncertain == ("worker",)
    state.finish_run("run", "needs_reconciliation", resumed)
    state.record_join_decision(
        "run",
        "worker",
        "majority-1",
        {"policy": "majority", "passed": 2, "required": 2},
        resumed,
    )
    event_types = [event.event_type for event in ledger.events("run")]
    assert "run.resumed" in event_types
    assert "attempt.interrupted" in event_types
    assert "node.uncertain" in event_types
    assert "run.needs_reconciliation" in event_types
    assert event_types[-1] == "join.decided"
    state.release_lease(resumed)


def test_terminal_join_snapshot_appends_one_frozen_decision_event(tmp_path: Path):
    # intent: the first terminal join snapshot and its audit fact commit once together.
    state, ledger = pending_run(tmp_path)
    initialize(ledger)
    lease = state.acquire_lease("run", ttl_seconds=30)
    waiting = {
        "policy": "majority",
        "threshold": 1,
        "expected": 1,
        "received": 0,
        "passed": 0,
        "failed": 0,
        "cancelled": 0,
        "missing": 1,
        "decision": "waiting",
        "settlements": {"worker": "running"},
    }
    state.record_join_state("run", "worker", waiting, lease)
    assert not [
        event for event in ledger.events("run") if event.event_type == "join.decided"
    ]

    decided = {
        **waiting,
        "received": 1,
        "passed": 1,
        "missing": 0,
        "decision": "succeeded",
        "settlements": {"worker": "succeeded"},
    }
    frozen = state.record_join_state("run", "worker", decided, lease)
    state.record_join_state(
        "run",
        "worker",
        {
            **decided,
            "passed": 0,
            "failed": 1,
            "decision": "failed",
            "settlements": {"worker": "failed"},
        },
        lease,
    )

    events = [
        event for event in ledger.events("run") if event.event_type == "join.decided"
    ]
    assert len(events) == 1
    assert events[0].event_key == "join:worker:decided"
    for key in (
        "policy",
        "threshold",
        "expected",
        "received",
        "passed",
        "failed",
        "cancelled",
        "missing",
        "decision",
    ):
        assert events[0].payload[key] == frozen[key]
    assert dict(events[0].payload["settlements"]) == frozen["settlements"]
    state.release_lease(lease)


def test_join_decision_event_failure_rolls_back_terminal_snapshot(tmp_path: Path):
    # intent: a terminal join cannot persist without its lifecycle evidence in the same transaction.
    state, ledger = pending_run(tmp_path)
    initialize(ledger)
    lease = state.acquire_lease("run", ttl_seconds=30)
    waiting = {
        "policy": "all",
        "threshold": 1,
        "expected": 1,
        "received": 0,
        "passed": 0,
        "failed": 0,
        "cancelled": 0,
        "missing": 1,
        "decision": "waiting",
        "settlements": {"worker": "running"},
    }
    state.record_join_state("run", "worker", waiting, lease)
    with sqlite3.connect(ledger.path) as connection:
        connection.execute("DELETE FROM lifecycle_heads WHERE run_id='run'")
        connection.commit()

    with pytest.raises(LifecycleError, match="LIFECYCLE_DELETED"):
        state.record_join_state(
            "run",
            "worker",
            {
                **waiting,
                "received": 1,
                "passed": 1,
                "missing": 0,
                "decision": "succeeded",
                "settlements": {"worker": "succeeded"},
            },
            lease,
        )
    assert state.join_state("run", "worker")["decision"] == "waiting"
    state.release_lease(lease)


def test_legacy_bootstrap_requires_explicit_authorization_and_records_marker(
    tmp_path: Path,
):
    # intent: old runs are distinguishable from evidence deletion on current runs.
    state = StateStore(tmp_path / "state.db")
    state.create_run(WORKFLOW, "legacy")
    ledger = LifecycleStore(state.path)
    provider = StaticRunContextProvider({"base_sha": "abc"})
    with pytest.raises(LifecycleError, match="LEGACY_BOOTSTRAP_REQUIRED"):
        ledger.initialize_context("legacy", provider)
    ledger.initialize_context("legacy", provider, allow_legacy_bootstrap=True)
    assert [event.event_type for event in ledger.events("legacy")] == [
        "run.legacy_bootstrapped"
    ]


def test_trace_limit_is_bounded_and_payload_secrets_are_redacted(tmp_path: Path):
    _state, ledger = pending_run(tmp_path)
    initialize(ledger)
    ledger.append(
        "run",
        "check:secret",
        "check.completed",
        payload={"authorization": "Bearer top-secret-value"},
    )
    trace = ledger.trace("run", limit=1)
    assert trace["truncated"] is True
    assert trace["events"][0]["payload"]["authorization"] == "[REDACTED]"
    with pytest.raises(LifecycleError, match="TRACE_LIMIT_INVALID"):
        ledger.trace("run", limit=0)
