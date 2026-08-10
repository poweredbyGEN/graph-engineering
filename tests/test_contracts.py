from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from graph_engineering.contracts import (
    _SCHEMA_ARRAY_KEYWORDS,
    _SCHEMA_MAP_KEYWORDS,
    _SCHEMA_SINGLE_KEYWORDS,
    MAX_SCHEMA_BYTES,
    MAX_SCHEMA_DEPTH,
    MAX_SCHEMA_NODES,
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


def _nested_schema(depth: int) -> dict:
    root: dict = {}
    current = root
    for _ in range(depth):
        child: dict = {}
        current["items"] = child
        current = child
    return root


OFFICIAL_2020_12_SCHEMA_MAP_KEYWORDS = {
    "$defs",
    "properties",
    "patternProperties",
    "dependentSchemas",
}
OFFICIAL_2020_12_SCHEMA_SINGLE_KEYWORDS = {
    "additionalProperties",
    "unevaluatedProperties",
    "propertyNames",
    "contains",
    "items",
    "unevaluatedItems",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
}
OFFICIAL_2020_12_SCHEMA_ARRAY_KEYWORDS = {
    "allOf",
    "anyOf",
    "oneOf",
    "prefixItems",
}
SUPPORTED_SCHEMA_KEYWORDS = sorted(
    OFFICIAL_2020_12_SCHEMA_MAP_KEYWORDS
    | {"definitions"}
    | OFFICIAL_2020_12_SCHEMA_SINGLE_KEYWORDS
    | OFFICIAL_2020_12_SCHEMA_ARRAY_KEYWORDS
)


def _at_schema_keyword(keyword: str, child: dict) -> tuple[dict, str]:
    if keyword in _SCHEMA_MAP_KEYWORDS:
        return {keyword: {"slot": child}}, f"#/{keyword}/slot"
    if keyword in _SCHEMA_SINGLE_KEYWORDS:
        return {keyword: child}, f"#/{keyword}"
    if keyword in _SCHEMA_ARRAY_KEYWORDS:
        return {keyword: [child]}, f"#/{keyword}/0"
    raise AssertionError(f"unknown schema-valued keyword: {keyword}")


def test_schema_location_vocabulary_matches_draft_2020_12():
    # intent: adding a schema-valued vocabulary keyword without policy traversal
    # reopens external refs, recursive refs, and resource-limit bypasses.
    assert _SCHEMA_MAP_KEYWORDS == (
        OFFICIAL_2020_12_SCHEMA_MAP_KEYWORDS | {"definitions"}
    )
    assert _SCHEMA_SINGLE_KEYWORDS == OFFICIAL_2020_12_SCHEMA_SINGLE_KEYWORDS
    assert _SCHEMA_ARRAY_KEYWORDS == OFFICIAL_2020_12_SCHEMA_ARRAY_KEYWORDS


@pytest.mark.parametrize("keyword", SUPPORTED_SCHEMA_KEYWORDS)
@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("https://example.invalid/hidden.json", "SCHEMA_EXTERNAL_REFERENCE"),
        ("#/$defs/missing", "SCHEMA_UNRESOLVED_REFERENCE"),
    ],
)
def test_every_schema_location_enforces_reference_policy(keyword, reference, expected):
    # intent: a hostile ref cannot hide under any schema-valued Draft 2020-12 keyword.
    value = workflow()
    schema, _ = _at_schema_keyword(keyword, {"$ref": reference})
    value["nodes"][0]["outputs"]["contract"]["schema"] = schema

    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)

    assert codes(caught.value) == {expected}


@pytest.mark.parametrize("keyword", SUPPORTED_SCHEMA_KEYWORDS)
def test_every_schema_location_enforces_local_reference_cycles(keyword):
    value = workflow()
    schema, pointer = _at_schema_keyword(keyword, {})
    if keyword in _SCHEMA_MAP_KEYWORDS:
        schema[keyword]["slot"]["$ref"] = pointer
    elif keyword in _SCHEMA_SINGLE_KEYWORDS:
        schema[keyword]["$ref"] = pointer
    else:
        schema[keyword][0]["$ref"] = pointer
    value["nodes"][0]["outputs"]["contract"]["schema"] = schema

    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)

    assert codes(caught.value) == {"SCHEMA_REFERENCE_CYCLE"}


@pytest.mark.parametrize("keyword", SUPPORTED_SCHEMA_KEYWORDS)
@pytest.mark.parametrize(
    ("child", "expected"),
    [
        (_nested_schema(MAX_SCHEMA_DEPTH + 1), "SCHEMA_DEPTH_EXCEEDED"),
        (
            {"enum": list(range(MAX_SCHEMA_NODES + 1))},
            "SCHEMA_NODE_LIMIT_EXCEEDED",
        ),
    ],
)
def test_every_schema_location_enforces_resource_bounds(keyword, child, expected):
    value = workflow()
    schema, _ = _at_schema_keyword(keyword, child)
    value["nodes"][0]["outputs"]["contract"]["schema"] = schema

    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)

    assert codes(caught.value) == {expected}


@pytest.mark.parametrize("contract_key", ["schema", "acceptance_schema"])
def test_unevaluated_items_external_ref_repro_fails_closed(contract_key):
    value = workflow()
    value["nodes"][0]["outputs"]["contract"][contract_key] = {
        "type": "array",
        "unevaluatedItems": {"$ref": "https://example.invalid/hidden.json"},
    }

    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)

    assert [issue.code for issue in caught.value.issues] == [
        "SCHEMA_EXTERNAL_REFERENCE"
    ]
    assert caught.value.issues[0].path.endswith(
        f".{contract_key}.unevaluatedItems.$ref"
    )


@pytest.mark.parametrize("contract_key", ["schema", "acceptance_schema"])
def test_schema_depth_is_bounded_before_jsonschema_recurses(contract_key):
    # intent: hostile schemas must become a stable contract error, never RecursionError.
    value = workflow()
    value["nodes"][0]["outputs"]["contract"][contract_key] = _nested_schema(2_000)

    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)

    assert codes(caught.value) == {"SCHEMA_DEPTH_EXCEEDED"}


def test_schema_width_and_bytes_have_independent_bounds():
    wide = workflow()
    wide["nodes"][0]["outputs"]["contract"]["schema"] = {
        "enum": list(range(MAX_SCHEMA_NODES + 1))
    }
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(wide)
    assert codes(caught.value) == {"SCHEMA_NODE_LIMIT_EXCEEDED"}

    oversized = workflow()
    oversized["nodes"][0]["outputs"]["contract"]["schema"] = {
        "description": "x" * (MAX_SCHEMA_BYTES + 1)
    }
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(oversized)
    assert codes(caught.value) == {"SCHEMA_BYTES_EXCEEDED"}


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"$ref": "https://example.invalid/schema.json"}, "SCHEMA_EXTERNAL_REFERENCE"),
        ({"$ref": "other-schema.json"}, "SCHEMA_EXTERNAL_REFERENCE"),
        ({"$ref": "#/$defs/missing"}, "SCHEMA_UNRESOLVED_REFERENCE"),
        ({"$ref": "#"}, "SCHEMA_REFERENCE_CYCLE"),
        (
            {
                "$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}},
                "$ref": "#/$defs/a",
            },
            "SCHEMA_REFERENCE_CYCLE",
        ),
        ({"$dynamicRef": "#node"}, "SCHEMA_RESOURCE_UNSUPPORTED"),
    ],
)
def test_schema_references_are_local_resolved_and_non_recursive(schema, expected):
    # intent: validation must never perform network I/O or accept recursive expansion.
    value = workflow()
    value["nodes"][0]["outputs"]["contract"]["schema"] = schema
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert codes(caught.value) == {expected}


def test_bounded_local_json_pointer_schema_remains_supported():
    value = workflow()
    value["nodes"][0]["outputs"]["contract"]["schema"] = {
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/value"}},
        "$defs": {"value": {"type": "integer"}},
    }
    validate_workflow(value)


def test_ref_shaped_instance_data_is_not_treated_as_schema_control_flow():
    # intent: resource controls inspect schema positions, not arbitrary const payloads.
    value = workflow()
    value["nodes"][0]["outputs"]["contract"]["schema"] = {
        "const": {"$ref": "https://example.invalid/literal", "$id": "literal"}
    }
    validate_workflow(value)


def test_in_memory_schema_cycle_is_rejected_without_recursion():
    value = workflow()
    schema: dict = {"type": "object"}
    schema["properties"] = {"again": schema}
    value["nodes"][0]["outputs"]["contract"]["schema"] = schema
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert codes(caught.value) == {"SCHEMA_OBJECT_CYCLE"}


def test_invalid_base_output_schema_is_rejected_at_preflight():
    # intent: output schemas receive the same meta-schema gate as acceptance schemas.
    value = workflow()
    value["nodes"][0]["outputs"]["contract"]["schema"] = {"type": "not-a-type"}
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "INVALID_OUTPUT_SCHEMA" in codes(caught.value)


def test_join_contract_validates_threshold_and_safe_artifact_flow():
    value = workflow()
    peer = copy.deepcopy(value["nodes"][1])
    peer["id"] = "api_peer"
    peer["required"] = False
    value["nodes"][1]["required"] = False
    value["nodes"].insert(2, peer)
    integration = value["nodes"][3]
    integration["needs"] = ["api", "api_peer"]
    integration["inputs"] = {}
    integration["join"] = {"policy": "n_of_m", "n": 2}
    validate_workflow(value)

    integration["join"]["n"] = 3
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "IMPOSSIBLE_JOIN_THRESHOLD" in codes(caught.value)


@pytest.mark.parametrize("policy", ["any", "n_of_m", "majority", "all_settled"])
def test_non_all_join_rejects_required_members(policy):
    # intent: a required failed voter would defeat an otherwise successful quorum.
    value = workflow()
    integration = value["nodes"][2]
    integration["inputs"] = {}
    integration["join"] = {"policy": policy}
    if policy == "n_of_m":
        integration["join"]["n"] = 1
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "REQUIRED_QUORUM_MEMBER" in codes(caught.value)


def test_all_join_retains_required_dependency_semantics():
    value = workflow()
    value["nodes"][2]["join"] = {"policy": "all"}
    validate_workflow(value)


def test_join_that_can_release_without_a_producer_rejects_static_input():
    # intent: a quorum consumer must use settlement state, not assume every artifact exists.
    value = workflow()
    value["nodes"][2]["join"] = {"policy": "any"}
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "UNSAFE_JOIN_INPUT" in codes(caught.value)


def test_join_requires_a_real_dependency_set():
    value = workflow()
    value["nodes"][0]["join"] = {"policy": "majority"}
    with pytest.raises(WorkflowValidationError) as caught:
        validate_workflow(value)
    assert "EMPTY_JOIN" in codes(caught.value)


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
