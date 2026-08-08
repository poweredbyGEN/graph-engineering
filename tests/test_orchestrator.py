from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from graph_engineering.config import CapabilityMismatchError, parse_agent_config
from graph_engineering.orchestrator import (
    CHANGE_SET_SCHEMA,
    CheckCommandReceipt,
    OrchestrationError,
    PortableRuntime,
)
from graph_engineering.runtime import ExecutionContext

WORKER = r"""
import json
import os
import pathlib
import re
import sys
import time

prompt = sys.stdin.read()
match = re.search(r"WRITE ([^ ]+) ([^\n]+)", prompt)
if match:
    target = pathlib.Path(match.group(1))
    target.parent.mkdir(parents=True, exist_ok=True)
    content = match.group(2)
    if '"integration_failure":' in prompt and content == "bad":
        content = "repaired"
    target.write_text(content + "\n", encoding="utf-8")
time.sleep(0.15)
print(json.dumps({
    "ok": True,
    "secret_seen": "GRAPH_TEST_SECRET" in os.environ,
}))
"""

AGENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok", "secret_seen"],
    "properties": {
        "ok": {"const": True},
        "secret_seen": {"const": False},
    },
}

INTEGRATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "base_sha",
        "changed_paths",
        "change_digests",
        "integration_digest",
    ],
    "properties": {
        "base_sha": {"type": "string"},
        "changed_paths": {"type": "array", "items": {"type": "string"}},
        "change_digests": {"type": "array", "items": {"type": "string"}},
        "integration_digest": {"type": "string"},
    },
}


def git(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Graph Test")
    git(root, "config", "user.email", "graph@example.com")
    (root / "src").mkdir()
    (root / "src" / "base.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", "--", "src/base.txt")
    git(root, "commit", "-qm", "base")
    return root


def config(*, write: bool = True, mcp: bool = True, model: str = "test-model"):
    return parse_agent_config(
        {
            "version": 1,
            "profiles": {
                "worker": {
                    "adapter": "subprocess",
                    "model": model,
                    "capabilities": {
                        "read": True,
                        "write": write,
                        "structured_output": True,
                        "worktree": write,
                        "resume": False,
                        "mcp": mcp,
                    },
                    "subprocess": {
                        "argv": [
                            sys.executable,
                            "-c",
                            WORKER.replace("{", "{{").replace("}", "}}"),
                        ],
                        "prompt_transport": "stdin",
                        "output_format": "json",
                        "env_allowlist": [],
                    },
                }
            },
        }
    )


def writer(node_id: str, path: str, content: str, *, scope: str | None = None) -> dict:
    return {
        "id": node_id,
        "kind": "agent",
        "task": f"WRITE {path} {content}",
        "needs": [],
        "inputs": {},
        "outputs": {
            "result": {"schema": AGENT_SCHEMA},
            "changeset": {"schema": copy.deepcopy(CHANGE_SET_SCHEMA)},
        },
        "profile": "worker",
        "workspace": "worktree",
        "write_scope": [scope or path],
        "permission": "write",
        "effect": "none",
        "checks": [
            {
                "id": "file_exists",
                "argv": [
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; assert Path({path!r}).is_file()",
                ],
            }
        ],
        "retry": {"max_attempts": 1, "no_progress_limit": 1},
        "required": True,
    }


def integration(*, passing: bool = True) -> dict:
    command = (
        "from pathlib import Path; "
        "assert Path('src/first.txt').read_text() == 'first\\n'; "
        "assert Path('src/second.txt').read_text() == 'second\\n'"
        if passing
        else "raise SystemExit(7)"
    )
    return {
        "id": "integrate",
        "kind": "integration",
        "task": "integrate accepted changes",
        "needs": ["writer_a", "writer_b"],
        "inputs": {
            "first": "writer_a.changeset",
            "second": "writer_b.changeset",
        },
        "outputs": {"result": {"schema": INTEGRATION_SCHEMA}},
        "workspace": "worktree",
        "write_scope": ["src/**"],
        "permission": "write",
        "checks": [{"id": "combined", "argv": [sys.executable, "-c", command]}],
        "retry": {"max_attempts": 1, "no_progress_limit": 1},
        "required": True,
    }


def workflow(*, passing: bool = True, first_scope: str | None = None) -> dict:
    nodes = [
        writer("writer_a", "src/first.txt", "first", scope=first_scope),
        writer("writer_b", "src/second.txt", "second"),
        integration(passing=passing),
    ]
    return {
        "version": "graph-engineering/v1alpha1",
        "id": "portable_runtime_test",
        "goal": "integrate two independent writer artifacts",
        "budgets": {
            "max_nodes": 10,
            "max_concurrency": 2,
            "max_attempts_per_node": 1,
            "max_total_attempts": 3,
            "timeout_seconds": 20,
        },
        "nodes": nodes,
        "outputs": {"result": "integrate.result"},
    }


def runtime(
    repo: Path,
    value: dict,
    *,
    agent_config=None,
    environ: dict[str, str] | None = None,
    executors=None,
    base: str = "HEAD",
) -> PortableRuntime:
    # intent: portable tests must not depend on the operator machine's scratch mount.
    runtime_environ = (
        os.environ | {"TMPDIR": str(repo.parent)} if environ is None else environ
    )
    return PortableRuntime(
        value,
        agent_config or config(),
        repo=repo,
        state_path=repo / ".graph-state.db",
        artifact_root=repo / ".graph-artifacts",
        environ=runtime_environ,
        executors=executors,
        base=base,
    )


def test_parallel_disjoint_writers_transfer_artifacts_into_one_integration(repo: Path):
    environment = os.environ | {
        "TMPDIR": str(repo.parent),
        "GRAPH_TEST_SECRET": "must-not-leak",
    }
    result = runtime(repo, workflow(), environ=environment).run(run_id="parallel")

    assert result.run.status == "succeeded"
    assert result.outputs["result"]["changed_paths"] == [
        "src/first.txt",
        "src/second.txt",
    ]
    assert len(result.outputs["result"]["change_digests"]) == 2
    assert {key.split("#")[0] for key in result.worktrees} == {
        "writer_a",
        "writer_b",
        "integrate",
    }
    assert len(set(result.worktrees.values())) == 3
    starts = [receipt.started_at_unix for receipt in result.agent_receipts.values()]
    assert max(starts) - min(starts) < 0.5
    integration_path = result.worktrees["integrate#1"]
    assert (integration_path / "src/first.txt").read_text() == "first\n"
    assert (integration_path / "src/second.txt").read_text() == "second\n"
    assert not (repo / "src/first.txt").exists()
    assert len(result.check_receipts) == 3


def test_agent_prompt_embeds_the_exact_canonical_output_contract(repo: Path):
    instance = runtime(repo, workflow())
    node = workflow()["nodes"][0]
    prompt = instance._prompt(
        node,
        ExecutionContext(
            run_id="prompt-contract",
            node_id="writer_a",
            attempt=1,
            inputs={"upstream": {"value": 7}},
            cancelled=lambda: False,
        ),
    )

    schema_json = json.dumps(
        AGENT_SCHEMA,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    assert schema_json in prompt
    assert '{"upstream":{"value":7}}' in prompt
    assert "Do not wrap it in Markdown" in prompt


def test_profile_capability_rejection_happens_before_any_worker_spawn(repo: Path):
    with pytest.raises(CapabilityMismatchError, match="write"):
        runtime(repo, workflow(), agent_config=config(write=False))
    assert not (repo / ".claude/worktrees/graph-runs").exists()


def test_unsupported_openai_adapter_is_rejected_before_worker_spawn(repo: Path):
    raw = {
        "version": 1,
        "profiles": {
            "worker": {
                "adapter": "openai-compatible",
                "model": "api-model",
                "capabilities": {
                    "read": True,
                    "write": True,
                    "structured_output": True,
                    "worktree": True,
                    "resume": False,
                    "mcp": True,
                },
                "openai_compatible": {
                    "endpoint_env": "API_ENDPOINT",
                    "api_key_env": "API_KEY",
                },
            }
        },
    }
    with pytest.raises(OrchestrationError, match="UNSUPPORTED_ADAPTER"):
        runtime(repo, workflow(), agent_config=parse_agent_config(raw))
    assert not (repo / ".claude/worktrees/graph-runs").exists()


def test_writer_scope_escape_is_rejected_before_integration(repo: Path):
    value = workflow(first_scope="src/allowed/**")
    result = runtime(repo, value).run(run_id="scope-escape")

    assert result.run.status == "failed"
    assert result.run.nodes["writer_a"]["status"] == "failed"
    assert "escaped write scope" in result.run.nodes["writer_a"]["error"]
    assert result.run.nodes["integrate"]["status"] == "blocked"


def test_failed_combined_gate_rejects_green_writer_artifacts(repo: Path):
    result = runtime(repo, workflow(passing=False)).run(run_id="bad-gate")

    assert result.run.status == "failed"
    assert result.run.nodes["writer_a"]["status"] == "succeeded"
    assert result.run.nodes["writer_b"]["status"] == "succeeded"
    assert result.run.nodes["integrate"]["status"] == "failed"
    assert result.outputs == {}


def test_failed_combined_gate_repairs_only_explicit_producer_then_reintegrates(
    repo: Path,
):
    value = workflow()
    value["budgets"].update(
        max_attempts_per_node=2,
        max_total_attempts=5,
    )
    writer_a = value["nodes"][0]
    writer_a["task"] = "WRITE src/first.txt bad"
    writer_a["retry"]["max_attempts"] = 2
    integrate = value["nodes"][2]
    integrate["checks"][0]["argv"] = [
        sys.executable,
        "-c",
        (
            "from pathlib import Path; "
            "assert Path('src/first.txt').read_text() == 'repaired\\n'; "
            "assert Path('src/second.txt').read_text() == 'second\\n'"
        ),
    ]
    integrate["retry"]["max_attempts"] = 2
    integrate["repair"] = {
        "routes": [
            {
                "id": "combined_to_writer_a",
                "check_ids": ["combined"],
                "targets": [{"node": "writer_a", "input": "integration_failure"}],
                "max_rounds": 1,
                "no_progress_limit": 1,
            }
        ]
    }

    result = runtime(repo, value).run(run_id="repair-composition")

    assert result.run.status == "succeeded"
    assert result.run.nodes["writer_a"]["attempt_count"] == 2
    assert result.run.nodes["writer_b"]["attempt_count"] == 1
    assert result.run.nodes["integrate"]["attempt_count"] == 2
    assert (result.worktrees["writer_a#1"] / "src/first.txt").read_text() == "bad\n"
    assert (
        result.worktrees["writer_a#2"] / "src/first.txt"
    ).read_text() == "repaired\n"
    assert (
        result.worktrees["integrate#2"] / "src/first.txt"
    ).read_text() == "repaired\n"
    assert "writer_b#2" not in result.worktrees
    assert len(result.check_receipts) == 5


def test_check_cannot_mutate_the_artifact_it_is_supposed_to_grade(repo: Path):
    value = workflow()
    value["nodes"][0]["checks"][0]["argv"] = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('src/first.txt').write_text('tampered\\n')",
    ]
    result = runtime(repo, value).run(run_id="mutating-check")

    assert result.run.status == "failed"
    assert result.run.nodes["writer_a"]["status"] == "failed"
    assert "mutated the accepted workspace" in result.run.nodes["writer_a"]["error"]
    assert result.run.nodes["integrate"]["status"] == "blocked"


def test_indirect_shell_launcher_is_rejected_during_preflight(repo: Path):
    value = workflow()
    value["nodes"][0]["checks"][0]["argv"] = [
        "/usr/bin/env",
        "bash",
        "-c",
        "true",
    ]

    with pytest.raises(OrchestrationError, match="SHELL_CHECK_FORBIDDEN"):
        runtime(repo, value)
    assert not (repo / ".claude/worktrees/graph-runs").exists()


def test_custom_write_executor_cannot_fall_back_to_the_main_checkout(repo: Path):
    node = {
        "id": "custom_writer",
        "kind": "transform",
        "task": "an unsafe in-process write",
        "needs": [],
        "inputs": {},
        "outputs": {"result": {"schema": {"type": "object"}}},
        "workspace": "worktree",
        "write_scope": ["src/custom.txt"],
        "permission": "write",
        "checks": [{"id": "accepted", "argv": ["true"]}],
        "required": True,
    }
    value = {
        "version": "graph-engineering/v1alpha1",
        "id": "custom_write_test",
        "goal": "prove custom writes fail closed",
        "budgets": {
            "max_nodes": 1,
            "max_concurrency": 1,
            "max_attempts_per_node": 1,
            "max_total_attempts": 1,
            "timeout_seconds": 10,
        },
        "nodes": [node],
        "outputs": {"result": "custom_writer.result"},
    }

    def mutate_main(_context):
        (repo / "src/custom.txt").write_text("unsafe\n", encoding="utf-8")
        return {"result": {}}

    with pytest.raises(OrchestrationError, match="UNSAFE_DETERMINISTIC_PERMISSION"):
        runtime(repo, value, executors={"custom_writer": mutate_main})
    assert not (repo / "src/custom.txt").exists()


def test_redacted_receipts_survive_a_fresh_process_resume(repo: Path):
    first = runtime(repo, workflow()).run(run_id="durable-receipts")
    resumed = runtime(repo, workflow()).run(run_id="durable-receipts", resume=True)

    assert first.run.status == resumed.run.status == "succeeded"
    assert len(resumed.agent_receipts) == len(first.agent_receipts) == 2
    assert len(resumed.check_receipts) == len(first.check_receipts) == 3
    assert set(resumed.worktrees) == {"writer_a#1", "writer_b#1", "integrate#1"}
    ledger = next((repo / ".graph-artifacts/receipts").glob("*.json"))
    assert "must-not-leak" not in ledger.read_text(encoding="utf-8")


def test_writer_retry_uses_a_fresh_attempt_worktree_without_stale_state(repo: Path):
    value = workflow()
    value["budgets"]["max_attempts_per_node"] = 2
    value["budgets"]["max_total_attempts"] = 4
    value["nodes"][0]["retry"] = {"max_attempts": 2, "no_progress_limit": 2}
    value["nodes"][0]["effect"] = "idempotent_write"
    value["nodes"][0]["idempotency_key"] = "writer-a"
    value["nodes"][0]["checks"][0]["argv"] = [
        sys.executable,
        "-c",
        "from pathlib import Path; raise SystemExit(Path.cwd().name.endswith('-a1'))",
    ]
    result = runtime(repo, value).run(run_id="writer-retry")

    assert result.run.status == "succeeded"
    assert result.run.nodes["writer_a"]["attempt_count"] == 2
    assert {key for key in result.worktrees if key.startswith("writer_a#")} == {
        "writer_a#1",
        "writer_a#2",
    }
    first = result.worktrees["writer_a#1"]
    second = result.worktrees["writer_a#2"]
    assert first != second
    assert (first / "src/first.txt").read_text() == "first\n"
    assert (second / "src/first.txt").read_text() == "first\n"
    resumed = runtime(repo, value).run(run_id="writer-retry", resume=True)
    assert "writer_a#2" in resumed.worktrees
    assert "writer_a#1" not in resumed.worktrees


def test_resume_rejects_head_movement_and_accepts_original_pinned_base(repo: Path):
    base = git(repo, "rev-parse", "HEAD")
    runtime(repo, workflow(), base=base).run(run_id="base-pinned")
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    git(repo, "add", "--", "later.txt")
    git(repo, "commit", "-qm", "move head")

    with pytest.raises(OrchestrationError, match="BASE_SHA_MISMATCH"):
        runtime(repo, workflow()).run(run_id="base-pinned", resume=True)
    resumed = runtime(repo, workflow(), base=base).run(
        run_id="base-pinned", resume=True
    )
    assert resumed.run.status == "succeeded"


def test_resume_rejects_changed_profile_model_or_adapter_identity(repo: Path):
    runtime(repo, workflow(), agent_config=config(model="model-a")).run(
        run_id="profile-pinned"
    )
    with pytest.raises(OrchestrationError, match="PROFILE_MANIFEST_MISMATCH"):
        runtime(repo, workflow(), agent_config=config(model="model-b")).run(
            run_id="profile-pinned", resume=True
        )


def test_valid_json_receipt_tampering_fails_closed_on_resume(repo: Path):
    runtime(repo, workflow()).run(run_id="tampered-receipts")
    ledger = next((repo / ".graph-artifacts/receipts").glob("*.json"))
    document = json.loads(ledger.read_text(encoding="utf-8"))
    document["body"]["check_receipts"][0]["exit_code"] = 99
    body = json.dumps(
        document["body"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    document["digest"] = hashlib.sha256(body).hexdigest()
    ledger.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(OrchestrationError, match="CORRUPT_RECEIPT_LEDGER"):
        runtime(repo, workflow()).run(run_id="tampered-receipts", resume=True)


def test_bound_but_missing_receipt_ledger_fails_closed_on_resume(repo: Path):
    runtime(repo, workflow()).run(run_id="missing-receipts")
    ledger = next((repo / ".graph-artifacts/receipts").glob("*.json"))
    ledger.unlink()

    with pytest.raises(OrchestrationError, match="CORRUPT_RECEIPT_LEDGER"):
        runtime(repo, workflow()).run(run_id="missing-receipts", resume=True)


def test_cross_instance_receipt_updates_merge_under_file_lock(repo: Path):
    first = runtime(repo, workflow())
    second = runtime(repo, workflow())
    barrier = threading.Barrier(2)

    def receipt(node_id: str) -> CheckCommandReceipt:
        return CheckCommandReceipt(
            run_id="concurrent-ledger",
            node_id=node_id,
            attempt=1,
            check_id="accepted",
            command_digest="a" * 64,
            cwd=".",
            exit_code=0,
            stdout_digest="b" * 64,
            stderr_digest="c" * 64,
            stdout_bytes=0,
            stderr_bytes=0,
        )

    def record(instance: PortableRuntime, node_id: str) -> None:
        barrier.wait(timeout=2)
        instance._record_check_receipt(receipt(node_id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(record, first, "first"),
            pool.submit(record, second, "second"),
        ]
        for future in futures:
            future.result(timeout=3)

    reader = runtime(repo, workflow())
    reader._load_receipts("concurrent-ledger")
    loaded = [
        item for item in reader._check_receipts if item.run_id == "concurrent-ledger"
    ]
    assert {item.node_id for item in loaded} == {"first", "second"}


def test_external_agent_requires_explicit_upstream_approval_result(repo: Path):
    approval_schema = {
        "type": "object",
        "properties": {"approved": {"const": True}},
        "required": ["approved"],
    }
    nodes = [
        {
            "id": "human_approval",
            "kind": "approval",
            "task": "human authorizes external access",
            "needs": [],
            "inputs": {},
            "outputs": {"result": {"schema": approval_schema}},
            "workspace": "read-only",
            "permission": "read",
            "checks": [{"id": "recorded", "argv": ["true"]}],
            "required": True,
        },
        {
            "id": "external_agent",
            "kind": "agent",
            "task": "perform approved external read",
            "needs": ["human_approval"],
            "inputs": {},
            "outputs": {"result": {"schema": AGENT_SCHEMA}},
            "profile": "worker",
            "workspace": "read-only",
            "permission": "external",
            "approval": "human_approval",
            "effect": "read",
            "checks": [{"id": "accepted", "argv": ["true"]}],
            "required": True,
        },
    ]
    value = {
        "version": "graph-engineering/v1alpha1",
        "id": "approval_test",
        "goal": "prove external authority is explicit",
        "budgets": {
            "max_nodes": 2,
            "max_concurrency": 1,
            "max_attempts_per_node": 1,
            "max_total_attempts": 2,
            "timeout_seconds": 10,
        },
        "nodes": nodes,
        "outputs": {"result": "external_agent.result"},
    }

    with pytest.raises(OrchestrationError, match="APPROVAL_REQUIRED"):
        runtime(repo, value)
    assert not (repo / ".claude/worktrees/graph-runs").exists()
