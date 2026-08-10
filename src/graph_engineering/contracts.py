"""Deterministic validation for the portable workflow contract."""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .supervision import graph_shape_issues

WORKFLOW_VERSION = "graph-engineering/v1alpha1"

# These are parser safety limits, not execution budgets.  They are deliberately
# generous relative to the canonical ChangeSet and checked before jsonschema is
# allowed to recurse over caller-controlled structures.
MAX_SCHEMA_DEPTH = 64
MAX_SCHEMA_NODES = 8_192
MAX_SCHEMA_BYTES = 256 * 1024
MAX_WORKFLOW_DEPTH = 128
MAX_WORKFLOW_NODES = 100_000
MAX_WORKFLOW_BYTES = 4 * 1024 * 1024

# Every Draft 2020-12 keyword whose value is itself a schema (or a collection
# of schemas) belongs here.  Keeping the vocabulary explicit prevents a $ref
# from hiding below a schema-valued keyword that the resource-policy scan does
# not visit.  ``definitions`` is retained as the supported pre-2020 alias for
# ``$defs``; annotation and assertion values such as ``examples`` and ``enum``
# are deliberately absent because their contents are instance data, not schema
# control flow.
_SCHEMA_MAP_KEYWORDS = frozenset(
    {
        "$defs",
        "definitions",
        "properties",
        "patternProperties",
        "dependentSchemas",
    }
)
_SCHEMA_SINGLE_KEYWORDS = frozenset(
    {
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
)
_SCHEMA_ARRAY_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})


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


@dataclass
class _JsonScan:
    locations: dict[tuple[str | int, ...], Any]
    container_edges: dict[tuple[str | int, ...], set[tuple[str | int, ...]]]


def _path(parts: tuple[str | int, ...]) -> str:
    return "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}" for part in parts
    )


def _string_bytes(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return MAX_WORKFLOW_BYTES + 1


def _primitive_bytes(value: Any) -> int:
    if value is None:
        return 4
    if isinstance(value, bool):
        return 5
    if isinstance(value, str):
        return _string_bytes(value)
    if isinstance(value, int):
        # Avoid converting attacker-controlled giant integers to decimal text.
        return max(1, (abs(value).bit_length() * 30_103) // 100_000 + 2)
    if isinstance(value, float):
        return 32
    return 0


def _scan_json_resource(
    value: Any,
    *,
    root_path: tuple[str | int, ...],
    max_depth: int,
    max_nodes: int,
    max_bytes: int,
    code_prefix: str,
) -> tuple[_JsonScan | None, ValidationIssue | None]:
    """Iteratively bound a JSON-like value without recursion or serialization."""

    locations: dict[tuple[str | int, ...], Any] = {}
    edges: dict[tuple[str | int, ...], set[tuple[str | int, ...]]] = defaultdict(set)
    active: set[int] = set()
    stack: list[tuple[bool, Any, tuple[str | int, ...], int]] = [
        (False, value, root_path, 0)
    ]
    node_count = 0
    byte_count = 0
    while stack:
        exiting, current, current_path, depth = stack.pop()
        if exiting:
            active.remove(id(current))
            continue
        if depth > max_depth:
            return None, ValidationIssue(
                f"{code_prefix}_DEPTH_EXCEEDED",
                _path(current_path),
                f"resource nesting exceeds the limit of {max_depth}",
            )
        node_count += 1
        if node_count > max_nodes:
            return None, ValidationIssue(
                f"{code_prefix}_NODE_LIMIT_EXCEEDED",
                _path(current_path),
                f"resource contains more than {max_nodes} values",
            )
        locations[current_path] = current
        if not isinstance(current, (dict, list)):
            if not isinstance(current, (str, int, float, bool)) and current is not None:
                return None, ValidationIssue(
                    f"{code_prefix}_TYPE_ERROR",
                    _path(current_path),
                    "resource must contain JSON values only",
                )
            byte_count += _primitive_bytes(current)
            if byte_count > max_bytes:
                return None, ValidationIssue(
                    f"{code_prefix}_BYTES_EXCEEDED",
                    _path(current_path),
                    f"resource exceeds the encoded-size limit of {max_bytes} bytes",
                )
            continue

        identity = id(current)
        if identity in active:
            return None, ValidationIssue(
                f"{code_prefix}_OBJECT_CYCLE",
                _path(current_path),
                "resource contains an in-memory container cycle",
            )
        if node_count + len(current) > max_nodes:
            return None, ValidationIssue(
                f"{code_prefix}_NODE_LIMIT_EXCEEDED",
                _path(current_path),
                f"resource contains more than {max_nodes} values",
            )
        active.add(identity)
        stack.append((True, current, current_path, depth))
        if isinstance(current, dict):
            if any(not isinstance(key, str) for key in current):
                return None, ValidationIssue(
                    f"{code_prefix}_TYPE_ERROR",
                    _path(current_path),
                    "object keys must be strings",
                )
            byte_count += sum(_string_bytes(key) for key in current)
            children = [(key, current[key]) for key in sorted(current, reverse=True)]
        else:
            children = list(reversed(list(enumerate(current))))
        if byte_count > max_bytes:
            return None, ValidationIssue(
                f"{code_prefix}_BYTES_EXCEEDED",
                _path(current_path),
                f"resource exceeds the encoded-size limit of {max_bytes} bytes",
            )
        for part, child in children:
            child_path = (*current_path, part)
            if isinstance(child, (dict, list)):
                edges[current_path].add(child_path)
            stack.append((False, child, child_path, depth + 1))
    return _JsonScan(locations, edges), None


def _schema_resources(
    workflow: dict[str, Any],
) -> list[tuple[tuple[str | int, ...], Any]]:
    found: list[tuple[tuple[str | int, ...], Any]] = []
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        return found
    for node_index, node in enumerate(nodes[:1_001]):
        if not isinstance(node, dict):
            continue
        outputs = node.get("outputs")
        if not isinstance(outputs, dict):
            continue
        for output_name, contract in outputs.items():
            if not isinstance(output_name, str) or not isinstance(contract, dict):
                continue
            base = ("nodes", node_index, "outputs", output_name)
            if "schema" in contract:
                found.append(((*base, "schema"), contract["schema"]))
            if "acceptance_schema" in contract:
                found.append(
                    ((*base, "acceptance_schema"), contract["acceptance_schema"])
                )
    return found


def _resolve_local_pointer(
    reference: str,
    locations: dict[tuple[str | int, ...], Any],
) -> tuple[str | int, ...] | None:
    if reference == "#":
        return ()
    if not reference.startswith("#/"):
        return None
    fragment = unquote(reference[2:])
    parts: list[str | int] = []
    current: Any = locations.get(())
    for raw in fragment.split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and token in current:
            part: str | int = token
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            part = int(token)
            if part >= len(current):
                return None
            current = current[part]
        else:
            return None
        parts.append(part)
    target = tuple(parts)
    return target if target in locations else None


def _json_schema_locations(scan: _JsonScan) -> set[tuple[str | int, ...]]:
    """Return only locations interpreted as schemas by Draft 2020-12."""
    schemas: set[tuple[str | int, ...]] = set()
    pending = [()]
    while pending:
        location = pending.pop()
        if location in schemas:
            continue
        value = scan.locations.get(location)
        if not isinstance(value, (dict, bool)):
            continue
        schemas.add(location)
        if not isinstance(value, dict):
            continue
        for keyword in _SCHEMA_MAP_KEYWORDS:
            members = value.get(keyword)
            if not isinstance(members, dict):
                continue
            pending.extend((*location, keyword, name) for name in members)
        for keyword in _SCHEMA_SINGLE_KEYWORDS:
            if keyword in value:
                pending.append((*location, keyword))
        for keyword in _SCHEMA_ARRAY_KEYWORDS:
            members = value.get(keyword)
            if isinstance(members, list):
                pending.extend(
                    (*location, keyword, index) for index in range(len(members))
                )
    return schemas


def _reference_cycle(
    scan: _JsonScan,
    references: list[tuple[tuple[str | int, ...], tuple[str | int, ...]]],
) -> bool:
    containers = {
        path
        for path, value in scan.locations.items()
        if isinstance(value, (dict, list))
    }
    edges = {path: set(scan.container_edges.get(path, ())) for path in containers}
    for source, target in references:
        if target in containers:
            edges.setdefault(source, set()).add(target)
    indegree = {path: 0 for path in containers}
    for targets in edges.values():
        for target in targets:
            indegree[target] += 1
    ready = deque(path for path, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        source = ready.popleft()
        visited += 1
        for target in edges.get(source, ()):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    return visited != len(containers)


def _schema_resource_issue(
    schema: Any, root_path: tuple[str | int, ...]
) -> ValidationIssue | None:
    if not isinstance(schema, dict):
        return ValidationIssue(
            "EXTERNAL_SCHEMA_UNSUPPORTED",
            _path(root_path),
            "artifact schemas must be embedded JSON Schema objects",
        )
    scan, issue = _scan_json_resource(
        schema,
        root_path=(),
        max_depth=MAX_SCHEMA_DEPTH,
        max_nodes=MAX_SCHEMA_NODES,
        max_bytes=MAX_SCHEMA_BYTES,
        code_prefix="SCHEMA",
    )
    if issue is not None:
        return ValidationIssue(issue.code, _path(root_path), issue.message)
    assert scan is not None
    references: list[tuple[tuple[str | int, ...], tuple[str | int, ...]]] = []
    schema_locations = _json_schema_locations(scan)
    for location in sorted(schema_locations, key=_path):
        value = scan.locations[location]
        if not isinstance(value, dict):
            continue
        for keyword in (
            "$id",
            "$anchor",
            "$dynamicAnchor",
            "$dynamicRef",
            "$recursiveRef",
        ):
            if keyword in value:
                return ValidationIssue(
                    "SCHEMA_RESOURCE_UNSUPPORTED",
                    _path((*root_path, *location, keyword)),
                    f"{keyword} is unsupported; schemas must be one embedded deterministic resource",
                )
        if "$ref" not in value:
            continue
        reference = value["$ref"]
        reference_path = (*root_path, *location, "$ref")
        if not isinstance(reference, str):
            return ValidationIssue(
                "SCHEMA_REFERENCE_INVALID",
                _path(reference_path),
                "$ref must be a string local JSON Pointer",
            )
        if not reference.startswith("#"):
            return ValidationIssue(
                "SCHEMA_EXTERNAL_REFERENCE",
                _path(reference_path),
                "external and relative schema references are not allowed",
            )
        target = _resolve_local_pointer(reference, scan.locations)
        if target is None or target not in schema_locations:
            return ValidationIssue(
                "SCHEMA_UNRESOLVED_REFERENCE",
                _path(reference_path),
                "local schema reference does not resolve to an embedded JSON Pointer",
            )
        references.append((location, target))
    if _reference_cycle(scan, references):
        return ValidationIssue(
            "SCHEMA_REFERENCE_CYCLE",
            _path(root_path),
            "schema containment and local references form a recursive cycle",
        )
    return None


def _resource_issues(workflow: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for resource_path, schema in _schema_resources(workflow):
        issue = _schema_resource_issue(schema, resource_path)
        if issue is not None:
            issues.append(issue)
    if issues:
        return issues
    _, issue = _scan_json_resource(
        workflow,
        root_path=(),
        max_depth=MAX_WORKFLOW_DEPTH,
        max_nodes=MAX_WORKFLOW_NODES,
        max_bytes=MAX_WORKFLOW_BYTES,
        code_prefix="WORKFLOW",
    )
    return [issue] if issue is not None else []


def load_workflow(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        value = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, RecursionError) as exc:
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

    resource_issues = _resource_issues(workflow)
    if resource_issues:
        raise WorkflowValidationError(resource_issues)
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
        for output_name, contract in node.get("outputs", {}).items():
            output_schema = contract["schema"]
            try:
                Draft202012Validator.check_schema(output_schema)
            except (SchemaError, RecursionError) as exc:
                message = getattr(
                    exc, "message", "schema validation exceeded safe recursion"
                )
                issues.append(
                    ValidationIssue(
                        "INVALID_OUTPUT_SCHEMA",
                        f"$.nodes[{index}].outputs.{output_name}.schema",
                        message,
                    )
                )
            acceptance_schema = contract.get("acceptance_schema")
            if acceptance_schema is None:
                continue
            try:
                Draft202012Validator.check_schema(acceptance_schema)
            except (SchemaError, RecursionError) as exc:
                message = getattr(
                    exc, "message", "schema validation exceeded safe recursion"
                )
                issues.append(
                    ValidationIssue(
                        "INVALID_ACCEPTANCE_SCHEMA",
                        f"$.nodes[{index}].outputs.{output_name}.acceptance_schema",
                        message,
                    )
                )
        join = node.get("join")
        if join is not None:
            dependencies = node.get("needs", [])
            if not dependencies:
                issues.append(
                    ValidationIssue(
                        "EMPTY_JOIN",
                        f"$.nodes[{index}].join",
                        "a join must declare at least one dependency in needs",
                    )
                )
            if join["policy"] == "n_of_m" and join["n"] > len(dependencies):
                issues.append(
                    ValidationIssue(
                        "IMPOSSIBLE_JOIN_THRESHOLD",
                        f"$.nodes[{index}].join.n",
                        f"threshold {join['n']} exceeds {len(dependencies)} dependencies",
                    )
                )
            if join["policy"] != "all":
                required_dependencies = sorted(
                    dependency
                    for dependency in dependencies
                    if dependency in by_id and by_id[dependency]["required"]
                )
                if required_dependencies:
                    issues.append(
                        ValidationIssue(
                            "REQUIRED_QUORUM_MEMBER",
                            f"$.nodes[{index}].needs",
                            "non-all joins may only consume optional dependencies; "
                            f"required members would defeat quorum semantics: {required_dependencies}",
                        )
                    )
            if join["policy"] != "all" and node.get("inputs"):
                issues.append(
                    ValidationIssue(
                        "UNSAFE_JOIN_INPUT",
                        f"$.nodes[{index}].inputs",
                        "a join that may admit failed or unsettled dependencies cannot bind their artifacts; consume the persisted join settlement instead",
                    )
                )
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

    issues.extend(
        ValidationIssue(issue.code, issue.path, issue.message)
        for issue in graph_shape_issues(workflow)
    )

    if issues:
        raise WorkflowValidationError(issues)
