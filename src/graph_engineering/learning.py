"""Deterministic run benchmarks and reviewed feedback-to-learning proposals."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .artifacts import canonical_json
from .lifecycle import LifecycleStore
from .session_ux import status_projection
from .state import StateStore
from .supervision import analyze_topology

BENCHMARK_VERSION = "graph-engineering/benchmark/v1"
BASELINE_VERSION = "graph-engineering/baseline/v1"
FEEDBACK_VERSION = "graph-engineering/feedback/v1"
LEARNING_PROPOSAL_VERSION = "graph-engineering/learning-proposal/v1"

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SENSITIVE = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]{8,}|-----BEGIN .*PRIVATE KEY-----|"
    r"(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+)"
)
_TARGETS = frozenset({"regression_test", "project_decision", "workflow", "skill"})
_SOURCES = frozenset({"human", "test", "verifier", "runtime"})
_MAX_INPUT_BYTES = 64 * 1024
_MAX_ITEMS = 100
_MAX_TEXT = 2_000


class LearningError(RuntimeError):
    """Stable error at the benchmark and learning boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _finite_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LearningError("LEARNING_SCHEMA", f"{path} must be a number")
    number = float(value)
    if not (-1e15 < number < 1e15):
        raise LearningError("LEARNING_SCHEMA", f"{path} is outside the supported range")
    return number


def _union_seconds(intervals: Sequence[tuple[float, float]]) -> float:
    ordered = sorted((start, max(start, end)) for start, end in intervals)
    if not ordered:
        return 0.0
    total = 0.0
    begin, end = ordered[0]
    for next_begin, next_end in ordered[1:]:
        if next_begin > end:
            total += end - begin
            begin, end = next_begin, next_end
        else:
            end = max(end, next_end)
    return total + end - begin


def benchmark_run(state_path: Path, run_id: str) -> dict[str, Any]:
    """Derive only metrics supported by durable state and lifecycle evidence."""

    store = StateStore(state_path)
    run = store.run(run_id)
    attempts = list(store.attempt_rows(run_id))
    progress = store.progress_rows(run_id)
    topology = analyze_topology(run["workflow"])
    lifecycle = LifecycleStore(state_path)
    _context, events = lifecycle.snapshot(run_id)

    created_at = float(run["created_at"])
    terminal_events = [event for event in events if event.event_type.startswith("run.")]
    terminal = next(
        (
            event
            for event in reversed(terminal_events)
            if event.event_type
            in {
                "run.succeeded",
                "run.failed",
                "run.cancelled",
                "run.needs_reconciliation",
            }
        ),
        None,
    )
    finished_at = float(terminal.created_at) if terminal is not None else None
    artifact_events = [
        event for event in events if event.event_type == "artifact.accepted"
    ]
    first_artifact = min((event.created_at for event in artifact_events), default=None)
    retry_count = sum(
        max(0, int(node["attempt_count"]) - 1)
        for node in store.node_rows(run_id).values()
    )
    rejected_attempts = sum(
        attempt["status"] in {"failed", "interrupted", "cancelled"}
        for attempt in attempts
    )
    gate_rejections = 0
    for attempt in attempts:
        raw = attempt.get("failure_json")
        if not raw:
            continue
        try:
            failure = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LearningError(
                "BENCHMARK_STATE_INVALID", "attempt failure evidence is invalid"
            ) from exc
        gate_rejections += int(
            isinstance(failure, dict) and bool(failure.get("check_id"))
        )

    critical = set(topology["critical_path"])
    critical_intervals = [
        (
            float(attempt["started_at"]),
            float(attempt["finished_at"] or run["updated_at"]),
        )
        for attempt in attempts
        if attempt["node_id"] in critical
    ]
    wall = max(0.0, (finished_at or float(run["updated_at"])) - created_at)
    projection = status_projection(state_path, run_id)
    metrics: dict[str, Any] = {
        "wall_seconds": round(wall, 3),
        "time_to_first_accepted_artifact_seconds": (
            round(max(0.0, first_artifact - created_at), 3)
            if first_artifact is not None
            else None
        ),
        "accepted_artifact_count": len(store.artifact_rows(run_id)),
        "rejected_attempt_count": rejected_attempts,
        "retry_count": retry_count,
        "repeated_failure_count": sum(
            int(row["repeated_failure"]) for row in progress.values()
        ),
        "deterministic_gate_rejection_count": gate_rejections,
        "useful_overlap_seconds": projection["useful_overlap_seconds"],
        "critical_path_utilization": (
            round(min(1.0, _union_seconds(critical_intervals) / wall), 6)
            if wall
            else None
        ),
        # These require an explicit human/independent-review record. Never infer them.
        "verifier_overturn_rate": None,
        "human_correction_count": None,
        "time_to_merged_deployed_live_proof_seconds": None,
    }
    state_snapshot = {
        "run": run,
        "nodes": store.node_rows(run_id),
        "attempts": attempts,
        "artifacts": list(store.artifact_rows(run_id)),
        "progress": progress,
    }
    evidence = {
        "state_snapshot_sha256": _sha(state_snapshot),
        "lifecycle_head": events[-1].digest if events else None,
        "workflow_sha256": _sha(run["workflow"]),
        "terminal_event": terminal.event_type if terminal is not None else None,
    }
    body = {
        "version": BENCHMARK_VERSION,
        "run_id": run_id,
        "workflow_id": run["workflow_id"],
        "status": run["status"],
        "critical_path": topology["critical_path"],
        "metrics": metrics,
        "evidence": evidence,
    }
    return {**body, "digest": _sha(body)}


def load_baseline(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LearningError("BASELINE_READ", "cannot read baseline") from exc
    if len(raw) > _MAX_INPUT_BYTES:
        raise LearningError("BASELINE_TOO_LARGE", "baseline exceeds 64 KiB")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise LearningError("BASELINE_SCHEMA", "baseline is not bounded JSON") from exc
    if not isinstance(body, dict) or set(body) != {
        "version",
        "id",
        "metrics",
        "evidence",
    }:
        raise LearningError("BASELINE_SCHEMA", "baseline has unexpected fields")
    if body["version"] != BASELINE_VERSION or not _ID.fullmatch(str(body["id"])):
        raise LearningError("BASELINE_SCHEMA", "baseline identity is invalid")
    if not isinstance(body["metrics"], dict) or not isinstance(body["evidence"], dict):
        raise LearningError(
            "BASELINE_SCHEMA", "baseline metrics/evidence must be objects"
        )
    for key, value in body["metrics"].items():
        if not isinstance(key, str) or len(key) > 128:
            raise LearningError("BASELINE_SCHEMA", "baseline metric name is invalid")
        if value is not None:
            _finite_number(value, f"metrics.{key}")
    if not body["evidence"]:
        raise LearningError("BASELINE_SCHEMA", "baseline requires evidence")
    return body


def compare_benchmark(
    report: Mapping[str, Any], baseline: Mapping[str, Any]
) -> dict[str, Any]:
    comparison: dict[str, Any] = {}
    for key in sorted(set(report["metrics"]) & set(baseline["metrics"])):
        graph_value = report["metrics"][key]
        baseline_value = baseline["metrics"][key]
        if graph_value is None or baseline_value is None:
            continue
        base = _finite_number(baseline_value, f"baseline.metrics.{key}")
        graph = _finite_number(graph_value, f"report.metrics.{key}")
        comparison[key] = {
            "graph": graph,
            "baseline": base,
            "delta": round(graph - base, 6),
            "percent_change": round((graph - base) / abs(base) * 100, 3)
            if base
            else None,
        }
    return {
        "version": "graph-engineering/benchmark-comparison/v1",
        "run_digest": report["digest"],
        "baseline_id": baseline["id"],
        "metrics": comparison,
    }


def _feedback_text(value: Any, path: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode()) > _MAX_TEXT
    ):
        raise LearningError(
            "FEEDBACK_SCHEMA", f"{path} must be non-empty and <= 2000 bytes"
        )
    if _SENSITIVE.search(value):
        raise LearningError(
            "FEEDBACK_SECRET", f"{path} contains credential-shaped data"
        )
    return value.strip()


def compile_feedback(path: Path) -> dict[str, Any]:
    """Compile feedback into reviewed enforcement proposals without applying it."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LearningError("FEEDBACK_READ", "cannot read feedback") from exc
    if len(raw) > _MAX_INPUT_BYTES:
        raise LearningError("FEEDBACK_TOO_LARGE", "feedback exceeds 64 KiB")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise LearningError("FEEDBACK_SCHEMA", "feedback is not bounded JSON") from exc
    required = {"version", "id", "submitted_by", "summary", "run_id", "items"}
    if not isinstance(body, dict) or set(body) != required:
        raise LearningError("FEEDBACK_SCHEMA", "feedback has unexpected fields")
    if body["version"] != FEEDBACK_VERSION or not _ID.fullmatch(str(body["id"])):
        raise LearningError("FEEDBACK_SCHEMA", "feedback identity is invalid")
    submitted_by = _feedback_text(body["submitted_by"], "submitted_by")
    summary = _feedback_text(body["summary"], "summary")
    if body["run_id"] is not None and not _ID.fullmatch(str(body["run_id"])):
        raise LearningError("FEEDBACK_SCHEMA", "run_id is invalid")
    items = body["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= _MAX_ITEMS:
        raise LearningError("FEEDBACK_SCHEMA", "items must contain 1..100 entries")
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        expected = {"id", "source", "observation", "evidence", "target", "verify_cmd"}
        if not isinstance(item, dict) or set(item) != expected:
            raise LearningError(
                "FEEDBACK_SCHEMA", f"items[{index}] has unexpected fields"
            )
        item_id = str(item["id"])
        if not _ID.fullmatch(item_id) or item_id in seen:
            raise LearningError(
                "FEEDBACK_SCHEMA", f"items[{index}].id is invalid or duplicate"
            )
        seen.add(item_id)
        source = str(item["source"])
        target = str(item["target"])
        if source not in _SOURCES or target not in _TARGETS:
            raise LearningError(
                "FEEDBACK_SCHEMA", f"items[{index}] source/target is invalid"
            )
        observation = _feedback_text(item["observation"], f"items[{index}].observation")
        evidence = item["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 32:
            raise LearningError(
                "FEEDBACK_SCHEMA", f"items[{index}].evidence is required"
            )
        evidence_values = [
            _feedback_text(value, f"items[{index}].evidence") for value in evidence
        ]
        verify_cmd = item["verify_cmd"]
        if verify_cmd is not None and (
            not isinstance(verify_cmd, list)
            or not 1 <= len(verify_cmd) <= 32
            or any(
                not isinstance(part, str) or not part or len(part) > 512
                for part in verify_cmd
            )
        ):
            raise LearningError(
                "FEEDBACK_SCHEMA", f"items[{index}].verify_cmd is invalid"
            )
        if verify_cmd is not None:
            verify_cmd = [
                _feedback_text(part, f"items[{index}].verify_cmd")
                for part in verify_cmd
            ]
        if target == "regression_test" and verify_cmd is None:
            raise LearningError(
                "FEEDBACK_SCHEMA", "regression-test feedback requires verify_cmd"
            )
        actions.append(
            {
                "id": item_id,
                "source": source,
                "observation": observation,
                "evidence": evidence_values,
                "target": target,
                "verify_cmd": verify_cmd,
                "sabotage_required": target == "regression_test",
                "invalidates_product_generation": target == "project_decision",
                "requires_workflow_validation": target == "workflow",
                "local_skill_proposal_only": target == "skill",
                "auto_apply": False,
                "required_review": "named_human",
            }
        )
    source = {
        "version": FEEDBACK_VERSION,
        "id": body["id"],
        "submitted_by": submitted_by,
        "summary": summary,
        "run_id": body["run_id"],
        "items": items,
    }
    proposal = {
        "version": LEARNING_PROPOSAL_VERSION,
        "source_id": body["id"],
        "source_digest": _sha(source),
        "run_id": body["run_id"],
        "summary": summary,
        "actions": actions,
        "policy": {
            "auto_apply": False,
            "auto_share_skills": False,
            "tests_are_authoritative": True,
            "named_human_review_required": True,
        },
    }
    return {**proposal, "digest": _sha(proposal)}


__all__ = [
    "BASELINE_VERSION",
    "BENCHMARK_VERSION",
    "FEEDBACK_VERSION",
    "LEARNING_PROPOSAL_VERSION",
    "LearningError",
    "benchmark_run",
    "compare_benchmark",
    "compile_feedback",
    "load_baseline",
]
