"""Portable execution layer that binds graphs to configured worker profiles.

The scheduler owns readiness and persistence.  This module owns the security-sensitive
edges around it: profile capability intersection, bounded process workspaces,
transferable change sets, and the single integration worktree.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .adapters import (
    SAFE_ENV,
    AdapterError,
    AdapterRequest,
    ExecutionLimits,
    ExecutionReceipt,
    _run_process,
    execute_profile,
)
from .artifacts import ArtifactError, canonical_json
from .builtins import BuiltinOperationError, builtin_executor
from .config import (
    A2AAdapter,
    AgentConfig,
    OpenAICompatibleAdapter,
    Profile,
    get_profile,
)
from .contracts import validate_workflow
from .forking import ForkError, verify_lineage
from .lifecycle import (
    LifecycleEvent,
    LifecycleStore,
    RunContext,
    RunContextProvider,
    StaticRunContextProvider,
)
from .project import (
    PROJECT_MANIFEST,
    ExecutionIdentity,
    ProjectPolicy,
    ProjectPolicyError,
    execution_identity,
    load_private_execution_binding,
    load_project_policy,
)
from .runtime import CheckResult, ExecutionContext, Executor, RunResult, Scheduler
from .worktrees import ChangeSet, Worktree, WorktreeError, WorktreeManager

CHANGE_SET_OUTPUT = "changeset"
CHANGE_SET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "base_sha",
        "patch_b64",
        "untracked_b64",
        "changed_paths",
        "digest",
    ],
    "properties": {
        "base_sha": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
        "patch_b64": {"type": "string"},
        "untracked_b64": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "changed_paths": {
            "type": "array",
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
        },
        "digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    },
}

_SHELLS = frozenset({"bash", "csh", "dash", "fish", "ksh", "sh", "tcsh", "zsh"})
_DIRECT_SIDE_EFFECT_TOOLS = frozenset(
    {
        "chmod",
        "chown",
        "curl",
        "dd",
        "gh",
        "mv",
        "nc",
        "netcat",
        "rm",
        "rsync",
        "scp",
        "ssh",
        "wget",
    }
)
_INDIRECT_EXECUTORS = frozenset(
    {"command", "doas", "env", "nice", "nohup", "sudo", "timeout", "xargs"}
)


class OrchestrationError(RuntimeError):
    """The portable runtime could not prove an execution boundary safe."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class CheckCommandReceipt:
    run_id: str
    node_id: str
    attempt: int
    check_id: str
    command_digest: str
    cwd: str
    exit_code: int
    stdout_digest: str
    stderr_digest: str
    stdout_bytes: int
    stderr_bytes: int


@dataclass(frozen=True)
class OrchestrationResult:
    run: RunResult
    outputs: Mapping[str, Any]
    agent_receipts: Mapping[str, ExecutionReceipt]
    check_receipts: tuple[CheckCommandReceipt, ...]
    worktrees: Mapping[str, Path]
    run_context: RunContext
    lifecycle_events: tuple[LifecycleEvent, ...]


def change_set_value(change: ChangeSet) -> dict[str, Any]:
    """Convert a change set to its canonical JSON artifact representation."""

    return {
        "base_sha": change.base_sha,
        "patch_b64": change.patch_b64,
        "untracked_b64": dict(change.untracked_b64),
        "changed_paths": list(change.changed_paths),
        "digest": change.digest,
    }


def change_set_from_value(value: Any) -> ChangeSet:
    """Reject malformed transfer artifacts before they reach git."""

    errors = sorted(
        Draft202012Validator(CHANGE_SET_SCHEMA).iter_errors(value),
        key=lambda error: list(error.path),
    )
    if errors:
        raise OrchestrationError(
            "INVALID_CHANGE_SET", f"change-set schema mismatch: {errors[0].message}"
        )
    assert isinstance(value, dict)
    return ChangeSet(
        base_sha=value["base_sha"],
        patch_b64=value["patch_b64"],
        untracked_b64=dict(value["untracked_b64"]),
        changed_paths=tuple(value["changed_paths"]),
        digest=value["digest"],
    )


def _attempt_name(node_id: str, attempt: int) -> str:
    suffix = hashlib.sha256(node_id.encode()).hexdigest()[:8]
    return f"{node_id[:48]}-{suffix}-a{attempt}"


def _required_capabilities(node: Mapping[str, Any]) -> frozenset[str]:
    required = {"read", "structured_output"}
    if node["workspace"] == "worktree":
        required.add("worktree")
    if node["permission"] in {"write", "destructive"}:
        required.update({"write", "worktree"})
    if node["permission"] == "external":
        required.add("mcp")
    return frozenset(required)


class PortableRuntime:
    """Execute a validated graph using configured agents and one integration owner."""

    def __init__(
        self,
        workflow: dict[str, Any],
        config: AgentConfig,
        *,
        repo: str | Path,
        state_path: str | Path,
        artifact_root: str | Path,
        base: str = "HEAD",
        executors: Mapping[str, Executor] | None = None,
        approvals: Mapping[str, Mapping[str, Any]] | None = None,
        environ: Mapping[str, str] | None = None,
        agent_limits: ExecutionLimits | None = None,
        context_provider: RunContextProvider | None = None,
        bootstrap_legacy_lifecycle: bool = False,
        project_policy: ProjectPolicy | None = None,
    ):
        validate_workflow(workflow)
        self.workflow = workflow
        self.nodes = {node["id"]: node for node in workflow["nodes"]}
        self.config = config
        self.manager = WorktreeManager(repo)
        self.base_sha = self.manager.resolve_base(base)
        self.project_policy = project_policy
        if (
            self.project_policy is None
            and (self.manager.repo / PROJECT_MANIFEST).exists()
        ):
            self.project_policy = load_project_policy(self.manager.repo)
        if self.project_policy is not None:
            if self.project_policy.repo != self.manager.repo:
                raise OrchestrationError(
                    "WRONG_REPOSITORY_ROOT",
                    "project policy belongs to a different repository root",
                )
            try:
                self.project_policy.preflight(workflow, base_sha=self.base_sha)
                private_binding = load_private_execution_binding(self.manager.repo)
            except ProjectPolicyError as exc:
                raise OrchestrationError(exc.code, exc.message) from exc
            self.private_execution_digest: str | None = private_binding.digest
            self.execution_identity: ExecutionIdentity | None = execution_identity(
                self.project_policy, workflow, base_sha=self.base_sha
            )
        else:
            self.private_execution_digest = None
            self.execution_identity = None
        self.state_path = Path(state_path)
        self.artifact_root = Path(artifact_root)
        self.custom_executors = dict(executors or {})
        self.approvals = {key: dict(value) for key, value in (approvals or {}).items()}
        self.environ = dict(os.environ if environ is None else environ)
        self.agent_limits = agent_limits or ExecutionLimits()
        caller_context = (
            context_provider.provide() if context_provider is not None else {}
        )
        self.context_provider = StaticRunContextProvider(
            {
                **caller_context,
                "workflow_digest": hashlib.sha256(canonical_json(workflow)).hexdigest(),
                "base_sha": self.base_sha,
                **(
                    {
                        "product_contract_version": self.project_policy.product_contract.version,
                        "product_contract_generation": self.project_policy.product_contract.generation,
                        "product_contract_digest": self.project_policy.product_contract.digest,
                        "project_policy_digest": self.project_policy.digest,
                        "private_execution_digest": self.private_execution_digest,
                        "repository_digest": self.execution_identity.repository_digest,
                    }
                    if self.project_policy is not None
                    else {}
                ),
            }
        )
        self.bootstrap_legacy_lifecycle = bootstrap_legacy_lifecycle
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._profiles: dict[str, Profile] = {}
        self._fallback_profiles: dict[
            str, tuple[tuple[dict[str, Any], Profile], ...]
        ] = {}
        self._worktrees: dict[tuple[str, int], Worktree] = {}
        self._snapshots: dict[tuple[str, int], ChangeSet] = {}
        self._agent_receipts: dict[str, ExecutionReceipt] = {}
        self._check_receipts: list[CheckCommandReceipt] = []
        self._receipts_loaded: set[str] = set()
        self._preflight()

    def _preflight(self) -> None:
        referenced_approvals = {
            node["approval"] for node in self.nodes.values() if node.get("approval")
        }
        missing_approvals = sorted(referenced_approvals - set(self.approvals))
        if missing_approvals:
            raise OrchestrationError(
                "APPROVAL_REQUIRED",
                f"explicit approval results are missing for {missing_approvals}",
            )

        for node in self.nodes.values():
            if not node.get("checks"):
                raise OrchestrationError(
                    "MISSING_CHECK", f"node {node['id']!r} has no deterministic gate"
                )
            for check in node["checks"]:
                self._validate_check_argv(node, check["argv"])

            if node["kind"] == "agent":
                profile = get_profile(
                    self.config,
                    node["profile"],
                    required=_required_capabilities(node),
                )
                if isinstance(profile.adapter, OpenAICompatibleAdapter):
                    raise OrchestrationError(
                        "UNSUPPORTED_ADAPTER",
                        f"profile {profile.name!r} uses openai-compatible execution, "
                        "which is not implemented by PortableRuntime",
                    )
                if isinstance(profile.adapter, A2AAdapter) and profile.capabilities.mcp:
                    raise OrchestrationError(
                        "A2A_AUTHORITY",
                        f"remote profile {profile.name!r} may not inherit local MCP authority",
                    )
                if node.get("model") not in {None, profile.model}:
                    raise OrchestrationError(
                        "MODEL_MISMATCH",
                        f"node {node['id']!r} is pinned to profile model {profile.model!r}",
                    )
                if node["workspace"] == "shared":
                    raise OrchestrationError(
                        "SHARED_AGENT_WORKSPACE",
                        f"agent {node['id']!r} must use a bounded isolated workspace",
                    )
                content = set(node["outputs"]) - {CHANGE_SET_OUTPUT}
                if len(content) != 1:
                    raise OrchestrationError(
                        "AGENT_OUTPUT_CONVENTION",
                        f"agent {node['id']!r} must declare exactly one model output",
                    )
                writing = node["permission"] in {"write", "destructive"}
                change_contract = node["outputs"].get(CHANGE_SET_OUTPUT)
                if writing and (
                    change_contract is None
                    or change_contract.get("schema") != CHANGE_SET_SCHEMA
                ):
                    raise OrchestrationError(
                        "CHANGE_SET_REQUIRED",
                        f"writing agent {node['id']!r} must declare the canonical changeset output",
                    )
                if not writing and change_contract is not None:
                    raise OrchestrationError(
                        "UNEXPECTED_CHANGE_SET",
                        f"read-only agent {node['id']!r} may not declare a changeset",
                    )
                self._profiles[node["id"]] = profile
                fallbacks: list[tuple[dict[str, Any], Profile]] = []
                for route in node.get("fallback", {}).get("routes", []):
                    fallback_profile = get_profile(
                        self.config,
                        route["profile"],
                        required=_required_capabilities(node),
                    )
                    if isinstance(fallback_profile.adapter, OpenAICompatibleAdapter):
                        raise OrchestrationError(
                            "UNSUPPORTED_ADAPTER",
                            f"fallback profile {fallback_profile.name!r} uses an unsupported adapter",
                        )
                    if (
                        isinstance(fallback_profile.adapter, A2AAdapter)
                        and fallback_profile.capabilities.mcp
                    ):
                        raise OrchestrationError(
                            "A2A_AUTHORITY",
                            f"remote fallback profile {fallback_profile.name!r} may not inherit local MCP authority",
                        )
                    if node.get("model") not in {None, fallback_profile.model}:
                        raise OrchestrationError(
                            "MODEL_MISMATCH",
                            f"fallback profile {fallback_profile.name!r} does not match the node model pin",
                        )
                    fallbacks.append((route, fallback_profile))
                self._fallback_profiles[node["id"]] = tuple(fallbacks)
                continue

            if node["kind"] == "integration":
                for name, binding in node["inputs"].items():
                    if not binding.endswith(f".{CHANGE_SET_OUTPUT}"):
                        raise OrchestrationError(
                            "INTEGRATION_INPUT",
                            f"integration input {name!r} must bind a changeset artifact",
                        )
                if len(node["outputs"]) != 1:
                    raise OrchestrationError(
                        "INTEGRATION_OUTPUT",
                        f"integration node {node['id']!r} must declare one result output",
                    )
                continue

            if node["kind"] == "approval":
                if node["id"] not in self.approvals:
                    raise OrchestrationError(
                        "APPROVAL_REQUIRED",
                        f"approval node {node['id']!r} needs an explicit result",
                    )
                continue

            if node["permission"] != "read":
                raise OrchestrationError(
                    "UNSAFE_DETERMINISTIC_PERMISSION",
                    f"custom node {node['id']!r} must be a pure read transform; "
                    "repository writes belong in an agent or integration node",
                )
            if node.get("operation") is not None:
                try:
                    builtin_executor(node["operation"])
                except BuiltinOperationError as exc:
                    raise OrchestrationError("INVALID_OPERATION", str(exc)) from exc
                continue
            if (
                node["id"] not in self.custom_executors
                and node["kind"] not in self.custom_executors
            ):
                raise OrchestrationError(
                    "MISSING_EXECUTOR",
                    f"node {node['id']!r} has no deterministic executor",
                )

    def _validate_check_argv(
        self, node: Mapping[str, Any], argv: Sequence[str]
    ) -> None:
        executable = Path(argv[0]).name.lower()
        if executable in _SHELLS or executable in _INDIRECT_EXECUTORS:
            raise OrchestrationError(
                "SHELL_CHECK_FORBIDDEN",
                f"node {node['id']!r} check invokes a shell or indirect launcher",
            )
        if executable in _DIRECT_SIDE_EFFECT_TOOLS:
            raise OrchestrationError(
                "UNSAFE_CHECK",
                f"node {node['id']!r} check directly invokes {executable!r}",
            )

    def _receipt_path(self, run_id: str) -> Path:
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        return self.artifact_root / "receipts" / f"{digest}.json"

    def _profile_manifest(self) -> dict[str, Any]:
        """Return the credential-free identity of every dispatched profile."""

        manifest = {
            node_id: {
                "profile": profile.name,
                "model": profile.model,
                "adapter_kind": profile.adapter_kind,
                "adapter": asdict(profile.adapter),
                "capabilities": asdict(profile.capabilities),
            }
            for node_id, profile in sorted(self._profiles.items())
        }
        for node_id, fallbacks in sorted(self._fallback_profiles.items()):
            for route, profile in fallbacks:
                manifest[f"{node_id}:fallback:{route['id']}"] = {
                    "profile": profile.name,
                    "model": profile.model,
                    "adapter_kind": profile.adapter_kind,
                    "adapter": asdict(profile.adapter),
                    "capabilities": asdict(profile.capabilities),
                    "on_codes": list(route["on_codes"]),
                    "max_uses": route["max_uses"],
                }
        return manifest

    def _bind_execution_identity(self, run_id: str) -> None:
        profiles = self._profile_manifest()
        encoded = canonical_json(profiles)
        digest = hashlib.sha256(encoded).hexdigest()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.state_path, timeout=30) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_manifests (
                    run_id TEXT PRIMARY KEY,
                    base_sha TEXT NOT NULL,
                    profile_manifest_sha256 TEXT NOT NULL,
                    profile_manifest_json TEXT NOT NULL,
                    project_policy_sha256 TEXT,
                    private_execution_sha256 TEXT,
                    repository_sha256 TEXT,
                    workflow_sha256 TEXT,
                    product_contract_sha256 TEXT,
                    product_contract_generation INTEGER
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(runtime_manifests)")
            }
            for name, kind in (
                ("project_policy_sha256", "TEXT"),
                ("private_execution_sha256", "TEXT"),
                ("repository_sha256", "TEXT"),
                ("workflow_sha256", "TEXT"),
                ("product_contract_sha256", "TEXT"),
                ("product_contract_generation", "INTEGER"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE runtime_manifests ADD COLUMN {name} {kind}"
                    )
            identity = self.execution_identity
            try:
                connection.execute(
                    "INSERT INTO runtime_manifests("
                    "run_id,base_sha,profile_manifest_sha256,profile_manifest_json,"
                    "project_policy_sha256,private_execution_sha256,repository_sha256,"
                    "workflow_sha256,product_contract_sha256,product_contract_generation) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        self.base_sha,
                        digest,
                        encoded.decode("utf-8"),
                        self.project_policy.digest if self.project_policy else None,
                        self.private_execution_digest,
                        identity.repository_digest if identity else None,
                        identity.workflow_digest if identity else None,
                        identity.product_contract_digest if identity else None,
                        identity.product_contract_generation if identity else None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise OrchestrationError(
                    "RUN_MANIFEST_EXISTS",
                    f"execution identity already exists for run {run_id!r}",
                ) from exc

    def _verify_execution_identity(self, run_id: str) -> None:
        try:
            with sqlite3.connect(self.state_path, timeout=30) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='runtime_manifests'"
                ).fetchone()
                row = (
                    connection.execute(
                        "SELECT base_sha,profile_manifest_sha256,profile_manifest_json,"
                        "project_policy_sha256,private_execution_sha256,repository_sha256,"
                        "workflow_sha256,product_contract_sha256,product_contract_generation "
                        "FROM runtime_manifests WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                    if table is not None
                    else None
                )
        except sqlite3.Error as exc:
            raise OrchestrationError(
                "CORRUPT_RUN_MANIFEST", "cannot read persisted execution identity"
            ) from exc
        if row is None:
            raise OrchestrationError(
                "RUN_MANIFEST_MISSING",
                f"run {run_id!r} has no persisted execution identity",
            )
        stored_base, stored_digest, stored_json = map(str, row[:3])
        try:
            stored_profiles = json.loads(stored_json)
            canonical_stored = canonical_json(stored_profiles)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OrchestrationError(
                "CORRUPT_RUN_MANIFEST", "persisted profile manifest is invalid"
            ) from exc
        if hashlib.sha256(canonical_stored).hexdigest() != stored_digest:
            raise OrchestrationError(
                "CORRUPT_RUN_MANIFEST", "persisted profile manifest digest is invalid"
            )
        if stored_base != self.base_sha:
            raise OrchestrationError(
                "BASE_SHA_MISMATCH",
                f"run is pinned to {stored_base}, not current base {self.base_sha}",
            )
        current_profiles = self._profile_manifest()
        current_digest = hashlib.sha256(canonical_json(current_profiles)).hexdigest()
        if stored_digest != current_digest:
            raise OrchestrationError(
                "PROFILE_MANIFEST_MISMATCH",
                "profile, model, adapter, or capability identity changed since launch",
            )
        identity = self.execution_identity
        expected = (
            self.project_policy.digest if self.project_policy else None,
            self.private_execution_digest,
            identity.repository_digest if identity else None,
            identity.workflow_digest if identity else None,
            identity.product_contract_digest if identity else None,
            identity.product_contract_generation if identity else None,
        )
        if tuple(row[3:]) != expected:
            raise OrchestrationError(
                "RUN_IDENTITY_MISMATCH",
                "project policy, private execution, repository, workflow, or product contract identity changed",
            )

    def _refresh_project_boundary(self) -> None:
        if self.project_policy is None:
            return
        try:
            # Preserve the stable drift taxonomy from the originally reviewed
            # boundary before parsing a potentially half-edited replacement.
            self.project_policy.preflight(self.workflow, base_sha=self.base_sha)
            current = load_project_policy(self.manager.repo)
            if current.digest != self.project_policy.digest:
                raise ProjectPolicyError(
                    "PROJECT_POLICY_DRIFT",
                    "checked-in project policy changed after validation",
                )
            current.preflight(self.workflow, base_sha=self.base_sha)
            binding = load_private_execution_binding(self.manager.repo)
            if binding.digest != self.private_execution_digest:
                raise ProjectPolicyError(
                    "PRIVATE_EXECUTION_DRIFT",
                    "private execution binding changed after validation",
                )
        except ProjectPolicyError as exc:
            raise OrchestrationError(exc.code, exc.message) from exc

    def _bind_receipt_ledger(self, run_id: str, digest: str) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.state_path, timeout=30) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS receipt_ledgers (
                    run_id TEXT PRIMARY KEY,
                    digest TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO receipt_ledgers(run_id, digest) VALUES(?, ?)
                ON CONFLICT(run_id) DO UPDATE SET digest=excluded.digest
                """,
                (run_id, digest),
            )

    def _receipt_binding(self, run_id: str) -> str | None:
        try:
            with sqlite3.connect(self.state_path, timeout=30) as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_ledgers'"
                ).fetchone()
                if table is None:
                    return None
                row = connection.execute(
                    "SELECT digest FROM receipt_ledgers WHERE run_id=?", (run_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise OrchestrationError(
                "CORRUPT_RECEIPT_LEDGER", "cannot read receipt ledger binding"
            ) from exc
        return None if row is None else str(row[0])

    def _verify_receipt_binding(self, run_id: str, payload: bytes) -> None:
        expected = self._receipt_binding(run_id)
        actual = hashlib.sha256(payload).hexdigest()
        if expected is None or expected != actual:
            raise OrchestrationError(
                "CORRUPT_RECEIPT_LEDGER",
                "receipt ledger does not match its durable run-state binding",
            )

    def _decode_receipt_ledger(
        self, payload: bytes, run_id: str
    ) -> tuple[dict[str, ExecutionReceipt], list[CheckCommandReceipt]]:
        try:
            envelope = json.loads(payload)
            if not isinstance(envelope, dict) or set(envelope) != {"body", "digest"}:
                raise ValueError("invalid receipt ledger envelope")
            body = envelope["body"]
            expected = envelope["digest"]
            actual = hashlib.sha256(canonical_json(body)).hexdigest()
            if not isinstance(expected, str) or expected != actual:
                raise ValueError("receipt ledger digest mismatch")
            if (
                not isinstance(body, dict)
                or body.get("version") != 1
                or body.get("run_id") != run_id
                or not isinstance(body.get("agent_receipts"), dict)
                or not isinstance(body.get("check_receipts"), list)
            ):
                raise ValueError("invalid receipt ledger body")
            agents = {
                key: ExecutionReceipt(**value)
                for key, value in body["agent_receipts"].items()
            }
            checks = [CheckCommandReceipt(**value) for value in body["check_receipts"]]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OrchestrationError(
                "CORRUPT_RECEIPT_LEDGER",
                f"cannot load durable receipts for run {run_id!r}",
            ) from exc
        if any(receipt.run_id != run_id for receipt in agents.values()) or any(
            receipt.run_id != run_id for receipt in checks
        ):
            raise OrchestrationError(
                "CORRUPT_RECEIPT_LEDGER", "receipt run identifiers do not match"
            )
        return agents, checks

    def _merge_receipts(
        self,
        run_id: str,
        agents: Mapping[str, ExecutionReceipt],
        checks: Sequence[CheckCommandReceipt],
    ) -> tuple[dict[str, ExecutionReceipt], list[CheckCommandReceipt]]:
        merged_agents = dict(agents)
        for key, receipt in self._agent_receipts.items():
            if receipt.run_id != run_id:
                continue
            prior = merged_agents.get(key)
            if prior is not None and prior != receipt:
                raise OrchestrationError(
                    "RECEIPT_CONFLICT", f"agent receipt {key!r} changed after recording"
                )
            merged_agents[key] = receipt

        by_check = {
            (receipt.node_id, receipt.attempt, receipt.check_id): receipt
            for receipt in checks
        }
        for receipt in self._check_receipts:
            if receipt.run_id != run_id:
                continue
            key = (receipt.node_id, receipt.attempt, receipt.check_id)
            prior = by_check.get(key)
            if prior is not None and prior != receipt:
                raise OrchestrationError(
                    "RECEIPT_CONFLICT", f"check receipt {key!r} changed after recording"
                )
            by_check[key] = receipt
        return merged_agents, [by_check[key] for key in sorted(by_check)]

    def _persist_receipts_locked(self, run_id: str) -> None:
        path = self._receipt_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(".lock")
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            if path.exists():
                existing_payload = path.read_bytes()
                self._verify_receipt_binding(run_id, existing_payload)
                existing_agents, existing_checks = self._decode_receipt_ledger(
                    existing_payload, run_id
                )
            else:
                existing_agents, existing_checks = {}, []
            agents, checks = self._merge_receipts(
                run_id, existing_agents, existing_checks
            )
            body = {
                "version": 1,
                "run_id": run_id,
                "agent_receipts": {
                    key: asdict(receipt) for key, receipt in sorted(agents.items())
                },
                "check_receipts": [asdict(receipt) for receipt in checks],
            }
            payload = canonical_json(
                {
                    "body": body,
                    "digest": hashlib.sha256(canonical_json(body)).hexdigest(),
                }
            )
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                directory = os.open(
                    path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                self._bind_receipt_ledger(run_id, hashlib.sha256(payload).hexdigest())
            finally:
                temporary.unlink(missing_ok=True)
            self._agent_receipts.update(agents)
            known = {
                (receipt.run_id, receipt.node_id, receipt.attempt, receipt.check_id)
                for receipt in self._check_receipts
            }
            self._check_receipts.extend(
                receipt
                for receipt in checks
                if (
                    receipt.run_id,
                    receipt.node_id,
                    receipt.attempt,
                    receipt.check_id,
                )
                not in known
            )

    def _load_receipts(self, run_id: str) -> None:
        with self._lock:
            if run_id in self._receipts_loaded:
                return
            path = self._receipt_path(run_id)
            if not path.exists():
                if self._receipt_binding(run_id) is not None:
                    raise OrchestrationError(
                        "CORRUPT_RECEIPT_LEDGER",
                        "receipt ledger is missing but its run-state binding exists",
                    )
                self._receipts_loaded.add(run_id)
                return
            try:
                lock_path = path.with_suffix(".lock")
                with lock_path.open("a+b") as lock_handle:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_SH)
                    payload = path.read_bytes()
                    self._verify_receipt_binding(run_id, payload)
                    agents, checks = self._decode_receipt_ledger(payload, run_id)
            except OSError as exc:
                raise OrchestrationError(
                    "CORRUPT_RECEIPT_LEDGER",
                    f"cannot load durable receipts for run {run_id!r}",
                ) from exc
            self._agent_receipts.update(agents)
            known = {
                (receipt.run_id, receipt.node_id, receipt.attempt, receipt.check_id)
                for receipt in self._check_receipts
            }
            self._check_receipts.extend(
                receipt
                for receipt in checks
                if (
                    receipt.run_id,
                    receipt.node_id,
                    receipt.attempt,
                    receipt.check_id,
                )
                not in known
            )
            self._receipts_loaded.add(run_id)

    def _record_agent_receipt(self, key: str, receipt: ExecutionReceipt) -> None:
        with self._lock:
            self._agent_receipts[key] = receipt
            self._persist_receipts_locked(receipt.run_id)

    def _record_check_receipt(self, receipt: CheckCommandReceipt) -> None:
        with self._lock:
            self._check_receipts.append(receipt)
            self._persist_receipts_locked(receipt.run_id)
            LifecycleStore(self.state_path).append_if_active(
                receipt.run_id,
                f"check:{receipt.node_id}:{receipt.attempt}:{receipt.check_id}",
                "check.completed",
                node_id=receipt.node_id,
                attempt=receipt.attempt,
                payload={
                    "check_id": receipt.check_id,
                    "exit_code": receipt.exit_code,
                    "stdout_digest": receipt.stdout_digest,
                    "stderr_digest": receipt.stderr_digest,
                },
            )

    def _prompt(self, node: Mapping[str, Any], context: ExecutionContext) -> str:
        inputs = canonical_json(dict(context.inputs)).decode("utf-8")
        output_name = next(
            name for name in node["outputs"] if name != CHANGE_SET_OUTPUT
        )
        schema = node["outputs"][output_name]["schema"]
        if not isinstance(schema, dict):
            raise OrchestrationError(
                "EXTERNAL_SCHEMA_UNSUPPORTED",
                f"agent output {node['id']}.{output_name} uses an external schema",
            )
        schema_json = canonical_json(schema).decode("utf-8")
        idempotency = (
            "Stable idempotency key for this run/node (reuse it for every external "
            f"write): {context.idempotency_key}\n\n"
            if context.idempotency_key is not None
            else ""
        )
        return (
            f"{node['task']}\n\n"
            f"{idempotency}"
            f"Explicit input artifacts (JSON):\n{inputs}\n\n"
            f"Required output contract for {output_name!r} (JSON Schema):\n"
            f"{schema_json}\n\n"
            "Return only one JSON value matching that contract. Do not wrap it in "
            "Markdown, prose, or a provider-specific envelope."
        )

    def _agent_executor(self, node: dict[str, Any]) -> Executor:
        primary = self._profiles[node["id"]]
        fallbacks = self._fallback_profiles.get(node["id"], ())

        def execute(context: ExecutionContext) -> Mapping[str, Any]:
            if context.cancelled():
                raise OrchestrationError(
                    "PROGRESS_DEADLINE_EXCEEDED",
                    "node reached its effective deadline before adapter dispatch",
                )
            key = (node["id"], context.attempt)
            output_name = next(
                name for name in node["outputs"] if name != CHANGE_SET_OUTPUT
            )
            schema = node["outputs"][output_name]["schema"]
            if not isinstance(schema, dict):
                raise OrchestrationError(
                    "EXTERNAL_SCHEMA_UNSUPPORTED",
                    f"agent output {node['id']}.{output_name} uses an external schema",
                )
            candidates: list[tuple[str, Profile, frozenset[str] | None]] = [
                ("primary", primary, None)
            ]
            candidates.extend(
                (route["id"], profile, frozenset(route["on_codes"]))
                for route, profile in fallbacks
            )
            last_error: AdapterError | None = None
            result = None
            selected_profile = primary
            worktree = None
            for candidate_index, (route_id, profile, on_codes) in enumerate(candidates):
                if on_codes is not None and (
                    last_error is None or last_error.code not in on_codes
                ):
                    continue
                attempt_id = _attempt_name(node["id"], context.attempt)
                if fallbacks:
                    attempt_id = f"{attempt_id}-f{candidate_index}"
                worktree = self.manager.create(
                    context.run_id, attempt_id, base=self.base_sha
                )
                request = AdapterRequest(
                    prompt=self._prompt(node, context),
                    cwd=worktree.path,
                    allowed_root=worktree.path,
                    node_id=node["id"],
                    run_id=context.run_id,
                    attempt=context.attempt,
                    result_schema=schema,
                    base_sha=self.base_sha,
                    idempotency_key=context.idempotency_key,
                    changeset_schema=(
                        CHANGE_SET_SCHEMA
                        if node["permission"] in {"write", "destructive"}
                        and isinstance(profile.adapter, A2AAdapter)
                        else None
                    ),
                    state_path=self.state_path,
                    cancelled=context.cancelled,
                    dispatch_guard=self._refresh_project_boundary,
                )
                try:
                    node_timeout = node.get(
                        "timeout_seconds", self.workflow["budgets"]["timeout_seconds"]
                    )
                    effective_timeout = min(
                        self.agent_limits.timeout_seconds, float(node_timeout)
                    )
                    if context.deadline_at is not None:
                        remaining = context.deadline_at - time.time()
                        if remaining <= 0:
                            raise OrchestrationError(
                                "PROGRESS_DEADLINE_EXCEEDED",
                                "node reached its effective deadline before adapter dispatch",
                            )
                        effective_timeout = min(effective_timeout, remaining)
                    limits = ExecutionLimits(
                        timeout_seconds=effective_timeout,
                        terminate_grace_seconds=self.agent_limits.terminate_grace_seconds,
                        max_stdout_bytes=self.agent_limits.max_stdout_bytes,
                        max_stderr_bytes=self.agent_limits.max_stderr_bytes,
                    )
                    result = execute_profile(
                        profile,
                        request,
                        limits=limits,
                        environ=self.environ,
                    )
                except AdapterError as exc:
                    if exc.receipt is not None:
                        receipt_key = f"{node['id']}#{context.attempt}"
                        if fallbacks:
                            receipt_key = f"{receipt_key}:{route_id}"
                        self._record_agent_receipt(receipt_key, exc.receipt)
                    last_error = exc
                    continue
                receipt_key = f"{node['id']}#{context.attempt}"
                if fallbacks:
                    receipt_key = f"{receipt_key}:{route_id}"
                self._record_agent_receipt(receipt_key, result.receipt)
                selected_profile = profile
                break
            if result is None or worktree is None:
                assert last_error is not None
                raise last_error
            with self._lock:
                self._worktrees[key] = worktree
            if context.cancelled():
                raise OrchestrationError(
                    "PROGRESS_DEADLINE_EXCEEDED",
                    "adapter result arrived after the effective node deadline",
                )

            writing = node["permission"] in {"write", "destructive"}
            scope = node.get("write_scope", []) if writing else []
            if writing and isinstance(selected_profile.adapter, A2AAdapter):
                change = change_set_from_value(result.changeset)
                self.manager.apply(worktree.path, change, write_scope=scope)
            change = self.manager.capture(worktree, write_scope=scope)
            with self._lock:
                self._snapshots[key] = change
            outputs: dict[str, Any] = {output_name: result.value}
            if writing:
                outputs[CHANGE_SET_OUTPUT] = change_set_value(change)
            return outputs

        return execute

    def _integration_executor(self, node: dict[str, Any]) -> Executor:
        def execute(context: ExecutionContext) -> Mapping[str, Any]:
            if context.cancelled():
                raise OrchestrationError(
                    "PROGRESS_DEADLINE_EXCEEDED",
                    "integration reached its effective deadline before execution",
                )
            attempt_id = _attempt_name(node["id"], context.attempt)
            worktree = self.manager.create(
                context.run_id, attempt_id, base=self.base_sha
            )
            key = (node["id"], context.attempt)
            with self._lock:
                self._worktrees[key] = worktree
            integration_scope = node.get("write_scope", [])
            change_digests: list[str] = []
            all_paths: set[str] = set()
            for input_name, binding in sorted(node["inputs"].items()):
                if context.cancelled():
                    raise OrchestrationError(
                        "PROGRESS_DEADLINE_EXCEEDED",
                        "integration reached its effective deadline while applying inputs",
                    )
                producer_id, _ = binding.split(".", 1)
                change = change_set_from_value(context.inputs[input_name])
                if change.base_sha != self.base_sha:
                    raise OrchestrationError(
                        "CHANGE_SET_BASE_MISMATCH",
                        f"{producer_id!r} artifact is not based on the pinned run SHA",
                    )
                producer_scope = self.nodes[producer_id].get("write_scope", [])
                escaped_producer = [
                    path
                    for path in change.changed_paths
                    if not any(
                        fnmatch.fnmatchcase(path, pattern) for pattern in producer_scope
                    )
                ]
                if escaped_producer:
                    raise OrchestrationError(
                        "PRODUCER_SCOPE_ESCAPE",
                        f"{producer_id!r} artifact escaped its declared scope: {escaped_producer}",
                    )
                self.manager.apply(worktree.path, change, write_scope=integration_scope)
                change_digests.append(change.digest)
                all_paths.update(change.changed_paths)

            snapshot = self.manager.capture(worktree, write_scope=integration_scope)
            if context.cancelled():
                raise OrchestrationError(
                    "PROGRESS_DEADLINE_EXCEEDED",
                    "integration result arrived after the effective node deadline",
                )
            with self._lock:
                self._snapshots[key] = snapshot
            output_name = next(iter(node["outputs"]))
            return {
                output_name: {
                    "base_sha": self.base_sha,
                    "changed_paths": sorted(all_paths),
                    "change_digests": sorted(change_digests),
                    "integration_digest": snapshot.digest,
                }
            }

        return execute

    def _workspace_for(self, context: ExecutionContext) -> Path:
        with self._lock:
            worktree = self._worktrees.get((context.node_id, context.attempt))
        return worktree.path if worktree is not None else self.manager.repo

    def _recover_worktrees(self, result: RunResult) -> None:
        """Reattach retained deterministic worktree paths in a fresh resume process."""

        for node_id, node in self.nodes.items():
            if node["kind"] not in {"agent", "integration"}:
                continue
            row = result.nodes[node_id]
            if row["status"] != "succeeded":
                continue
            accepted_attempt = int(row["attempt_count"])
            for attempt in (accepted_attempt,):
                attempt_id = _attempt_name(node_id, attempt)
                path = (self.manager.root / result.run_id / attempt_id).resolve()
                if not path.is_relative_to(self.manager.root) or not path.is_dir():
                    continue
                record = Worktree(
                    run_id=result.run_id,
                    node_id=attempt_id,
                    path=path,
                    branch=f"graph/{result.run_id}/{attempt_id}",
                    base_sha=self.base_sha,
                )
                scope = (
                    node.get("write_scope", [])
                    if node["permission"] in {"write", "destructive"}
                    else []
                )
                try:
                    snapshot = self.manager.capture(record, write_scope=scope)
                except WorktreeError as exc:
                    raise OrchestrationError(
                        "WORKTREE_RECOVERY_FAILED",
                        f"retained worktree for {node_id!r} is no longer trustworthy",
                    ) from exc
                with self._lock:
                    self._worktrees[(node_id, attempt)] = record
                    self._snapshots[(node_id, attempt)] = snapshot

    def _check_runner(
        self,
        check: dict[str, Any],
        context: ExecutionContext,
        _outputs: Mapping[str, Any],
    ) -> CheckResult:
        node = self.nodes[context.node_id]
        cwd = self._workspace_for(context).resolve(strict=True)
        allowed = (
            self.manager.root if cwd != self.manager.repo else self.manager.repo
        ).resolve(strict=True)
        if not cwd.is_relative_to(allowed):
            raise OrchestrationError("CHECK_CWD_ESCAPE", "check cwd escaped its root")
        env = {
            name: self.environ[name]
            for name in sorted(SAFE_ENV)
            if name in self.environ
        }
        timeout = check.get("timeout_seconds", node.get("timeout_seconds", 900))
        if context.deadline_at is not None:
            remaining = context.deadline_at - time.time()
            if remaining <= 0:
                raise OrchestrationError(
                    "PROGRESS_DEADLINE_EXCEEDED",
                    "deterministic check reached the effective node deadline",
                )
            timeout = min(float(timeout), remaining)
        limits = ExecutionLimits(
            timeout_seconds=timeout,
            terminate_grace_seconds=2,
            max_stdout_bytes=2 * 1024 * 1024,
            max_stderr_bytes=2 * 1024 * 1024,
        )
        completed = _run_process(
            check["argv"], cwd=cwd, env=env, stdin=None, limits=limits
        )
        receipt = CheckCommandReceipt(
            run_id=context.run_id,
            node_id=context.node_id,
            attempt=context.attempt,
            check_id=check["id"],
            command_digest=hashlib.sha256(
                "\0".join(check["argv"]).encode("utf-8")
            ).hexdigest(),
            cwd=str(cwd.relative_to(self.manager.repo)),
            exit_code=completed.exit_code,
            stdout_digest=hashlib.sha256(completed.stdout).hexdigest(),
            stderr_digest=hashlib.sha256(completed.stderr).hexdigest(),
            stdout_bytes=len(completed.stdout),
            stderr_bytes=len(completed.stderr),
        )
        self._record_check_receipt(receipt)

        key = (context.node_id, context.attempt)
        with self._lock:
            expected = self._snapshots.get(key)
            worktree = self._worktrees.get(key)
        if expected is not None and worktree is not None:
            scope = node.get("write_scope", [])
            current = self.manager.capture(worktree, write_scope=scope)
            if current.digest != expected.digest:
                return CheckResult(False, "check mutated the accepted workspace")

        failure = completed.failure_code
        if failure is not None:
            return CheckResult(False, f"{failure}: {completed.failure_message}")
        stderr = completed.stderr.decode("utf-8", errors="replace")[-2000:]
        stdout = completed.stdout.decode("utf-8", errors="replace")[-2000:]
        if completed.exit_code != 0:
            return CheckResult(
                False,
                f"exit={completed.exit_code}; stdout={stdout!r}; stderr={stderr!r}",
            )
        return CheckResult(True, f"exit=0; stdout={stdout!r}; stderr={stderr!r}")

    def _executors(self) -> dict[str, Executor]:
        result = dict(self.custom_executors)
        for node in self.nodes.values():
            if node["kind"] == "agent":
                result[node["id"]] = self._agent_executor(node)
            elif node["kind"] == "integration":
                result[node["id"]] = self._integration_executor(node)
            elif node["kind"] == "approval":
                approved = dict(self.approvals[node["id"]])
                result[node["id"]] = lambda _context, value=approved: dict(value)
            elif node.get("operation") is not None:
                result[node["id"]] = builtin_executor(node["operation"])
        return result

    def run(
        self, run_id: str | None = None, *, resume: bool = False
    ) -> OrchestrationResult:
        """Run or resume the graph; artifacts persist and receipts are returned."""

        with self._run_lock:
            return self._run_once(run_id, resume=resume)

    def _run_once(self, run_id: str | None, *, resume: bool) -> OrchestrationResult:
        """Execute one run while the public method prevents shared-map races."""

        self._refresh_project_boundary()

        scheduler = Scheduler(
            self.workflow,
            self.state_path,
            self.artifact_root,
            self._executors(),
            self._check_runner,
        )
        lifecycle = LifecycleStore(self.state_path)
        if resume:
            if run_id is None:
                raise ValueError("resume requires run_id")
            if scheduler.state.run(run_id)["workflow"] != self.workflow:
                raise ValueError("resume workflow does not match persisted workflow")
            self._verify_execution_identity(run_id)
            self._load_receipts(run_id)
            actual_run_id = run_id
        else:
            actual_run_id = scheduler.state.create_run(
                self.workflow, run_id or uuid.uuid4().hex, lifecycle=True
            )
            self._bind_execution_identity(actual_run_id)
        context_provider = self.context_provider
        try:
            lineage = verify_lineage(self.state_path, actual_run_id)
        except ForkError as exc:
            raise OrchestrationError(exc.code, exc.message) from exc
        if lineage is not None:
            context_provider = StaticRunContextProvider(
                {**self.context_provider.provide(), "fork_lineage": lineage}
            )
        run_context = lifecycle.initialize_context(
            actual_run_id,
            context_provider,
            allow_legacy_bootstrap=resume and self.bootstrap_legacy_lifecycle,
        )
        if lineage is not None:
            scheduler.state.activate_fork(actual_run_id)
        # The portable layer creates and binds a run before any executor can spawn;
        # Scheduler then enters through its verified resume path in both cases.
        result = scheduler.run(actual_run_id, resume=True, lifecycle_resume=resume)
        if resume:
            self._recover_worktrees(result)
        outputs: dict[str, Any] = {}
        for name, binding in self.workflow["outputs"].items():
            node_id, output_name = binding.split(".", 1)
            record = scheduler.state.artifact(result.run_id, node_id, output_name)
            if record is None:
                continue
            schema = self.nodes[node_id]["outputs"][output_name]["schema"]
            if not isinstance(schema, dict):
                raise ArtifactError(f"external schema is unsupported: {binding}")
            outputs[name] = scheduler.artifacts.get(record["digest"], schema).value
        with self._lock:
            paths = {
                f"{node_id}#{attempt}": record.path
                for (node_id, attempt), record in self._worktrees.items()
                if record.run_id == result.run_id
            }
            agent_receipts = {
                key: receipt
                for key, receipt in self._agent_receipts.items()
                if receipt.run_id == result.run_id
            }
            check_receipts = tuple(
                receipt
                for receipt in self._check_receipts
                if receipt.run_id == result.run_id
            )
        run_context, lifecycle_events = lifecycle.snapshot(result.run_id)
        return OrchestrationResult(
            run=result,
            outputs=outputs,
            agent_receipts=agent_receipts,
            check_receipts=check_receipts,
            worktrees=paths,
            run_context=run_context,
            lifecycle_events=lifecycle_events,
        )


__all__ = [
    "CHANGE_SET_OUTPUT",
    "CHANGE_SET_SCHEMA",
    "CheckCommandReceipt",
    "OrchestrationError",
    "OrchestrationResult",
    "PortableRuntime",
    "RunContextProvider",
    "change_set_from_value",
    "change_set_value",
]
