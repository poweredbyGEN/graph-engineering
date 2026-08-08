"""Thin deterministic command line for the graph-engineering runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path
from typing import Any

from . import __version__
from .adapters import ExecutionReceipt, probe_profile
from .artifacts import canonical_json
from .config import DEFAULT_USER_CONFIG, ConfigError, load_agent_config
from .contracts import (
    ValidationIssue,
    WorkflowValidationError,
    validate_workflow,
)
from .orchestrator import (
    CheckCommandReceipt,
    OrchestrationError,
    PortableRuntime,
)
from .state import StateStore
from .worktrees import WorktreeError

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_MEMORY_FILESYSTEMS = frozenset({"ramfs", "tmpfs"})
_MOUNTINFO = Path("/proc/self/mountinfo")


class CliError(RuntimeError):
    """Stable operator-facing failure."""

    def __init__(self, code: str, message: str, *, path: str | None = None):
        self.code = code
        self.message = message
        self.path = path
        super().__init__(message)


def _load_json_workflow(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError("WORKFLOW_READ_ERROR", str(exc), path=str(source)) from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        issue = ValidationIssue(
            "PARSE_ERROR",
            f"$ (line {exc.lineno}, column {exc.colno})",
            exc.msg,
        )
        raise WorkflowValidationError([issue]) from exc
    if not isinstance(value, dict):
        raise WorkflowValidationError(
            [ValidationIssue("TYPE_ERROR", "$", "workflow document must be an object")]
        )
    validate_workflow(value)
    return value


def _topology(workflow: Mapping[str, Any]) -> dict[str, Any]:
    nodes = {node["id"]: node for node in workflow["nodes"]}
    dependents: dict[str, list[str]] = defaultdict(list)
    indegree = {node_id: len(node.get("needs", [])) for node_id, node in nodes.items()}
    for node_id, node in nodes.items():
        for dependency in node.get("needs", []):
            dependents[dependency].append(node_id)
    for children in dependents.values():
        children.sort()

    frontier = sorted(node_id for node_id, degree in indegree.items() if degree == 0)
    layers: list[list[str]] = []
    order: list[str] = []
    while frontier:
        layer = frontier
        layers.append(layer)
        order.extend(layer)
        next_frontier: list[str] = []
        for node_id in layer:
            for child in dependents[node_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_frontier.append(child)
        frontier = sorted(next_frontier)

    longest: dict[str, tuple[str, ...]] = {}
    for node_id in order:
        needs = sorted(nodes[node_id].get("needs", []))
        if not needs:
            longest[node_id] = (node_id,)
            continue
        candidates = (longest[dependency] + (node_id,) for dependency in needs)
        longest[node_id] = min(candidates, key=lambda path: (-len(path), path))
    critical_path = min(longest.values(), key=lambda path: (-len(path), path))

    edges = [
        {"from": dependency, "to": node_id}
        for node_id in sorted(nodes)
        for dependency in sorted(nodes[node_id].get("needs", []))
    ]
    critical_edges = [
        {"from": source, "to": target} for source, target in pairwise(critical_path)
    ]
    return {
        "workflow_id": workflow["id"],
        "goal": workflow["goal"],
        "node_count": len(nodes),
        "edge_count": len(edges),
        "max_concurrency": workflow["budgets"]["max_concurrency"],
        "ready_layers": layers,
        "critical_path": list(critical_path),
        "critical_dependencies": critical_edges,
        "dependencies": {
            node_id: sorted(nodes[node_id].get("needs", []))
            for node_id in sorted(nodes)
        },
        "unlocks": {node_id: dependents.get(node_id, []) for node_id in sorted(nodes)},
        "edges": edges,
    }


def _config(args: argparse.Namespace, repo: Path):
    user_path = Path(args.config).expanduser() if args.config else DEFAULT_USER_CONFIG
    return load_agent_config(
        user_path=user_path,
        project_path=repo / ".graph-engineering.toml",
        project_local_path=repo / ".graph-engineering.local.toml",
    )


def _private_config_checks(
    args: argparse.Namespace, repo: Path
) -> list[dict[str, Any]]:
    paths = [
        Path(args.config).expanduser() if args.config else DEFAULT_USER_CONFIG,
        repo / ".graph-engineering.local.toml",
    ]
    checks: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            if path == paths[0]:
                checks.append(
                    {
                        "name": "private-config",
                        "ok": False,
                        "detail": "file not found",
                    }
                )
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        checks.append(
            {
                "name": (
                    "private-config" if path == paths[0] else "project-private-config"
                ),
                "ok": mode & 0o077 == 0,
                "detail": f"mode {mode:04o}",
            }
        )
    return checks


def _scratch_storage_check() -> dict[str, Any]:
    configured = os.environ.get("TMPDIR")
    if not configured:
        return {
            "name": "scratch-filesystem",
            "ok": False,
            "detail": "TMPDIR is not configured",
        }
    try:
        scratch = Path(configured).expanduser().resolve(strict=True)
        lines = _MOUNTINFO.read_text(encoding="utf-8").splitlines()
        candidates: list[tuple[int, str]] = []
        for line in lines:
            left, separator, right = line.partition(" - ")
            fields = left.split()
            filesystem = right.split()[0] if separator and right.split() else ""
            if len(fields) < 5 or not filesystem:
                continue
            mount_text = (
                fields[4]
                .replace("\\040", " ")
                .replace("\\011", "\t")
                .replace("\\012", "\n")
                .replace("\\134", "\\")
            )
            mount = Path(mount_text)
            if scratch == mount or scratch.is_relative_to(mount):
                candidates.append((len(mount.parts), filesystem))
        if not candidates:
            raise ValueError("mount not found")
        filesystem = max(candidates)[1]
    except (OSError, RuntimeError, ValueError):
        return {
            "name": "scratch-filesystem",
            "ok": False,
            "detail": "filesystem type could not be proven",
        }
    memory_backed = filesystem.lower() in _MEMORY_FILESYSTEMS
    return {
        "name": "scratch-filesystem",
        "ok": not memory_backed,
        "detail": (
            f"memory-backed {filesystem}"
            if memory_backed
            else f"filesystem {filesystem}"
        ),
    }


def _doctor(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve(strict=True)
    config = _config(args, repo)
    checks = [*_private_config_checks(args, repo), _scratch_storage_check()]
    if not config.profiles:
        checks.append(
            {"name": "profiles", "ok": False, "detail": "no profiles configured"}
        )
    selected = sorted(config.profiles)
    if args.profile:
        if args.profile not in config.profiles:
            raise CliError("UNKNOWN_PROFILE", f"unknown profile {args.profile!r}")
        selected = [args.profile]
    profiles: list[dict[str, Any]] = []
    for name in selected:
        profile = config.profiles[name]
        probes = probe_profile(profile, cwd=repo, allowed_root=repo)
        profiles.append(
            {
                "name": name,
                "adapter": profile.adapter_kind,
                "capabilities": sorted(profile.capabilities.enabled()),
                "ok": all(probe.ok for probe in probes),
                "checks": [asdict(probe) for probe in probes],
            }
        )
    ok = all(check["ok"] for check in checks) and all(
        profile["ok"] for profile in profiles
    )
    return {"ok": ok, "command": "doctor", "checks": checks, "profiles": profiles}


def _state_path(args: argparse.Namespace) -> Path:
    if args.state:
        return Path(args.state).expanduser().resolve()
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    ).expanduser()
    return (
        state_home / "graph-engineering" / "runs" / args.run_id / "state.db"
    ).resolve()


def _receipt_path(state_path: Path, run_id: str) -> Path:
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    return state_path.parent / "artifacts" / "receipts" / f"{digest}.json"


def _read_receipts(
    state_path: Path, run_id: str
) -> tuple[dict[str, ExecutionReceipt], tuple[CheckCommandReceipt, ...]]:
    path = _receipt_path(state_path, run_id)
    try:
        with sqlite3.connect(state_path) as connection:
            try:
                row = connection.execute(
                    "SELECT digest FROM receipt_ledgers WHERE run_id=?", (run_id,)
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
                row = None
    except sqlite3.Error as exc:
        raise CliError(
            "CORRUPT_RECEIPT_LEDGER",
            f"cannot read receipt binding for run {run_id!r}",
            path=str(state_path),
        ) from exc
    if not path.exists():
        if row is not None:
            raise CliError(
                "CORRUPT_RECEIPT_LEDGER",
                f"receipt file is missing for run {run_id!r}",
                path=str(path),
            )
        return {}, ()
    payload = path.read_bytes()
    try:
        if row is None or row[0] != hashlib.sha256(payload).hexdigest():
            raise ValueError("durable state binding mismatch")
        envelope = json.loads(payload)
        if not isinstance(envelope, dict) or set(envelope) != {"body", "digest"}:
            raise ValueError("invalid envelope")
        body = envelope["body"]
        if envelope["digest"] != hashlib.sha256(canonical_json(body)).hexdigest():
            raise ValueError("body digest mismatch")
        if body.get("version") != 1 or body.get("run_id") != run_id:
            raise ValueError("run identity mismatch")
        agents = {
            key: ExecutionReceipt(**value)
            for key, value in body["agent_receipts"].items()
        }
        checks = tuple(CheckCommandReceipt(**value) for value in body["check_receipts"])
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        sqlite3.Error,
    ) as exc:
        raise CliError(
            "CORRUPT_RECEIPT_LEDGER",
            f"cannot trust receipts for run {run_id!r}",
            path=str(path),
        ) from exc
    return agents, checks


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if not _RUN_ID.fullmatch(args.run_id):
        raise CliError("INVALID_RUN_ID", "run id is not portable")
    workflow = _load_json_workflow(args.workflow)
    repo = Path(args.repo).expanduser().resolve(strict=True)
    state_path = _state_path(args)
    artifact_root = state_path.parent / "artifacts"
    config = _config(args, repo)
    unsafe_config = next(
        (check for check in _private_config_checks(args, repo) if not check["ok"]),
        None,
    )
    if unsafe_config is not None:
        raise CliError(
            "PRIVATE_CONFIG_NOT_READY",
            f"{unsafe_config['name']}: {unsafe_config['detail']}",
        )
    try:
        runtime = PortableRuntime(
            workflow,
            config,
            repo=repo,
            state_path=state_path,
            artifact_root=artifact_root,
        )
        result = runtime.run(run_id=args.run_id, resume=args.resume)
    except WorktreeError as exc:
        raise CliError("REPOSITORY_INVALID", str(exc), path=str(repo)) from exc
    except ValueError as exc:
        code = "RESUME_MISMATCH" if args.resume else "RUN_INVALID"
        raise CliError(code, str(exc), path=str(state_path)) from exc
    payload = {
        "ok": result.run.status == "succeeded",
        "command": "run",
        "run_id": result.run.run_id,
        "status": result.run.status,
        "state": str(state_path),
        "nodes": result.run.nodes,
        "outputs": result.outputs,
        "worktrees": {key: str(value) for key, value in result.worktrees.items()},
        "agent_receipts": {
            key: asdict(value) for key, value in result.agent_receipts.items()
        },
        "check_receipts": [asdict(value) for value in result.check_receipts],
    }
    return payload, 0 if payload["ok"] else 1


def _status(args: argparse.Namespace) -> dict[str, Any]:
    if not _RUN_ID.fullmatch(args.run_id):
        raise CliError("INVALID_RUN_ID", "run id is not portable")
    state_path = Path(args.state).expanduser().resolve(strict=True)
    store = StateStore(state_path)
    run = store.run(args.run_id)
    agents, checks = _read_receipts(state_path, args.run_id)
    worktrees = sorted({receipt.cwd for receipt in checks})
    return {
        "ok": run["status"] == "succeeded",
        "command": "status",
        "run_id": args.run_id,
        "workflow_id": run["workflow_id"],
        "status": run["status"],
        "state": str(state_path),
        "nodes": store.node_rows(args.run_id),
        "attempts": list(store.attempt_rows(args.run_id)),
        "artifact_receipts": list(store.artifact_rows(args.run_id)),
        "agent_receipts": {key: asdict(value) for key, value in agents.items()},
        "check_receipts": [asdict(value) for value in checks],
        "worktrees": worktrees,
    }


def _error_payload(command: str | None, exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, WorkflowValidationError):
        return {
            "ok": False,
            "command": command,
            "error": {
                "code": "WORKFLOW_INVALID",
                "message": "workflow validation failed",
                "issues": [asdict(issue) for issue in exc.issues],
            },
        }
    if isinstance(exc, ConfigError):
        return {
            "ok": False,
            "command": command,
            "error": {
                "code": exc.code,
                "path": exc.path,
                "message": exc.message,
            },
        }
    if isinstance(exc, OrchestrationError):
        return {
            "ok": False,
            "command": command,
            "error": {"code": exc.code, "message": exc.message},
        }
    if isinstance(exc, CliError):
        error = {"code": exc.code, "message": exc.message}
        if exc.path is not None:
            error["path"] = exc.path
        return {"ok": False, "command": command, "error": error}
    if isinstance(exc, KeyError):
        return {
            "ok": False,
            "command": command,
            "error": {"code": "RUN_NOT_FOUND", "message": str(exc)},
        }
    return {
        "ok": False,
        "command": command,
        "error": {"code": "IO_ERROR", "message": str(exc)},
    }


def _print_human(payload: Mapping[str, Any]) -> None:
    if not payload.get("ok") and "error" in payload:
        error = payload["error"]
        print(f"{error['code']}: {error['message']}", file=sys.stderr)
        for issue in error.get("issues", []):
            print(
                f"  {issue['code']} at {issue['path']}: {issue['message']}",
                file=sys.stderr,
            )
        return
    command = payload.get("command")
    if command == "validate":
        print(f"valid: {payload['workflow_id']} ({payload['node_count']} nodes)")
    elif command == "plan":
        print(f"workflow: {payload['workflow_id']}")
        for index, layer in enumerate(payload["ready_layers"]):
            print(f"layer {index}: {', '.join(layer)}")
        print("critical path: " + " -> ".join(payload["critical_path"]))
    elif command == "doctor":
        for check in payload["checks"]:
            print(
                f"{'ok' if check['ok'] else 'FAIL'} {check['name']}: {check['detail']}"
            )
        for profile in payload["profiles"]:
            print(f"{'ok' if profile['ok'] else 'FAIL'} profile {profile['name']}")
            for check in profile["checks"]:
                print(
                    f"  {'ok' if check['ok'] else 'FAIL'} {check['name']}: {check['detail']}"
                )
    elif command in {"run", "status"}:
        print(f"run {payload['run_id']}: {payload['status']}")
        print(f"state: {payload['state']}")
        for node_id, node in sorted(payload["nodes"].items()):
            print(f"  {node_id}: {node['status']} (attempts={node['attempt_count']})")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graph-engineer",
        description="Validate, inspect, and execute evidence-gated development graphs.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate one JSON workflow")
    validate.add_argument("workflow")
    validate.add_argument("--json", action="store_true", dest="json_output")

    plan = subparsers.add_parser("plan", help="show deterministic dependency layers")
    plan.add_argument("workflow")
    plan.add_argument("--json", action="store_true", dest="json_output")

    doctor = subparsers.add_parser("doctor", help="check private worker readiness")
    doctor.add_argument("--repo", default=".")
    doctor.add_argument("--config")
    doctor.add_argument("--profile")
    doctor.add_argument("--json", action="store_true", dest="json_output")

    run = subparsers.add_parser("run", help="execute or resume a validated workflow")
    run.add_argument("workflow")
    run.add_argument("--repo", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--state")
    run.add_argument("--config")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--json", action="store_true", dest="json_output")

    status = subparsers.add_parser("status", help="inspect durable run evidence")
    status.add_argument("--state", required=True)
    status.add_argument("--run-id", required=True)
    status.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            workflow = _load_json_workflow(args.workflow)
            payload = {
                "ok": True,
                "command": "validate",
                "workflow_id": workflow["id"],
                "node_count": len(workflow["nodes"]),
            }
            exit_code = 0
        elif args.command == "plan":
            payload = {
                "ok": True,
                "command": "plan",
                **_topology(_load_json_workflow(args.workflow)),
            }
            exit_code = 0
        elif args.command == "doctor":
            payload = _doctor(args)
            exit_code = 0 if payload["ok"] else 1
        elif args.command == "run":
            payload, exit_code = _run(args)
        else:
            payload = _status(args)
            exit_code = 0 if payload["ok"] else 1
    except (
        WorkflowValidationError,
        ConfigError,
        OrchestrationError,
        CliError,
        KeyError,
        OSError,
    ) as exc:
        payload = _error_payload(args.command, exc)
        exit_code = 2
    if args.json_output:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        _print_human(payload)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
