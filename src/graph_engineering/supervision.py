"""Pure graph-shape analysis and bounded node-progress decisions."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from .artifacts import canonical_json


@dataclass(frozen=True, order=True)
class ShapeIssue:
    code: str
    path: str
    message: str


def _graph(
    workflow: Mapping[str, Any],
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, set[str]],
    dict[str, list[str]],
]:
    nodes = {str(node["id"]): node for node in workflow["nodes"]}
    needs = {
        node_id: set(map(str, node.get("needs", ()))) for node_id, node in nodes.items()
    }
    dependents: dict[str, list[str]] = defaultdict(list)
    for node_id, dependencies in needs.items():
        for dependency in dependencies:
            if dependency in nodes:
                dependents[dependency].append(node_id)
    for children in dependents.values():
        children.sort()
    return nodes, needs, dependents


def _ancestors(node_id: str, needs: Mapping[str, set[str]]) -> set[str]:
    found: set[str] = set()
    pending = list(needs.get(node_id, ()))
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(needs.get(current, ()))
    return found


def analyze_topology(workflow: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stable terminal slice, ready layers, unlocks, and critical path."""

    nodes, needs, dependents = _graph(workflow)
    indegree = {node_id: len(dependencies) for node_id, dependencies in needs.items()}
    frontier = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    initial_frontier = list(frontier)
    layers: list[list[str]] = []
    order: list[str] = []
    while frontier:
        layer = frontier
        layers.append(layer)
        order.extend(layer)
        next_frontier: list[str] = []
        for node_id in layer:
            for child in dependents.get(node_id, ()):
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_frontier.append(child)
        frontier = sorted(next_frontier)

    output_producers = {
        str(binding).partition(".")[0] for binding in workflow["outputs"].values()
    }
    contributing = set(output_producers)
    for producer in output_producers:
        contributing.update(_ancestors(producer, needs))

    longest: dict[str, tuple[str, ...]] = {}
    for node_id in order:
        dependencies = sorted(needs[node_id])
        if not dependencies:
            longest[node_id] = (node_id,)
        else:
            candidates = (
                longest[dependency] + (node_id,) for dependency in dependencies
            )
            longest[node_id] = min(candidates, key=lambda path: (-len(path), path))
    output_paths = [
        longest[node_id] for node_id in sorted(output_producers) if node_id in longest
    ]
    critical_path = (
        min(output_paths, key=lambda path: (-len(path), path)) if output_paths else ()
    )

    transitive_unlocks: dict[str, list[str]] = {}
    for node_id in sorted(nodes):
        found: set[str] = set()
        pending = list(dependents.get(node_id, ()))
        while pending:
            current = pending.pop()
            if current in found:
                continue
            found.add(current)
            pending.extend(dependents.get(current, ()))
        transitive_unlocks[node_id] = sorted(found)

    edges = [
        {"from": dependency, "to": node_id}
        for node_id in sorted(nodes)
        for dependency in sorted(needs[node_id])
    ]
    return {
        "workflow_id": workflow["id"],
        "goal": workflow["goal"],
        "node_count": len(nodes),
        "edge_count": len(edges),
        "max_concurrency": workflow["budgets"]["max_concurrency"],
        "initial_frontier": initial_frontier,
        "ready_layers": layers,
        "terminal_slice": [node_id for node_id in order if node_id in contributing],
        "ancillary_nodes": sorted(set(nodes) - contributing),
        "critical_path": list(critical_path),
        "critical_dependencies": [
            {"from": source, "to": target} for source, target in pairwise(critical_path)
        ],
        "dependencies": {node_id: sorted(needs[node_id]) for node_id in sorted(nodes)},
        "unlocks": {
            node_id: list(dependents.get(node_id, ())) for node_id in sorted(nodes)
        },
        "transitive_unlocks": transitive_unlocks,
        "edges": edges,
    }


def graph_shape_issues(workflow: Mapping[str, Any]) -> tuple[ShapeIssue, ...]:
    """Reject only graph smells that are provable from the declared contract."""

    nodes, needs, _dependents = _graph(workflow)
    positions = {str(node["id"]): index for index, node in enumerate(workflow["nodes"])}
    output_producers = {
        str(binding).partition(".")[0] for binding in workflow["outputs"].values()
    }
    contributing = set(output_producers)
    for producer in output_producers:
        contributing.update(_ancestors(producer, needs))

    issues: list[ShapeIssue] = []
    for node_id, node in nodes.items():
        path = f"$.nodes[{positions[node_id]}]"
        outputs = node.get("outputs", {})
        if node.get("kind") != "integration" and len(outputs) > 3:
            issues.append(
                ShapeIssue(
                    "MEGA_NODE",
                    f"{path}.outputs",
                    "a non-integration node may declare at most three independently verifiable outputs",
                )
            )
        if (
            node.get("kind") == "agent"
            and node.get("permission") == "read"
            and not outputs
        ):
            issues.append(
                ShapeIssue(
                    "RESEARCH_OUTPUT_REQUIRED",
                    f"{path}.outputs",
                    "a read-only research/review agent must declare a typed output",
                )
            )
        if (
            node.get("kind") == "agent"
            and not node.get("required", True)
            and node_id not in contributing
        ):
            issues.append(
                ShapeIssue(
                    "DISCONNECTED_OPTIONAL_NODE",
                    f"{path}.required",
                    f"optional node {node_id!r} cannot affect a workflow output",
                )
            )
        join = node.get("join")
        if join is not None and len(needs[node_id]) < 2 and not node.get("inputs"):
            issues.append(
                ShapeIssue(
                    "REDUNDANT_BARRIER",
                    f"{path}.join",
                    "a barrier needs at least two independent settlements to have a cross-set dependency",
                )
            )
        if node.get("kind") == "integration" and join is None:
            input_producers = {
                str(binding).partition(".")[0]
                for binding in node.get("inputs", {}).values()
            }
            unused_optional = sorted(
                dependency
                for dependency in needs[node_id]
                if dependency in nodes
                and not bool(nodes[dependency].get("required", True))
                and dependency not in input_producers
            )
            if unused_optional:
                issues.append(
                    ShapeIssue(
                        "OPTIONAL_INTEGRATION_DELAY",
                        f"{path}.needs",
                        "unrelated optional dependencies delay integration without contributing artifacts: "
                        f"{unused_optional}",
                    )
                )
    return tuple(sorted(issues))


def live_topology(
    workflow: Mapping[str, Any], rows: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Project the current ready frontier and remaining static critical path."""

    topology = analyze_topology(workflow)
    ready: list[str] = []
    terminal = {
        "succeeded",
        "failed",
        "optional_failed",
        "blocked",
        "cancelled",
        "uncertain",
    }
    for node in workflow["nodes"]:
        node_id = str(node["id"])
        if rows[node_id]["status"] != "pending":
            continue
        dependency_statuses = [
            str(rows[dependency]["status"]) for dependency in node.get("needs", ())
        ]
        join = node.get("join")
        if join is None or join["policy"] == "all":
            dispatchable = all(
                status in {"succeeded", "optional_failed"}
                for status in dependency_statuses
            )
        elif join["policy"] == "all_settled":
            dispatchable = all(status in terminal for status in dependency_statuses)
        else:
            passed = sum(status == "succeeded" for status in dependency_statuses)
            threshold = (
                1
                if join["policy"] == "any"
                else int(join["n"])
                if join["policy"] == "n_of_m"
                else len(dependency_statuses) // 2 + 1
            )
            dispatchable = passed >= threshold
        if dispatchable:
            ready.append(node_id)
    return {
        "ready_frontier": sorted(ready),
        "running": sorted(
            node_id for node_id, row in rows.items() if row["status"] == "running"
        ),
        "critical_path": topology["critical_path"],
        "critical_path_remaining": [
            node_id
            for node_id in topology["critical_path"]
            if rows[node_id]["status"] not in terminal
        ],
        "terminal_slice": topology["terminal_slice"],
        "ancillary_nodes": topology["ancillary_nodes"],
        "transitive_unlocks": topology["transitive_unlocks"],
    }


def progress_budget(
    workflow: Mapping[str, Any], node: Mapping[str, Any]
) -> dict[str, int | None]:
    progress = node.get("progress", {})
    attempts = int(node.get("retry", {}).get("max_attempts", 1))
    per_attempt = node.get("timeout_seconds")
    workflow_timeout = int(
        workflow.get("budgets", {}).get("timeout_seconds", per_attempt or 86_400)
    )
    default_elapsed = (
        min(workflow_timeout, int(per_attempt) * attempts)
        if per_attempt is not None
        else workflow_timeout
    )
    return {
        "max_elapsed_seconds": int(
            progress.get("max_elapsed_seconds", default_elapsed)
        ),
        "max_commands": (
            int(progress.get("max_deterministic_checks", progress.get("max_commands")))
            if progress.get("max_deterministic_checks") is not None
            or progress.get("max_commands") is not None
            else None
        ),
        "no_progress_limit": int(node.get("retry", {}).get("no_progress_limit", 1)),
    }


def artifact_set_digest(
    artifacts: Mapping[str, tuple[str, Mapping[str, Any]]],
) -> str | None:
    if not artifacts:
        return None
    return hashlib.sha256(
        canonical_json(
            {
                name: artifact_digest
                for name, (artifact_digest, _schema) in sorted(artifacts.items())
            }
        )
    ).hexdigest()


def next_progress_observation(
    previous: Mapping[str, Any],
    *,
    observed_at: float,
    artifact_digest: str | None,
    failure_digest: str | None,
    deterministic_check_delta: int | None,
    succeeded: bool,
) -> dict[str, Any]:
    """Compute a bounded decision without granting another attempt."""

    started_at = float(previous["started_at"])
    prior_artifact = previous.get("artifact_digest")
    prior_failure = previous.get("failure_digest")
    artifact_delta = artifact_digest is not None and artifact_digest != prior_artifact
    repeated_failure = failure_digest is not None and failure_digest == prior_failure
    no_progress = int(previous.get("no_progress_count") or 0)
    if artifact_delta:
        no_progress = 0
    elif repeated_failure:
        no_progress += 1

    prior_checks = previous.get(
        "deterministic_check_count", previous.get("command_count")
    )
    if deterministic_check_delta is None:
        checks = prior_checks
    else:
        checks = int(prior_checks or 0) + int(deterministic_check_delta)
    last_progress = (
        observed_at
        if artifact_delta
        else float(previous.get("last_meaningful_progress_at") or started_at)
    )
    elapsed = max(0.0, observed_at - started_at)
    decision = "complete" if succeeded else "continue"
    reason = "accepted artifact" if succeeded else "within progress budget"
    if not succeeded and elapsed >= float(previous["max_elapsed_seconds"]):
        decision, reason = "stop", "elapsed budget exhausted"
    elif (
        not succeeded
        and previous.get("max_commands") is not None
        and checks is not None
        and checks >= int(previous["max_commands"])
    ):
        decision, reason = "stop", "deterministic check budget exhausted"
    elif not succeeded and no_progress >= int(previous["no_progress_limit"]):
        decision, reason = "stop", "repeated no-progress digest"

    return {
        "observations": int(previous.get("observations") or 0) + 1,
        "deterministic_check_count": checks,
        "artifact_digest": artifact_digest or prior_artifact,
        "artifact_delta": artifact_delta,
        "failure_digest": failure_digest,
        "repeated_failure": repeated_failure,
        "no_progress_count": no_progress,
        "last_meaningful_progress_at": last_progress,
        "last_observed_at": observed_at,
        "elapsed_seconds": elapsed,
        "decision": decision,
        "reason": reason,
    }


__all__ = [
    "ShapeIssue",
    "analyze_topology",
    "artifact_set_digest",
    "graph_shape_issues",
    "live_topology",
    "next_progress_observation",
    "progress_budget",
]
