from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from graph_engineering import (
    CheckResult,
    RunLeaseError,
    Scheduler,
    WorkflowValidationError,
)
from graph_engineering.state import StateStore

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
}


def n(name, **extra):
    value = {
        "id": name,
        "kind": "transform",
        "task": name,
        "needs": [],
        "inputs": {},
        "outputs": {"result": {"schema": SCHEMA}},
        "workspace": "read-only",
        "permission": "read",
        "required": True,
        "checks": [{"id": "ok", "argv": ["true"]}],
        "retry": {"max_attempts": 1, "no_progress_limit": 1},
    }
    value.update(extra)
    return value


def wf(nodes, **budget_overrides):
    budgets = {
        "max_nodes": 20,
        "max_concurrency": 4,
        "max_attempts_per_node": 3,
        "max_total_attempts": 30,
        "timeout_seconds": 10,
    }
    budgets.update(budget_overrides)
    return {
        "version": "graph-engineering/v1alpha1",
        "id": "adversarial",
        "goal": "probe",
        "budgets": budgets,
        "nodes": nodes,
        "outputs": {"result": f"{nodes[-1]['id']}.result"},
    }


def ok(*_):
    return CheckResult(True, "ok")


def test_two_resumers_never_execute_the_same_node_twice(tmp_path):
    value = wf([n("only")])
    db, art = tmp_path / "state.db", tmp_path / "art"
    seed = Scheduler(value, db, art, {}, ok)
    run_id = seed.state.create_run(value, "shared")
    first_started = threading.Event()
    release = threading.Event()
    calls = []

    def execute(context):
        calls.append(context.attempt)
        first_started.set()
        assert release.wait(3)
        return {"result": {"value": context.attempt}}

    one = Scheduler(value, db, art, {"only": execute}, ok)
    two = Scheduler(value, db, art, {"only": execute}, ok)
    errors = []
    first = threading.Thread(target=lambda: capture(one, run_id, errors))
    first.start()
    assert first_started.wait(2)
    second = threading.Thread(target=lambda: capture(two, run_id, errors))
    second.start()
    second.join(2)
    release.set()
    first.join(3)
    assert calls == [1]
    assert len(errors) == 1 and isinstance(errors[0], RunLeaseError)


def capture(engine, run_id, errors):
    try:
        engine.run(run_id, resume=True)
    except Exception as exc:  # noqa: BLE001 - test captures the competing owner outcome
        errors.append(exc)


def test_resume_recomputes_a_persisted_join_after_interrupted_member(tmp_path):
    # intent: a crash cannot lose or manufacture votes; settlement is rebuilt from fenced node state.
    members = [n("a", required=False), n("b", required=False)]
    join = n(
        "join",
        needs=["a", "b"],
        join={"policy": "all_settled"},
    )
    value = wf([*members, join])
    db, art = tmp_path / "state.db", tmp_path / "art"
    seed = Scheduler(value, db, art, {}, ok)
    run_id = seed.state.create_run(value, "resume-join")
    lease = seed.state.acquire_lease(run_id)
    seed.state.set_node_status(run_id, "a", "optional_failed", "refuted", lease)
    seed.state.start_attempt(run_id, "b", lease)
    seed.state.record_join_state(
        run_id,
        "join",
        {
            "policy": "all_settled",
            "threshold": 2,
            "expected": 2,
            "received": 1,
            "passed": 0,
            "failed": 1,
            "cancelled": 0,
            "missing": 1,
            "decision": "waiting",
            "settlements": {"a": "optional_failed", "b": "running"},
        },
        lease,
    )
    seed.state.release_lease(lease)

    seen = []

    def execute(context):
        if context.node_id == "join":
            seen.append(context.join)
        return {"result": {"value": 1}}

    result = Scheduler(
        value,
        db,
        art,
        {"b": execute, "join": execute},
        ok,
    ).run(run_id, resume=True)

    assert result.status == "succeeded"
    assert result.nodes["b"]["attempt_count"] == 2
    assert seen[0]["decision"] == "succeeded"
    assert seen[0]["received"] == 2
    assert seen[0]["settlements"] == {"a": "optional_failed", "b": "succeeded"}


def test_resume_preserves_exact_terminal_join_decision_before_node_start(tmp_path):
    members = [n(name, required=False) for name in ("a", "b", "slow")]
    join = n(
        "join",
        needs=["a", "b", "slow"],
        join={"policy": "majority"},
    )
    value = wf([*members, join])
    db, art = tmp_path / "state.db", tmp_path / "art"
    seed = Scheduler(value, db, art, {}, ok)
    run_id = seed.state.create_run(value, "resume-terminal-join")
    lease = seed.state.acquire_lease(run_id)
    for node_id in ("a", "b"):
        number = seed.state.start_attempt(run_id, node_id, lease)
        artifact = seed.artifacts.put({"value": 1}, SCHEMA)
        seed.state.succeed_attempt(
            run_id,
            node_id,
            number,
            artifact.digest,
            {"result": (artifact.digest, SCHEMA)},
            lease,
        )
    seed.state.start_attempt(run_id, "slow", lease)
    released = {
        "policy": "majority",
        "threshold": 2,
        "expected": 3,
        "received": 2,
        "passed": 2,
        "failed": 0,
        "cancelled": 0,
        "missing": 1,
        "decision": "succeeded",
        "settlements": {"a": "succeeded", "b": "succeeded", "slow": "running"},
    }
    seed.state.record_join_state(run_id, "join", released, lease)
    seed.state.release_lease(lease)

    seen = []

    def execute(context):
        if context.node_id == "join":
            seen.append(context.join)
        return {"result": {"value": 1}}

    result = Scheduler(
        value,
        db,
        art,
        {"slow": execute, "join": execute},
        ok,
    ).run(run_id, resume=True)

    assert result.status == "succeeded"
    persisted = seed.state.join_state(run_id, "join")
    assert persisted is not None
    for key, expected in released.items():
        assert persisted[key] == expected
        assert seen[0][key] == expected


def test_cancelled_run_persists_cancelled_join_settlements(tmp_path):
    members = [n("a", required=False), n("b", required=False)]
    join = n("join", needs=["a", "b"], join={"policy": "majority"})
    value = wf([*members, join])
    db, art = tmp_path / "state.db", tmp_path / "art"
    engine = Scheduler(value, db, art, {}, ok)
    run_id = engine.state.create_run(value, "cancelled-join")
    engine.state.request_cancel(run_id)

    result = engine.run(run_id, resume=True)

    assert result.status == "cancelled"
    persisted = engine.state.join_state(run_id, "join")
    assert persisted is not None
    assert persisted["received"] == 2
    assert persisted["cancelled"] == 2
    assert persisted["failed"] == 0
    assert persisted["missing"] == 0


def test_resume_cannot_mint_a_fresh_whole_workflow_timeout(tmp_path):
    # intent: sabotage persisted age; a restart must keep the original run deadline.
    value = wf([n("only")], timeout_seconds=1)
    db, art = tmp_path / "state.db", tmp_path / "art"
    seed = Scheduler(value, db, art, {}, ok)
    run_id = seed.state.create_run(value, "expired-run")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE runs SET created_at=?, updated_at=? WHERE id=?",
            (time.time() - 3_600, time.time() - 3_600, run_id),
        )

    calls = 0

    def execute(_context):
        nonlocal calls
        calls += 1
        return {"result": {"value": 1}}

    result = Scheduler(value, db, art, {"only": execute}, ok).run(run_id, resume=True)

    assert result.status == "cancelled"
    assert result.nodes["only"]["attempt_count"] == 0
    assert calls == 0


def test_resume_revalidates_schema_bounds_before_any_executor_runs(tmp_path):
    # intent: a resumed run cannot bypass the same hostile-schema preflight as a new run.
    valid = wf([n("only")])
    db, art = tmp_path / "state.db", tmp_path / "art"
    seed = Scheduler(valid, db, art, {}, ok)
    run_id = seed.state.create_run(valid, "bounded-resume")
    hostile = wf([n("only")])
    schema: dict = {}
    current = schema
    for _ in range(2_000):
        child: dict = {}
        current["items"] = child
        current = child
    hostile["nodes"][0]["outputs"]["result"]["schema"] = schema
    calls = 0

    def execute(_context):
        nonlocal calls
        calls += 1
        return {"result": {"value": 1}}

    with pytest.raises(WorkflowValidationError, match="SCHEMA_DEPTH_EXCEEDED"):
        Scheduler(hostile, db, art, {"only": execute}, ok).run(run_id, resume=True)
    assert calls == 0


def test_expired_successful_run_remains_terminal_on_resume(tmp_path):
    # intent: a deadline stops pending work; it must not rewrite accepted terminal state.
    value = wf([n("only")], timeout_seconds=10)
    db, art = tmp_path / "state.db", tmp_path / "art"
    calls = 0

    def execute(_context):
        nonlocal calls
        calls += 1
        return {"result": {"value": 1}}

    engine = Scheduler(value, db, art, {"only": execute}, ok)
    first = engine.run("completed-run")
    assert first.status == "succeeded"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE runs SET created_at=?, updated_at=? WHERE id=?",
            (time.time() - 3_600, time.time() - 3_600, "completed-run"),
        )

    resumed = Scheduler(value, db, art, {"only": execute}, ok).run(
        "completed-run", resume=True
    )

    assert resumed.status == "succeeded"
    assert resumed.nodes["only"]["status"] == "succeeded"
    assert resumed.nodes["only"]["attempt_count"] == 1
    assert calls == 1


def test_expired_cancelled_run_remains_cancelled_on_resume(tmp_path):
    # intent: terminal cancellation is durable and must not degrade into failure.
    value = wf([n("only")], timeout_seconds=1)
    db, art = tmp_path / "state.db", tmp_path / "art"
    seed = Scheduler(value, db, art, {}, ok)
    run_id = seed.state.create_run(value, "cancelled-run")
    lease = seed.state.acquire_lease(run_id)
    seed.state.finish_run(run_id, "cancelled", lease)
    seed.state.release_lease(lease)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE runs SET created_at=?, updated_at=? WHERE id=?",
            (time.time() - 3_600, time.time() - 3_600, run_id),
        )

    result = Scheduler(value, db, art, {"only": lambda _: pytest.fail()}, ok).run(
        run_id, resume=True
    )

    assert result.status == "cancelled"
    assert result.nodes["only"]["status"] == "cancelled"
    assert result.nodes["only"]["attempt_count"] == 0


def test_lease_renews_then_expires_with_higher_generation(tmp_path):
    value = wf([n("only")])
    state = StateStore(tmp_path / "state.db")
    run_id = state.create_run(value)
    first = state.acquire_lease(run_id, ttl_seconds=1)
    time.sleep(0.1)
    state.renew_lease(first)
    time.sleep(0.2)
    with pytest.raises(RunLeaseError):
        state.acquire_lease(run_id, ttl_seconds=1)
    time.sleep(0.85)
    second = state.acquire_lease(run_id, ttl_seconds=1)
    assert second.generation == first.generation + 1


def test_stale_attempt_cannot_overwrite_new_generation_success(tmp_path):
    value = wf([n("only")])
    state = StateStore(tmp_path / "state.db")
    run_id = state.create_run(value)
    old_lease = state.acquire_lease(run_id, ttl_seconds=1)
    old = state.start_attempt(run_id, "only", old_lease)
    state.release_lease(old_lease)
    new_lease = state.acquire_lease(run_id, ttl_seconds=1)
    state.recover_interrupted(run_id, new_lease, {"only": True})
    new = state.start_attempt(run_id, "only", new_lease)
    state.succeed_attempt(run_id, "only", new, "b" * 64, {}, new_lease)
    with pytest.raises(RunLeaseError):
        state.finish_attempt(
            run_id, "only", old, "failed", "a" * 64, "late failure", old_lease
        )
    assert state.node_rows(run_id)["only"]["status"] == "succeeded"


def test_unknown_write_requires_explicit_reconciliation_before_replay(tmp_path):
    write = n(
        "write",
        permission="write",
        workspace="worktree",
        write_scope=["output.json"],
    )
    value = wf([write])
    db, art = tmp_path / "state.db", tmp_path / "art"
    engine = Scheduler(value, db, art, {}, ok)
    run_id = engine.state.create_run(value, "uncertain-run")
    lease = engine.state.acquire_lease(run_id, ttl_seconds=1)
    engine.state.start_attempt(run_id, "write", lease)
    engine.state.release_lease(lease)
    calls = 0

    def execute(_):
        nonlocal calls
        calls += 1
        return {"result": {"value": 1}}

    resumed = Scheduler(value, db, art, {"write": execute}, ok).run(run_id, resume=True)
    assert resumed.status == "needs_reconciliation"
    assert resumed.nodes["write"]["status"] == "uncertain"
    assert calls == 0
    engine = Scheduler(value, db, art, {"write": execute}, ok)
    engine.reconcile_node(run_id, "write", "retry")
    accepted = engine.run(run_id, resume=True)
    assert accepted.status == "succeeded"
    assert calls == 1


def test_idempotent_write_with_stable_key_is_replayable(tmp_path):
    write = n(
        "write",
        permission="write",
        workspace="worktree",
        write_scope=["output.json"],
        effect="idempotent_write",
        idempotency_key="workflow-run-and-node",
    )
    value = wf([write])
    db, art = tmp_path / "state.db", tmp_path / "art"
    engine = Scheduler(value, db, art, {}, ok)
    run_id = engine.state.create_run(value)
    lease = engine.state.acquire_lease(run_id, ttl_seconds=1)
    engine.state.start_attempt(run_id, "write", lease)
    engine.state.release_lease(lease)
    dispatched_keys = []

    def execute(context):
        dispatched_keys.append(context.idempotency_key)
        return {"result": {"value": 1}}

    result = Scheduler(value, db, art, {"write": execute}, ok).run(run_id, resume=True)
    assert result.status == "succeeded"
    assert result.nodes["write"]["attempt_count"] == 2
    assert len(dispatched_keys) == 1
    assert dispatched_keys[0] is not None


def test_failed_non_idempotent_write_never_auto_retries(tmp_path):
    write = n(
        "write",
        permission="write",
        workspace="worktree",
        write_scope=["output.json"],
        effect="non_idempotent_write",
        retry={"max_attempts": 3, "no_progress_limit": 3},
    )
    value = wf([write])
    calls = 0

    def fail(_context):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider outcome unknown")

    engine = Scheduler(
        value, tmp_path / "state.db", tmp_path / "art", {"write": fail}, ok
    )
    result = engine.run(run_id="non-idempotent")

    assert result.status == "needs_reconciliation"
    assert result.nodes["write"]["status"] == "uncertain"
    assert result.nodes["write"]["attempt_count"] == 1
    assert calls == 1


def test_runtime_rejects_unrecognized_effect_instead_of_stripping_it(tmp_path):
    value = wf([n("only", effect="pure")])
    with pytest.raises(WorkflowValidationError, match="SCHEMA_ERROR"):
        Scheduler(value, tmp_path / "state.db", tmp_path / "art", {}, ok)


def test_declared_node_timeout_is_bounded_and_late_result_is_fenced(tmp_path):
    value = wf([n("only", timeout_seconds=1)], timeout_seconds=10)
    engine = Scheduler(
        value,
        tmp_path / "state.db",
        tmp_path / "art",
        {"only": lambda _: time.sleep(1.5) or {"result": {"value": 1}}},
        ok,
    )
    started = time.monotonic()
    result = engine.run()
    elapsed = time.monotonic() - started
    assert result.status == "failed"
    assert elapsed < 1.3, f"node timeout ignored; elapsed={elapsed:.2f}s"
    time.sleep(0.6)
    assert engine.state.node_rows(result.run_id)["only"]["status"] == "failed"


def test_progress_deadline_fences_late_green_output_without_persisting_artifact(
    tmp_path,
):
    # intent: a green schema/check result arriving after the durable progress budget
    # cannot be accepted merely because the executor's larger node timeout remains.
    only = n("only", timeout_seconds=5)
    only["progress"] = {"max_elapsed_seconds": 1}
    value = wf([only], timeout_seconds=10)
    check_calls = 0

    def green_check(*_args):
        nonlocal check_calls
        check_calls += 1
        return CheckResult(True, "green")

    engine = Scheduler(
        value,
        tmp_path / "state.db",
        tmp_path / "art",
        {"only": lambda _context: time.sleep(1.25) or {"result": {"value": 1}}},
        green_check,
    )

    started = time.monotonic()
    result = engine.run("progress-deadline")

    assert result.status == "failed"
    # Keep a generous process-level bound: this suite may run on a loaded shared
    # host, while the state/error assertions below prove which deadline fired.
    assert time.monotonic() - started < 3
    assert engine.state.artifact(result.run_id, "only", "result") is None
    time.sleep(0.35)
    assert not list((tmp_path / "art").rglob("*.json"))
    assert check_calls == 0
    attempt = engine.state.attempt_rows(result.run_id)[0]
    assert attempt["status"] == "failed"
    assert "progress budget" in attempt["error"]


def test_resume_with_expired_progress_deadline_dispatches_zero_workers(tmp_path):
    # intent: process resume retains the original node start/deadline and cannot
    # mint a fresh execution allowance before dispatch.
    only = n("only", timeout_seconds=5)
    only["progress"] = {"max_elapsed_seconds": 1}
    only["retry"] = {"max_attempts": 2, "no_progress_limit": 2}
    value = wf([only], timeout_seconds=10)
    db = tmp_path / "state.db"
    engine = Scheduler(value, db, tmp_path / "art", {}, ok)
    run_id = engine.state.create_run(value, "expired-resume")
    lease = engine.state.acquire_lease(run_id)
    number = engine.state.start_attempt(run_id, "only", lease)
    engine.state.finish_attempt(
        run_id,
        "only",
        number,
        "failed",
        "a" * 64,
        "first failure",
        lease,
        deterministic_check_count=1,
    )
    engine.state.set_node_status(run_id, "only", "pending", "retry", lease)
    engine.state.release_lease(lease)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE node_progress SET deadline_at=?,started_at=? "
            "WHERE run_id=? AND node_id=?",
            (time.time() - 1, time.time() - 2, run_id, "only"),
        )

    calls = 0

    def must_not_dispatch(_context):
        nonlocal calls
        calls += 1
        return {"result": {"value": 1}}

    resumed = Scheduler(
        value, db, tmp_path / "art", {"only": must_not_dispatch}, ok
    ).run(run_id, resume=True)

    assert resumed.status == "failed"
    assert calls == 0
    assert resumed.nodes["only"]["attempt_count"] == 1
    progress = engine.state.progress_rows(run_id)["only"]
    assert progress["decision"] == "stop"
    assert progress["reason"] == "elapsed budget exhausted on resume"


def test_declared_check_timeout_is_enforced(tmp_path):
    only = n("only")
    only["checks"][0]["timeout_seconds"] = 1
    value = wf([only])

    check_started = threading.Event()
    check_finished = threading.Event()

    def slow_check(*_):
        check_started.set()
        time.sleep(3)
        check_finished.set()
        return True

    started = time.monotonic()
    result = Scheduler(
        value,
        tmp_path / "state.db",
        tmp_path / "art",
        {"only": lambda _: {"result": {"value": 1}}},
        slow_check,
    ).run()
    assert result.status == "failed"
    assert check_started.is_set()
    assert time.monotonic() - started < 2.5
    assert check_finished.wait(3)
    assert not any(
        worker.name.startswith("graph-check-only") for worker in threading.enumerate()
    )


def test_cancellation_fences_noncooperative_executor_and_returns_boundedly(tmp_path):
    value = wf([n("only", timeout_seconds=5)])
    started = threading.Event()
    cancel = threading.Event()

    def ignores_cancel(_):
        started.set()
        time.sleep(0.5)
        return {"result": {"value": 1}}

    engine = Scheduler(
        value, tmp_path / "state.db", tmp_path / "art", {"only": ignores_cancel}, ok
    )
    holder = {}
    thread = threading.Thread(
        target=lambda: holder.setdefault("result", engine.run(cancel_event=cancel))
    )
    thread.start()
    assert started.wait(1)
    before = time.monotonic()
    cancel.set()
    thread.join(0.3)
    assert not thread.is_alive()
    assert time.monotonic() - before < 0.3
    assert holder["result"].status == "cancelled"
    time.sleep(0.55)
    assert not any(
        worker.name.startswith("graph-node-only") for worker in threading.enumerate()
    )


def test_missing_declared_workflow_output_fails_even_for_optional_node(tmp_path):
    optional = n("optional", required=False)
    value = wf([optional])
    result = Scheduler(
        value,
        tmp_path / "state.db",
        tmp_path / "art",
        {"optional": lambda _: (_ for _ in ()).throw(RuntimeError("absent"))},
        ok,
    ).run()
    assert result.status == "failed"
    assert result.nodes["optional"]["status"] == "failed"


def test_total_attempt_budget_never_overshoots_under_concurrency(tmp_path):
    nodes = [n(f"n{i}", required=i == 7) for i in range(8)]
    value = wf(nodes, max_concurrency=8, max_total_attempts=8)
    calls = 0
    lock = threading.Lock()

    def fail(_):
        nonlocal calls
        with lock:
            calls += 1
        raise RuntimeError("fail")

    result = Scheduler(
        value, tmp_path / "state.db", tmp_path / "art", {"transform": fail}, ok
    ).run()
    assert result.status == "failed"
    assert calls == 8
    assert sum(row["attempt_count"] for row in result.nodes.values()) == 8


def test_resume_rejects_corrupt_accepted_terminal_artifact(tmp_path):
    value = wf([n("only")])
    db, art = tmp_path / "state.db", tmp_path / "art"
    engine = Scheduler(value, db, art, {"only": lambda _: {"result": {"value": 1}}}, ok)
    first = engine.run(run_id="corrupt")
    record = engine.state.artifact(first.run_id, "only", "result")
    (art / record["digest"][:2] / f"{record['digest']}.json").write_text('{"value":2}')
    resumed = Scheduler(
        value, db, art, {"only": lambda _: {"result": {"value": 1}}}, ok
    ).run(first.run_id, resume=True)
    assert resumed.status == "failed"


def test_success_path_revalidates_artifact_after_check_tampering(tmp_path):
    value = wf([n("only")])
    art = tmp_path / "art"

    def corrupt_after_write(*_):
        deadline = time.time() + 1
        while time.time() < deadline:
            paths = list(art.glob("*/*.json"))
            if paths:
                paths[0].write_text('{"value":999}', encoding="utf-8")
                return True
            time.sleep(0.01)
        return False

    result = Scheduler(
        value,
        tmp_path / "state.db",
        art,
        {"only": lambda _: {"result": {"value": 1}}},
        corrupt_after_write,
    ).run()
    assert result.status == "failed"
    assert result.nodes["only"]["status"] == "failed"


def test_concurrent_state_initialization_migrates_once(tmp_path):
    path = tmp_path / "state.db"
    barrier = threading.Barrier(8)
    errors = []

    def initialize():
        try:
            barrier.wait()
            StateStore(path)
        except Exception as exc:  # noqa: BLE001 - test records every initializer failure
            errors.append(exc)

    threads = [threading.Thread(target=initialize) for _ in range(8)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert not errors
