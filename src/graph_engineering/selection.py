"""Task-specific execution selection without repository adoption ceremony.

The selector is deliberately conservative: a graph is an optimization that must be
supported by task evidence.  Missing estimates are reasons to stay linear, never
values to invent.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .artifacts import canonical_json

SELECTION_VERSION = "graph-engineering/selection/v1"
ExecutionMode = Literal["LINEAR", "TRANSIENT_GRAPH", "DURABLE_GRAPH"]


class SelectionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SelectionError("BRIEF_INVALID", f"{field} must be a number")
    number = float(value)
    if number < 0 or (positive and number <= 0):
        raise SelectionError("BRIEF_INVALID", f"{field} is outside the supported range")
    return number


@dataclass(frozen=True)
class TaskBrief:
    task: str
    independent_lanes: int = 1
    estimated_linear_seconds: float | None = None
    estimated_graph_seconds: float | None = None
    estimated_linear_cost_usd: float | None = None
    estimated_graph_cost_usd: float | None = None
    repetitions: int = 1
    high_value: bool = False
    long_running: bool = False
    resumable: bool = False
    effectful: bool = False
    acceptance_suite: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TaskBrief:
        allowed = {
            "task",
            "independent_lanes",
            "estimated_linear_seconds",
            "estimated_graph_seconds",
            "estimated_linear_cost_usd",
            "estimated_graph_cost_usd",
            "repetitions",
            "high_value",
            "long_running",
            "resumable",
            "effectful",
            "acceptance_suite",
        }
        extras = set(value) - allowed
        if extras:
            raise SelectionError("BRIEF_INVALID", "brief has unexpected fields")
        task = value.get("task")
        if not isinstance(task, str) or not task.strip() or len(task.encode()) > 8_192:
            raise SelectionError(
                "BRIEF_INVALID", "task must be non-empty and <= 8192 bytes"
            )
        lanes = value.get("independent_lanes", 1)
        repetitions = value.get("repetitions", 1)
        if (
            isinstance(lanes, bool)
            or not isinstance(lanes, int)
            or not 1 <= lanes <= 64
        ):
            raise SelectionError("BRIEF_INVALID", "independent_lanes must be 1..64")
        if (
            isinstance(repetitions, bool)
            or not isinstance(repetitions, int)
            or not 1 <= repetitions <= 1_000_000
        ):
            raise SelectionError("BRIEF_INVALID", "repetitions must be 1..1000000")
        flags: dict[str, bool] = {}
        for field in ("high_value", "long_running", "resumable", "effectful"):
            raw = value.get(field, False)
            if not isinstance(raw, bool):
                raise SelectionError("BRIEF_INVALID", f"{field} must be boolean")
            flags[field] = raw
        estimates: dict[str, float | None] = {}
        for field in (
            "estimated_linear_seconds",
            "estimated_graph_seconds",
            "estimated_linear_cost_usd",
            "estimated_graph_cost_usd",
        ):
            raw = value.get(field)
            estimates[field] = (
                None
                if raw is None
                else _number(raw, field, positive=field.endswith("seconds"))
            )
        suite = value.get("acceptance_suite")
        if suite is not None and (
            not isinstance(suite, str) or not suite.strip() or len(suite) > 512
        ):
            raise SelectionError("BRIEF_INVALID", "acceptance_suite is invalid")
        return cls(
            task=task.strip(),
            independent_lanes=lanes,
            repetitions=repetitions,
            acceptance_suite=suite.strip() if suite else None,
            **flags,
            **estimates,
        )


def choose_execution(
    brief: TaskBrief,
    *,
    promotion: Mapping[str, Any] | None = None,
    graphify: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic mode and the exact evidence supporting it."""

    reasons: list[dict[str, Any]] = []
    frontier = brief.independent_lanes >= 2
    reasons.append(
        {
            "criterion": "parallel_frontier",
            "met": frontier,
            "evidence": {"independent_lanes": brief.independent_lanes},
        }
    )
    if graphify is not None:
        reasons.append(
            {
                "criterion": "graphify_dependency_evidence",
                "met": bool(graphify.get("trusted_for_selection")),
                "evidence": dict(graphify),
            }
        )
    estimates_known = (
        brief.estimated_linear_seconds is not None
        and brief.estimated_graph_seconds is not None
    )
    speedup = (
        1.0 - brief.estimated_graph_seconds / brief.estimated_linear_seconds
        if estimates_known and brief.estimated_linear_seconds
        else None
    )
    forecasted_benefit = speedup is not None and speedup >= 0.10
    reasons.append(
        {
            "criterion": "forecasted_wall_time_benefit",
            "met": forecasted_benefit,
            "evidence": {
                "estimated_linear_seconds": brief.estimated_linear_seconds,
                "estimated_graph_seconds": brief.estimated_graph_seconds,
                "forecasted_speedup_percent": (
                    round(speedup * 100, 3) if speedup is not None else None
                ),
                "minimum_speedup_percent": 10.0,
            },
        }
    )
    cost_reported = (
        brief.estimated_linear_cost_usd is not None
        and brief.estimated_graph_cost_usd is not None
    )
    reasons.append(
        {
            "criterion": "forecast_cost_reported",
            "met": cost_reported,
            "evidence": {
                "estimated_linear_cost_usd": brief.estimated_linear_cost_usd,
                "estimated_graph_cost_usd": brief.estimated_graph_cost_usd,
            },
        }
    )
    graph_earned = frontier and forecasted_benefit and cost_reported
    inherent_durability = brief.long_running or brief.resumable or brief.effectful
    reviewed_promotion = bool(
        promotion
        and promotion.get("eligible") is True
        and promotion.get("reviewed") is True
        and isinstance(promotion.get("reviewed_by"), str)
        and bool(promotion.get("reviewed_by"))
    )
    durable = graph_earned and (inherent_durability or reviewed_promotion)
    mode: ExecutionMode = (
        "DURABLE_GRAPH" if durable else "TRANSIENT_GRAPH" if graph_earned else "LINEAR"
    )
    reasons.append(
        {
            "criterion": "durability_need",
            "met": inherent_durability or reviewed_promotion,
            "evidence": {
                "repetitions": brief.repetitions,
                "high_value": brief.high_value,
                "long_running": brief.long_running,
                "resumable": brief.resumable,
                "effectful": brief.effectful,
                "reviewed_promotion": reviewed_promotion,
            },
        }
    )
    candidate = graph_earned and (brief.repetitions >= 3 or brief.high_value)
    body = {
        "version": SELECTION_VERSION,
        "mode": mode,
        "task_digest": hashlib.sha256(brief.task.encode()).hexdigest(),
        "reasons": reasons,
        "graph_earned": graph_earned,
        "durable_promotion_candidate": candidate and not durable,
        "acceptance_suite": brief.acceptance_suite,
        "next_step": {
            "LINEAR": "run one evidence loop; do not initialize graph infrastructure",
            "TRANSIENT_GRAPH": "fan out only the independent lanes; keep one integration owner",
            "DURABLE_GRAPH": "run the reviewed durable workflow with checkpoints and receipts",
        }[mode],
    }
    return {**body, "digest": hashlib.sha256(canonical_json(body)).hexdigest()}


def graphify_dependency_evidence(repo: Path, focus_paths: list[str]) -> dict[str, Any]:
    """Read bounded, fresh, tracked Graphify edges for explicitly named paths.

    Node count is intentionally absent: graph volume says nothing about whether a
    task has a parallel frontier.  An absent manifest entry, changed mtime, or
    untracked source path excludes that path and every edge touching it.
    """

    resolved = repo.expanduser().resolve(strict=True)
    graph_path = resolved / "graphify-out" / "graph.json"
    manifest_path = resolved / "graphify-out" / "manifest.json"
    requested = list(dict.fromkeys(focus_paths))
    if len(requested) > 32 or any(
        not path or Path(path).is_absolute() or ".." in Path(path).parts
        for path in requested
    ):
        raise SelectionError(
            "GRAPHIFY_INPUT_INVALID",
            "focus paths must be 1..32 repository-relative paths",
        )
    base = {
        "available": graph_path.is_file(),
        "requested_paths": requested,
        "fresh_tracked_paths": [],
        "ignored_paths": requested,
        "dependency_edges": [],
        "trusted_for_selection": False,
    }
    if not requested or not graph_path.is_file() or not manifest_path.is_file():
        return base
    if (
        graph_path.stat().st_size > 32 * 1024 * 1024
        or manifest_path.stat().st_size > 2 * 1024 * 1024
    ):
        raise SelectionError(
            "GRAPHIFY_TOO_LARGE", "Graphify evidence exceeds bounded input limits"
        )
    try:
        tracked_raw = subprocess.run(
            ["git", "ls-files", "-z", "--", *requested],
            cwd=resolved,
            check=True,
            capture_output=True,
            timeout=10,
        ).stdout
        tracked = set(tracked_raw.decode(errors="strict").rstrip("\0").split("\0"))
        manifest = json.loads(manifest_path.read_bytes())
        graph = json.loads(graph_path.read_bytes())
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.SubprocessError,
    ) as exc:
        raise SelectionError(
            "GRAPHIFY_READ", "cannot verify Graphify evidence"
        ) from exc
    if not isinstance(manifest, dict) or not isinstance(graph, dict):
        raise SelectionError("GRAPHIFY_INVALID", "Graphify evidence is malformed")
    fresh: set[str] = set()
    for path in requested:
        entry = manifest.get(path)
        source = resolved / path
        if (
            path in tracked
            and isinstance(entry, dict)
            and isinstance(entry.get("mtime"), (int, float))
            and source.is_file()
            and abs(source.stat().st_mtime - float(entry["mtime"])) < 0.001
        ):
            fresh.add(path)
    node_paths: dict[str, str] = {}
    nodes = graph.get("nodes")
    links = graph.get("links")
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise SelectionError(
            "GRAPHIFY_INVALID", "Graphify nodes and links must be lists"
        )
    for node in nodes[:100_000]:
        if (
            isinstance(node, dict)
            and node.get("source_file") in fresh
            and isinstance(node.get("id"), str)
        ):
            node_paths[node["id"]] = node["source_file"]
    edges: list[dict[str, str]] = []
    for link in links[:250_000]:
        if not isinstance(link, dict):
            continue
        source_path = node_paths.get(str(link.get("source")))
        target_path = node_paths.get(str(link.get("target")))
        if source_path and target_path and source_path != target_path:
            edges.append(
                {
                    "source_file": source_path,
                    "target_file": target_path,
                    "relation": str(link.get("relation") or "unknown")[:128],
                }
            )
            if len(edges) == 256:
                break
    return {
        "available": True,
        "requested_paths": requested,
        "fresh_tracked_paths": sorted(fresh),
        "ignored_paths": sorted(set(requested) - fresh),
        "dependency_edges": edges,
        "trusted_for_selection": len(fresh) == len(requested),
    }


def load_brief(path: str) -> TaskBrief:
    try:
        raw = Path(path).expanduser().read_bytes()
        if len(raw) > 64 * 1024:
            raise SelectionError("BRIEF_TOO_LARGE", "brief exceeds 64 KiB")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SelectionError(
            "BRIEF_READ", "cannot read bounded JSON task brief"
        ) from exc
    if not isinstance(value, dict):
        raise SelectionError("BRIEF_INVALID", "brief must be an object")
    return TaskBrief.from_mapping(value)


__all__ = [
    "SELECTION_VERSION",
    "SelectionError",
    "TaskBrief",
    "choose_execution",
    "graphify_dependency_evidence",
    "load_brief",
]
