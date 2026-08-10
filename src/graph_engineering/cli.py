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
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from . import __version__
from .adapters import (
    AdapterError,
    AdapterRequest,
    ExecutionLimits,
    ExecutionReceipt,
    execute_profile,
    probe_profile,
)
from .artifacts import canonical_json
from .capabilities import capability_manifest
from .compilation import CompilationError, accept_proposal, compile_proposal
from .config import (
    DEFAULT_USER_CONFIG,
    ConfigError,
    SubprocessAdapter,
    load_agent_config,
)
from .contracts import (
    ValidationIssue,
    WorkflowValidationError,
    validate_workflow,
)
from .learning import (
    LearningError,
    benchmark_run,
    compare_benchmark,
    compile_feedback,
    load_baseline,
)
from .forking import FORK_VERSION, ForkError, create_fork
from .lifecycle import LifecycleError, LifecycleStore
from .orchestrator import (
    CheckCommandReceipt,
    OrchestrationError,
    PortableRuntime,
)
from .project import (
    ProjectPolicyError,
    RunScopeRegistry,
    discover_repo,
    discover_workflows,
    execution_identity,
    load_assessment,
    load_private_execution_binding,
    load_project_policy,
    matching_active_runs,
    scaffold_project,
)
from .session_ux import (
    HANDOFF_VERSION,
    SessionUxError,
    assess_repo,
    export_handoff,
    status_projection,
    verify_handoff,
)
from .state import StateStore
from .supervision import analyze_topology, live_topology
from .worktrees import WorktreeError, WorktreeManager

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_MEMORY_FILESYSTEMS = frozenset({"ramfs", "tmpfs"})
_MOUNTINFO = Path("/proc/self/mountinfo")
_SMOKE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"const": True}},
}
_MAX_SMOKE_PROFILES = 16
_SMOKE_CONFIG_BINDINGS = (
    ("home", ".claude.json", ".claude.json"),
    ("home", ".claude/.credentials.json", ".claude/.credentials.json"),
    ("home", ".codex/auth.json", ".codex/auth.json"),
    ("home", ".grok/auth.json", ".grok/auth.json"),
    ("xdg_config", ".config/opencode", "opencode"),
    ("xdg_data", ".local/share/opencode/auth.json", "opencode/auth.json"),
)


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
    except RecursionError as exc:
        raise WorkflowValidationError(
            [
                ValidationIssue(
                    "PARSE_RESOURCE_LIMIT",
                    "$",
                    "workflow JSON nesting exceeds the parser safety limit",
                )
            ]
        ) from exc
    if not isinstance(value, dict):
        raise WorkflowValidationError(
            [ValidationIssue("TYPE_ERROR", "$", "workflow document must be an object")]
        )
    validate_workflow(value)
    return value


def _topology(workflow: Mapping[str, Any]) -> dict[str, Any]:
    return analyze_topology(workflow)


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


def _smoke_receipt(receipt: ExecutionReceipt) -> dict[str, Any]:
    projected = {
        "command_digest": receipt.command_digest,
        "result_schema_digest": receipt.result_schema_digest,
        "stdout_digest": receipt.stdout_digest,
        "stderr_digest": receipt.stderr_digest,
        "stdout_bytes": receipt.stdout_bytes,
        "stderr_bytes": receipt.stderr_bytes,
        "exit_code": receipt.exit_code,
        "duration_ms": receipt.duration_ms,
        "transport": receipt.transport,
    }
    for name in (
        "remote_task_id_digest",
        "agent_card_digest",
        "capability_digest",
        "protocol_version",
    ):
        value = getattr(receipt, name)
        if value is not None:
            projected[name] = value
    return projected


def _smoke_profile(
    profile, *, timeout_seconds: float, environ: Mapping[str, str]
) -> dict[str, Any]:
    started = time.monotonic()
    if profile.capabilities.mcp:
        return {
            "status": "failed",
            "code": "MCP_NOT_ALLOWED",
            "duration_ms": 0,
            "receipt": None,
        }
    scratch = Path(environ["TMPDIR"]).expanduser().resolve(strict=True)
    receipt: ExecutionReceipt | None = None
    code = "OK"
    status = "passed"
    with TemporaryDirectory(prefix="graph-engineering-smoke-", dir=scratch) as root:
        isolation = Path(root)
        repo = isolation / "repo"
        repo.mkdir(mode=0o700)
        state = isolation / "disposable-state"
        home = state / "home"
        xdg_config = state / "xdg-config"
        xdg_cache = state / "xdg-cache"
        xdg_state = state / "xdg-state"
        xdg_data = state / "xdg-data"
        process_tmp = state / "tmp"
        for directory in (
            home,
            xdg_config,
            xdg_cache,
            xdg_state,
            xdg_data,
            process_tmp,
        ):
            directory.mkdir(parents=True, mode=0o700)
        real_home = Path(environ["HOME"]).expanduser().resolve(strict=True)
        binding_roots = {
            "home": home,
            "xdg_config": xdg_config,
            "xdg_data": xdg_data,
        }
        readonly_bindings: list[tuple[Path, Path]] = []
        for root_name, source_name, target_name in _SMOKE_CONFIG_BINDINGS:
            source = real_home / source_name
            if not source.exists():
                continue
            target = binding_roots[root_name] / target_name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                target.mkdir(exist_ok=True)
            else:
                target.touch(mode=0o600)
            readonly_bindings.append((source, target))
        confined_environment = tuple(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(xdg_config),
                "XDG_CACHE_HOME": str(xdg_cache),
                "XDG_STATE_HOME": str(xdg_state),
                "XDG_DATA_HOME": str(xdg_data),
                "TMPDIR": str(process_tmp),
            }.items()
        )
        writable_bindings: list[tuple[Path, Path]] = []
        hardcoded_tmp = Path("/tmp").resolve(strict=True)
        if not scratch.is_relative_to(hardcoded_tmp):
            writable_bindings.append((process_tmp, hardcoded_tmp))
        hardcoded_state = real_home / ".local" / "state"
        if hardcoded_state.is_dir():
            writable_bindings.append((xdg_state, hardcoded_state))
        hardcoded_data = real_home / ".local" / "share"
        if hardcoded_data.is_dir():
            writable_bindings.append((xdg_data, hardcoded_data))
        try:
            smoke_profile = profile
            codex_exec = False
            if isinstance(profile.adapter, SubprocessAdapter):
                executable = Path(profile.adapter.argv[0]).name
                codex_exec = (
                    executable in {"codex", "codex.js"}
                    and len(profile.adapter.argv) > 1
                    and profile.adapter.argv[1] == "exec"
                )
            if (
                isinstance(profile.adapter, SubprocessAdapter)
                and Path(profile.adapter.argv[0]).name == "grok"
                and "--sandbox" in profile.adapter.argv
                and "--tools" in profile.adapter.argv
            ):
                argv = list(profile.adapter.argv)
                argv[argv.index("--sandbox") + 1] = "off"
                # Grok's strict sandbox cannot nest inside the outer bwrap
                # boundary.  The smoke needs no model tools, so remove them
                # before relaxing only that inner sandbox.  This prevents a
                # connected smoke from reading the host through read/grep.
                argv[argv.index("--tools") + 1] = ""
                smoke_profile = replace(
                    profile, adapter=replace(profile.adapter, argv=tuple(argv))
                )
            result = execute_profile(
                smoke_profile,
                AdapterRequest(
                    prompt=(
                        "Readiness smoke only. Do not call tools or MCP and do not write "
                        'files. Return exactly this JSON object: {"ok":true}'
                    ),
                    cwd=repo,
                    allowed_root=repo,
                    node_id="doctor-smoke",
                    run_id="doctor-smoke",
                    attempt=1,
                    result_schema=_SMOKE_SCHEMA,
                    state_path=isolation / "smoke-state.db",
                    confine_writes=True,
                    confined_writable_roots=(state,),
                    confined_writable_bindings=tuple(writable_bindings),
                    confined_readonly_bindings=tuple(readonly_bindings),
                    confined_environment=confined_environment,
                    # Codex exits before its final JSONL event when ptraced and
                    # needs more than the generic 4 MiB disposable-state cap.
                    # Its doctor path remains inside the same read-only bwrap
                    # namespace; only attempted-write classification is omitted.
                    audit_write_attempts=not codex_exec,
                    confined_max_file_bytes=(
                        16 * 1024 * 1024 if codex_exec else 4 * 1024 * 1024
                    ),
                ),
                limits=ExecutionLimits(
                    timeout_seconds=timeout_seconds,
                    terminate_grace_seconds=min(0.5, timeout_seconds / 4),
                    max_stdout_bytes=64 * 1024,
                    max_stderr_bytes=16 * 1024,
                ),
                environ=environ,
            )
            receipt = result.receipt
        except AdapterError as exc:
            status = "failed"
            code = exc.code
            receipt = exc.receipt
        if any(repo.iterdir()):
            status = "failed"
            code = "WRITE_DETECTED"
    return {
        "status": status,
        "code": code,
        "duration_ms": (
            receipt.duration_ms
            if receipt is not None
            else round((time.monotonic() - started) * 1000)
        ),
        "receipt": _smoke_receipt(receipt) if receipt is not None else None,
    }


def _doctor(args: argparse.Namespace) -> dict[str, Any]:
    repo = discover_repo(args.repo)
    config = _config(args, repo)
    checks = [*_private_config_checks(args, repo), _scratch_storage_check()]
    try:
        policy = load_project_policy(repo)
        try:
            load_private_execution_binding(repo)
        except ProjectPolicyError as exc:
            checks.append(
                {
                    "name": "private-execution",
                    "ok": False,
                    "detail": exc.code,
                }
            )
        else:
            checks.append(
                {
                    "name": "private-execution",
                    "ok": True,
                    "detail": "host and checkout are privately authorized",
                }
            )
        workflows = discover_workflows(policy)
        checks.append(
            {
                "name": "project-manifest",
                "ok": not policy.unresolved,
                "detail": (
                    "reviewed"
                    if not policy.unresolved
                    else f"unresolved fields ({len(policy.unresolved)})"
                ),
            }
        )
        checks.append(
            {
                "name": "project-workflows",
                "ok": bool(workflows),
                "detail": f"{len(workflows)} checked-in workflow(s)",
            }
        )
        base_sha = WorktreeManager(repo).resolve_base("HEAD")
        for index, path in enumerate(workflows, 1):
            try:
                workflow = _load_json_workflow(path)
                policy.preflight(workflow, base_sha=base_sha)
            except (WorkflowValidationError, ProjectPolicyError) as exc:
                checks.append(
                    {
                        "name": f"workflow-policy-{index}",
                        "ok": False,
                        "detail": getattr(exc, "code", "WORKFLOW_INVALID"),
                    }
                )
            else:
                checks.append(
                    {
                        "name": f"workflow-policy-{index}",
                        "ok": True,
                        "detail": "validated against project policy",
                    }
                )
    except ProjectPolicyError as exc:
        checks.append({"name": "project-manifest", "ok": False, "detail": exc.code})
    if not config.profiles:
        checks.append(
            {"name": "profiles", "ok": False, "detail": "no profiles configured"}
        )
    requested = list(args.profile or ())
    if len(requested) > _MAX_SMOKE_PROFILES:
        raise CliError("PROFILE_LIMIT", "at most 16 profiles may be selected")
    if len(requested) != len(set(requested)):
        raise CliError("DUPLICATE_PROFILE", "profile selections must be unique")
    unknown = sorted(set(requested) - set(config.profiles))
    if unknown:
        raise CliError("UNKNOWN_PROFILE", f"unknown profiles: {unknown}")
    selected = requested or sorted(config.profiles)
    profiles: list[dict[str, Any]] = []
    for name in selected:
        profile = config.profiles[name]
        probes = probe_profile(profile, cwd=repo, allowed_root=repo)
        profile_ready = all(probe.ok for probe in probes)
        item = {
            "name": name,
            "adapter": profile.adapter_kind,
            "capabilities": sorted(profile.capabilities.enabled()),
            "ok": profile_ready,
            "checks": [asdict(probe) for probe in probes],
            "smoke": None,
        }
        if args.smoke and profile_ready:
            item["smoke"] = _smoke_profile(
                profile, timeout_seconds=args.timeout, environ=os.environ
            )
            item["ok"] = item["smoke"]["status"] == "passed"
        profiles.append(item)
    ok = all(check["ok"] for check in checks) and all(
        profile["ok"] for profile in profiles
    )
    return {
        "ok": ok,
        "command": "doctor",
        "mode": "smoke" if args.smoke else "static",
        "checks": checks,
        "profiles": profiles,
    }


def _state_path(args: argparse.Namespace) -> Path:
    if args.state:
        return Path(args.state).expanduser().resolve()
    return (_default_state_root() / args.run_id / "state.db").resolve()


def _default_state_root() -> Path:
    state_home = Path(
        os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    ).expanduser()
    return (state_home / "graph-engineering" / "runs").resolve()


def _read_document(path: str | Path, *, code: str) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionUxError(code, "document is missing or invalid JSON") from exc
    if not isinstance(value, dict):
        raise SessionUxError(code, "document must be an object")
    return value


def _write_document(path: str | Path, value: Mapping[str, Any]) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(value) + b"\n"
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
    except OSError as exc:
        raise SessionUxError(
            "OUTPUT_WRITE_ERROR", "output must be a new writable file"
        ) from exc
    return target


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
    repo = discover_repo(args.repo)
    state_path = _state_path(args)
    artifact_root = state_path.parent / "artifacts"
    config = _config(args, repo)
    if args.handoff and not args.resume:
        raise SessionUxError("HANDOFF_REQUIRES_RESUME", "--handoff requires --resume")
    if args.handoff:
        verify_handoff(
            _read_document(args.handoff, code="HANDOFF_READ_ERROR"),
            state_path,
            args.run_id,
            workflow,
        )
    project_policy = load_project_policy(repo)
    unsafe_config = next(
        (check for check in _private_config_checks(args, repo) if not check["ok"]),
        None,
    )
    if unsafe_config is not None:
        raise CliError(
            "PRIVATE_CONFIG_NOT_READY",
            f"{unsafe_config['name']}: {unsafe_config['detail']}",
        )
    base_sha = WorktreeManager(repo).resolve_base("HEAD")
    identity = execution_identity(project_policy, workflow, base_sha=base_sha)
    registry = RunScopeRegistry(_default_state_root())
    registry.claim(
        identity,
        run_id=args.run_id,
        state_path=state_path,
        resume=args.resume,
    )
    try:
        runtime = PortableRuntime(
            workflow,
            config,
            repo=repo,
            state_path=state_path,
            artifact_root=artifact_root,
            base=base_sha,
            bootstrap_legacy_lifecycle=args.bootstrap_legacy_lifecycle,
            project_policy=project_policy,
        )
        result = runtime.run(run_id=args.run_id, resume=args.resume)
    except WorktreeError as exc:
        raise CliError("REPOSITORY_INVALID", str(exc), path=str(repo)) from exc
    except ValueError as exc:
        code = "RESUME_MISMATCH" if args.resume else "RUN_INVALID"
        raise CliError(code, str(exc), path=str(state_path)) from exc
    finally:
        registry.release_if_inactive(
            identity, run_id=args.run_id, state_path=state_path
        )
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
        "run_context": result.run_context.as_dict(),
        "lifecycle": {
            "event_count": len(result.lifecycle_events),
            "head_digest": (
                result.lifecycle_events[-1].digest if result.lifecycle_events else None
            ),
        },
    }
    return payload, 0 if payload["ok"] else 1


def _init(args: argparse.Namespace) -> dict[str, Any]:
    repo = discover_repo(args.repo)
    assessment = (
        load_assessment(args.from_assessment, repo)
        if args.from_assessment is not None
        else None
    )
    created = scaffold_project(repo, assessment=assessment)
    policy = load_project_policy(repo)
    private_config = _private_config_checks(args, repo)
    workflows = discover_workflows(policy)
    workflow_reports: list[dict[str, Any]] = []
    active: list[Mapping[str, str]] = []
    state_root = (
        Path(args.state_root).expanduser().resolve()
        if args.state_root
        else _default_state_root()
    )
    base_sha = WorktreeManager(repo).resolve_base("HEAD")
    for path in workflows:
        try:
            workflow = _load_json_workflow(path)
            policy.preflight(workflow, base_sha=base_sha)
            load_private_execution_binding(repo)
        except (WorkflowValidationError, ProjectPolicyError) as exc:
            workflow_reports.append(
                {"path": str(path), "ok": False, "detail": str(exc)}
            )
            continue
        matches = matching_active_runs(
            state_root, execution_identity(policy, workflow, base_sha=base_sha)
        )
        active.extend(matches)
        workflow_reports.append(
            {
                "path": str(path),
                "ok": True,
                "detail": "validated against frozen product contract",
                "matching_active_runs": list(matches),
            }
        )
    ready = (
        not created
        and not policy.unresolved
        and bool(workflows)
        and all(report["ok"] for report in workflow_reports)
        and bool(private_config)
        and all(check["ok"] for check in private_config)
        and not active
    )
    return {
        "ok": ready,
        "command": "init",
        "repo": str(repo),
        "created": [str(path) for path in created],
        "reused": not created,
        "unresolved": list(policy.unresolved),
        "private_config": private_config,
        "workflows": workflow_reports,
        "matching_active_runs": active,
        "launch_blocked_by_active_run": bool(active),
        "assessment_recommendation": (
            {
                "workflow_templates": list(
                    assessment["recommended_init"]["workflow_templates"]
                ),
                "require_private_config": assessment["recommended_init"][
                    "require_private_config"
                ],
            }
            if assessment is not None
            else None
        ),
        "next_commands": [
            "graph-engineer doctor --repo "
            + str(repo)
            + (f" --config {Path(args.config).expanduser()}" if args.config else ""),
            *[f"graph-engineer validate {path} --repo {repo}" for path in workflows],
        ],
    }


def _status(args: argparse.Namespace) -> dict[str, Any]:
    if not _RUN_ID.fullmatch(args.run_id):
        raise CliError("INVALID_RUN_ID", "run id is not portable")
    state_path = Path(args.state).expanduser().resolve(strict=True)
    store = StateStore(state_path)
    run = store.run(args.run_id)
    agents, checks = _read_receipts(state_path, args.run_id)
    worktrees = sorted({receipt.cwd for receipt in checks})
    lifecycle = None
    if run.get("lifecycle_state") == "active":
        context, events = LifecycleStore(state_path).snapshot(args.run_id)
        lifecycle = {
            "context_digest": context.digest,
            "event_count": len(events),
            "head_digest": events[-1].digest if events else None,
        }
    elif run.get("lifecycle_state") == "pending":
        raise LifecycleError(
            "LIFECYCLE_INCOMPLETE", "run lifecycle bootstrap did not complete"
        )
    nodes = store.node_rows(args.run_id)
    payload = {
        "ok": run["status"] == "succeeded",
        "command": "status",
        "run_id": args.run_id,
        "workflow_id": run["workflow_id"],
        "status": run["status"],
        "state": str(state_path),
        "nodes": nodes,
        "attempts": list(store.attempt_rows(args.run_id)),
        "artifact_receipts": list(store.artifact_rows(args.run_id)),
        "agent_receipts": {key: asdict(value) for key, value in agents.items()},
        "check_receipts": [asdict(value) for value in checks],
        "worktrees": worktrees,
        "lifecycle": lifecycle,
        "supervision": {
            "progress": store.progress_rows(args.run_id),
            "topology": live_topology(run["workflow"], nodes),
        },
    }
    if args.projection:
        payload["projection"] = status_projection(state_path, args.run_id)
    return payload


def _handoff(args: argparse.Namespace) -> dict[str, Any]:
    if not _RUN_ID.fullmatch(args.run_id):
        raise CliError("INVALID_RUN_ID", "run id is not portable")
    state_path = Path(args.state).expanduser().resolve(strict=True)
    document = export_handoff(state_path, args.run_id)
    output = str(_write_document(args.output, document)) if args.output else None
    return {
        "ok": True,
        "command": "handoff",
        "version": HANDOFF_VERSION,
        "run_id": args.run_id,
        "digest": document["digest"],
        "output": output,
        "handoff": document if output is None else None,
    }


def _assess(args: argparse.Namespace) -> dict[str, Any]:
    repo = Path(args.repo).expanduser().resolve(strict=True)
    private_config = (
        Path(args.config).expanduser() if args.config else DEFAULT_USER_CONFIG
    )
    assessment = assess_repo(repo, private_config)
    if args.output and Path(args.output).expanduser().resolve().is_relative_to(repo):
        raise SessionUxError(
            "ASSESSMENT_OUTPUT_SCOPE",
            "assessment output must stay outside the assessed repository",
        )
    output = str(_write_document(args.output, assessment)) if args.output else None
    return {
        "ok": True,
        "command": "assess",
        "assessment": assessment,
        "output": output,
    }


def _compile(args: argparse.Namespace) -> dict[str, Any]:
    repo = discover_repo(args.repo)
    output_path = Path(args.output).expanduser().resolve()
    if output_path.is_relative_to(repo):
        raise SessionUxError(
            "PROPOSAL_OUTPUT_SCOPE",
            "proposal output must stay outside the repository until human acceptance",
        )
    workflow = _load_json_workflow(args.candidate)
    proposal = compile_proposal(
        repo,
        assessment_path=Path(args.assessment).expanduser().resolve(strict=True),
        workflow=workflow,
        proposed_by=args.proposed_by,
    )
    output = str(_write_document(output_path, proposal))
    return {
        "ok": True,
        "command": "compile",
        "proposal_id": proposal["id"],
        "proposal_digest": proposal["digest"],
        "output": output,
        "dispatch_authorized": False,
    }


def _accept(args: argparse.Namespace) -> dict[str, Any]:
    repo = discover_repo(args.repo)
    proposal_path = Path(args.proposal).expanduser().resolve(strict=True)
    if proposal_path.is_relative_to(repo):
        raise SessionUxError(
            "PROPOSAL_INPUT_SCOPE",
            "an unaccepted proposal must stay outside the repository",
        )
    workflow_output = Path(args.workflow_output).expanduser().resolve()
    acceptance_output = Path(args.acceptance_output).expanduser().resolve()
    expected_workflows = (repo / ".graph-engineering" / "workflows").resolve()
    expected_reviews = (repo / ".graph-engineering" / "reviews").resolve()
    if workflow_output.parent != expected_workflows:
        raise SessionUxError(
            "WORKFLOW_OUTPUT_SCOPE",
            "accepted workflows must be written directly under .graph-engineering/workflows",
        )
    if acceptance_output.parent != expected_reviews:
        raise SessionUxError(
            "ACCEPTANCE_OUTPUT_SCOPE",
            "acceptance receipts must be written directly under .graph-engineering/reviews",
        )
    proposal = _read_document(proposal_path, code="PROPOSAL_READ_ERROR")
    workflow, acceptance = accept_proposal(
        repo,
        proposal,
        expected_digest=args.proposal_digest,
        reviewed_by=args.reviewed_by,
    )
    if workflow_output.exists() or acceptance_output.exists():
        raise SessionUxError(
            "OUTPUT_WRITE_ERROR",
            "workflow and acceptance outputs must both be new files",
        )
    # Evidence lands first. A crash may leave an inert receipt, but never a runnable
    # workflow without its acceptance evidence.
    acceptance_path = _write_document(acceptance_output, acceptance)
    workflow_path = _write_document(workflow_output, workflow)
    return {
        "ok": True,
        "command": "accept",
        "workflow_id": workflow["id"],
        "workflow_digest": acceptance["workflow_digest"],
        "proposal_digest": acceptance["proposal_digest"],
        "workflow_output": str(workflow_path),
        "acceptance_output": str(acceptance_path),
        "dispatch_authorized": True,
    }


def _trace(args: argparse.Namespace) -> dict[str, Any]:
    if not _RUN_ID.fullmatch(args.run_id):
        raise CliError("INVALID_RUN_ID", "run id is not portable")
    state_path = Path(args.state).expanduser().resolve(strict=True)
    payload = LifecycleStore(state_path).trace(args.run_id, limit=args.limit)
    store = StateStore(state_path)
    run = store.run(args.run_id)
    nodes = store.node_rows(args.run_id)
    return {
        "ok": True,
        "command": "trace",
        "state": str(state_path),
        **payload,
        "supervision": {
            "progress": store.progress_rows(args.run_id),
            "topology": live_topology(run["workflow"], nodes),
        },
    }


def _benchmark(args: argparse.Namespace) -> dict[str, Any]:
    if not _RUN_ID.fullmatch(args.run_id):
        raise CliError("INVALID_RUN_ID", "run id is not portable")
    state_path = Path(args.state).expanduser().resolve(strict=True)
    report = benchmark_run(state_path, args.run_id)
    comparison = (
        compare_benchmark(report, load_baseline(Path(args.baseline).expanduser()))
        if args.baseline
        else None
    )
    document = {"report": report, "comparison": comparison}
    output = str(_write_document(args.output, document)) if args.output else None
    return {
        "ok": True,
        "command": "benchmark",
        "run_id": args.run_id,
        "state": str(state_path),
        "output": output,
        **document,
    }


def _feedback(args: argparse.Namespace) -> dict[str, Any]:
    proposal = compile_feedback(Path(args.input).expanduser())
    output = str(_write_document(args.output, proposal)) if args.output else None
    return {
        "ok": True,
        "command": "feedback",
        "output": output,
        "proposal": proposal,
    }


def _events(args: argparse.Namespace) -> dict[str, Any]:
    if not _RUN_ID.fullmatch(args.run_id):
        raise CliError("INVALID_RUN_ID", "run id is not portable")
    state_path = Path(args.state).expanduser().resolve(strict=True)
    batch = LifecycleStore(state_path).stream(
        args.run_id,
        cursor=args.cursor,
        limit=args.limit,
        wait_seconds=args.wait,
    )
    return {
        "ok": True,
        "command": "events",
        "state": str(state_path),
        **batch,
    }


def _fork(args: argparse.Namespace) -> dict[str, Any]:
    if not _RUN_ID.fullmatch(args.run_id) or not _RUN_ID.fullmatch(args.new_run_id):
        raise CliError("INVALID_RUN_ID", "run id is not portable")
    state_path = Path(args.state).expanduser().resolve(strict=True)
    lineage = create_fork(
        state_path,
        args.run_id,
        args.at_sequence,
        args.new_run_id,
    )
    return {
        "ok": True,
        "command": "fork",
        "version": FORK_VERSION,
        "state": str(state_path),
        "run_id": args.new_run_id,
        "status": "pending",
        "lineage": lineage,
    }


def _error_payload(command: str | None, exc: BaseException) -> dict[str, Any]:
    if command == "doctor":
        code = getattr(exc, "code", "DOCTOR_FAILED")
        return {
            "ok": False,
            "command": command,
            "error": {
                "code": code,
                "message": "readiness could not be established",
            },
        }
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
    if isinstance(exc, ProjectPolicyError):
        error = {"code": exc.code, "message": exc.message}
        if exc.path is not None:
            error["path"] = exc.path
        return {"ok": False, "command": command, "error": error}
    if isinstance(exc, LifecycleError):
        return {
            "ok": False,
            "command": command,
            "error": {"code": exc.code, "message": exc.message},
        }
    if isinstance(exc, LearningError):
        return {
            "ok": False,
            "command": command,
            "error": {"code": exc.code, "message": exc.message},
        }
    if isinstance(exc, ForkError):
        return {
            "ok": False,
            "command": command,
            "error": {"code": exc.code, "message": exc.message},
        }
    if isinstance(exc, SessionUxError):
        return {
            "ok": False,
            "command": command,
            "error": {"code": exc.code, "message": exc.message},
        }
    if isinstance(exc, CompilationError):
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
    if command == "capabilities":
        print(f"graph-engineer {payload['package_version']}")
        print("commands: " + ", ".join(payload["cli_commands"]))
        print("joins: " + ", ".join(payload["runtime"]["join_policies"]))
    elif command == "validate":
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
            if profile["smoke"] is not None:
                print(
                    f"  smoke {profile['smoke']['status']}: {profile['smoke']['code']}"
                )
    elif command == "init":
        action = "reused" if payload["reused"] else "scaffolded"
        print(f"{action}: {payload['repo']}")
        for field in payload["unresolved"]:
            print(f"  unresolved: {field}")
        for run in payload["matching_active_runs"]:
            print(f"  active run: {run['run_id']} ({run['state']})")
    elif command in {"run", "status"}:
        print(f"run {payload['run_id']}: {payload['status']}")
        print(f"state: {payload['state']}")
        for node_id, node in sorted(payload["nodes"].items()):
            print(f"  {node_id}: {node['status']} (attempts={node['attempt_count']})")
        if payload.get("projection"):
            projection = payload["projection"]
            print("critical path: " + " -> ".join(projection["critical_path"]))
            print(f"useful overlap: {projection['useful_overlap_seconds']}s")
    elif command == "handoff":
        print(f"handoff {payload['run_id']}: {payload['digest']}")
        if payload["output"]:
            print(f"output: {payload['output']}")
    elif command == "assess":
        assessment = payload["assessment"]
        print(
            f"assessment: {'ready' if assessment['summary']['ready'] else 'gaps found'}"
        )
        for gap in assessment["gaps"]:
            print(f"  {gap['priority']} {gap['id']}: {gap['remediation']}")
    elif command == "compile":
        print(f"proposal {payload['proposal_id']}: {payload['proposal_digest']}")
        print(f"review required: {payload['output']}")
    elif command == "accept":
        print(
            f"accepted workflow {payload['workflow_id']}: {payload['workflow_digest']}"
        )
        print(f"workflow: {payload['workflow_output']}")
    elif command == "trace":
        print(f"run {payload['run_id']}: {payload['event_count']} lifecycle events")
        if payload["truncated"]:
            print("  ... earlier events omitted ...")
        for event in payload["events"]:
            subject = f" {event['node_id']}" if event["node_id"] else ""
            attempt = f"#{event['attempt']}" if event["attempt"] is not None else ""
            print(f"  {event['sequence']:04d} {event['event_type']}{subject}{attempt}")
    elif command == "benchmark":
        print(f"benchmark {payload['run_id']}: {payload['report']['digest']}")
        for key, value in sorted(payload["report"]["metrics"].items()):
            print(f"  {key}: {'unavailable' if value is None else value}")
    elif command == "feedback":
        proposal = payload["proposal"]
        print(f"learning proposal {proposal['source_id']}: {proposal['digest']}")
        for action in proposal["actions"]:
            print(f"  {action['id']}: {action['target']} (review required)")
    elif command == "events":
        for event in payload["events"]:
            print(json.dumps(event, sort_keys=True, separators=(",", ":")))
        if payload["terminal"]:
            print("stream terminal")
        print(f"cursor: {payload['next_cursor']}")
    elif command == "fork":
        parent = payload["lineage"]["parent_run_id"]
        sequence = payload["lineage"]["parent_event"]["sequence"]
        print(f"fork {payload['run_id']}: {parent}@{sequence}")
        print(f"state: {payload['state']}")


def _profile_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise argparse.ArgumentTypeError("profile must be a portable name <= 128 bytes")
    return value


def _smoke_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not 0.1 <= parsed <= 120:
        raise argparse.ArgumentTypeError("timeout must be between 0.1 and 120 seconds")
    return parsed


def _parser_commands(parser: argparse.ArgumentParser) -> list[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    return []


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graph-engineer",
        description="Validate, inspect, and execute evidence-gated development graphs.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capabilities = subparsers.add_parser(
        "capabilities", help="print the authoritative runtime capability manifest"
    )
    capabilities.add_argument("--json", action="store_true", dest="json_output")

    validate = subparsers.add_parser("validate", help="validate one JSON workflow")
    validate.add_argument("workflow")
    validate.add_argument("--repo")
    validate.add_argument("--json", action="store_true", dest="json_output")

    plan = subparsers.add_parser("plan", help="show deterministic dependency layers")
    plan.add_argument("workflow")
    plan.add_argument("--json", action="store_true", dest="json_output")

    doctor = subparsers.add_parser("doctor", help="check private worker readiness")
    doctor.add_argument("--repo", default=".")
    doctor.add_argument("--config")
    doctor.add_argument("--profile", action="append", type=_profile_name)
    doctor.add_argument("--smoke", action="store_true")
    doctor.add_argument("--timeout", type=_smoke_timeout, default=30.0)
    doctor.add_argument("--json", action="store_true", dest="json_output")

    init = subparsers.add_parser(
        "init", help="discover or scaffold a reviewed project graph boundary"
    )
    init.add_argument("--repo", default=".")
    init.add_argument("--config")
    init.add_argument("--from-assessment")
    init.add_argument("--state-root")
    init.add_argument("--json", action="store_true", dest="json_output")

    run = subparsers.add_parser("run", help="execute or resume a validated workflow")
    run.add_argument("workflow")
    run.add_argument("--repo", required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--state")
    run.add_argument("--config")
    run.add_argument("--resume", action="store_true")
    run.add_argument("--handoff")
    run.add_argument("--bootstrap-legacy-lifecycle", action="store_true")
    run.add_argument("--json", action="store_true", dest="json_output")

    status = subparsers.add_parser("status", help="inspect durable run evidence")
    status.add_argument("--state", required=True)
    status.add_argument("--run-id", required=True)
    status.add_argument("--projection", action="store_true")
    status.add_argument("--json", action="store_true", dest="json_output")

    trace = subparsers.add_parser("trace", help="inspect the tamper-evident lifecycle")
    trace.add_argument("--state", required=True)
    trace.add_argument("--run-id", required=True)
    trace.add_argument("--limit", type=int, default=500)
    trace.add_argument("--json", action="store_true", dest="json_output")

    events = subparsers.add_parser(
        "events", help="read a bounded reconnectable lifecycle event batch"
    )
    events.add_argument("--state", required=True)
    events.add_argument("--run-id", required=True)
    events.add_argument("--cursor")
    events.add_argument("--limit", type=int, default=100)
    events.add_argument("--wait", type=float, default=0)
    events.add_argument("--json", action="store_true", dest="json_output")

    fork = subparsers.add_parser(
        "fork", help="create an immutable fresh run from a verified checkpoint"
    )
    fork.add_argument("--state", required=True)
    fork.add_argument("--run-id", required=True, help="parent run id")
    fork.add_argument("--at-sequence", required=True, type=int)
    fork.add_argument("--new-run-id", required=True)
    fork.add_argument("--json", action="store_true", dest="json_output")

    handoff = subparsers.add_parser(
        "handoff", help="export a bound cross-engine handoff"
    )
    handoff.add_argument("--state", required=True)
    handoff.add_argument("--run-id", required=True)
    handoff.add_argument("--output")
    handoff.add_argument("--json", action="store_true", dest="json_output")

    assess = subparsers.add_parser("assess", help="audit graph-engineering adoption")
    assess.add_argument("--repo", default=".")
    assess.add_argument("--config")
    assess.add_argument("--output")
    assess.add_argument("--json", action="store_true", dest="json_output")

    compile_command = subparsers.add_parser(
        "compile", help="validate and bind a model-proposed workflow for human review"
    )
    compile_command.add_argument("--repo", required=True)
    compile_command.add_argument("--assessment", required=True)
    compile_command.add_argument("--candidate", required=True)
    compile_command.add_argument("--proposed-by", required=True)
    compile_command.add_argument("--output", required=True)
    compile_command.add_argument("--json", action="store_true", dest="json_output")

    accept = subparsers.add_parser(
        "accept",
        help="accept an immutable workflow proposal as its named human reviewer",
    )
    accept.add_argument("--repo", required=True)
    accept.add_argument("--proposal", required=True)
    accept.add_argument("--proposal-digest", required=True)
    accept.add_argument("--reviewed-by", required=True)
    accept.add_argument("--workflow-output", required=True)
    accept.add_argument("--acceptance-output", required=True)
    accept.add_argument("--json", action="store_true", dest="json_output")
    benchmark = subparsers.add_parser(
        "benchmark", help="derive evidence-bound outcome metrics for one run"
    )
    benchmark.add_argument("--state", required=True)
    benchmark.add_argument("--run-id", required=True)
    benchmark.add_argument("--baseline")
    benchmark.add_argument("--output")
    benchmark.add_argument("--json", action="store_true", dest="json_output")

    feedback = subparsers.add_parser(
        "feedback", help="compile human/test feedback into reviewed learning proposals"
    )
    feedback.add_argument("--input", required=True)
    feedback.add_argument("--output")
    feedback.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capabilities":
            payload = {
                "ok": True,
                "command": "capabilities",
                **capability_manifest(_parser_commands(parser)),
            }
            exit_code = 0
        elif args.command == "validate":
            workflow = _load_json_workflow(args.workflow)
            if args.repo:
                repo = discover_repo(args.repo)
                policy = load_project_policy(repo)
                policy.preflight(
                    workflow, base_sha=WorktreeManager(repo).resolve_base("HEAD")
                )
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
        elif args.command == "init":
            payload = _init(args)
            exit_code = 0 if payload["ok"] else 1
        elif args.command == "run":
            payload, exit_code = _run(args)
        elif args.command == "trace":
            payload = _trace(args)
            exit_code = 0
        elif args.command == "events":
            payload = _events(args)
            exit_code = 0
        elif args.command == "fork":
            payload = _fork(args)
            exit_code = 0
        elif args.command == "handoff":
            payload = _handoff(args)
            exit_code = 0
        elif args.command == "assess":
            payload = _assess(args)
            exit_code = 0
        elif args.command == "compile":
            payload = _compile(args)
            exit_code = 0
        elif args.command == "accept":
            payload = _accept(args)
        elif args.command == "benchmark":
            payload = _benchmark(args)
            exit_code = 0
        elif args.command == "feedback":
            payload = _feedback(args)
            exit_code = 0
        else:
            payload = _status(args)
            exit_code = 0 if payload["ok"] else 1
    except (
        WorkflowValidationError,
        ConfigError,
        OrchestrationError,
        ProjectPolicyError,
        LifecycleError,
        LearningError,
        ForkError,
        SessionUxError,
        CompilationError,
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
