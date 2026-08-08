from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from graph_engineering.contracts import (
    WorkflowValidationError,
    load_workflow,
    validate_workflow,
)
from graph_engineering.orchestrator import CHANGE_SET_SCHEMA


def workflow() -> dict:
    return {
        "version": "graph-engineering/v1alpha1",
        "id": "dev_change",
        "goal": "Produce a verified integrated change",
        "budgets": {
            "max_nodes": 10,
            "max_concurrency": 3,
            "max_attempts_per_node": 2,
            "max_total_attempts": 20,
            "timeout_seconds": 1800,
        },
        "nodes": [
            {
                "id": "scope",
                "kind": "agent",
                "task": "Define the contract",
                "needs": [],
                "inputs": {},
                "outputs": {"contract": {"schema": {"type": "object"}}},
                "profile": "claude",
                "workspace": "read-only",
                "permission": "read",
                "required": True,
            },
            {
                "id": "api",
                "kind": "agent",
                "task": "Implement the API",
                "needs": ["scope"],
                "inputs": {"contract": "scope.contract"},
                "outputs": {"change": {"schema": {"type": "object"}}},
                "profile": "codex",
                "workspace": "worktree",
                "write_scope": ["src/api/**"],
                "permission": "write",
                "effect": "none",
                "checks": [{"id": "tests", "argv": ["pytest", "-q"]}],
                "retry": {"max_attempts": 2, "no_progress_limit": 1},
                "required": True,
            },
            {
                "id": "integrate",
                "kind": "integration",
                "task": "Integrate accepted changes",
                "needs": ["api"],
                "inputs": {"change": "api.change"},
                "outputs": {"result": {"schema": {"type": "object"}}},
                "workspace": "worktree",
                "write_scope": ["**"],
                "permission": "write",
                "checks": [{"id": "combined", "argv": ["pytest", "-q"]}],
                "required": True,
            },
        ],
        "outputs": {"result": "integrate.result"},
    }


def codes(exc: WorkflowValidationError) -> set[str]:
    return {issue.code for issue in exc.issues}


def repair_workflow() -> dict:
    value = workflow()
    value["nodes"][1]["outputs"] = {"changeset": {"schema": {"type": "object"}}}
    integration = value["nodes"][2]
    integration["inputs"] = {"change": "api.changeset"}
    integration["retry"] = {"max_attempts": 2, "no_progress_limit": 1}
    integration["repair"] = {
        "routes": [
            {
                "id": "combined_to_api",
                "check_ids": ["combined"],
                "targets": [{"node": "api", "input": "integration_failure"}],
                "max_rounds": 1,
                "no_progress_limit": 1,
            }
        ]
    }
    return value


def test_valid_workflow_passes():
    validate_workflow(workflow())


def test_explicit_bounded_repair_route_passes():
    validate_workflow(repair_workflow())


def test_public_repair_workflow_example_matches_runtime_contract():
    path = Path(__file__).parents[1] / "examples" / "repair-route.workflow.json"
    value = load_workflow(path)
    changeset = value["nodes"][0]["outputs"]["changeset"]["schema"]

    assert json.loads(json.dumps(changeset)) == CHANGE_SET_SCHEMA


@pytest.mark.parametrize(
    ("sabotage", "expected"),
    [
        (
            lambda value: value["nodes"][2]["repair"]["routes"][0]["check_ids"].append(
                "ghost"
            ),
            "UNKNOWN_REPAIR_CHECK",
        ),
        (
            lambda value: value["nodes"][2]["repair"]["routes"][0]["targets"][0].update(
                node="scope"
            ),
            "INVALID_REPAIR_TARGET",
        ),
        (
            lambda value: value["nodes"][2]["retry"].update(max_attempts=1),
            "REPAIR_ATTEMPT_BUDGET",
        ),
        (
            lambda value: value["nodes"][1]["inputs"].update(
                integration_failure="scope.contract"
            ),
            "REPAIR_INPUT_COLLISION",
        ),
        (
            lambda value: value["nodes"][1].update(effect="non_idempotent_write"),
            "UNSAFE_REPAIR_TARGET",
        ),
        (
            lambda value: value["nodes"][1].pop("effect"),
            "UNSAFE_REPAIR_TARGET",
        ),
    ],
)
def test_repair_contract_sabotage_fails_closed(sabotage, expected):
    # intent: repair routing must be explicit, typed, and budgeted before execution.
    value = repair_workflow()
    sabotage(value)
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert expected in codes(caught.value)


def test_unknown_fields_fail_closed():
    # intent: a misspelled safety field must not be silently ignored by a permissive parser.
    value = workflow()
    value["nodes"][1]["permision"] = "write"
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "SCHEMA_ERROR" in codes(caught.value)


def test_duplicate_node_ids_are_rejected():
    # intent: duplicate IDs alias state, branches, and artifacts across two jobs.
    value = workflow()
    value["nodes"].append(copy.deepcopy(value["nodes"][1]))
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "DUPLICATE_NODE" in codes(caught.value)


def test_cycle_detector_blocks_deadlocked_graph():
    # intent: sabotage-check the topological guard; a cycle can never enter the ready queue.
    value = workflow()
    value["nodes"][0]["needs"] = ["integrate"]
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "CYCLE" in codes(caught.value)


def test_input_requires_real_ordered_producer():
    # intent: artifact flow, not list order, creates an edge.
    value = workflow()
    value["nodes"][1]["needs"] = []
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "UNORDERED_INPUT" in codes(caught.value)


def test_missing_output_is_rejected():
    # intent: consumers may not wait forever for an artifact the producer never declared.
    value = workflow()
    value["nodes"][1]["inputs"]["contract"] = "scope.ghost"
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "MISSING_OUTPUT" in codes(caught.value)


def test_writer_requires_isolated_worktree():
    # intent: parallel writes in a shared checkout make evidence attribution impossible.
    value = workflow()
    value["nodes"][1]["workspace"] = "shared"
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "UNISOLATED_WRITE" in codes(caught.value)


def test_parallel_overlapping_writers_need_integration_join():
    # intent: isolation prevents clobbering, but overlapping diffs still need an explicit join.
    value = workflow()
    second = copy.deepcopy(value["nodes"][1])
    second["id"] = "api_two"
    second["inputs"] = {"contract": "scope.contract"}
    value["nodes"].insert(2, second)
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "UNJOINED_OVERLAP" in codes(caught.value)


def test_external_action_requires_named_approval():
    # intent: graph execution does not manufacture authority for an external write.
    value = workflow()
    value["nodes"][0]["permission"] = "external"
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "SCHEMA_ERROR" in codes(caught.value)


def test_arbitrary_configured_profile_is_portable():
    # intent: workflow topology must not hard-code a vendor allowlist.
    value = workflow()
    value["nodes"][1]["profile"] = "team.kimi-k3"
    validate_workflow(value)


def test_declared_node_budget_is_enforced():
    # intent: schema maxItems is not a substitute for the workflow's smaller runtime ceiling.
    value = workflow()
    value["budgets"]["max_nodes"] = 2
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "NODE_BUDGET_EXCEEDED" in codes(caught.value)


def test_node_retry_cannot_exceed_workflow_budget():
    # intent: a node cannot silently weaken the enclosing cost and convergence contract.
    value = workflow()
    value["nodes"][1]["retry"]["max_attempts"] = 3
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "NODE_ATTEMPT_BUDGET_EXCEEDED" in codes(caught.value)


def test_writer_must_declare_scope_even_when_isolated():
    # intent: a worktree prevents clobbering but does not make integration conflicts bounded.
    value = workflow()
    del value["nodes"][1]["write_scope"]
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "MISSING_WRITE_SCOPE" in codes(caught.value)


def test_required_node_must_contribute_to_output():
    # intent: required disconnected work is a fake edge and wasted wall-clock/cost.
    value = workflow()
    orphan = copy.deepcopy(value["nodes"][0])
    orphan["id"] = "orphan"
    value["nodes"].append(orphan)
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "DISCONNECTED_REQUIRED_NODE" in codes(caught.value)


def test_external_approval_is_a_real_upstream_node():
    # intent: an approval label is not authority unless an approval node gates this edge.
    value = workflow()
    value["nodes"][0].update(
        permission="external", approval="human_gate", effect="non_idempotent_write"
    )
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "INVALID_APPROVAL" in codes(caught.value)


def test_idempotent_effect_requires_stable_key():
    # intent: retries cannot claim exactly-once behavior without a declared idempotency key.
    value = workflow()
    value["nodes"][0].update(
        permission="external", approval="human_gate", effect="idempotent_write"
    )
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "SCHEMA_ERROR" in codes(caught.value)


def test_required_consumer_cannot_bind_optional_producer_output():
    # intent: optional_failed must never make a consumer ready with a missing artifact.
    value = workflow()
    producer = value["nodes"][0]
    consumer = value["nodes"][1]
    producer["required"] = False
    consumer["needs"] = [producer["id"]]
    consumer["inputs"] = {"upstream": f"{producer['id']}.contract"}

    with pytest.raises(WorkflowValidationError, match="OPTIONAL_PRODUCER_INPUT"):
        validate_workflow(value)
