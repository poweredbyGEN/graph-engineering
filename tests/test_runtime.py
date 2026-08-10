from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from graph_engineering import CheckResult, Scheduler
from graph_engineering.contracts import WorkflowValidationError
from graph_engineering.lifecycle import LifecycleStore, StaticRunContextProvider

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
}


def node(
    node_id: str,
    *,
    needs: list[str] | None = None,
    inputs: dict[str, str] | None = None,
    required: bool = True,
    checks: bool = True,
    join: dict | None = None,
) -> dict:
    value = {
        "id": node_id,
        "kind": "transform",
        "task": f"execute {node_id}",
        "needs": needs or [],
        "inputs": inputs or {},
        "outputs": {"result": {"schema": SCHEMA}},
        "workspace": "read-only",
        "permission": "read",
        "retry": {"max_attempts": 3, "no_progress_limit": 2},
        "required": required,
    }
    if checks:
        value["checks"] = [{"id": "accept", "argv": ["test", node_id]}]
    if join is not None:
        value["join"] = join
    return value


def workflow(nodes: list[dict], *, concurrency: int = 3) -> dict:
    return {
        "version": "graph-engineering/v1alpha1",
        "id": "runtime_test",
        "goal": "exercise deterministic scheduling",
        "budgets": {
            "max_nodes": 20,
            "max_concurrency": concurrency,
            "max_attempts_per_node": 3,
            "max_total_attempts": 30,
            "timeout_seconds": 10,
        },
        "nodes": nodes,
        "outputs": {"result": f"{nodes[-1]['id']}.result"},
    }


def passing_check(check, context, outputs):
    return CheckResult(True, f"{check['id']} passed")


def scheduler(tmp_path, value, executors, check_runner=passing_check):
    return Scheduler(
        value, tmp_path / "state.db", tmp_path / "artifacts", executors, check_runner
    )


def repair_workflow(*, max_rounds: int = 1, no_progress_limit: int = 1) -> dict:
    change_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {"value": {"type": "integer"}},
    }

    def producer(node_id: str, path: str) -> dict:
        return {
            "id": node_id,
            "kind": "agent",
            "task": f"produce {path}",
            "needs": [],
            "inputs": {},
            "outputs": {"changeset": {"schema": change_schema}},
            "profile": "test",
            "workspace": "worktree",
            "write_scope": [path],
            "permission": "write",
            "effect": "none",
            "checks": [{"id": "unit", "argv": ["test", node_id]}],
            "retry": {
                "max_attempts": 1 + max_rounds,
                "no_progress_limit": 1,
            },
            "required": True,
        }

    integration = {
        "id": "integrate",
        "kind": "integration",
        "task": "combine producer changes",
        "needs": ["producer_a", "producer_b"],
        "inputs": {
            "a": "producer_a.changeset",
            "b": "producer_b.changeset",
        },
        "outputs": {"result": {"schema": SCHEMA}},
        "workspace": "worktree",
        "write_scope": ["src/**"],
        "permission": "write",
        "checks": [{"id": "combined", "argv": ["test", "integrate"]}],
        "retry": {
            "max_attempts": 1 + max_rounds,
            "no_progress_limit": 1,
        },
        "repair": {
            "routes": [
                {
                    "id": "combined_to_a",
                    "check_ids": ["combined"],
                    "targets": [{"node": "producer_a", "input": "integration_failure"}],
                    "max_rounds": max_rounds,
                    "no_progress_limit": no_progress_limit,
                }
            ]
        },
        "required": True,
    }
    return {
        "version": "graph-engineering/v1alpha1",
        "id": "repair_test",
        "goal": "repair only the producer named by the failed combined check",
        "budgets": {
            "max_nodes": 3,
            "max_concurrency": 2,
            "max_attempts_per_node": 1 + max_rounds,
            "max_total_attempts": 3 + 2 * max_rounds,
            "timeout_seconds": 10,
        },
        "nodes": [
            producer("producer_a", "src/a"),
            producer("producer_b", "src/b"),
            integration,
        ],
        "outputs": {"result": "integrate.result"},
    }


def test_ready_queue_overlaps_independent_nodes_and_orders_consumer(tmp_path):
    barrier = threading.Barrier(2)
    times: dict[str, float] = {}

    def worker(context):
        times[f"{context.node_id}_start"] = time.monotonic()
        barrier.wait(timeout=2)
        time.sleep(0.08)
        times[f"{context.node_id}_end"] = time.monotonic()
        return {"result": {"value": 1}}

    def join(context):
        times["join_start"] = time.monotonic()
        return {
            "result": {
                "value": context.inputs["a"]["value"] + context.inputs["b"]["value"]
            }
        }

    value = workflow(
        [
            node("a"),
            node("b"),
            node(
                "join",
                needs=["a", "b"],
                inputs={"a": "a.result", "b": "b.result"},
                join={"policy": "all"},
            ),
        ],
        concurrency=2,
    )
    result = scheduler(tmp_path, value, {"a": worker, "b": worker, "join": join}).run()
    assert result.status == "succeeded"
    assert max(times["a_start"], times["b_start"]) < min(times["a_end"], times["b_end"])
    assert times["join_start"] >= max(times["a_end"], times["b_end"])


@pytest.mark.parametrize(
    ("join", "passing", "expected_threshold"),
    [
        ({"policy": "any"}, {"a"}, 1),
        ({"policy": "n_of_m", "n": 2}, {"a", "b"}, 2),
        ({"policy": "majority"}, {"a", "b"}, 2),
    ],
)
def test_quorum_join_contains_partial_failure_and_persists_settlement(
    tmp_path, join, passing, expected_threshold
):
    members = [node(name, required=False) for name in ("a", "b", "c")]
    consumer = node("join", needs=["a", "b", "c"], join=join)
    seen = []

    def member(context):
        if context.node_id not in passing:
            raise RuntimeError("negative vote")
        return {"result": {"value": 1}}

    def consume(context):
        seen.append(context.join)
        return {"result": {"value": context.join["passed"]}}

    engine = scheduler(
        tmp_path,
        workflow([*members, consumer]),
        {"a": member, "b": member, "c": member, "join": consume},
    )
    result = engine.run(run_id=f"join-{join['policy']}")

    assert result.status == "succeeded"
    assert result.nodes["join"]["status"] == "succeeded"
    assert seen[0]["decision"] == "succeeded"
    assert seen[0]["threshold"] == expected_threshold
    persisted = engine.state.join_state(result.run_id, "join")
    assert persisted is not None
    assert persisted["expected"] == 3
    assert persisted["decision"] == "succeeded"
    assert persisted["passed"] >= expected_threshold
    assert persisted["received"] == (
        persisted["passed"] + persisted["failed"] + persisted["cancelled"]
    )
    assert persisted["cancelled"] == 0
    assert persisted["missing"] == persisted["expected"] - persisted["received"]
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
        "settlements",
    ):
        assert persisted[key] == seen[0][key]


def test_majority_releases_downstream_before_slow_member_settles(tmp_path):
    times: dict[str, float] = {}

    def member(context):
        if context.node_id == "slow":
            time.sleep(0.2)
            times["slow_end"] = time.monotonic()
        return {"result": {"value": 1}}

    def consume(context):
        times["join_start"] = time.monotonic()
        assert context.join["missing"] == 1
        return {"result": {"value": 2}}

    value = workflow(
        [
            node("fast_a", required=False),
            node("fast_b", required=False),
            node("slow", required=False),
            node(
                "join",
                needs=["fast_a", "fast_b", "slow"],
                join={"policy": "majority"},
            ),
        ],
        concurrency=4,
    )
    engine = scheduler(
        tmp_path,
        value,
        {
            "fast_a": member,
            "fast_b": member,
            "slow": member,
            "join": consume,
        },
    )
    result = engine.run()

    assert result.status == "succeeded"
    assert times["join_start"] < times["slow_end"]
    persisted = engine.state.join_state(result.run_id, "join")
    assert persisted is not None
    assert persisted["decision"] == "succeeded"
    assert persisted["received"] == 2
    assert persisted["missing"] == 1
    assert persisted["settlements"]["slow"] == "running"


def test_scheduler_journals_frozen_quorum_release_once_across_resume(tmp_path):
    # intent: production scheduling exposes the exact early-release decision durably, without replay duplicates.
    def member(context):
        if context.node_id == "slow":
            time.sleep(0.2)
        return {"result": {"value": 1}}

    value = workflow(
        [
            node("fast_a", required=False),
            node("fast_b", required=False),
            node("slow", required=False),
            node(
                "join",
                needs=["fast_a", "fast_b", "slow"],
                join={"policy": "majority"},
            ),
        ],
        concurrency=4,
    )
    engine = scheduler(
        tmp_path,
        value,
        {
            "fast_a": member,
            "fast_b": member,
            "slow": member,
            "join": lambda context: {"result": {"value": context.join["passed"]}},
        },
    )
    run_id = engine.state.create_run(value, "join-lifecycle", lifecycle=True)
    ledger = LifecycleStore(engine.state.path)
    ledger.initialize_context(run_id, StaticRunContextProvider({"base_sha": "abc"}))

    result = engine.run(run_id, resume=True, lifecycle_resume=False)
    assert result.status == "succeeded"
    frozen = engine.state.join_state(run_id, "join")
    assert frozen is not None
    assert frozen["decision"] == "succeeded"
    assert frozen["missing"] == 1

    resumed = engine.run(run_id, resume=True, lifecycle_resume=False)
    assert resumed.status == "succeeded"
    events = [
        event for event in ledger.events(run_id) if event.event_type == "join.decided"
    ]
    assert len(events) == 1
    assert events[0].payload["decision"] == frozen["decision"]
    assert events[0].payload["missing"] == frozen["missing"]
    assert dict(events[0].payload["settlements"]) == frozen["settlements"]


def test_impossible_quorum_blocks_consumer_without_calling_it(tmp_path):
    called = False

    def fail(_context):
        raise RuntimeError("refuted")

    def consume(_context):
        nonlocal called
        called = True
        return {"result": {"value": 1}}

    def slow_success(_context):
        time.sleep(0.2)
        return {"result": {"value": 1}}

    rejected = [node("a", required=False), node("b", required=False)]
    for member in rejected:
        member["retry"] = {"max_attempts": 1, "no_progress_limit": 1}
    value = workflow(
        [
            *rejected,
            node("c", required=False),
            node(
                "join",
                needs=["a", "b", "c"],
                join={"policy": "n_of_m", "n": 2},
            ),
        ]
    )
    engine = scheduler(
        tmp_path,
        value,
        {
            "a": fail,
            "b": fail,
            "c": slow_success,
            "join": consume,
        },
    )
    result = engine.run()

    assert result.status == "failed"
    assert result.nodes["join"]["status"] == "blocked"
    assert "became impossible" in result.nodes["join"]["error"]
    assert not called
    persisted = engine.state.join_state(result.run_id, "join")
    assert persisted["decision"] == "failed"
    assert persisted["received"] == 2
    assert persisted["missing"] == 1
    assert persisted["settlements"]["c"] == "running"


def test_all_settled_waits_for_every_outcome_but_does_not_require_success(tmp_path):
    seen = []

    def consume(context):
        seen.append(context.join)
        return {"result": {"value": context.join["received"]}}

    value = workflow(
        [
            node("pass", required=False),
            node("fail", required=False),
            node(
                "join",
                needs=["pass", "fail"],
                join={"policy": "all_settled"},
            ),
        ]
    )
    result = scheduler(
        tmp_path,
        value,
        {
            "pass": lambda _: {"result": {"value": 1}},
            "fail": lambda _: (_ for _ in ()).throw(RuntimeError("no vote")),
            "join": consume,
        },
    ).run()

    assert result.status == "succeeded"
    assert seen[0]["received"] == 2
    assert seen[0]["passed"] == 1
    assert seen[0]["failed"] == 1
    assert seen[0]["missing"] == 0


def test_invalid_artifact_fails_producer_and_blocks_consumer(tmp_path):
    consumer_called = False

    def consume(context):
        nonlocal consumer_called
        consumer_called = True
        return {"result": {"value": 2}}

    value = workflow(
        [
            node("producer"),
            node("consumer", needs=["producer"], inputs={"source": "producer.result"}),
        ]
    )
    result = scheduler(
        tmp_path,
        value,
        {"producer": lambda _: {"result": {"value": "invalid"}}, "consumer": consume},
    ).run()
    assert result.status == "failed"
    assert result.nodes["producer"]["status"] == "failed"
    assert result.nodes["consumer"]["status"] == "blocked"
    assert not consumer_called


def test_optional_failure_is_contained(tmp_path):
    value = workflow([node("optional", required=False), node("required")])
    result = scheduler(
        tmp_path,
        value,
        {
            "optional": lambda _: (_ for _ in ()).throw(RuntimeError("non-critical")),
            "required": lambda _: {"result": {"value": 1}},
        },
    ).run()
    assert result.status == "succeeded"
    assert result.nodes["optional"]["status"] == "optional_failed"
    assert result.nodes["required"]["status"] == "succeeded"


def test_retry_is_localized_to_failing_node(tmp_path):
    calls = {"flaky": 0, "stable": 0}

    def flaky(_):
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("transient")
        return {"result": {"value": 1}}

    def stable(_):
        calls["stable"] += 1
        return {"result": {"value": 1}}

    result = scheduler(
        tmp_path,
        workflow([node("flaky", required=False), node("stable")]),
        {"flaky": flaky, "stable": stable},
    ).run()
    assert result.status == "succeeded"
    assert calls == {"flaky": 2, "stable": 1}
    assert result.nodes["flaky"]["attempt_count"] == 2


def test_failed_integration_routes_typed_evidence_to_only_named_producer(tmp_path):
    value = repair_workflow()
    calls = {"producer_a": 0, "producer_b": 0, "integrate": 0}
    received = []

    def producer_a(context):
        calls["producer_a"] += 1
        if "integration_failure" in context.inputs:
            received.append(context.inputs["integration_failure"])
            return {"changeset": {"value": 2}}
        return {"changeset": {"value": 1}}

    def producer_b(_context):
        calls["producer_b"] += 1
        return {"changeset": {"value": 2}}

    def integrate(context):
        calls["integrate"] += 1
        return {
            "result": {
                "value": context.inputs["a"]["value"] + context.inputs["b"]["value"]
            }
        }

    def check(check, context, outputs):
        if context.node_id == "integrate" and outputs["result"]["value"] != 4:
            return CheckResult(False, "expected combined value 4, got 3")
        return CheckResult(True, f"{check['id']} passed")

    engine = scheduler(
        tmp_path,
        value,
        {
            "producer_a": producer_a,
            "producer_b": producer_b,
            "integrate": integrate,
        },
        check,
    )
    result = engine.run(run_id="typed-repair")

    assert result.status == "succeeded"
    assert calls == {"producer_a": 2, "producer_b": 1, "integrate": 2}
    assert result.nodes["producer_a"]["attempt_count"] == 2
    assert result.nodes["producer_b"]["attempt_count"] == 1
    assert result.nodes["integrate"]["attempt_count"] == 2
    assert received == [
        {
            "code": "CHECK_FAILED",
            "integration_node": "integrate",
            "integration_attempt": 1,
            "check_id": "combined",
            "evidence": "expected combined value 4, got 3",
            "failure_digest": received[0]["failure_digest"],
        }
    ]
    assert len(received[0]["failure_digest"]) == 64
    assert engine.state.artifact("typed-repair", "producer_b", "changeset") is not None


def test_repeated_identical_integration_failure_stops_repair_cycle(tmp_path):
    value = repair_workflow(max_rounds=3, no_progress_limit=1)
    calls = {"producer_a": 0, "producer_b": 0, "integrate": 0}

    def unchanged(context):
        calls[context.node_id] += 1
        return {"changeset": {"value": 1 if context.node_id == "producer_a" else 2}}

    def integrate(context):
        calls["integrate"] += 1
        return {"result": {"value": 3}}

    def check(check, context, _outputs):
        if context.node_id == "integrate":
            return CheckResult(False, "same deterministic composition failure")
        return CheckResult(True, f"{check['id']} passed")

    result = scheduler(
        tmp_path,
        value,
        {
            "producer_a": unchanged,
            "producer_b": unchanged,
            "integrate": integrate,
        },
        check,
    ).run(run_id="no-progress-repair")

    assert result.status == "failed"
    assert calls == {"producer_a": 2, "producer_b": 1, "integrate": 2}
    assert result.nodes["integrate"]["status"] == "failed"


def test_unmapped_failure_never_guesses_a_repair_target(tmp_path):
    value = repair_workflow()
    value["nodes"][-1]["checks"].append(
        {"id": "unrouted", "argv": ["test", "unrouted"]}
    )
    value["nodes"][-1]["repair"]["routes"][0]["check_ids"] = ["combined"]
    calls = {"producer_a": 0, "producer_b": 0, "integrate": 0}

    def producer(context):
        calls[context.node_id] += 1
        return {"changeset": {"value": 1}}

    def integrate(_context):
        calls["integrate"] += 1
        return {"result": {"value": 2}}

    def check(check, context, _outputs):
        if context.node_id == "integrate" and check["id"] == "unrouted":
            return CheckResult(False, "unmapped check failure")
        return CheckResult(True, "passed")

    result = scheduler(
        tmp_path,
        value,
        {
            "producer_a": producer,
            "producer_b": producer,
            "integrate": integrate,
        },
        check,
    ).run(run_id="unmapped-repair")

    assert result.status == "failed"
    assert calls == {"producer_a": 1, "producer_b": 1, "integrate": 1}
    assert result.nodes["integrate"]["status"] == "failed"


def test_non_idempotent_success_is_never_replayed_by_repair_route(tmp_path):
    # intent: a green side effect may already have happened and cannot be auto-repaired.
    value = repair_workflow()
    value["nodes"][0]["effect"] = "non_idempotent_write"

    with pytest.raises(WorkflowValidationError, match="UNSAFE_REPAIR_TARGET"):
        scheduler(tmp_path, value, {})


def test_resume_completes_repair_if_owner_dies_after_failure_is_persisted(
    tmp_path, monkeypatch
):
    value = repair_workflow()
    calls = {"producer_a": 0, "producer_b": 0, "integrate": 0}

    def producer(context):
        calls[context.node_id] += 1
        repaired = "integration_failure" in context.inputs
        value = 2 if context.node_id == "producer_b" or repaired else 1
        return {"changeset": {"value": value}}

    def integrate(context):
        calls["integrate"] += 1
        return {
            "result": {
                "value": context.inputs["a"]["value"] + context.inputs["b"]["value"]
            }
        }

    def check(_check, context, outputs):
        if context.node_id == "integrate" and outputs["result"]["value"] != 4:
            return CheckResult(False, "durable failure evidence")
        return CheckResult(True, "passed")

    engine = scheduler(
        tmp_path,
        value,
        {
            "producer_a": producer,
            "producer_b": producer,
            "integrate": integrate,
        },
        check,
    )
    route_repair = engine.state.route_repair
    monkeypatch.setattr(
        engine.state,
        "route_repair",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("owner died")),
    )
    with pytest.raises(RuntimeError, match="owner died"):
        engine.run(run_id="repair-after-crash")

    monkeypatch.setattr(engine.state, "route_repair", route_repair)
    result = engine.run(run_id="repair-after-crash", resume=True)

    assert result.status == "succeeded"
    assert calls == {"producer_a": 2, "producer_b": 1, "integrate": 2}


def test_expired_resume_does_not_route_repair_or_delete_accepted_artifact(
    tmp_path, monkeypatch
):
    # intent: deadline enforcement must precede destructive repair-state mutation.
    value = repair_workflow()
    calls = {"producer_a": 0, "producer_b": 0, "integrate": 0}

    def producer(context):
        calls[context.node_id] += 1
        return {"changeset": {"value": 1 if context.node_id == "producer_a" else 2}}

    def integrate(_context):
        calls["integrate"] += 1
        return {"result": {"value": 3}}

    def check(_check, context, _outputs):
        return CheckResult(context.node_id != "integrate", "combined failure")

    engine = scheduler(
        tmp_path,
        value,
        {
            "producer_a": producer,
            "producer_b": producer,
            "integrate": integrate,
        },
        check,
    )
    route_repair = engine.state.route_repair
    monkeypatch.setattr(
        engine.state,
        "route_repair",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("owner died")),
    )
    with pytest.raises(RuntimeError, match="owner died"):
        engine.run(run_id="expired-repair")

    accepted = engine.state.artifact("expired-repair", "producer_a", "changeset")
    assert accepted is not None
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute(
            "UPDATE runs SET created_at=?, updated_at=? WHERE id=?",
            (time.time() - 3_600, time.time() - 3_600, "expired-repair"),
        )
    monkeypatch.setattr(engine.state, "route_repair", route_repair)

    result = engine.run(run_id="expired-repair", resume=True)

    assert result.status == "cancelled"
    assert result.nodes["producer_a"]["status"] == "succeeded"
    assert (
        engine.state.artifact("expired-repair", "producer_a", "changeset") == accepted
    )
    assert calls == {"producer_a": 1, "producer_b": 1, "integrate": 1}


def test_resume_marks_interrupted_attempt_and_reruns_only_incomplete_node(tmp_path):
    value = workflow([node("complete", required=False), node("interrupted")])
    first = scheduler(
        tmp_path,
        value,
        {
            "complete": lambda _: {"result": {"value": 1}},
            "interrupted": lambda _: {"result": {"value": 2}},
        },
    )
    run_id = first.state.create_run(value, "killed-run")
    lease = first.state.acquire_lease(run_id, ttl_seconds=0.2)
    complete_attempt = first.state.start_attempt(run_id, "complete", lease)
    accepted = first._attempt(
        run_id, first.nodes["complete"], complete_attempt, threading.Event()
    )
    first._finalize_attempt(run_id, first.nodes["complete"], accepted, lease)
    first.state.start_attempt(run_id, "interrupted", lease)
    first.state.release_lease(lease)  # owner exits before recording completion

    calls = {"complete": 0, "interrupted": 0}

    def execute(context):
        calls[context.node_id] += 1
        return {"result": {"value": 3}}

    resumed = scheduler(
        tmp_path, value, {"complete": execute, "interrupted": execute}
    ).run(run_id, resume=True)
    assert resumed.status == "succeeded"
    assert calls == {"complete": 0, "interrupted": 1}
    assert resumed.nodes["complete"]["attempt_count"] == 1
    assert resumed.nodes["interrupted"]["attempt_count"] == 2
    with sqlite3.connect(tmp_path / "state.db") as connection:
        old_attempt = connection.execute(
            "SELECT status FROM attempts WHERE run_id='killed-run' AND node_id='interrupted' AND number=1"
        ).fetchone()
    assert old_attempt == ("interrupted",)


def test_identical_failure_digest_stops_no_progress_retry_loop(tmp_path):
    calls = 0

    def stuck(_):
        nonlocal calls
        calls += 1
        raise RuntimeError("same failure")

    result = scheduler(tmp_path, workflow([node("stuck")]), {"stuck": stuck}).run()
    assert result.status == "failed"
    assert calls == 3
    assert result.nodes["stuck"]["no_progress_count"] == 2
    assert len(result.nodes["stuck"]["last_digest"]) == 64


@pytest.mark.parametrize("fault", ["executor", "check", "runner"])
def test_missing_executor_or_check_fails_closed(tmp_path, fault):
    value = workflow([node("only", checks=fault != "check")])
    executors = (
        {} if fault == "executor" else {"only": lambda _: {"result": {"value": 1}}}
    )
    check_runner = None if fault == "runner" else passing_check
    result = scheduler(tmp_path, value, executors, check_runner).run()
    assert result.status == "failed"
    assert result.nodes["only"]["status"] == "failed"
    assert result.nodes["only"]["error"]


def test_cancellation_marks_unstarted_dependents_and_cleans_running_attempt(tmp_path):
    started = threading.Event()
    cancel = threading.Event()

    def long_running(context):
        started.set()
        while not context.cancelled():
            time.sleep(0.01)
        return {"result": {"value": 1}}

    value = workflow([node("first"), node("second", needs=["first"])], concurrency=1)
    engine = scheduler(
        tmp_path,
        value,
        {"first": long_running, "second": lambda _: {"result": {"value": 2}}},
    )
    holder = {}
    thread = threading.Thread(
        target=lambda: holder.setdefault("result", engine.run(cancel_event=cancel))
    )
    thread.start()
    assert started.wait(timeout=2)
    cancel.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert holder["result"].status == "cancelled"
    assert holder["result"].nodes["first"]["status"] == "cancelled"
    assert holder["result"].nodes["second"]["status"] == "cancelled"
