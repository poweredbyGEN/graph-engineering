from __future__ import annotations

import copy
from pathlib import Path

import pytest

from graph_engineering import CheckResult, Scheduler
from graph_engineering.contracts import WorkflowValidationError, validate_workflow
from graph_engineering.state import StateStore
from graph_engineering.supervision import analyze_topology, live_topology

SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
}


def node(node_id: str, *, needs: list[str] | None = None) -> dict:
    return {
        "id": node_id,
        "kind": "transform",
        "task": f"produce {node_id}",
        "needs": needs or [],
        "inputs": {},
        "outputs": {"result": {"schema": copy.deepcopy(SCHEMA)}},
        "workspace": "read-only",
        "permission": "read",
        "checks": [{"id": "accept", "argv": ["test", node_id]}],
        "retry": {"max_attempts": 2, "no_progress_limit": 1},
        "required": True,
    }


def workflow(nodes: list[dict]) -> dict:
    return {
        "version": "graph-engineering/v1alpha1",
        "id": "supervision_test",
        "goal": "prove bounded progress and graph shape",
        "budgets": {
            "max_nodes": 20,
            "max_concurrency": 4,
            "max_attempts_per_node": 3,
            "max_total_attempts": 20,
            "timeout_seconds": 30,
        },
        "nodes": nodes,
        "outputs": {"result": f"{nodes[-1]['id']}.result"},
    }


def issue_codes(value: dict) -> set[str]:
    with pytest.raises(WorkflowValidationError) as raised:
        validate_workflow(value)
    return {issue.code for issue in raised.value.issues}


def test_progress_contract_names_deterministic_checks_and_keeps_legacy_alias():
    modern = workflow([node("worker")])
    modern["nodes"][0]["progress"] = {"max_deterministic_checks": 3}
    validate_workflow(modern)

    legacy = workflow([node("worker")])
    legacy["nodes"][0]["progress"] = {"max_commands": 3}
    validate_workflow(legacy)

    ambiguous = workflow([node("worker")])
    ambiguous["nodes"][0]["progress"] = {
        "max_deterministic_checks": 3,
        "max_commands": 3,
    }
    assert "SCHEMA_ERROR" in issue_codes(ambiguous)


def test_mechanical_shape_linter_rejects_mega_node_without_model_calls():
    # intent: one agent cannot hide several independently verifiable deliverables in one node.
    value = workflow([node("mega")])
    value["nodes"][0]["outputs"] = {
        name: {"schema": copy.deepcopy(SCHEMA)}
        for name in ("api", "cli", "docs", "tests")
    }
    value["outputs"] = {name: f"mega.{name}" for name in value["nodes"][0]["outputs"]}

    assert "MEGA_NODE" in issue_codes(value)


def test_mechanical_shape_linter_rejects_dead_optional_work():
    # intent: optional work that unlocks nothing cannot consume fleet time invisibly.
    dead = node("dead")
    dead.update({"kind": "agent", "profile": "worker", "required": False})
    value = workflow([dead, node("result")])

    assert "DISCONNECTED_OPTIONAL_NODE" in issue_codes(value)


def test_shape_linter_rejects_uncontracted_research_and_fake_barrier():
    # intent: research must hand off typed data and a one-input barrier is only a fake edge.
    research = node("research")
    research.update({"kind": "agent", "profile": "research", "outputs": {}})
    fake = node("fake", needs=["research"])
    fake["join"] = {"policy": "all"}
    value = workflow([research, fake])

    codes = issue_codes(value)
    assert "RESEARCH_OUTPUT_REQUIRED" in codes
    assert "REDUNDANT_BARRIER" in codes


def test_shape_linter_rejects_optional_work_that_only_delays_integration():
    # intent: integration cannot wait for an optional sibling whose artifact it never consumes.
    required = node("required")
    optional = node("optional")
    optional["required"] = False
    integrate = node("integrate", needs=["required", "optional"])
    integrate["kind"] = "integration"
    value = workflow([required, optional, integrate])

    assert "OPTIONAL_INTEGRATION_DELAY" in issue_codes(value)


def test_topology_exposes_terminal_slice_critical_path_and_live_frontier():
    # intent: operators can see the neck and the runnable frontier without model judgment.
    value = workflow(
        [
            node("root"),
            node("side"),
            node("middle", needs=["root"]),
            node("result", needs=["middle", "side"]),
        ]
    )
    topology = analyze_topology(value)
    assert topology["initial_frontier"] == ["root", "side"]
    assert topology["critical_path"] == ["root", "middle", "result"]
    assert topology["terminal_slice"] == ["root", "side", "middle", "result"]
    rows = {
        "root": {"status": "succeeded"},
        "side": {"status": "pending"},
        "middle": {"status": "pending"},
        "result": {"status": "pending"},
    }
    live = live_topology(value, rows)
    assert live["ready_frontier"] == ["middle", "side"]
    assert live["critical_path_remaining"] == ["middle", "result"]
    assert live["transitive_unlocks"]["root"] == ["middle", "result"]


def test_live_frontier_reports_early_quorum_release():
    # intent: status shows a quorum consumer ready while an unrelated voter is still running.
    members = [node(name) for name in ("a", "b", "c")]
    for member in members:
        member["required"] = False
    join = node("join", needs=["a", "b", "c"])
    join["join"] = {"policy": "majority"}
    value = workflow([*members, join])
    rows = {
        "a": {"status": "succeeded"},
        "b": {"status": "succeeded"},
        "c": {"status": "running"},
        "join": {"status": "pending"},
    }

    assert live_topology(value, rows)["ready_frontier"] == ["join"]


def _scheduler(
    tmp_path: Path, values: list[int], *, no_progress_limit: int
) -> Scheduler:
    candidate = node("worker")
    candidate["retry"] = {
        "max_attempts": len(values),
        "no_progress_limit": no_progress_limit,
    }
    value = workflow([candidate])
    value["budgets"]["max_attempts_per_node"] = len(values)
    value["budgets"]["max_total_attempts"] = len(values)
    pending = iter(values)

    def executor(_context):
        return {"result": {"value": next(pending)}}

    return Scheduler(
        value,
        tmp_path / "state.db",
        tmp_path / "artifacts",
        {"worker": executor},
        lambda *_args: CheckResult(False, "same deterministic failure"),
    )


def test_no_artifact_delta_and_repeated_error_digest_reach_bounded_stop(tmp_path: Path):
    # intent: identical artifacts and failures stop locally instead of buying blind attempts.
    engine = _scheduler(tmp_path, [1, 1, 2], no_progress_limit=1)
    result = engine.run("no-progress")

    assert result.status == "failed"
    assert result.nodes["worker"]["attempt_count"] == 2
    progress = engine.state.progress_rows(result.run_id)["worker"]
    assert progress["artifact_delta"] == 0
    assert progress["repeated_failure"] == 1
    assert progress["no_progress_count"] == 1
    assert progress["decision"] == "stop"
    assert progress["reason"] == "repeated no-progress digest"
    assert progress["deterministic_check_count"] == 2
    assert "command_count" not in progress
    assert len(progress["failure_digest"]) == 64


def test_artifact_delta_resets_no_progress_and_updates_last_progress(tmp_path: Path):
    # intent: changed accepted-shape evidence resets the bounded no-progress counter.
    engine = _scheduler(tmp_path, [1, 1, 2], no_progress_limit=2)
    result = engine.run("artifact-progress")

    assert result.nodes["worker"]["attempt_count"] == 3
    progress = engine.state.progress_rows(result.run_id)["worker"]
    assert progress["artifact_delta"] == 1
    assert progress["repeated_failure"] == 1
    assert progress["no_progress_count"] == 0
    assert progress["decision"] == "continue"
    assert progress["last_meaningful_progress_at"] == progress["last_observed_at"]


def test_reopened_state_preserves_original_elapsed_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # intent: process resume cannot mint a fresh per-node elapsed allowance.
    value = workflow([node("worker")])
    value["nodes"][0]["progress"] = {"max_elapsed_seconds": 7, "max_commands": 5}
    state_path = tmp_path / "state.db"
    state = StateStore(state_path)
    state.create_run(value, "resume-budget")
    lease = state.acquire_lease("resume-budget")
    attempt = state.start_attempt("resume-budget", "worker", lease)
    state.finish_attempt(
        "resume-budget",
        "worker",
        attempt,
        "failed",
        "a" * 64,
        "first",
        lease,
        command_count=1,
    )
    state.set_node_status("resume-budget", "worker", "pending", "retry", lease)
    before = state.progress_rows("resume-budget")["worker"]
    state.release_lease(lease)

    reopened = StateStore(state_path)
    resumed = reopened.acquire_lease("resume-budget", ttl_seconds=100)
    after = reopened.progress_rows("resume-budget")["worker"]
    assert after["started_at"] == before["started_at"]
    assert after["deadline_at"] == before["deadline_at"]
    assert after["max_elapsed_seconds"] == 7
    assert after["max_deterministic_checks"] == 5
    assert "max_commands" not in after
    monkeypatch.setattr(
        "graph_engineering.state.time.time", lambda: float(after["deadline_at"]) + 1
    )
    assert not reopened.admit_resumed_attempt("resume-budget", "worker", resumed)
    assert reopened.node_rows("resume-budget")["worker"]["attempt_count"] == 1
    stopped = reopened.progress_rows("resume-budget")["worker"]
    assert stopped["decision"] == "stop"
    assert stopped["reason"] == "elapsed budget exhausted on resume"
    reopened.release_lease(resumed)
