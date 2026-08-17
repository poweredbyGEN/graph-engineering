from __future__ import annotations

import sqlite3
import sys

import pytest

from graph_engineering.builtins import (
    OPERATIONS,
    BuiltinOperationError,
    builtin_executor,
)
from graph_engineering.config import parse_agent_config
from graph_engineering.contracts import WorkflowValidationError, validate_workflow
from graph_engineering.orchestrator import PortableRuntime
from graph_engineering.runtime import CheckResult, ExecutionContext, Scheduler

SCHEMA = {}


def context(inputs):
    return ExecutionContext("run", "node", 1, inputs, lambda: False)


@pytest.mark.parametrize(
    ("name", "inputs", "config", "expected"),
    [
        (
            "schema_validate",
            {"value": 3},
            {"schema": {"type": "integer"}},
            {"valid": True, "errors": []},
        ),
        ("select", {"value": {"a": 1, "b": 2}}, {"fields": ["b"]}, {"b": 2}),
        (
            "map",
            {"value": [{"a": 1}, {"a": 2}]},
            {"fields": {"value": "a"}},
            [{"value": 1}, {"value": 2}],
        ),
        ("stable_union", {"a": [2, 1], "b": [1, 3]}, {"inputs": ["a", "b"]}, [2, 1, 3]),
        ("dedupe", {"value": [{"a": 1}, {"a": 1}, {"a": 2}]}, {}, [{"a": 1}, {"a": 2}]),
        ("sort", {"value": [{"n": 2}, {"n": 1}]}, {"by": ["n"]}, [{"n": 1}, {"n": 2}]),
        (
            "typed_predicate",
            {"value": 2},
            {"predicate": {"op": "gt", "value": 1}},
            {"matched": True},
        ),
        (
            "risk_router",
            {"value": 7},
            {"medium_at": 4, "high_at": 7},
            {"risk": "high", "score": 7},
        ),
        (
            "verdict_reducer",
            {"value": [{"verdict": "pass"}, {"verdict": "fail"}]},
            {},
            {"verdict": "fail", "counts": {"pass": 1, "warn": 0, "fail": 1}},
        ),
    ],
)
def test_closed_builtin_registry_operations(name, inputs, config, expected):
    # intent: every advertised operation must be deterministic and executable without eval.
    executor = builtin_executor({"name": name, "output": "result", "config": config})
    assert executor(context(inputs)) == {"result": expected}


def test_builtin_registry_rejects_unregistered_code_surface():
    # intent: sabotage-check the closed registry; a workflow cannot smuggle Python or shell.
    assert set(OPERATIONS) == {
        "schema_validate",
        "select",
        "map",
        "stable_union",
        "dedupe",
        "sort",
        "typed_predicate",
        "risk_router",
        "verdict_reducer",
    }
    with pytest.raises(BuiltinOperationError):
        builtin_executor(
            {"name": "eval", "output": "result", "config": {"code": "1+1"}}
        )


def operation_node(
    node_id, operation, *, needs=None, inputs=None, route=None, loop=None, retries=1
):
    value = {
        "id": node_id,
        "kind": "transform",
        "task": f"run {operation['name']}",
        "needs": needs or [],
        "inputs": inputs or {},
        "outputs": {"result": {"schema": SCHEMA}},
        "workspace": "read-only",
        "permission": "read",
        "effect": "none",
        "checks": [{"id": "proof", "argv": [sys.executable, "-c", "pass"]}],
        "retry": {"max_attempts": retries, "no_progress_limit": retries},
        "required": True,
        "operation": {**operation, "output": "result"},
    }
    if route is not None:
        value["route"] = route
    if loop is not None:
        value["loop"] = loop
    return value


def graph(nodes, output, *, attempts=20):
    return {
        "version": "graph-engineering/v1alpha1",
        "id": "builtin_route_test",
        "goal": "exercise typed deterministic control flow",
        "budgets": {
            "max_nodes": 20,
            "max_concurrency": 3,
            "max_attempts_per_node": 5,
            "max_total_attempts": attempts,
            "timeout_seconds": 30,
        },
        "nodes": nodes,
        "outputs": {"result": output},
    }


def test_portable_runtime_executes_builtin_without_custom_executor(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    node = operation_node(
        "risk",
        {"name": "risk_router", "config": {"literal": 8, "medium_at": 4, "high_at": 7}},
    )
    value = graph([node], "risk.result")
    runtime = PortableRuntime(
        value,
        parse_agent_config({"version": 1, "profiles": {}}),
        repo=repo,
        state_path=repo / "state.db",
        artifact_root=repo / "artifacts",
        environ={"TMPDIR": str(tmp_path)},
    )
    result = runtime.run()
    assert result.run.status == "succeeded"
    assert result.outputs["result"] == {"risk": "high", "score": 8}


def test_conditional_routes_persist_selected_and_skipped_edges(tmp_path):
    router = operation_node(
        "router",
        {
            "name": "select",
            "config": {"literal": {"route": "high"}, "fields": ["route"]},
        },
    )
    high = operation_node(
        "high",
        {
            "name": "select",
            "config": {"literal": {"accepted": True}, "fields": ["accepted"]},
        },
        needs=["router"],
        route={
            "source": "router.result",
            "path": ["route"],
            "predicate": {"op": "equals", "value": "high"},
        },
    )
    low = operation_node(
        "low",
        {
            "name": "select",
            "config": {"literal": {"accepted": False}, "fields": ["accepted"]},
        },
        needs=["router"],
        route={
            "source": "router.result",
            "path": ["route"],
            "predicate": {"op": "equals", "value": "low"},
        },
    )
    low["required"] = False
    value = graph([router, high, low], "high.result")
    executors = {
        node["id"]: builtin_executor(node["operation"]) for node in value["nodes"]
    }
    scheduler = Scheduler(
        value,
        tmp_path / "state.db",
        tmp_path / "artifacts",
        executors,
        lambda *_: CheckResult(True),
    )
    result = scheduler.run("conditional")
    assert result.status == "succeeded"
    assert result.nodes["high"]["status"] == "succeeded"
    assert result.nodes["low"]["status"] == "skipped"
    with sqlite3.connect(tmp_path / "state.db") as connection:
        assert connection.execute(
            "SELECT node_id,matched FROM route_states ORDER BY node_id"
        ).fetchall() == [("high", 1), ("low", 0)]


def test_bounded_loop_checkpoints_each_iteration_and_resumes_without_replay(tmp_path):
    calls = {"worker": 0}
    worker = operation_node(
        "worker",
        {"name": "select", "config": {"literal": {"value": 0}, "fields": ["value"]}},
        retries=3,
    )
    controller = operation_node(
        "controller",
        {
            "name": "typed_predicate",
            "config": {
                "input": "value",
                "path": ["value"],
                "predicate": {"op": "lt", "value": 3},
            },
        },
        needs=["worker"],
        inputs={"value": "worker.result"},
        loop={
            "target": "worker",
            "output": "result",
            "path": ["matched"],
            "predicate": {"op": "equals", "value": True},
            "max_iterations": 3,
        },
        retries=3,
    )
    value = graph([worker, controller], "controller.result", attempts=6)

    def execute_worker(_context):
        calls["worker"] += 1
        return {"result": {"value": calls["worker"]}}

    executors = {
        "worker": execute_worker,
        "controller": builtin_executor(controller["operation"]),
    }
    scheduler = Scheduler(
        value,
        tmp_path / "state.db",
        tmp_path / "artifacts",
        executors,
        lambda *_: CheckResult(True),
    )
    result = scheduler.run("loop")
    assert result.status == "succeeded"
    assert result.nodes["worker"]["attempt_count"] == 3
    assert result.nodes["controller"]["attempt_count"] == 3
    with sqlite3.connect(tmp_path / "state.db") as connection:
        assert connection.execute(
            "SELECT iterations,last_controller_attempt,matched FROM loop_states"
        ).fetchone() == (3, 3, 0)
    resumed = scheduler.run("loop", resume=True)
    assert resumed.status == "succeeded"
    assert calls["worker"] == 3


def test_loop_contract_rejects_unbounded_or_effectful_replay():
    worker = operation_node(
        "worker", {"name": "dedupe", "config": {"literal": []}}, retries=2
    )
    controller = operation_node(
        "controller",
        {
            "name": "typed_predicate",
            "config": {"literal": True, "predicate": {"op": "equals", "value": True}},
        },
        needs=["worker"],
        loop={
            "target": "worker",
            "output": "result",
            "predicate": {"op": "equals", "value": True},
            "max_iterations": 2,
        },
        retries=2,
    )
    worker["permission"] = "external"
    worker["effect"] = "non_idempotent_write"
    worker["approval"] = "missing"
    value = graph([worker, controller], "controller.result", attempts=4)
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "UNSAFE_LOOP_EFFECT" in {issue.code for issue in caught.value.issues}


def test_bounded_loop_fails_closed_when_predicate_remains_true_at_ceiling(tmp_path):
    # intent: sabotage-check the hard stop; a loop cannot silently accept its last bad value.
    worker = operation_node(
        "worker", {"name": "dedupe", "config": {"literal": [1]}}, retries=2
    )
    controller = operation_node(
        "controller",
        {
            "name": "typed_predicate",
            "config": {
                "literal": True,
                "predicate": {"op": "equals", "value": True},
            },
        },
        needs=["worker"],
        loop={
            "target": "worker",
            "output": "result",
            "path": ["matched"],
            "predicate": {"op": "equals", "value": True},
            "max_iterations": 2,
        },
        retries=2,
    )
    value = graph([worker, controller], "controller.result", attempts=4)
    executors = {
        node["id"]: builtin_executor(node["operation"]) for node in value["nodes"]
    }
    scheduler = Scheduler(
        value,
        tmp_path / "state.db",
        tmp_path / "artifacts",
        executors,
        lambda *_: CheckResult(True),
    )
    result = scheduler.run("ceiling")
    assert result.status == "failed"
    assert result.nodes["controller"]["status"] == "failed"
    assert result.nodes["controller"]["attempt_count"] == 2
