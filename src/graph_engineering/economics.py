"""Outcome economics and evidence-gated durable-template promotion."""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .artifacts import canonical_json

OUTCOME_VERSION = "graph-engineering/outcome/v2"
_LEGACY_OUTCOME_VERSIONS = {"graph-engineering/outcome/v1"}
PROMOTION_VERSION = "graph-engineering/promotion/v1"
_MODES = {"LINEAR", "TRANSIENT_GRAPH", "DURABLE_GRAPH"}
_FAILURES = {"none", "expected_rejection", "operational_failure"}
_OBJECTIVES = {"latency", "quality", "cost"}
_OUTCOME_LOG_ENV = "GRAPH_ENGINEERING_OUTCOME_LOG"


class EconomicsError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def validate_outcome(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "version",
        "guard_metrics",
        "id",
        "match_id",
        "task_id",
        "mode",
        "objective",
        "acceptance_suite",
        "accepted",
        "wall_seconds",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "model",
        "verifier_decisions",
        "verifier_overturns",
        "cold_adoption_seconds",
        "integration_failures",
        "escaped_defects",
        "failure_class",
        "merged_proof_seconds",
        "deployed_proof_seconds",
        "live_proof_seconds",
        "evidence",
    }
    version = value.get("version")
    if version in _LEGACY_OUTCOME_VERSIONS:
        # Legacy records predate guard metrics; validate and digest them under
        # their own field set so existing outcome logs stay verifiable.
        required = required - {"guard_metrics"}
    elif version != OUTCOME_VERSION:
        raise EconomicsError(
            "OUTCOME_INVALID", "outcome has unexpected fields or version"
        )
    if set(value) != required:
        raise EconomicsError(
            "OUTCOME_INVALID", "outcome has unexpected fields or version"
        )
    for field in ("id", "match_id", "task_id", "acceptance_suite", "model"):
        raw = value[field]
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 512:
            raise EconomicsError("OUTCOME_INVALID", f"{field} is invalid")
    if (
        value["mode"] not in _MODES
        or value["failure_class"] not in _FAILURES
        or value["objective"] not in _OBJECTIVES
    ):
        raise EconomicsError(
            "OUTCOME_INVALID", "mode, objective, or failure_class is invalid"
        )
    if not isinstance(value["accepted"], bool):
        raise EconomicsError("OUTCOME_INVALID", "accepted must be boolean")
    if version == OUTCOME_VERSION:
        guards = value["guard_metrics"]
        if not isinstance(guards, list) or len(guards) > 32:
            raise EconomicsError(
                "OUTCOME_INVALID", "guard_metrics must be a list of at most 32 entries"
            )
        for entry in guards:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"name", "regressed"}
                or not isinstance(entry["name"], str)
                or not entry["name"].strip()
                or len(entry["name"]) > 256
                or not isinstance(entry["regressed"], bool)
            ):
                raise EconomicsError(
                    "OUTCOME_INVALID",
                    "guard_metrics entries must be {name, regressed}",
                )
        # intent: Goodhart guard. An objective satisfied while a declared
        # countermetric regresses is a failed outcome, never a win.
        if value["accepted"] and any(entry["regressed"] for entry in guards):
            raise EconomicsError(
                "OUTCOME_INVALID",
                "accepted cannot be true while a guard metric is regressed",
            )
    normalized = dict(value)
    for field in (
        "wall_seconds",
        "cost_usd",
        "cold_adoption_seconds",
        "merged_proof_seconds",
        "deployed_proof_seconds",
        "live_proof_seconds",
    ):
        raw = value[field]
        if raw is not None and (
            isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0
        ):
            raise EconomicsError(
                "OUTCOME_INVALID", f"{field} must be non-negative or null"
            )
        normalized[field] = None if raw is None else float(raw)
    for field in (
        "input_tokens",
        "output_tokens",
        "verifier_decisions",
        "verifier_overturns",
        "integration_failures",
        "escaped_defects",
    ):
        raw = value[field]
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise EconomicsError(
                "OUTCOME_INVALID", f"{field} must be a non-negative integer"
            )
    if value["verifier_overturns"] > value["verifier_decisions"]:
        raise EconomicsError("OUTCOME_INVALID", "verifier_overturns exceeds decisions")
    if (
        not isinstance(value["evidence"], list)
        or not value["evidence"]
        or any(
            not isinstance(item, str) or not item or len(item) > 1024
            for item in value["evidence"]
        )
    ):
        raise EconomicsError(
            "OUTCOME_INVALID", "evidence must be a non-empty string list"
        )
    body = dict(normalized)
    return {**body, "digest": hashlib.sha256(canonical_json(body)).hexdigest()}


def load_outcomes(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise EconomicsError("OUTCOME_READ", "cannot read outcome JSON") from exc
    if len(raw) > 1_000_000 or not isinstance(value, (dict, list)):
        raise EconomicsError("OUTCOME_INVALID", "outcome input must be bounded JSON")
    values = value if isinstance(value, list) else [value]
    if not 1 <= len(values) <= 1_000 or any(
        not isinstance(item, dict) for item in values
    ):
        raise EconomicsError(
            "OUTCOME_INVALID", "outcome input must contain 1..1000 records"
        )
    return [validate_outcome(item) for item in values]


def outcome_log_path() -> Path:
    override = os.environ.get(_OUTCOME_LOG_ENV)
    if override:
        return Path(override)
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "graph-engineering" / "outcomes.jsonl"


def record_outcomes(records: Sequence[Mapping[str, Any]]) -> Path:
    """Append validated outcome evidence; explicit recording fails loudly."""

    path = outcome_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"".join(canonical_json(record) + b"\n" for record in records)
    try:
        with path.open("ab") as handle:
            handle.write(payload)
    except OSError as exc:
        raise EconomicsError(
            "OUTCOME_RECORD", "cannot persist outcome evidence"
        ) from exc
    return path


def summarize_outcomes() -> dict[str, Any]:
    path = outcome_log_path()
    records: list[dict[str, Any]] = []
    corrupt = 0
    if path.exists():
        for line in path.read_bytes().splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise TypeError
                supplied_digest = raw.pop("digest", None)
                record = validate_outcome(raw)
                if supplied_digest != record["digest"]:
                    raise ValueError
                records.append(record)
            except (TypeError, ValueError, EconomicsError, json.JSONDecodeError):
                corrupt += 1
    return {
        "log": str(path),
        "total": len(records),
        "accepted": sum(record["accepted"] for record in records),
        "input_tokens": sum(record["input_tokens"] for record in records),
        "output_tokens": sum(record["output_tokens"] for record in records),
        "cost_usd": round(sum(record["cost_usd"] or 0 for record in records), 6),
        "by_model": {
            model: sum(record["model"] == model for record in records)
            for model in sorted({record["model"] for record in records})
        },
        "verifier_decisions": sum(record["verifier_decisions"] for record in records),
        "verifier_overturns": sum(record["verifier_overturns"] for record in records),
        "cold_adoption_seconds": round(
            sum(record["cold_adoption_seconds"] or 0 for record in records), 3
        ),
        "integration_failures": sum(
            record["integration_failures"] for record in records
        ),
        "escaped_defects": sum(record["escaped_defects"] for record in records),
        "guard_metric_regressions": sum(
            any(entry["regressed"] for entry in record.get("guard_metrics", ()))
            for record in records
        ),
        "expected_rejections": sum(
            record["failure_class"] == "expected_rejection" for record in records
        ),
        "operational_failures": sum(
            record["failure_class"] == "operational_failure" for record in records
        ),
        "merged_proofs": sum(
            record["merged_proof_seconds"] is not None for record in records
        ),
        "deployed_proofs": sum(
            record["deployed_proof_seconds"] is not None for record in records
        ),
        "live_proofs": sum(
            record["live_proof_seconds"] is not None for record in records
        ),
        "corrupt_lines": corrupt,
    }


def promotion_evidence(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Require repeated, accepted, matched wins; never promote on activity metrics."""

    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for record in records:
        groups[str(record["match_id"])].append(record)
    pairs: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for match_id, group in sorted(groups.items()):
        linear = [item for item in group if item["mode"] == "LINEAR"]
        graph = [item for item in group if item["mode"] == "TRANSIENT_GRAPH"]
        if len(linear) != 1 or len(graph) != 1:
            rejected.append(
                {
                    "match_id": match_id,
                    "reason": "requires one linear and one transient run",
                }
            )
            continue
        base, candidate = linear[0], graph[0]
        if base["acceptance_suite"] != candidate["acceptance_suite"]:
            rejected.append(
                {"match_id": match_id, "reason": "acceptance suites differ"}
            )
            continue
        if not base["accepted"] or not candidate["accepted"]:
            rejected.append(
                {"match_id": match_id, "reason": "both runs must pass acceptance"}
            )
            continue
        if any(
            entry.get("regressed")
            for run in (base, candidate)
            for entry in run.get("guard_metrics", ())
        ):
            rejected.append(
                {
                    "match_id": match_id,
                    "reason": "a run reports a regressed guard metric",
                }
            )
            continue
        if base["cost_usd"] is None or candidate["cost_usd"] is None:
            rejected.append(
                {"match_id": match_id, "reason": "both costs must be reported"}
            )
            continue
        if candidate["escaped_defects"] > base["escaped_defects"]:
            rejected.append(
                {
                    "match_id": match_id,
                    "reason": "transient graph worsened escaped defects",
                }
            )
            continue
        if base["objective"] != candidate["objective"]:
            rejected.append(
                {"match_id": match_id, "reason": "declared objectives differ"}
            )
            continue
        objective = candidate["objective"]
        base_end_to_end = base["wall_seconds"] + (base["cold_adoption_seconds"] or 0)
        graph_end_to_end = candidate["wall_seconds"] + (
            candidate["cold_adoption_seconds"] or 0
        )
        won = (
            graph_end_to_end < base_end_to_end
            if objective == "latency"
            else candidate["cost_usd"] < base["cost_usd"]
            if objective == "cost"
            else candidate["escaped_defects"] < base["escaped_defects"]
        )
        if not won:
            rejected.append(
                {
                    "match_id": match_id,
                    "reason": f"transient graph did not win declared {objective} objective",
                }
            )
            continue
        pairs.append(
            {
                "match_id": match_id,
                "acceptance_suite": base["acceptance_suite"],
                "objective": objective,
                "linear_end_to_end_seconds": base_end_to_end,
                "graph_end_to_end_seconds": graph_end_to_end,
                "linear_cost_usd": base["cost_usd"],
                "graph_cost_usd": candidate["cost_usd"],
                "linear_escaped_defects": base["escaped_defects"],
                "graph_escaped_defects": candidate["escaped_defects"],
                "linear_tokens": base["input_tokens"] + base["output_tokens"],
                "graph_tokens": candidate["input_tokens"] + candidate["output_tokens"],
            }
        )
    suites = {pair["acceptance_suite"] for pair in pairs}
    objectives = {pair["objective"] for pair in pairs}
    eligible = len(pairs) >= 3 and len(suites) == 1 and len(objectives) == 1
    total_linear_wall = sum(pair["linear_end_to_end_seconds"] for pair in pairs)
    total_graph_wall = sum(pair["graph_end_to_end_seconds"] for pair in pairs)
    body = {
        "version": PROMOTION_VERSION,
        "eligible": eligible,
        "review_required": True,
        "matched_wins": len(pairs),
        "minimum_matched_wins": 3,
        "equal_acceptance_suite": len(suites) == 1 and bool(suites),
        "declared_objective": next(iter(objectives)) if len(objectives) == 1 else None,
        "wall_speedup_percent": (
            round((1 - total_graph_wall / total_linear_wall) * 100, 3)
            if total_linear_wall
            else None
        ),
        "linear_cost_usd": round(sum(pair["linear_cost_usd"] for pair in pairs), 6),
        "graph_cost_usd": round(sum(pair["graph_cost_usd"] for pair in pairs), 6),
        "linear_tokens": sum(pair["linear_tokens"] for pair in pairs),
        "graph_tokens": sum(pair["graph_tokens"] for pair in pairs),
        "linear_escaped_defects": sum(pair["linear_escaped_defects"] for pair in pairs),
        "graph_escaped_defects": sum(pair["graph_escaped_defects"] for pair in pairs),
        "pairs": pairs,
        "rejected": rejected,
        "decision": "ELIGIBLE_FOR_NAMED_REVIEW" if eligible else "KEEP_TRANSIENT",
    }
    return {**body, "digest": hashlib.sha256(canonical_json(body)).hexdigest()}


__all__ = [
    "OUTCOME_VERSION",
    "PROMOTION_VERSION",
    "EconomicsError",
    "load_outcomes",
    "outcome_log_path",
    "promotion_evidence",
    "record_outcomes",
    "summarize_outcomes",
    "validate_outcome",
]
