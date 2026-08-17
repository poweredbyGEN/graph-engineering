from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import pytest

from graph_engineering.config import parse_agent_config
from graph_engineering.contracts import WorkflowValidationError, validate_workflow
from graph_engineering.orchestrator import (
    CHANGE_SET_SCHEMA,
    OrchestrationError,
    PortableRuntime,
)
from graph_engineering.role_policy import bind_role_policy, load_role_policy

RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"const": True}},
}
VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict"],
    "properties": {"verdict": {"const": "pass"}},
}


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Graph Test")
    _git(root, "config", "user.email", "graph@example.com")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "--", "tracked.txt")
    _git(root, "commit", "-qm", "base")
    return root


def _config(*profiles: str):
    capabilities = {
        "read": True,
        "write": True,
        "structured_output": True,
        "worktree": True,
        "resume": False,
        "mcp": False,
    }
    return parse_agent_config(
        {
            "version": 1,
            "profiles": {
                name: {
                    "adapter": "subprocess",
                    "model": f"{name}-model",
                    "capabilities": capabilities,
                    "subprocess": {
                        "argv": [
                            sys.executable,
                            "-c",
                            "print('{{\"ok\":true}}')",
                        ],
                        "prompt_transport": "stdin",
                        "output_format": "json",
                        "env_allowlist": [],
                    },
                }
                for name in profiles
            },
        }
    )


def _usage_config():
    raw = {
        "version": 1,
        "profiles": {
            "producer": {
                "adapter": "subprocess",
                "model": "producer-model",
                "capabilities": {
                    "read": True,
                    "write": True,
                    "structured_output": True,
                    "worktree": True,
                    "resume": False,
                    "mcp": False,
                },
                "subprocess": {
                    "argv": [
                        sys.executable,
                        "-c",
                        (
                            "import json; print(json.dumps(dict(type='result', "
                            "structured_output=dict(ok=True), usage=dict("
                            "input_tokens=101, output_tokens=2, cost_usd=0.0002))))"
                        ),
                    ],
                    "prompt_transport": "stdin",
                    "output_format": "jsonl",
                    "env_allowlist": [],
                },
            }
        },
    }
    return parse_agent_config(raw)


def _install_policy(repo: Path, *profiles: str) -> dict:
    directory = repo / ".graph-engineering"
    directory.mkdir()
    value = {
        "version": "graph-engineering/role-policy/v1",
        "generation": 1,
        "approved_by": "Policy-Owner",
        "profiles": {
            name: ["read", "write", "structured_output", "worktree"]
            for name in profiles
        },
        "tools": [],
        "write_scopes": ["src"],
        "effects": [],
        "deployment_targets": [],
        "approval_boundaries": ["release_approval"],
        "effect_approvals": {},
        "cost": {
            "max_input_tokens": 1000,
            "max_output_tokens": 500,
            "max_total_tokens": 1500,
            "max_cost_microusd": 100000,
        },
    }
    (directory / "role-policy.json").write_text(json.dumps(value), encoding="utf-8")
    binding = bind_role_policy(load_role_policy(repo))
    return {
        "version": binding.version,
        "generation": binding.generation,
        "digest": binding.digest,
    }


def _authority(profile: str, *, approval: bool = False) -> dict:
    return {
        "profile": profile,
        "capabilities": ["read", "structured_output"],
        "tools": [],
        "write_scopes": [],
        "effects": [],
        "deployment_targets": [],
        "approval_boundaries": ["release_approval"] if approval else [],
        "cost": {
            "max_input_tokens": 100,
            "max_output_tokens": 50,
            "max_total_tokens": 150,
            "max_cost_microusd": 10000,
        },
    }


def _agent(
    node_id: str,
    profile: str,
    *,
    needs: list[str] | None = None,
    inputs: dict[str, str] | None = None,
    outputs: dict | None = None,
    risk: str = "low",
    verifiers: list[dict] | None = None,
    approval: str | None = None,
    raw_evidence_outputs: list[str] | None = None,
) -> dict:
    verification = {
        "risk": risk,
        "verifiers": verifiers or [],
        "raw_evidence_outputs": raw_evidence_outputs or [],
    }
    if approval is not None:
        verification["approval"] = approval
    return {
        "id": node_id,
        "kind": "agent",
        "task": f"Run {node_id}.",
        "needs": needs or [],
        "inputs": inputs or {},
        "outputs": outputs or {"result": {"schema": copy.deepcopy(RESULT_SCHEMA)}},
        "profile": profile,
        "authority": _authority(profile, approval=approval is not None),
        "verification": verification,
        "workspace": "read-only",
        "permission": "read",
        "checks": [{"id": "proof", "argv": [sys.executable, "-c", "pass"]}],
        "required": True,
    }


def _approval(*verifiers: str) -> dict:
    return {
        "id": "release_approval",
        "kind": "approval",
        "task": "Record the named human decision.",
        "needs": list(verifiers),
        "inputs": {},
        "outputs": {"result": {"schema": copy.deepcopy(RESULT_SCHEMA)}},
        "workspace": "read-only",
        "permission": "read",
        "checks": [{"id": "approval_record", "argv": [sys.executable, "-c", "pass"]}],
        "required": True,
    }


def _writer(**kwargs) -> dict:
    node = _agent(**kwargs)
    node["workspace"] = "worktree"
    node["permission"] = "write"
    node["write_scope"] = ["src"]
    node["authority"]["capabilities"] = [
        "read",
        "write",
        "structured_output",
        "worktree",
    ]
    node["authority"]["write_scopes"] = ["src"]
    return node


def _verifier(
    node_id: str,
    profile: str,
    *,
    producer: str = "producer",
    input_name: str = "raw_change",
) -> dict:
    return _agent(
        node_id,
        profile,
        needs=[producer],
        inputs={input_name: f"{producer}.changeset"},
        outputs={"verdict": {"schema": copy.deepcopy(VERDICT_SCHEMA)}},
    )


def _workflow(binding: dict, nodes: list[dict], output: str) -> dict:
    return {
        "version": "graph-engineering/v1alpha1",
        "id": "policy_bound",
        "goal": "Prove policy and risk boundaries before execution.",
        "role_policy": binding,
        "budgets": {
            "max_nodes": len(nodes),
            "max_concurrency": max(1, len(nodes)),
            "max_attempts_per_node": 1,
            "max_total_attempts": len(nodes),
            "timeout_seconds": 30,
        },
        "nodes": nodes,
        "outputs": {"result": output},
    }


def _runtime(
    repo: Path,
    workflow: dict,
    config,
    *,
    approvals: dict[str, dict] | None = None,
) -> PortableRuntime:
    return PortableRuntime(
        workflow,
        config,
        repo=repo,
        state_path=repo / "state.sqlite",
        artifact_root=repo / "artifacts",
        approvals=approvals,
        environ=os.environ | {"TMPDIR": str(repo.parent)},
    )


def test_policy_bound_low_risk_workflow_passes_runtime_preflight(repo: Path):
    binding = _install_policy(repo, "producer")
    workflow = _workflow(binding, [_agent("producer", "producer")], "producer.result")

    validate_workflow(workflow)
    runtime = _runtime(repo, workflow, _config("producer"))
    assert runtime.role_policy is not None
    assert runtime.role_policy.digest == binding["digest"]


def test_authority_expansion_fails_closed_at_runtime_preflight(repo: Path):
    binding = _install_policy(repo, "producer")
    workflow = _workflow(binding, [_agent("producer", "producer")], "producer.result")
    workflow["nodes"][0]["authority"]["tools"] = ["shell"]

    with pytest.raises(OrchestrationError) as caught:
        _runtime(repo, workflow, _config("producer"))
    assert caught.value.code == "TOOL_EXPANSION"


def test_policy_bound_fallback_declares_equivalent_narrowed_authority(repo: Path):
    binding = _install_policy(repo, "primary", "backup")
    node = _agent("producer", "primary")
    node["fallback"] = {
        "routes": [
            {
                "id": "backup",
                "profile": "backup",
                "on_codes": ["WORKER_EXIT"],
                "max_uses": 1,
            }
        ]
    }
    workflow = _workflow(binding, [node], "producer.result")

    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(workflow)
    assert "FALLBACK_AUTHORITY_REQUIRED" in {
        issue.code for issue in caught.value.issues
    }

    route = node["fallback"]["routes"][0]
    route["authority"] = copy.deepcopy(node["authority"])
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(workflow)
    assert "FALLBACK_AUTHORITY_MISMATCH" in {
        issue.code for issue in caught.value.issues
    }

    route["authority"]["profile"] = "backup"
    validate_workflow(workflow)
    _runtime(repo, workflow, _config("primary", "backup"))


def test_reported_provider_usage_cannot_exceed_reviewed_node_ceiling(repo: Path):
    binding = _install_policy(repo, "producer")
    workflow = _workflow(binding, [_agent("producer", "producer")], "producer.result")

    result = _runtime(repo, workflow, _usage_config()).run(run_id="cost-ceiling")

    assert result.run.status == "failed"
    assert "COST_BUDGET_EXCEEDED" in result.run.nodes["producer"]["error"]
    receipt = result.agent_receipts["producer#1"]
    assert (receipt.input_tokens, receipt.output_tokens, receipt.cost_microusd) == (
        101,
        2,
        200,
    )


def test_policy_bound_execution_rejects_missing_provider_usage(repo: Path):
    binding = _install_policy(repo, "producer")
    workflow = _workflow(binding, [_agent("producer", "producer")], "producer.result")

    result = _runtime(repo, workflow, _config("producer")).run(run_id="missing-usage")

    assert result.run.status == "failed"
    assert "USAGE_REPORT_REQUIRED" in result.run.nodes["producer"]["error"]


def test_medium_rejects_same_profile_verifier_at_runtime_preflight(repo: Path):
    binding = _install_policy(repo, "shared")
    producer = _writer(
        node_id="producer",
        profile="shared",
        outputs={
            "result": {"schema": copy.deepcopy(RESULT_SCHEMA)},
            "changeset": {"schema": copy.deepcopy(CHANGE_SET_SCHEMA)},
        },
        risk="medium",
        verifiers=[
            {
                "node": "verifier",
                "lens": "correctness",
                "raw_evidence_inputs": ["raw_change"],
            }
        ],
        raw_evidence_outputs=["changeset"],
    )
    verifier = _verifier("verifier", "shared")
    workflow = _workflow(binding, [producer, verifier], "verifier.verdict")
    validate_workflow(workflow)

    with pytest.raises(OrchestrationError) as caught:
        _runtime(repo, workflow, _config("shared"))
    assert caught.value.code == "VERIFIER_NOT_INDEPENDENT"


def test_medium_accepts_fresh_profile_and_raw_changeset(repo: Path):
    binding = _install_policy(repo, "producer", "reviewer")
    producer = _writer(
        node_id="producer",
        profile="producer",
        outputs={
            "result": {"schema": copy.deepcopy(RESULT_SCHEMA)},
            "changeset": {"schema": copy.deepcopy(CHANGE_SET_SCHEMA)},
        },
        risk="medium",
        verifiers=[
            {
                "node": "verifier",
                "lens": "correctness",
                "raw_evidence_inputs": ["raw_change"],
            }
        ],
        raw_evidence_outputs=["changeset"],
    )
    verifier = _verifier("verifier", "reviewer")
    workflow = _workflow(binding, [producer, verifier], "verifier.verdict")

    validate_workflow(workflow)
    _runtime(repo, workflow, _config("producer", "reviewer"))


def test_verifier_cannot_receive_producer_narrative_summary(repo: Path):
    binding = _install_policy(repo, "producer", "reviewer")
    producer = _writer(
        node_id="producer",
        profile="producer",
        outputs={"summary": {"schema": copy.deepcopy(RESULT_SCHEMA)}},
        risk="medium",
        verifiers=[
            {
                "node": "verifier",
                "lens": "correctness",
                "raw_evidence_inputs": ["raw_change"],
            }
        ],
    )
    verifier = _agent(
        "verifier",
        "reviewer",
        needs=["producer"],
        inputs={"raw_change": "producer.summary"},
    )
    workflow = _workflow(binding, [producer, verifier], "verifier.result")

    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(workflow)
    assert "SUMMARY_CONTAMINATION" in {issue.code for issue in caught.value.issues}


def test_verifier_cannot_substitute_unrelated_decoy_evidence(repo: Path):
    binding = _install_policy(repo, "producer", "reviewer", "decoy")
    producer = _writer(
        node_id="producer",
        profile="producer",
        outputs={
            "result": {"schema": copy.deepcopy(RESULT_SCHEMA)},
            "changeset": {"schema": copy.deepcopy(CHANGE_SET_SCHEMA)},
        },
        risk="medium",
        verifiers=[
            {
                "node": "verifier",
                "lens": "correctness",
                "raw_evidence_inputs": ["raw_change"],
            }
        ],
        raw_evidence_outputs=["changeset"],
    )
    decoy = _agent("decoy", "decoy")
    verifier = _agent(
        "verifier",
        "reviewer",
        needs=["producer", "decoy"],
        inputs={"raw_change": "decoy.result"},
        outputs={"verdict": {"schema": copy.deepcopy(VERDICT_SCHEMA)}},
    )
    workflow = _workflow(binding, [producer, decoy, verifier], "verifier.verdict")

    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(workflow)
    assert "SUMMARY_CONTAMINATION" in {issue.code for issue in caught.value.issues}


def test_medium_rejects_non_gating_verifier_verdict(repo: Path):
    binding = _install_policy(repo, "producer", "reviewer")
    producer = _writer(
        node_id="producer",
        profile="producer",
        outputs={
            "result": {"schema": copy.deepcopy(RESULT_SCHEMA)},
            "changeset": {"schema": copy.deepcopy(CHANGE_SET_SCHEMA)},
        },
        risk="medium",
        verifiers=[
            {
                "node": "verifier",
                "lens": "correctness",
                "raw_evidence_inputs": ["raw_change"],
            }
        ],
        raw_evidence_outputs=["changeset"],
    )
    verifier = _verifier("verifier", "reviewer")
    verifier["outputs"]["verdict"]["schema"]["properties"]["verdict"] = {
        "enum": ["pass", "fail"]
    }
    workflow = _workflow(binding, [producer, verifier], "verifier.verdict")

    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(workflow)
    assert "VERIFIER_VERDICT_NOT_GATING" in {
        issue.code for issue in caught.value.issues
    }


def test_high_risk_requires_named_approval_and_accepts_valid_topology(repo: Path):
    binding = _install_policy(repo, "producer", "security", "correctness")
    producer = _writer(
        node_id="producer",
        profile="producer",
        outputs={
            "result": {"schema": copy.deepcopy(RESULT_SCHEMA)},
            "changeset": {"schema": copy.deepcopy(CHANGE_SET_SCHEMA)},
        },
        risk="high",
        verifiers=[
            {
                "node": "security_review",
                "lens": "security",
                "raw_evidence_inputs": ["raw_security_change"],
            },
            {
                "node": "correctness_review",
                "lens": "correctness",
                "raw_evidence_inputs": ["raw_correctness_change"],
            },
        ],
        approval="release_approval",
        raw_evidence_outputs=["changeset"],
    )
    security = _verifier(
        "security_review", "security", input_name="raw_security_change"
    )
    correctness = _verifier(
        "correctness_review", "correctness", input_name="raw_correctness_change"
    )
    approval = _approval("security_review", "correctness_review")
    workflow = _workflow(
        binding,
        [producer, security, correctness, approval],
        "release_approval.result",
    )
    config = _config("producer", "security", "correctness")
    validate_workflow(workflow)

    with pytest.raises(OrchestrationError) as caught:
        _runtime(repo, workflow, config, approvals={"release_approval": {}})
    assert caught.value.code == "HIGH_RISK_APPROVAL_REQUIRED"

    _runtime(
        repo,
        workflow,
        config,
        approvals={"release_approval": {"approved_by": "Mav"}},
    )
