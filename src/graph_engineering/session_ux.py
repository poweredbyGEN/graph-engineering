"""Portable handoff, bounded status projection, and adoption assessment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .artifacts import canonical_json
from .lifecycle import LifecycleStore
from .project import (
    ASSESSMENT_VERSION,
    planning_capsule_status,
    repository_digest,
    validate_assessment,
)
from .state import StateStore
from .supervision import analyze_topology

HANDOFF_VERSION = "graph-engineering/handoff/v1"
STATUS_PROJECTION_VERSION = "graph-engineering/status-projection/v1"
MAX_LANES = 100
MAX_FAILURES = 100
MAX_TEXT = 512
MAX_SOURCE_FILES = 20_000
MAX_SOURCE_FILE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_TOTAL_BYTES = 64 * 1024 * 1024

_SECRET = re.compile(
    r"(?i)(bearer\s+[a-z0-9._~+/=-]{8,}|(?:token|password|secret|api[_-]?key)\s*[:=]\s*\S+)"
)
_TERMINAL = frozenset(
    {"succeeded", "failed", "optional_failed", "blocked", "cancelled", "uncertain"}
)


class SessionUxError(RuntimeError):
    """Stable error for handoff and projection boundaries."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _sha(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _safe_text(value: Any) -> str | None:
    if value is None:
        return None
    clean = _SECRET.sub("[REDACTED]", str(value).replace("\n", " "))
    encoded = clean.encode("utf-8")
    if len(encoded) <= MAX_TEXT:
        return clean
    return encoded[: MAX_TEXT - 16].decode("utf-8", errors="ignore") + "...[TRUNCATED]"


def _source_identity(repo: Path) -> dict[str, str]:
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    head_sha = head.stdout.strip() if head.returncode == 0 else ""
    tracked_result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    untracked_result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", "-z"],
        check=False,
        capture_output=True,
        timeout=10,
    )
    tracked = {
        item.decode("utf-8", errors="surrogateescape")
        for item in tracked_result.stdout.split(b"\0")
        if item
    }
    untracked = {
        item.decode("utf-8", errors="surrogateescape")
        for item in untracked_result.stdout.split(b"\0")
        if item
    }
    paths = sorted(tracked | untracked)
    if len(paths) > MAX_SOURCE_FILES:
        raise SessionUxError(
            "ASSESSMENT_SOURCE_LIMIT", "repository exceeds 20000 source files"
        )
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for relative in paths:
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise SessionUxError(
                "ASSESSMENT_SOURCE_ESCAPE",
                f"source path {relative!r} escapes repository",
            )
        path = repo / relative
        if path.is_symlink():
            # Git stores a symlink (mode 120000) as a blob holding the target
            # path text; snapshot exactly that and never follow the link.
            # Following would either escape the repository (absolute/external
            # targets abort assess) or double-count in-repo content.
            payload = os.readlink(path).encode("utf-8", errors="surrogateescape")
            total_bytes += len(payload)
            records.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "tracked": relative in tracked,
                }
            )
            continue
        try:
            if not path.resolve().is_relative_to(repo.resolve()):
                raise SessionUxError(
                    "ASSESSMENT_SOURCE_ESCAPE",
                    f"source path {relative!r} escapes repository",
                )
        except OSError as exc:
            raise SessionUxError(
                "ASSESSMENT_SOURCE_READ", f"cannot resolve source path {relative!r}"
            ) from exc
        if path.is_dir():
            # Gitlink (submodule) entries appear in `git ls-files` as tracked
            # paths but are directories on disk; their content belongs to the
            # sub-repository, not this snapshot. Reading one as a file raises
            # IsADirectoryError and aborted assess on any repo with submodules.
            continue
        try:
            size = path.stat().st_size
            if size > MAX_SOURCE_FILE_BYTES:
                raise SessionUxError(
                    "ASSESSMENT_SOURCE_LIMIT", f"source path {relative!r} exceeds 5 MiB"
                )
            if total_bytes + size > MAX_SOURCE_TOTAL_BYTES:
                raise SessionUxError(
                    "ASSESSMENT_SOURCE_LIMIT",
                    "repository source snapshot exceeds 64 MiB",
                )
            payload = path.read_bytes()
        except SessionUxError:
            raise
        except OSError:
            raise SessionUxError(
                "ASSESSMENT_SOURCE_READ", f"cannot read source path {relative!r}"
            ) from None
        if len(payload) > MAX_SOURCE_FILE_BYTES:
            raise SessionUxError(
                "ASSESSMENT_SOURCE_LIMIT", f"source path {relative!r} exceeds 5 MiB"
            )
        total_bytes += len(payload)
        if total_bytes > MAX_SOURCE_TOTAL_BYTES:
            raise SessionUxError(
                "ASSESSMENT_SOURCE_LIMIT", "repository source snapshot exceeds 64 MiB"
            )
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "tracked": relative in tracked,
            }
        )
    return {
        "head_sha": head_sha,
        "source_digest": _sha({"head_sha": head_sha, "records": records}),
    }


def envelope(version: str, body: Mapping[str, Any]) -> dict[str, Any]:
    bounded = dict(body)
    return {"version": version, "body": bounded, "digest": _sha(bounded)}


def decode_envelope(value: Any, version: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "body", "digest"}:
        raise SessionUxError("HANDOFF_INVALID", "document is not a strict envelope")
    if value["version"] != version or not isinstance(value["body"], dict):
        raise SessionUxError("HANDOFF_VERSION", f"expected {version}")
    if value["digest"] != _sha(value["body"]):
        raise SessionUxError("HANDOFF_TAMPERED", "envelope digest does not match")
    return dict(value["body"])


def _runtime_manifest(state_path: Path, run_id: str) -> dict[str, str]:
    try:
        with sqlite3.connect(state_path) as connection:
            row = connection.execute(
                "SELECT base_sha,profile_manifest_sha256,profile_manifest_json,"
                "project_policy_sha256,private_execution_sha256,repository_sha256 "
                "FROM runtime_manifests WHERE run_id=?",
                (run_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise SessionUxError(
            "HANDOFF_STATE_INVALID", "runtime manifest is unreadable"
        ) from exc
    if row is None:
        raise SessionUxError("HANDOFF_STATE_INVALID", "runtime manifest is missing")
    base_sha, profile_digest, raw_profiles = map(str, row[:3])
    try:
        profiles = json.loads(raw_profiles)
    except json.JSONDecodeError as exc:
        raise SessionUxError(
            "HANDOFF_STATE_INVALID", "profile manifest is invalid"
        ) from exc
    if _sha(profiles) != profile_digest:
        raise SessionUxError(
            "HANDOFF_STATE_INVALID", "profile manifest digest is invalid"
        )
    policy_digest, private_digest, repository_digest = row[3:]
    if any(
        not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
        for item in (policy_digest, private_digest, repository_digest)
    ):
        raise SessionUxError(
            "HANDOFF_STATE_INVALID", "project execution identity is missing or invalid"
        )
    return {
        "base_sha": base_sha,
        "profile_manifest_sha256": profile_digest,
        "project_policy_sha256": str(policy_digest),
        "private_execution_sha256": str(private_digest),
        "repository_sha256": str(repository_digest),
    }


def _receipt_digest(state_path: Path, run_id: str) -> str | None:
    try:
        with sqlite3.connect(state_path) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_ledgers'"
            ).fetchone()
            row = (
                connection.execute(
                    "SELECT digest FROM receipt_ledgers WHERE run_id=?", (run_id,)
                ).fetchone()
                if table is not None
                else None
            )
    except sqlite3.Error as exc:
        raise SessionUxError(
            "HANDOFF_STATE_INVALID", "receipt binding is unreadable"
        ) from exc
    if row is None:
        return None
    path = (
        state_path.parent
        / "artifacts"
        / "receipts"
        / f"{hashlib.sha256(run_id.encode()).hexdigest()}.json"
    )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise SessionUxError(
            "HANDOFF_RECEIPTS_MISSING", "bound receipt ledger is missing"
        ) from exc
    actual = hashlib.sha256(payload).hexdigest()
    if actual != row[0]:
        raise SessionUxError(
            "HANDOFF_RECEIPTS_TAMPERED", "receipt binding does not match"
        )
    return actual


def handoff_body(state_path: Path, run_id: str) -> dict[str, Any]:
    """Build a credential-free snapshot bound to every durable resume identity."""

    store = StateStore(state_path)
    run = store.run(run_id)
    workflow = run["workflow"]
    manifest = _runtime_manifest(state_path, run_id)
    context, events = LifecycleStore(state_path).snapshot(run_id)
    nodes = store.node_rows(run_id)
    attempts = list(store.attempt_rows(run_id))
    artifacts = list(store.artifact_rows(run_id))
    for artifact in artifacts:
        digest = str(artifact["digest"])
        path = state_path.parent / "artifacts" / digest[:2] / f"{digest}.json"
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise SessionUxError(
                "HANDOFF_ARTIFACT_MISSING", "an accepted artifact is missing"
            ) from exc
        if hashlib.sha256(payload).hexdigest() != digest:
            raise SessionUxError(
                "HANDOFF_ARTIFACT_TAMPERED",
                "an accepted artifact digest does not match",
            )
    workflow_digest = _sha(workflow)
    project_contract = {
        "workflow_sha256": workflow_digest,
        "base_sha": manifest["base_sha"],
        "profile_manifest_sha256": manifest["profile_manifest_sha256"],
        "project_policy_sha256": manifest["project_policy_sha256"],
        "private_execution_sha256": manifest["private_execution_sha256"],
        "repository_sha256": manifest["repository_sha256"],
    }
    failures = [
        {
            "node_id": node_id,
            "status": row["status"],
            "error": _safe_text(row.get("error")),
        }
        for node_id, row in sorted(nodes.items())
        if row["status"]
        in {"failed", "optional_failed", "blocked", "cancelled", "uncertain"}
    ][:MAX_FAILURES]
    prohibited: list[dict[str, str]] = []
    workflow_nodes = {node["id"]: node for node in workflow["nodes"]}
    for item in failures:
        node = workflow_nodes[item["node_id"]]
        effect = node.get("effect")
        replay_safe = effect in {"none", "read"} or (
            effect == "idempotent_write" and bool(node.get("idempotency_key"))
        )
        if item["status"] == "uncertain" or not replay_safe:
            prohibited.append(
                {
                    "node_id": item["node_id"],
                    "route": "reconciliation_required",
                    "reason": "interrupted or non-replay-safe effect",
                }
            )
    run_material = {
        "status": run["status"],
        "cancel_requested": bool(run["cancel_requested"]),
        "created_at": run["created_at"],
        "nodes": [nodes[key] for key in sorted(nodes)],
        "attempts": attempts,
    }
    return {
        "run_id": run_id,
        "workflow_id": run["workflow_id"],
        "workflow_sha256": workflow_digest,
        "project_contract_sha256": _sha(project_contract),
        **manifest,
        "run_sha256": _sha(run_material),
        "lifecycle": {
            "context_sha256": context.digest,
            "event_count": len(events),
            "head_sha256": events[-1].digest if events else None,
        },
        "artifacts_sha256": _sha(artifacts),
        "receipts_sha256": _receipt_digest(state_path, run_id),
        "status": run["status"],
        "completed_nodes": sorted(
            node_id for node_id, row in nodes.items() if row["status"] in _TERMINAL
        ),
        "remaining_nodes": sorted(
            node_id for node_id, row in nodes.items() if row["status"] not in _TERMINAL
        ),
        "failures": failures,
        "prohibited_retry_paths": prohibited,
    }


def export_handoff(state_path: Path, run_id: str) -> dict[str, Any]:
    return envelope(HANDOFF_VERSION, handoff_body(state_path, run_id))


def verify_handoff(
    document: Mapping[str, Any],
    state_path: Path,
    run_id: str,
    workflow: Mapping[str, Any],
) -> dict[str, Any]:
    body = decode_envelope(dict(document), HANDOFF_VERSION)
    if body.get("run_id") != run_id:
        raise SessionUxError("HANDOFF_RUN_MISMATCH", "handoff names another run")
    if body.get("workflow_sha256") != _sha(workflow):
        raise SessionUxError("HANDOFF_CONTRACT_DRIFT", "workflow contract changed")
    current = handoff_body(state_path, run_id)
    if body != current:
        changed = sorted(
            key for key in set(body) | set(current) if body.get(key) != current.get(key)
        )
        raise SessionUxError(
            "HANDOFF_STATE_DRIFT", f"durable handoff bindings changed: {changed[:12]}"
        )
    return body


def status_projection(state_path: Path, run_id: str) -> dict[str, Any]:
    """Return a bounded, redacted JSON projection suitable for Herdr ingestion."""

    store = StateStore(state_path)
    run = store.run(run_id)
    workflow = run["workflow"]
    nodes = store.node_rows(run_id)
    attempts = list(store.attempt_rows(run_id))
    artifacts = list(store.artifact_rows(run_id))
    by_attempt: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        by_attempt.setdefault(str(attempt["node_id"]), []).append(attempt)
    by_artifact: dict[str, list[str]] = {}
    for artifact in artifacts:
        by_artifact.setdefault(str(artifact["node_id"]), []).append(
            str(artifact["digest"])
        )
    workflow_nodes = {node["id"]: node for node in workflow["nodes"]}
    topology = analyze_topology(workflow)
    children = {
        node_id: sorted(
            node["id"] for node in workflow["nodes"] if node_id in node.get("needs", [])
        )
        for node_id in workflow_nodes
    }
    lanes: list[dict[str, Any]] = []
    for node_id in sorted(workflow_nodes)[:MAX_LANES]:
        node = workflow_nodes[node_id]
        row = nodes[node_id]
        history = by_attempt.get(node_id, [])
        latest = history[-1] if history else None
        started = float(latest["started_at"]) if latest else None
        timeout = node.get("timeout_seconds", workflow["budgets"]["timeout_seconds"])
        dependencies = [
            dep for dep in node.get("needs", []) if nodes[dep]["status"] != "succeeded"
        ]
        if row["status"] == "uncertain":
            route = "reconcile"
        elif row["status"] in {"failed", "optional_failed"}:
            routes = [route["id"] for route in node.get("repair", {}).get("routes", [])]
            route = f"repair:{routes[0]}" if routes else "stop_and_review"
        elif row["status"] == "pending" and dependencies:
            route = "wait:" + ",".join(sorted(dependencies)[:8])
        elif row["status"] == "pending":
            route = "ready"
        elif row["status"] == "running":
            route = "collect_and_gate"
        elif row["status"] == "succeeded" and children[node_id]:
            route = "unlock:" + ",".join(children[node_id][:8])
        else:
            route = "terminal"
        digests = sorted(by_artifact.get(node_id, []))
        lanes.append(
            {
                "node_id": node_id,
                "status": row["status"],
                "attempt": row["attempt_count"],
                "started_at": started,
                "deadline_at": started + float(timeout)
                if started is not None
                else None,
                "artifact_delta": {"count": len(digests), "digests": digests[:16]},
                "blocker": _safe_text(row.get("error")),
                "next_route": route,
            }
        )
    intervals = sorted(
        (
            float(item["started_at"]),
            float(item["finished_at"] or time.time()),
        )
        for item in attempts
        if item.get("started_at") is not None
    )
    total = sum(max(0.0, end - start) for start, end in intervals)
    union = 0.0
    if intervals:
        begin, end = intervals[0]
        for next_begin, next_end in intervals[1:]:
            if next_begin > end:
                union += max(0.0, end - begin)
                begin, end = next_begin, next_end
            else:
                end = max(end, next_end)
        union += max(0.0, end - begin)
    return {
        "version": STATUS_PROJECTION_VERSION,
        "run_id": run_id,
        "status": run["status"],
        "critical_path": topology["critical_path"],
        "terminal_slice": topology["terminal_slice"],
        "ancillary_nodes": topology["ancillary_nodes"],
        "useful_overlap_seconds": round(max(0.0, total - union), 3),
        "lane_count": len(workflow_nodes),
        "lanes_omitted": max(0, len(workflow_nodes) - len(lanes)),
        "lanes": lanes,
    }


def assess_repo(repo: Path, private_config: Path | None = None) -> dict[str, Any]:
    """Read-only adoption audit without a synthetic maturity score."""

    workflow_dir = repo / ".graph-engineering" / "workflows"
    workflows = (
        sorted(workflow_dir.glob("*.json"))[:100] if workflow_dir.is_dir() else []
    )
    valid_workflows = 0
    workflow_values: list[dict[str, Any]] = []
    from .contracts import validate_workflow  # avoid import cost for other commands

    for path in workflows:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                validate_workflow(value)
                workflow_values.append(value)
                valid_workflows += 1
        except (OSError, ValueError):
            continue
    relevant = sorted(
        path
        for path in repo.iterdir()
        if path.name
        in {
            "pyproject.toml",
            "package.json",
            "Cargo.toml",
            "go.mod",
            "Makefile",
            "justfile",
            ".woodpecker.yml",
        }
    )
    test_dirs = [
        path for name in ("tests", "test", "spec") if (path := repo / name).is_dir()
    ]
    evidence_files = [
        path
        for path in relevant
        if path.name
        in {"pyproject.toml", "package.json", "Makefile", "justfile", ".woodpecker.yml"}
    ]
    runner_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")[:1_000_000]
        for path in evidence_files
    ).lower()
    runner_proofs = {
        "tests": bool(test_dirs)
        and any(term in runner_text for term in ("pytest", " test", "test:")),
        "lint": any(
            term in runner_text for term in ("ruff", "eslint", "clippy", "golangci")
        ),
        "types": any(
            term in runner_text for term in ("mypy", "pyright", "typecheck", "tsc")
        ),
        "build": any(
            term in runner_text
            for term in ("build-system", '"build"', "cargo build", "go build")
        ),
    }
    config_ready = False
    profile_count = 0
    if private_config is not None and private_config.exists():
        try:
            config_ready = stat.S_IMODE(private_config.stat().st_mode) & 0o077 == 0
        except OSError:
            config_ready = False
    if config_ready:
        try:
            from .adapters import probe_profile
            from .config import load_agent_config

            config = load_agent_config(
                user_path=private_config,
                project_path=repo / ".graph-engineering.toml",
                project_local_path=repo / ".graph-engineering.local.toml",
            )
            profile_count = len(config.profiles)
            config_ready = profile_count > 0 and all(
                all(
                    probe.ok
                    for probe in probe_profile(profile, cwd=repo, allowed_root=repo)
                )
                for profile in config.profiles.values()
            )
        except (OSError, RuntimeError, ValueError):
            # Config and doctor errors collapse to bounded readiness only.
            config_ready = False
    all_nodes = [node for workflow in workflow_values for node in workflow["nodes"]]
    writers = [
        node
        for node in all_nodes
        if node["kind"] == "agent" and node["permission"] in {"write", "destructive"}
    ]
    isolation = bool(writers) and all(
        node["workspace"] == "worktree" for node in writers
    )
    integration = any(node["kind"] == "integration" for node in all_nodes)
    bounded = bool(all_nodes) and all(
        node.get("retry", {}).get("max_attempts", 1) >= 1
        and node.get("retry", {}).get("no_progress_limit", 1) >= 1
        and node.get("effect") is not None
        for node in writers
    )
    external = any(node["permission"] == "external" for node in all_nodes)
    a2a_named = any("a2a" in str(node.get("profile", "")).lower() for node in all_nodes)
    transport = (
        "a2a"
        if a2a_named
        else "mcp"
        if external
        else "subprocess"
        if all_nodes
        else "none"
    )
    capabilities = {
        "planning_capsule": planning_capsule_status(repo),
        "manifest_workflows": {"ready": valid_workflows > 0, "count": valid_workflows},
        "deterministic_gates": {
            "ready": all(runner_proofs.values()),
            "test_roots": [path.name for path in test_dirs[:8]],
            "runner_manifests": [path.name for path in evidence_files[:8]],
            "proofs": runner_proofs,
        },
        "private_profiles": {"ready": config_ready, "profile_count": profile_count},
        "isolation_integration": {"ready": isolation and integration},
        "bounded_effects": {"ready": bounded},
        "lifecycle_handoff": {"ready": valid_workflows > 0},
        "evidence_runners": {
            "ready": bool(evidence_files),
            "declarations": [path.name for path in evidence_files[:8]],
        },
        "transport_need": {"ready": True, "recommended": transport},
    }
    definitions = [
        (
            "planning-capsule",
            "critical",
            "planning_capsule",
            [
                ".graph-engineering/PROJECT.md",
                ".graph-engineering/product-contract.json",
            ],
            "Answer and human-approve the repository planning capsule before dependency fan-out.",
            "product-contract v2 is approved, complete, and bound to unchanged brief/decision digests",
            ["graph-engineer", "init", "--repo", ".", "--json"],
        ),
        (
            "workflow",
            "critical",
            "manifest_workflows",
            [".graph-engineering/workflows"],
            "Add and validate one reviewed JSON workflow under .graph-engineering/workflows/.",
            "graph-engineer validate accepts the selected workflow",
            ["graph-engineer", "validate", "<workflow.json>"],
        ),
        (
            "gates",
            "critical",
            "deterministic_gates",
            ["pyproject.toml", "package.json", "Makefile", "tests"],
            "Declare real test plus lint/type/build runners and use them as node checks.",
            "test, lint, type, and build commands each exit zero and fail under sabotage",
            None,
        ),
        (
            "profiles",
            "high",
            "private_profiles",
            ["~/.config/graph-engineering/config.toml"],
            "Create a mode-0600 private worker config and run graph-engineer doctor.",
            "doctor reports every selected profile ready without printing values",
            ["graph-engineer", "doctor", "--repo", ".", "--json"],
        ),
        (
            "isolation",
            "high",
            "isolation_integration",
            [".graph-engineering/workflows"],
            "Use isolated worktrees for writers and one integration node for accepted changesets.",
            "workflow validation proves isolated writers and one integration owner",
            ["graph-engineer", "validate", "<workflow.json>"],
        ),
        (
            "effects",
            "high",
            "bounded_effects",
            [".graph-engineering/workflows"],
            "Classify writer effects and set finite retry and no-progress limits.",
            "every writer declares an effect plus finite retry/no-progress ceilings",
            ["graph-engineer", "validate", "<workflow.json>"],
        ),
        (
            "handoff",
            "medium",
            "lifecycle_handoff",
            [".graph-engineering/workflows"],
            "Run through the portable runtime so lifecycle, resume, and handoff evidence are durable.",
            "a run exports a verified handoff and rejects a tampered copy",
            [
                "graph-engineer",
                "handoff",
                "--state",
                "<state.db>",
                "--run-id",
                "<run-id>",
                "--json",
            ],
        ),
        (
            "evidence",
            "medium",
            "evidence_runners",
            ["pyproject.toml", "package.json", "Makefile", ".woodpecker.yml"],
            "Declare the repository's repeatable evidence commands in a standard manifest or CI file.",
            "a clean checkout exposes at least one deterministic evidence runner",
            None,
        ),
    ]
    gaps = [
        {
            "id": identifier,
            "priority": priority,
            "area": area,
            "evidence": "not proven by bounded repository inspection",
            "fix_sites": fix_sites,
            "remediation": remediation,
            "acceptance": acceptance,
            "verify_cmd": verify_cmd,
        }
        for (
            identifier,
            priority,
            area,
            fix_sites,
            remediation,
            acceptance,
            verify_cmd,
        ) in definitions
        if not capabilities[area]["ready"]
    ]
    priorities = {
        name: sum(gap["priority"] == name for gap in gaps)
        for name in ("critical", "high", "medium")
    }
    assessment = {
        "version": ASSESSMENT_VERSION,
        "repo_digest": repository_digest(repo),
        "source": _source_identity(repo),
        "summary": {"ready": not gaps, **priorities},
        "capabilities": capabilities,
        "gaps": gaps,
        "recommended_init": {
            "workflow_templates": (
                ["mobile-automation-vertical-slice", "contract-matrix-class-prove"]
                if not workflows
                else []
            ),
            "require_private_config": not config_ready,
            "transport": transport,
        },
    }
    return dict(validate_assessment(assessment, repo))


__all__ = [
    "ASSESSMENT_VERSION",
    "HANDOFF_VERSION",
    "STATUS_PROJECTION_VERSION",
    "SessionUxError",
    "assess_repo",
    "decode_envelope",
    "envelope",
    "export_handoff",
    "status_projection",
    "verify_handoff",
]
