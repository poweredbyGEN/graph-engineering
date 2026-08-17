"""Closed deterministic operations for portable workflow transform nodes.

The registry is deliberately data-only: workflow authors choose a known operation and
provide JSON configuration.  There is no expression evaluator, import hook, or shell escape.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from jsonschema import Draft202012Validator

from .artifacts import canonical_json


class BuiltinOperationError(ValueError):
    """A built-in operation received invalid typed input or configuration."""


Operation = Callable[[Mapping[str, Any], Mapping[str, Any]], Any]


def _value_at(value: Any, path: Sequence[str | int]) -> Any:
    current = value
    for component in path:
        if isinstance(component, int):
            if not isinstance(current, list) or component >= len(current):
                raise BuiltinOperationError(
                    f"path component {component!r} is unavailable"
                )
            current = current[component]
        else:
            if not isinstance(current, Mapping) or component not in current:
                raise BuiltinOperationError(
                    f"path component {component!r} is unavailable"
                )
            current = current[component]
    return current


def _source(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    if "literal" in config:
        return config["literal"]
    name = config.get("input", "value")
    if not isinstance(name, str) or name not in inputs:
        raise BuiltinOperationError(f"input {name!r} is unavailable")
    path = config.get("path", [])
    if not isinstance(path, list) or any(
        not isinstance(part, (str, int)) for part in path
    ):
        raise BuiltinOperationError(
            "path must be an array of string or integer components"
        )
    return _value_at(inputs[name], path)


def _identity(value: Any) -> bytes:
    return canonical_json(value)


def _schema_validate(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    value = _source(inputs, config)
    schema = config.get("schema")
    if not isinstance(schema, dict):
        raise BuiltinOperationError("schema_validate requires an inline JSON schema")
    Draft202012Validator.check_schema(schema)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(value),
        key=lambda error: list(error.path),
    )
    return {
        "valid": not errors,
        "errors": [
            {"path": list(error.path), "message": error.message} for error in errors
        ],
    }


def _select(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    value = _source(inputs, config)
    fields = config.get("fields")
    if (
        not isinstance(value, Mapping)
        or not isinstance(fields, list)
        or not all(isinstance(field, str) for field in fields)
    ):
        raise BuiltinOperationError(
            "select requires an object and a string fields array"
        )
    missing = [field for field in fields if field not in value]
    if missing:
        raise BuiltinOperationError(f"selected fields are unavailable: {missing}")
    return {field: value[field] for field in fields}


def _map(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    value = _source(inputs, config)
    fields = config.get("fields")
    if (
        not isinstance(value, list)
        or not isinstance(fields, dict)
        or not all(
            isinstance(target, str) and isinstance(source, str)
            for target, source in fields.items()
        )
    ):
        raise BuiltinOperationError(
            "map requires an array of objects and an output-to-input fields object"
        )
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise BuiltinOperationError(f"map item {index} is not an object")
        missing = [source for source in fields.values() if source not in item]
        if missing:
            raise BuiltinOperationError(f"map item {index} lacks fields {missing}")
        result.append({target: item[source] for target, source in fields.items()})
    return result


def _arrays(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> list[list[Any]]:
    names = config.get("inputs")
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(name, str) for name in names)
    ):
        raise BuiltinOperationError(
            "operation requires a non-empty inputs string array"
        )
    values = []
    for name in names:
        value = inputs.get(name)
        if not isinstance(value, list):
            raise BuiltinOperationError(f"input {name!r} is not an array")
        values.append(value)
    return values


def _stable_union(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    seen: set[bytes] = set()
    result = []
    for values in _arrays(inputs, config):
        for value in values:
            key = _identity(value)
            if key not in seen:
                seen.add(key)
                result.append(value)
    return result


def _dedupe(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    value = _source(inputs, config)
    if not isinstance(value, list):
        raise BuiltinOperationError("dedupe requires an array")
    return _stable_union({"items": value}, {"inputs": ["items"]})


def _sort(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    value = _source(inputs, config)
    if not isinstance(value, list):
        raise BuiltinOperationError("sort requires an array")
    by = config.get("by", [])
    if not isinstance(by, list) or any(not isinstance(part, (str, int)) for part in by):
        raise BuiltinOperationError("sort by must be a typed path")
    reverse = config.get("direction", "asc") == "desc"
    try:
        return sorted(
            value, key=lambda item: _value_at(item, by) if by else item, reverse=reverse
        )
    except TypeError as exc:
        raise BuiltinOperationError(
            "sort keys must have mutually comparable JSON types"
        ) from exc


def evaluate_predicate(value: Any, predicate: Mapping[str, Any]) -> bool:
    """Evaluate the small, closed predicate language used by routes and operations."""

    op = predicate.get("op")
    expected = predicate.get("value")
    if op == "equals":
        return type(value) is type(expected) and value == expected
    if op == "not_equals":
        return not (type(value) is type(expected) and value == expected)
    if op == "in":
        choices = predicate.get("values")
        if not isinstance(choices, list):
            raise BuiltinOperationError("in predicate requires values")
        return any(
            type(value) is type(choice) and value == choice for choice in choices
        )
    if op == "exists":
        return value is not None
    if op in {"gt", "gte", "lt", "lte"}:
        if (
            isinstance(value, bool)
            or isinstance(expected, bool)
            or not isinstance(value, (int, float))
            or not isinstance(expected, (int, float))
        ):
            raise BuiltinOperationError(f"{op} requires numeric operands")
        return {
            "gt": value > expected,
            "gte": value >= expected,
            "lt": value < expected,
            "lte": value <= expected,
        }[op]
    if op == "type_is":
        names = {
            "null": type(None),
            "boolean": bool,
            "integer": int,
            "number": (int, float),
            "string": str,
            "array": list,
            "object": dict,
        }
        expected_type = names.get(expected)
        if expected_type is None:
            raise BuiltinOperationError(f"unknown JSON type {expected!r}")
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return isinstance(value, expected_type)
    raise BuiltinOperationError(f"unknown predicate operation {op!r}")


def _typed_predicate(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    predicate = config.get("predicate")
    if not isinstance(predicate, Mapping):
        raise BuiltinOperationError("typed_predicate requires predicate configuration")
    return {"matched": evaluate_predicate(_source(inputs, config), predicate)}


def _risk_router(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    value = _source(inputs, config)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BuiltinOperationError("risk_router requires a numeric score")
    medium = config.get("medium_at")
    high = config.get("high_at")
    if (
        not isinstance(medium, (int, float))
        or not isinstance(high, (int, float))
        or medium > high
    ):
        raise BuiltinOperationError(
            "risk thresholds must be numeric and medium_at <= high_at"
        )
    level = "high" if value >= high else "medium" if value >= medium else "low"
    return {"risk": level, "score": value}


def _verdict_reducer(inputs: Mapping[str, Any], config: Mapping[str, Any]) -> Any:
    values = _source(inputs, config)
    if not isinstance(values, list) or not values:
        raise BuiltinOperationError(
            "verdict_reducer requires a non-empty verdict array"
        )
    field = config.get("field", "verdict")
    order = config.get("severity", ["pass", "warn", "fail"])
    if (
        not isinstance(field, str)
        or not isinstance(order, list)
        or not all(isinstance(item, str) for item in order)
    ):
        raise BuiltinOperationError("verdict reducer configuration is invalid")
    rank = {name: index for index, name in enumerate(order)}
    verdicts = []
    for item in values:
        verdict = item.get(field) if isinstance(item, Mapping) else item
        if verdict not in rank:
            raise BuiltinOperationError(f"unknown verdict {verdict!r}")
        verdicts.append(verdict)
    reduced = max(verdicts, key=rank.__getitem__)
    return {
        "verdict": reduced,
        "counts": {name: verdicts.count(name) for name in order},
    }


OPERATIONS: Mapping[str, Operation] = {
    "schema_validate": _schema_validate,
    "select": _select,
    "map": _map,
    "stable_union": _stable_union,
    "dedupe": _dedupe,
    "sort": _sort,
    "typed_predicate": _typed_predicate,
    "risk_router": _risk_router,
    "verdict_reducer": _verdict_reducer,
}


def builtin_executor(
    operation: Mapping[str, Any],
) -> Callable[[Any], Mapping[str, Any]]:
    name = operation.get("name")
    output = operation.get("output", "result")
    if name not in OPERATIONS or not isinstance(output, str):
        raise BuiltinOperationError(f"unknown built-in operation {name!r}")
    config = operation.get("config", {})
    if not isinstance(config, Mapping):
        raise BuiltinOperationError("operation config must be an object")

    def execute(context: Any) -> Mapping[str, Any]:
        # Round-trip config to detach it from mutable workflow dictionaries.
        safe_config = json.loads(json.dumps(config))
        return {output: OPERATIONS[name](context.inputs, safe_config)}

    return execute


__all__ = [
    "OPERATIONS",
    "BuiltinOperationError",
    "builtin_executor",
    "evaluate_predicate",
]
