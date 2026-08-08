"""Deterministic validation for the portable workflow contract."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

WORKFLOW_VERSION = "graph-engineering/v1alpha1"


@dataclass(frozen=True, order=True)
class ValidationIssue:
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.code} at {self.path}: {self.message}"


class WorkflowValidationError(ValueError):
    """Raised with every deterministic contract violation, in stable order."""

    def __init__(self, issues: Iterable[ValidationIssue]):
        self.issues = tuple(sorted(issues))
        super().__init__("\n".join(str(issue) for issue in self.issues))


def workflow_schema() -> dict[str, Any]:
    path = files("graph_engineering.schemas").joinpath("workflow-v1alpha1.schema.json")
    return json.loads(path.read_text(encoding="utf-8"))


def load_workflow(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise WorkflowValidationError(
            [ValidationIssue("PARSE_ERROR", "$", f"cannot read workflow: {exc}")]
        ) from exc
    if not isinstance(value, dict):
        raise WorkflowValidationError(
            [ValidationIssue("TYPE_ERROR", "$", "workflow document must be an object")]
        )
    validate_workflow(value)
    return value


def _schema_issues(workflow: dict[str, Any]) -> list[ValidationIssue]:
    validator = Draft202012Validator(workflow_schema())
    issues: list[ValidationIssue] = []
    for error in validator.iter_errors(workflow):
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        issues.append(ValidationIssue("SCHEMA_ERROR", path, error.message))
    return issues


def _ancestors(node_id: str, needs: dict[str, set[str]]) -> set[str]:
    found: set[str] = set()
    stack = list(needs.get(node_id, ()))
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.add(current)
        stack.extend(needs.get(current, ()))
    return found


def _scope_prefix(scope: str) -> str:
    for token in ("*", "?", "["):
        if token in scope:
            scope = scope.split(token, 1)[0]
    return scope.rstrip("/")


def _scopes_overlap(left: str, right: str) -> bool:
    a, b = _scope_prefix(left), _scope_prefix(right)
    return not a or not b or a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")


def validate_workflow(workflow: dict[str, Any]) -> None:
    """Validate syntax and graph semantics without invoking a model or executing code."""

    issues = _schema_issues(workflow)
    if issues:
        raise WorkflowValidationError(issues)

    nodes: list[dict[str, Any]] = workflow["nodes"]
    budgets = workflow["budgets"]
    if len(nodes) > budgets["max_nodes"]:
        issues.append(
            ValidationIssue(
                "NODE_BUDGET_EXCEEDED",
                "$.budgets.max_nodes",
                f"workflow declares {len(nodes)} nodes but max_nodes is {budgets['max_nodes']}",
            )
        )
    if budgets["max_concurrency"] > budgets["max_nodes"]:
        issues.append(
            ValidationIssue(
                "INVALID_CONCURRENCY_BUDGET",
                "$.budgets.max_concurrency",
                "max_concurrency cannot exceed max_nodes",
            )
        )
    if budgets["max_total_attempts"] < len(nodes):
        issues.append(
            ValidationIssue(
                "IMPOSSIBLE_ATTEMPT_BUDGET",
                "$.budgets.max_total_attempts",
                "max_total_attempts must permit at least one attempt per node",
            )
        )
    by_id: dict[str, dict[str, Any]] = {}
    positions: dict[str, int] = {}
    for index, node in enumerate(nodes):
        node_id = node["id"]
        if node_id in by_id:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_NODE",
                    f"$.nodes[{index}].id",
                    f"duplicate node id {node_id!r}",
                )
            )
        else:
            by_id[node_id] = node
            positions[node_id] = index

    needs: dict[str, set[str]] = {
        node_id: set(node.get("needs", [])) for node_id, node in by_id.items()
    }
    dependents: dict[str, set[str]] = defaultdict(set)
    indegree: dict[str, int] = {node_id: 0 for node_id in by_id}
    for node_id, dependencies in needs.items():
        for dependency in dependencies:
            if dependency not in by_id:
                issues.append(
                    ValidationIssue(
                        "MISSING_DEPENDENCY",
                        f"$.nodes[{positions[node_id]}].needs",
                        f"unknown dependency {dependency!r}",
                    )
                )
                continue
            dependents[dependency].add(node_id)
            indegree[node_id] += 1

    queue = deque(
        sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    )
    visited: list[str] = []
    while queue:
        node_id = queue.popleft()
        visited.append(node_id)
        for child in sorted(dependents[node_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(visited) != len(by_id):
        cycle_nodes = sorted(
            node_id for node_id, degree in indegree.items() if degree > 0
        )
        issues.append(
            ValidationIssue(
                "CYCLE", "$.nodes", f"required-edge cycle contains {cycle_nodes}"
            )
        )

    for node_id, node in by_id.items():
        index = positions[node_id]
        node_attempts = node.get("retry", {}).get("max_attempts", 1)
        if node_attempts > budgets["max_attempts_per_node"]:
            issues.append(
                ValidationIssue(
                    "NODE_ATTEMPT_BUDGET_EXCEEDED",
                    f"$.nodes[{index}].retry.max_attempts",
                    f"node allows {node_attempts} attempts but workflow limit is "
                    f"{budgets['max_attempts_per_node']}",
                )
            )
        check_ids: set[str] = set()
        for check_index, check in enumerate(node.get("checks", [])):
            if check["id"] in check_ids:
                issues.append(
                    ValidationIssue(
                        "DUPLICATE_CHECK",
                        f"$.nodes[{index}].checks[{check_index}].id",
                        f"duplicate check id {check['id']!r}",
                    )
                )
            check_ids.add(check["id"])
        for input_name, binding in node.get("inputs", {}).items():
            producer_id, _, output_name = binding.partition(".")
            if not output_name or producer_id not in by_id:
                issues.append(
                    ValidationIssue(
                        "MISSING_PRODUCER",
                        f"$.nodes[{index}].inputs.{input_name}",
                        f"binding {binding!r} does not name an existing node output",
                    )
                )
                continue
            if output_name not in by_id[producer_id].get("outputs", {}):
                issues.append(
                    ValidationIssue(
                        "MISSING_OUTPUT",
                        f"$.nodes[{index}].inputs.{input_name}",
                        f"producer {producer_id!r} has no output {output_name!r}",
                    )
                )
            if not by_id[producer_id]["required"]:
                issues.append(
                    ValidationIssue(
                        "OPTIONAL_PRODUCER_INPUT",
                        f"$.nodes[{index}].inputs.{input_name}",
                        f"binding {binding!r} may be absent because producer "
                        f"{producer_id!r} is optional",
                    )
                )
            if producer_id not in _ancestors(node_id, needs):
                issues.append(
                    ValidationIssue(
                        "UNORDERED_INPUT",
                        f"$.nodes[{index}].inputs.{input_name}",
                        f"producer {producer_id!r} is not a dependency of {node_id!r}",
                    )
                )

    repair_attempt_floor = len(nodes)
    for node_id, node in by_id.items():
        repair = node.get("repair")
        if repair is None:
            continue
        index = positions[node_id]
        if node["kind"] != "integration":
            issues.append(
                ValidationIssue(
                    "REPAIR_ON_NON_INTEGRATION",
                    f"$.nodes[{index}].repair",
                    "only an integration node may declare typed repair routes",
                )
            )
            continue

        check_ids = {check["id"] for check in node.get("checks", [])}
        input_producers = {
            binding.partition(".")[0]
            for binding in node.get("inputs", {}).values()
            if binding.endswith(".changeset")
        }
        route_ids: set[str] = set()
        routed_checks: set[str] = set()
        integration_rounds = 0
        target_rounds: dict[str, int] = defaultdict(int)
        for route_index, route in enumerate(repair["routes"]):
            route_path = f"$.nodes[{index}].repair.routes[{route_index}]"
            if route["id"] in route_ids:
                issues.append(
                    ValidationIssue(
                        "DUPLICATE_REPAIR_ROUTE",
                        f"{route_path}.id",
                        f"duplicate repair route id {route['id']!r}",
                    )
                )
            route_ids.add(route["id"])
            integration_rounds += route["max_rounds"]
            repair_attempt_floor += (len(route["targets"]) + 1) * route["max_rounds"]

            for check_id in route["check_ids"]:
                if check_id not in check_ids:
                    issues.append(
                        ValidationIssue(
                            "UNKNOWN_REPAIR_CHECK",
                            f"{route_path}.check_ids",
                            f"repair route names unknown integration check {check_id!r}",
                        )
                    )
                if check_id in routed_checks:
                    issues.append(
                        ValidationIssue(
                            "AMBIGUOUS_REPAIR_CHECK",
                            f"{route_path}.check_ids",
                            f"check {check_id!r} is handled by more than one repair route",
                        )
                    )
                routed_checks.add(check_id)

            seen_targets: set[tuple[str, str]] = set()
            seen_target_nodes: set[str] = set()
            for target_index, target in enumerate(route["targets"]):
                target_path = f"{route_path}.targets[{target_index}]"
                target_key = (target["node"], target["input"])
                if target_key in seen_targets:
                    issues.append(
                        ValidationIssue(
                            "DUPLICATE_REPAIR_TARGET",
                            target_path,
                            f"duplicate repair target {target_key!r}",
                        )
                    )
                seen_targets.add(target_key)
                if target["node"] in seen_target_nodes:
                    issues.append(
                        ValidationIssue(
                            "DUPLICATE_REPAIR_TARGET_NODE",
                            f"{target_path}.node",
                            f"repair route targets {target['node']!r} more than once",
                        )
                    )
                seen_target_nodes.add(target["node"])
                producer = by_id.get(target["node"])
                if producer is None:
                    issues.append(
                        ValidationIssue(
                            "UNKNOWN_REPAIR_TARGET",
                            f"{target_path}.node",
                            f"repair target {target['node']!r} does not exist",
                        )
                    )
                    continue
                if target["node"] not in input_producers:
                    issues.append(
                        ValidationIssue(
                            "UNBOUND_REPAIR_TARGET",
                            f"{target_path}.node",
                            "repair targets must directly produce a changeset input for the integration",
                        )
                    )
                if (
                    producer["kind"] != "agent"
                    or producer["permission"] != "write"
                    or not producer["required"]
                ):
                    issues.append(
                        ValidationIssue(
                            "INVALID_REPAIR_TARGET",
                            f"{target_path}.node",
                            "repair targets must be required non-destructive writing agent nodes",
                        )
                    )
                effect = producer.get("effect")
                replay_safe = effect in {"none", "read"} or (
                    effect == "idempotent_write"
                    and bool(producer.get("idempotency_key"))
                )
                if not replay_safe:
                    issues.append(
                        ValidationIssue(
                            "UNSAFE_REPAIR_TARGET",
                            f"{target_path}.node",
                            "an automatic repair target must declare a replay-safe effect",
                        )
                    )
                if target["input"] in producer.get("inputs", {}):
                    issues.append(
                        ValidationIssue(
                            "REPAIR_INPUT_COLLISION",
                            f"{target_path}.input",
                            f"repair input {target['input']!r} collides with a static input",
                        )
                    )
                other_consumers = dependents[target["node"]] - {node_id}
                if other_consumers:
                    issues.append(
                        ValidationIssue(
                            "REPAIR_TARGET_FANOUT",
                            f"{target_path}.node",
                            f"repair target also feeds {sorted(other_consumers)}; localized invalidation is unsafe",
                        )
                    )
                target_rounds[target["node"]] += route["max_rounds"]

        integration_attempts = node.get("retry", {}).get("max_attempts", 1)
        if integration_attempts < 1 + integration_rounds:
            issues.append(
                ValidationIssue(
                    "REPAIR_ATTEMPT_BUDGET",
                    f"$.nodes[{index}].retry.max_attempts",
                    f"integration needs at least {1 + integration_rounds} attempts for its repair routes",
                )
            )
        for target_id, rounds in sorted(target_rounds.items()):
            target = by_id[target_id]
            target_attempts = target.get("retry", {}).get("max_attempts", 1)
            if target_attempts < 1 + rounds:
                issues.append(
                    ValidationIssue(
                        "REPAIR_ATTEMPT_BUDGET",
                        f"$.nodes[{positions[target_id]}].retry.max_attempts",
                        f"repair target needs at least {1 + rounds} attempts",
                    )
                )

    if budgets["max_total_attempts"] < repair_attempt_floor:
        issues.append(
            ValidationIssue(
                "REPAIR_TOTAL_ATTEMPT_BUDGET",
                "$.budgets.max_total_attempts",
                f"repair routes require a worst-case budget of at least {repair_attempt_floor} attempts",
            )
        )

    writers = [
        node for node in nodes if node.get("permission") in {"write", "destructive"}
    ]
    for node in writers:
        index = positions[node["id"]]
        if not node.get("write_scope"):
            issues.append(
                ValidationIssue(
                    "MISSING_WRITE_SCOPE",
                    f"$.nodes[{index}].write_scope",
                    "write and destructive nodes must declare their bounded write_scope",
                )
            )
        if node.get("workspace") != "worktree":
            issues.append(
                ValidationIssue(
                    "UNISOLATED_WRITE",
                    f"$.nodes[{index}].workspace",
                    "nodes with write_scope must use an isolated worktree",
                )
            )

    integration_nodes = [node for node in nodes if node["kind"] == "integration"]
    for left_index, left in enumerate(writers):
        for right in writers[left_index + 1 :]:
            ordered = left["id"] in _ancestors(right["id"], needs) or right[
                "id"
            ] in _ancestors(left["id"], needs)
            overlap = any(
                _scopes_overlap(a, b)
                for a in left.get("write_scope", [])
                for b in right.get("write_scope", [])
            )
            if ordered or not overlap:
                continue
            joined = any(
                {left["id"], right["id"]}.issubset(_ancestors(node["id"], needs))
                for node in integration_nodes
            )
            if not joined:
                issues.append(
                    ValidationIssue(
                        "UNJOINED_OVERLAP",
                        "$.nodes",
                        f"parallel writers {left['id']!r} and {right['id']!r} overlap without "
                        "a downstream integration node",
                    )
                )

    for output_name, binding in workflow["outputs"].items():
        producer_id, _, artifact_name = binding.partition(".")
        if producer_id not in by_id or artifact_name not in by_id.get(
            producer_id, {}
        ).get("outputs", {}):
            issues.append(
                ValidationIssue(
                    "INVALID_WORKFLOW_OUTPUT",
                    f"$.outputs.{output_name}",
                    f"binding {binding!r} does not resolve to a declared output",
                )
            )

    output_producers = {
        binding.partition(".")[0] for binding in workflow["outputs"].values()
    }
    contributing = set(output_producers)
    for producer_id in output_producers:
        contributing.update(_ancestors(producer_id, needs))
    for node_id, node in by_id.items():
        if node["required"] and node_id not in contributing:
            issues.append(
                ValidationIssue(
                    "DISCONNECTED_REQUIRED_NODE",
                    f"$.nodes[{positions[node_id]}].required",
                    f"required node {node_id!r} cannot affect a workflow output",
                )
            )

        approval_id = node.get("approval")
        if approval_id is None:
            continue
        approval_node = by_id.get(approval_id)
        if approval_node is None or approval_node.get("kind") != "approval":
            issues.append(
                ValidationIssue(
                    "INVALID_APPROVAL",
                    f"$.nodes[{positions[node_id]}].approval",
                    f"approval {approval_id!r} must name an approval node",
                )
            )
        elif approval_id not in _ancestors(node_id, needs):
            issues.append(
                ValidationIssue(
                    "UNORDERED_APPROVAL",
                    f"$.nodes[{positions[node_id]}].approval",
                    f"approval node {approval_id!r} must be an upstream dependency",
                )
            )

    if issues:
        raise WorkflowValidationError(issues)
