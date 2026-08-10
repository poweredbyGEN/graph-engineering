from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from graph_engineering.artifacts import canonical_json
from graph_engineering.cli import main
from graph_engineering.config import AgentConfig, Routing
from graph_engineering.forking import (
    ForkError,
    build_lineage,
    create_fork,
    verify_lineage,
)
from graph_engineering.lifecycle import (
    LifecycleError,
    LifecycleStore,
    StaticRunContextProvider,
)
from graph_engineering.orchestrator import PortableRuntime
from graph_engineering.state import StateStore


def _sha(value) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _workflow(effect: str = "none") -> dict:
    node = {
        "id": "worker",
        "kind": "agent",
        "required": True,
        "effect": effect,
    }
    return {"id": "fork-test", "nodes": [node]}


def _parent(
    tmp_path: Path, *, effect: str = "none"
) -> tuple[Path, StateStore, LifecycleStore]:
    state_path = tmp_path / "state.db"
    workflow = _workflow(effect)
    state = StateStore(state_path)
    state.create_run(workflow, "parent", lifecycle=True)
    profile_manifest: dict = {}
    digests = [hashlib.sha256(name.encode()).hexdigest() for name in "pqrw"]
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "CREATE TABLE runtime_manifests ("
            "run_id TEXT PRIMARY KEY,base_sha TEXT NOT NULL,"
            "profile_manifest_sha256 TEXT NOT NULL,profile_manifest_json TEXT NOT NULL,"
            "project_policy_sha256 TEXT,private_execution_sha256 TEXT,repository_sha256 TEXT,"
            "workflow_sha256 TEXT,product_contract_sha256 TEXT,"
            "product_contract_generation INTEGER)"
        )
        connection.execute(
            "INSERT INTO runtime_manifests VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "parent",
                "a" * 40,
                _sha(profile_manifest),
                json.dumps(profile_manifest),
                digests[0],
                digests[1],
                digests[2],
                _sha(workflow),
                digests[3],
                1,
            ),
        )
    ledger = LifecycleStore(state_path)
    ledger.initialize_context(
        "parent",
        StaticRunContextProvider(
            {"base_sha": "a" * 40, "workflow_digest": _sha(workflow)}
        ),
    )
    return state_path, state, ledger


def test_fork_is_fresh_append_only_lineage_and_survives_crash_resume(tmp_path: Path):
    # intent: time travel creates a distinct run and never rewrites parent evidence.
    state_path, _parent_state, parent_ledger = _parent(tmp_path)
    parent_before = [event.as_dict() for event in parent_ledger.events("parent")]
    lineage = create_fork(state_path, "parent", 1, "child")

    reopened = StateStore(state_path)
    assert reopened.run("child")["lifecycle_state"] == "pending"
    assert reopened.run("child")["status"] == "pending"
    assert all(
        row["status"] == "pending" for row in reopened.node_rows("child").values()
    )
    assert verify_lineage(state_path, "child") == lineage
    assert [
        event.as_dict() for event in parent_ledger.events("parent")
    ] == parent_before

    child_ledger = LifecycleStore(state_path)
    child_ledger.initialize_context(
        "child",
        StaticRunContextProvider(
            {
                "base_sha": "a" * 40,
                "workflow_digest": lineage["workflow_digest"],
                "fork_lineage": verify_lineage(state_path, "child"),
            }
        ),
    )
    assert child_ledger.events("child")[0].event_type == "run.forked"
    create_fork(state_path, "child", 1, "grandchild")
    assert StateStore(state_path).run("grandchild")["status"] == "pending"
    reopened.activate_fork("child")
    assert reopened.run("child")["status"] == "running"
    lease = reopened.acquire_lease("child", ttl_seconds=30)
    reopened.start_attempt("child", "worker", lease)
    reopened.release_lease(lease)

    # A fresh process resumes the same child budget and fences the interrupted attempt.
    after_crash = StateStore(state_path)
    resumed = after_crash.acquire_lease("child", ttl_seconds=30, lifecycle_resume=True)
    assert after_crash.recover_interrupted("child", resumed, {"worker": True}) == ()
    assert after_crash.node_rows("child")["worker"]["status"] == "pending"
    after_crash.release_lease(resumed)


def test_fork_rejects_unsafe_effect_and_in_flight_checkpoint(tmp_path: Path):
    # intent: a child cannot silently replay a prior external or otherwise uncertain effect.
    state_path, state, ledger = _parent(tmp_path, effect="non_idempotent_write")
    lease = state.acquire_lease("parent", ttl_seconds=30)
    attempt = state.start_attempt("parent", "worker", lease)
    ledger.append(
        "parent",
        "check:midflight",
        "check.completed",
        node_id="worker",
        attempt=attempt,
    )
    with pytest.raises(ForkError, match="FORK_EVENT_IN_FLIGHT"):
        create_fork(state_path, "parent", len(ledger.events("parent")), "inflight")
    state.finish_attempt("parent", "worker", attempt, "failed", "b" * 64, "boom", lease)
    state.release_lease(lease)
    with pytest.raises(ForkError, match="FORK_EFFECT_REPLAY_UNSAFE"):
        create_fork(state_path, "parent", len(ledger.events("parent")), "unsafe")
    with pytest.raises(KeyError):
        state.run("unsafe")


def test_fork_rejects_artifact_parent_and_lineage_tampering(tmp_path: Path):
    # intent: every lineage claim is checked before creation and again before child resume.
    state_path, _state, ledger = _parent(tmp_path)
    payload = canonical_json({"ok": True})
    digest = hashlib.sha256(payload).hexdigest()
    path = state_path.parent / "artifacts" / digest[:2] / f"{digest}.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    ledger.append(
        "parent",
        "artifact:worker:1:result",
        "artifact.accepted",
        node_id="worker",
        attempt=1,
        payload={"output_name": "result", "digest": digest},
    )
    path.write_bytes(b"tampered")
    with pytest.raises(ForkError, match="FORK_ARTIFACT_CORRUPT"):
        create_fork(state_path, "parent", 2, "bad-artifact")

    path.write_bytes(payload)
    create_fork(state_path, "parent", 2, "child")
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE lifecycle_events SET digest=? WHERE run_id='parent' AND sequence=2",
            ("f" * 64,),
        )
    with pytest.raises(LifecycleError, match="EVENT_LEDGER_CORRUPT"):
        verify_lineage(state_path, "child")


def test_fork_rejects_stored_lineage_digest_drift(tmp_path: Path):
    # intent: editing lineage JSON cannot redirect a child to another parent checkpoint.
    state_path, _state, _ledger = _parent(tmp_path)
    create_fork(state_path, "parent", 1, "child")
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE run_forks SET lineage_json=? WHERE run_id='child'",
            ('{"version":"changed"}',),
        )
    with pytest.raises(ValueError, match="lineage digest mismatch"):
        verify_lineage(state_path, "child")


def test_fork_insert_rechecks_parent_evidence_atomically(tmp_path: Path):
    # intent: parent drift between planning and BEGIN IMMEDIATE leaves no child.
    state_path, state, _ledger = _parent(tmp_path)
    lineage = build_lineage(state_path, "parent", 1)
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "UPDATE lifecycle_contexts SET digest=? WHERE run_id='parent'",
            ("f" * 64,),
        )
    with pytest.raises(ValueError, match="evidence drifted"):
        state.create_fork_run("parent", "child", lineage)
    with pytest.raises(KeyError):
        state.run("child")


def test_fork_cli_exposes_exact_lineage_without_mutating_parent(tmp_path: Path, capsys):
    # intent: operators get an explicit machine-readable fork command and stable error taxonomy.
    state_path, _state, ledger = _parent(tmp_path)
    assert (
        main(
            [
                "fork",
                "--state",
                str(state_path),
                "--run-id",
                "parent",
                "--at-sequence",
                "1",
                "--new-run-id",
                "child",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "pending"
    assert (
        payload["lineage"]["parent_event"]["digest"]
        == ledger.events("parent")[0].digest
    )
    assert (
        main(
            [
                "fork",
                "--state",
                str(state_path),
                "--run-id",
                "parent",
                "--at-sequence",
                "1",
                "--new-run-id",
                "child",
                "--json",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "FORK_RUN_EXISTS"


def test_portable_runtime_resumes_fresh_fork_with_bound_context(tmp_path: Path):
    # intent: the public resume path verifies lineage, activates the child, and executes it.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Graph Test"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "graph@example.com"],
        check=True,
    )
    (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    workflow = {
        "version": "graph-engineering/v1alpha1",
        "id": "fork-runtime",
        "goal": "prove a child resumes",
        "budgets": {
            "max_nodes": 2,
            "max_concurrency": 1,
            "max_attempts_per_node": 1,
            "max_total_attempts": 1,
            "timeout_seconds": 10,
        },
        "nodes": [
            {
                "id": "worker",
                "kind": "transform",
                "task": "return deterministic evidence",
                "needs": [],
                "inputs": {},
                "outputs": {
                    "result": {
                        "schema": {
                            "type": "object",
                            "required": ["value"],
                            "properties": {"value": {"type": "integer"}},
                        }
                    }
                },
                "workspace": "read-only",
                "permission": "read",
                "effect": "read",
                "checks": [{"id": "accepted", "argv": ["true"]}],
                "retry": {"max_attempts": 1, "no_progress_limit": 1},
                "required": True,
            }
        ],
        "outputs": {"result": "worker.result"},
    }
    config = AgentConfig({}, {}, {}, Routing())

    def runtime() -> PortableRuntime:
        return PortableRuntime(
            workflow,
            config,
            repo=repo,
            state_path=repo / "state.db",
            artifact_root=repo / "artifacts",
            executors={"worker": lambda _context: {"result": {"value": 1}}},
        )

    parent = runtime().run(run_id="parent")
    assert parent.run.status == "succeeded", parent.run.nodes
    create_fork(repo / "state.db", "parent", len(parent.lifecycle_events), "child")
    child = runtime().run(run_id="child", resume=True)
    assert child.run.status == "succeeded"
    assert child.run_context.values["fork_lineage"]["parent_run_id"] == "parent"
    assert child.lifecycle_events[0].event_type == "run.forked"
