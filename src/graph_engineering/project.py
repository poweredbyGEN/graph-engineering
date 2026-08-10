"""Checked-in project policy and frozen product-contract boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import tempfile
import time
import tomllib
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .artifacts import canonical_json
from .contracts import validate_workflow

PROJECT_VERSION = "graph-engineering/project/v1"
PRODUCT_CONTRACT_VERSION = "graph-engineering/product-contract/v2"
ASSESSMENT_VERSION = "graph-engineering/assessment/v1"
PROJECT_MANIFEST = Path(".graph-engineering/project.json")
WORKFLOW_DIRECTORY = Path(".graph-engineering/workflows")
PRIVATE_PROJECT_CONFIG = Path(".graph-engineering.local.toml")
PROJECT_BRIEF = Path(".graph-engineering/PROJECT.md")
DECISION_INDEX = Path(".graph-engineering/decisions/README.md")
_PROJECT_BRIEF_TEMPLATE = """# Project capsule

Status: **DRAFT — do not fan out**

This is the canonical entrypoint for future agents. The machine authority is
[`product-contract.json`](product-contract.json); this brief must summarize the same generation.
Repository policy is in [`project.json`](project.json), append-only decisions are indexed in
[`decisions/README.md`](decisions/README.md), and executable graphs live in [`workflows/`](workflows/).

## Problem, users, outcomes

UNRESOLVED: state the problem, target users, and measurable outcomes.

## Scope and journeys

UNRESOLVED: state in-scope work, explicit exclusions, and end-to-end journeys.

## Surfaces, data, and permissions

UNRESOLVED: cover UI, API, events, jobs, integrations, tables, stores, migrations, auth, and
permissions. Use a reasoned N/A where an axis does not apply.

## Invariants, compatibility, and recovery

UNRESOLVED: state invariants, compatibility commitments, failure behavior, and recovery paths.

## Delivery and proof

UNRESOLVED: state rollout, rollback, live proof, risks, assumptions/hypotheses, open decisions with
owners, and acceptance criteria. Each criterion needs deterministic argv or an explicit human gate.

## Freeze

UNRESOLVED: a named human must approve this generation before dependency audit or fan-out.
"""

_DECISION_INDEX_TEMPLATE = """# Decision index

Append decisions; never rewrite an accepted record silently. Add `NNNN-short-title.md`, then append
one row here. Product-contract open decisions name an owner until resolved by one of these records.

| ID | Status | Owner | Decision | Record |
|---|---|---|---|---|

Record template (copy into a new numbered file):

```markdown
# NNNN: Title
Status: proposed | accepted | superseded
Owner: name
Context: why this decision exists
Decision: what was chosen
Consequences: compatibility, rollout, rollback, and follow-up
Supersedes: none | NNNN
```
"""
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DEPLOY_WORD = re.compile(r"\b(deploy|release|publish)\b", re.IGNORECASE)
_SCP_WORD = re.compile(r"\bscp\b", re.IGNORECASE)
_SCP_REMOTE = re.compile(
    r"^(?:[^/@:\s]+@)?(?P<host>\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):(?P<path>.+)$"
)


class ProjectPolicyError(RuntimeError):
    """A checked-in project contract is absent, malformed, stale, or unsafe."""

    def __init__(self, code: str, message: str, *, path: str | None = None):
        self.code = code
        self.message = message
        self.path = path
        super().__init__(f"{code}: {message}")


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectPolicyError(
            "PROJECT_READ_ERROR", str(exc), path=str(path)
        ) from exc
    if not isinstance(value, dict):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", "document must be a JSON object", path=str(path)
        )
    return value


def _exact_keys(value: Mapping[str, Any], required: set[str], path: str) -> None:
    actual = set(value)
    if actual != required:
        raise ProjectPolicyError(
            "PROJECT_SCHEMA",
            f"{path} fields must be exactly {sorted(required)}; got {sorted(actual)}",
        )


def _bounded_string(value: Any, path: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", f"{path} must be a non-empty string <= {maximum} bytes"
        )
    return value


def _git(repo: Path, *args: str, required: bool = True) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repo), *args),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        if not required:
            return ""
        raise ProjectPolicyError(
            "REPOSITORY_INVALID", completed.stderr.strip() or "git command failed"
        )
    return completed.stdout.strip()


def discover_repo(start: str | Path) -> Path:
    requested = Path(start).expanduser().resolve(strict=True)
    root = _git(requested, "rev-parse", "--show-toplevel")
    repo = Path(root).resolve(strict=True)
    if requested != repo:
        raise ProjectPolicyError(
            "WRONG_REPOSITORY_ROOT",
            f"--repo must name the canonical git root {repo}, not {requested}",
        )
    return repo


def _remote_review_required() -> ProjectPolicyError:
    return ProjectPolicyError(
        "REMOTE_REVIEW_REQUIRED",
        "origin cannot be reduced to a safe public repository identity; review it before init",
    )


def _public_remote_identity(value: str, *, allow_empty: bool = False) -> str:
    """Return only a public repository identity, never remote authentication."""

    if not value and allow_empty:
        return ""
    if value == "UNRESOLVED":
        return value
    if (
        not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "\\" in value
        or "?" in value
        or "#" in value
    ):
        raise _remote_review_required()

    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise _remote_review_required() from exc
    if parsed.scheme:
        if parsed.scheme.lower() not in {"https", "ssh"} or not hostname:
            raise _remote_review_required()
        decoded_path = urllib.parse.unquote(parsed.path)
        path = parsed.path.rstrip("/").removesuffix(".git")
        if (
            not path.startswith("/")
            or path == "/"
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in decoded_path
            )
            or ".." in decoded_path.split("/")
        ):
            raise _remote_review_required()
        host = hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        authority = f"{host}:{port}" if port is not None else host
        return f"{parsed.scheme.lower()}://{authority}{path}"

    match = _SCP_REMOTE.fullmatch(value)
    if match is None:
        raise _remote_review_required()
    path = match.group("path").rstrip("/").removesuffix(".git")
    if not path or ".." in path.split("/"):
        raise _remote_review_required()
    return f"{match.group('host').lower()}:{path}"


def _public_repository_key(value: str) -> Mapping[str, Any] | None:
    """Reduce a supported remote to a credential- and transport-free identity."""

    public = _public_remote_identity(value, allow_empty=True)
    if not public or public == "UNRESOLVED":
        return None
    if "://" in public:
        parsed = urllib.parse.urlsplit(public)
        host = parsed.hostname
        if host is None:  # pragma: no cover - already enforced by the sanitizer
            raise _remote_review_required()
        port = parsed.port
        if (parsed.scheme == "https" and port == 443) or (
            parsed.scheme == "ssh" and port == 22
        ):
            port = None
        path = urllib.parse.unquote(parsed.path).lstrip("/")
    else:
        match = _SCP_REMOTE.fullmatch(public)
        if match is None:  # pragma: no cover - already enforced by the sanitizer
            raise _remote_review_required()
        host = match.group("host").strip("[]").lower()
        port = None
        path = urllib.parse.unquote(match.group("path")).lstrip("/")
    if not host or not path:
        raise _remote_review_required()
    return {
        "kind": "public-remote/v1",
        "host": host.lower(),
        "port": port,
        "path": path,
    }


def _unresolved_repository_key(repo: Path) -> Mapping[str, Any]:
    """Return a bounded path-free fallback for repositories without an origin.

    This identity is sufficient to bind a read-only assessment across checkout renames. A
    checked-in project still records ``repository.canonical_remote`` as unresolved and therefore
    fails preflight before execution, so a coincidentally shared root commit cannot authorize work.
    """

    roots = _git(repo, "rev-list", "--max-parents=0", "HEAD").splitlines()
    if (
        not roots
        or len(roots) > 64
        or any(not re.fullmatch(r"[0-9a-f]{40,64}", root) for root in roots)
    ):
        raise ProjectPolicyError(
            "REPOSITORY_IDENTITY_UNRESOLVED",
            "repository has no safe public remote or bounded git-root identity",
        )
    return {"kind": "git-root/v1", "roots": sorted(set(roots))}


def repository_digest(repo: Path) -> str:
    """Hash only portable public repository identity, never checkout or credential data."""

    remote = _git(repo, "remote", "get-url", "origin", required=False).strip()
    identity = _public_repository_key(remote)
    if identity is None:
        identity = _unresolved_repository_key(repo)
    return hashlib.sha256(canonical_json(identity)).hexdigest()


def assessment_source(repo: Path) -> Mapping[str, str]:
    """Hash a bounded tracked + relevant-untracked tree for assessment freshness."""

    head_sha = _git(repo, "rev-parse", "HEAD")
    tracked = set(filter(None, _git(repo, "ls-files", "-z").split("\0")))
    untracked = set(
        filter(
            None,
            _git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0"),
        )
    )
    paths = sorted(tracked | untracked)
    if len(paths) > 20_000:
        raise ProjectPolicyError(
            "ASSESSMENT_SOURCE_LIMIT", "assessment source exceeds 20,000 files"
        )
    records: list[Mapping[str, Any]] = []
    total_bytes = 0
    for relative in paths:
        unresolved = repo / relative
        if unresolved.is_symlink():
            # Git stores a symlink (mode 120000) as a blob holding the target
            # path text; snapshot exactly that and never follow the link.
            payload = os.readlink(unresolved).encode("utf-8", errors="surrogateescape")
            total_bytes += len(payload)
            records.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "tracked": relative in tracked,
                }
            )
            continue
        path = unresolved.resolve()
        if path != repo and not path.is_relative_to(repo):
            raise ProjectPolicyError(
                "ASSESSMENT_SOURCE_ESCAPE",
                f"source path escapes repository: {relative}",
            )
        if path.is_dir():
            # Gitlink (submodule) entries appear in `git ls-files` as tracked
            # paths but are directories on disk; their content belongs to the
            # sub-repository, not this snapshot.
            continue
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ProjectPolicyError(
                "ASSESSMENT_SOURCE_READ", str(exc), path=str(path)
            ) from exc
        total_bytes += len(payload)
        if len(payload) > 5 * 1024 * 1024 or total_bytes > 64 * 1024 * 1024:
            raise ProjectPolicyError(
                "ASSESSMENT_SOURCE_LIMIT",
                "assessment source exceeds the 5 MiB file or 64 MiB total bound",
            )
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "tracked": relative in tracked,
            }
        )
    source_digest = hashlib.sha256(
        canonical_json({"head_sha": head_sha, "records": records})
    ).hexdigest()
    return {"head_sha": head_sha, "source_digest": source_digest}


@dataclass(frozen=True)
class FrozenProductContract:
    version: str
    generation: int
    digest: str
    path: Path
    value: Mapping[str, Any]
    sources: tuple[tuple[Path, str], ...]


@dataclass(frozen=True)
class PrivateExecutionBinding:
    """Credential-free identity for a private host/checkout authorization."""

    digest: str


@dataclass(frozen=True)
class ExecutionIdentity:
    repository_digest: str
    base_sha: str
    product_contract_digest: str
    product_contract_generation: int
    workflow_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository_digest": self.repository_digest,
            "base_sha": self.base_sha,
            "product_contract_digest": self.product_contract_digest,
            "product_contract_generation": self.product_contract_generation,
            "workflow_digest": self.workflow_digest,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict())).hexdigest()


@dataclass(frozen=True)
class ProjectPolicy:
    repo: Path
    path: Path
    digest: str
    canonical_remote: str
    allowed_roots: tuple[str, ...]
    base_branch: str
    routing: Mapping[str, str]
    deploy_adapter: str
    deploy_targets: tuple[str, ...]
    prohibited_operations: frozenset[str]
    required_checks: tuple[Mapping[str, Any], ...]
    live_required: bool
    live_checks: tuple[Mapping[str, Any], ...]
    unresolved: tuple[str, ...]
    product_contract: FrozenProductContract

    def _assert_current(self) -> None:
        current_manifest = _read_object(self.path)
        if hashlib.sha256(canonical_json(current_manifest)).hexdigest() != self.digest:
            raise ProjectPolicyError(
                "PROJECT_POLICY_DRIFT",
                "checked-in project policy changed after validation",
                path=str(self.path),
            )
        current = _read_object(self.product_contract.path)
        digest = hashlib.sha256(canonical_json(current)).hexdigest()
        if digest != self.product_contract.digest:
            raise ProjectPolicyError(
                "PRODUCT_CONTRACT_DRIFT",
                "frozen product contract changed after policy load; invalidate derived work explicitly",
                path=str(self.product_contract.path),
            )
        for path, expected_digest in self.product_contract.sources:
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ProjectPolicyError(
                    "PRODUCT_SOURCE_DRIFT",
                    "frozen planning source is unreadable",
                    path=str(path),
                ) from exc
            if digest != expected_digest:
                raise ProjectPolicyError(
                    "PRODUCT_SOURCE_DRIFT",
                    "frozen planning source changed; approve a new generation before fan-out",
                    path=str(path),
                )

    def preflight(self, workflow: Mapping[str, Any], *, base_sha: str) -> None:
        """Reject project, contract, base, and effect drift before any worker starts."""

        if self.unresolved:
            raise ProjectPolicyError(
                "PROJECT_POLICY_UNRESOLVED",
                "review these fields before launch: " + ", ".join(self.unresolved),
                path=str(self.path),
            )
        self._assert_current()
        actual_remote = _public_remote_identity(
            _git(self.repo, "remote", "get-url", "origin", required=False),
            allow_empty=True,
        )
        if actual_remote != self.canonical_remote:
            raise ProjectPolicyError(
                "WRONG_REPOSITORY",
                "git origin does not match the checked-in canonical repository",
                path=str(self.repo),
            )
        remote_base = _git(
            self.repo,
            "rev-parse",
            "--verify",
            f"refs/remotes/origin/{self.base_branch}",
            required=False,
        )
        expected_base = remote_base or _git(
            self.repo, "rev-parse", "--verify", f"refs/heads/{self.base_branch}"
        )
        if base_sha != expected_base:
            raise ProjectPolicyError(
                "STALE_BASE",
                f"execution base {base_sha} is not {self.base_branch} at {expected_base}",
            )
        binding = workflow.get("product_contract")
        expected_binding = {
            "version": self.product_contract.version,
            "generation": self.product_contract.generation,
            "digest": self.product_contract.digest,
        }
        if binding != expected_binding:
            raise ProjectPolicyError(
                "PRODUCT_CONTRACT_MISMATCH",
                "workflow was not derived from the current frozen product contract",
            )

        declared_checks = {
            (check["id"], tuple(check["argv"]))
            for node in workflow["nodes"]
            for check in node.get("checks", [])
        }
        for check in (*self.required_checks, *self.live_checks):
            if (check["id"], tuple(check["argv"])) not in declared_checks:
                raise ProjectPolicyError(
                    "REQUIRED_CHECK_MISSING",
                    f"workflow omits project check {check['id']!r}",
                )

        for node in workflow["nodes"]:
            task = node["task"]
            if _SCP_WORD.search(task) or any(
                Path(check["argv"][0]).name.lower() == "scp"
                for check in node.get("checks", [])
            ):
                raise ProjectPolicyError(
                    "DIRECT_SCP_FORBIDDEN",
                    f"node {node['id']!r} requests prohibited direct SCP",
                )
            deployment = node.get("deployment")
            effectful = node["permission"] in {"external", "destructive"}
            if _DEPLOY_WORD.search(task) and deployment is None:
                raise ProjectPolicyError(
                    "UNSANCTIONED_DEPLOY",
                    f"node {node['id']!r} has deploy intent without a sanctioned adapter/target",
                )
            if deployment is not None and (
                not effectful
                or deployment["adapter"] != self.deploy_adapter
                or deployment["target"] not in self.deploy_targets
            ):
                raise ProjectPolicyError(
                    "UNSANCTIONED_DEPLOY",
                    f"node {node['id']!r} deployment is outside project policy",
                )
            if node["permission"] not in {"write", "destructive"}:
                continue
            for scope in node.get("write_scope", []):
                if not _scope_allowed(scope, self.allowed_roots):
                    raise ProjectPolicyError(
                        "WRITE_SCOPE_FORBIDDEN",
                        f"node {node['id']!r} scope {scope!r} is outside allowed roots",
                    )


def _scope_allowed(scope: str, roots: Sequence[str]) -> bool:
    clean = scope.replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    if (
        not clean
        or clean.startswith("/")
        or any(part == ".." for part in clean.split("/"))
    ):
        return False
    return any(
        root == "."
        or clean == root.rstrip("/")
        or clean.startswith(root.rstrip("/") + "/")
        for root in roots
    )


def _planning_source(root: Path, value: Any, path: str) -> tuple[Path, str]:
    if not isinstance(value, dict):
        raise ProjectPolicyError("PROJECT_SCHEMA", f"{path} must be an object")
    _exact_keys(value, {"path", "digest"}, path)
    relative = Path(_bounded_string(value["path"], f"{path}.path"))
    source = (root / relative).resolve()
    if source == root or not source.is_relative_to(root) or not source.is_file():
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", f"{path}.path must name a repository file"
        )
    digest = str(value["digest"])
    if not _SHA256.fullmatch(digest):
        raise ProjectPolicyError("PROJECT_SCHEMA", f"{path}.digest must be sha256")
    try:
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError as exc:
        raise ProjectPolicyError(
            "PROJECT_READ_ERROR", "planning source is unreadable", path=str(source)
        ) from exc
    if actual != digest:
        raise ProjectPolicyError(
            "PRODUCT_SOURCE_DRIFT",
            "planning source digest does not match the frozen product contract",
            path=str(source),
        )
    return source, digest


def _planning_text(value: Any, path: str, questions: list[str], prompt: str) -> None:
    text = _bounded_string(value, path, maximum=4000)
    if text == "UNRESOLVED":
        questions.append(f"{path}: {prompt}")


def _planning_list(value: Any, path: str, questions: list[str], prompt: str) -> None:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 64
        or any(
            not isinstance(item, str) or not item or len(item) > 2000 for item in value
        )
    ):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", f"{path} must be 1..64 bounded strings"
        )
    if value == ["UNRESOLVED"]:
        questions.append(f"{path}: {prompt}")
    elif "UNRESOLVED" in value:
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", f"{path} cannot mix answers with UNRESOLVED"
        )


def _planning_coverage(
    value: Any,
    path: str,
    questions: list[str],
    prompt: str,
    *,
    item_kind: str = "text",
) -> None:
    if not isinstance(value, dict):
        raise ProjectPolicyError("PROJECT_SCHEMA", f"{path} must be an object")
    _exact_keys(value, {"items", "na_reason"}, path)
    items = value["items"]
    reason = value["na_reason"]
    if not isinstance(items, list) or len(items) > 64:
        raise ProjectPolicyError("PROJECT_SCHEMA", f"{path}.items must be bounded")
    if items and reason is not None:
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", f"{path} must use items or na_reason, never both"
        )
    if not items:
        if reason == "UNRESOLVED":
            questions.append(
                f"{path}: {prompt} Supply items or an explicit N/A reason."
            )
            return
        _bounded_string(reason, f"{path}.na_reason", maximum=2000)
        return
    if item_kind == "text":
        if any(
            not isinstance(item, str)
            or not item
            or item == "UNRESOLVED"
            or len(item) > 2000
            for item in items
        ):
            raise ProjectPolicyError(
                "PROJECT_SCHEMA", f"{path}.items must be bounded resolved strings"
            )
        return
    ids: set[str] = set()
    expected = (
        {"id", "statement", "status", "evidence"}
        if item_kind == "assumption"
        else {"id", "question", "owner"}
    )
    for index, item in enumerate(items):
        item_path = f"{path}.items[{index}]"
        if not isinstance(item, dict):
            raise ProjectPolicyError("PROJECT_SCHEMA", f"{item_path} must be an object")
        _exact_keys(item, expected, item_path)
        identifier = item["id"]
        if (
            not isinstance(identifier, str)
            or not _ID.fullmatch(identifier)
            or identifier in ids
        ):
            raise ProjectPolicyError(
                "PROJECT_SCHEMA", f"{item_path}.id is invalid or duplicate"
            )
        ids.add(identifier)
        if item_kind == "assumption":
            _planning_text(
                item["statement"], f"{item_path}.statement", questions, prompt
            )
            if item["status"] not in {
                "assumption",
                "hypothesis",
                "validated",
                "rejected",
            }:
                raise ProjectPolicyError(
                    "PROJECT_SCHEMA", f"{item_path}.status is invalid"
                )
            evidence = item["evidence"]
            if (
                not isinstance(evidence, list)
                or len(evidence) > 64
                or any(
                    not isinstance(entry, str) or not entry or len(entry) > 2000
                    for entry in evidence
                )
            ):
                raise ProjectPolicyError(
                    "PROJECT_SCHEMA", f"{item_path}.evidence must be bounded strings"
                )
        else:
            _planning_text(item["question"], f"{item_path}.question", questions, prompt)
            _planning_text(item["owner"], f"{item_path}.owner", questions, prompt)


def _acceptance_questions(value: Any, questions: list[str]) -> None:
    path = "product.answers.acceptance_criteria"
    if not isinstance(value, list) or not value or len(value) > 64:
        raise ProjectPolicyError("PROJECT_SCHEMA", f"{path} must be a non-empty array")
    ids: set[str] = set()
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            raise ProjectPolicyError("PROJECT_SCHEMA", f"{item_path} must be an object")
        _exact_keys(
            item, {"id", "criterion", "proof_class", "argv", "human_gate"}, item_path
        )
        identifier = item["id"]
        if (
            not isinstance(identifier, str)
            or not _ID.fullmatch(identifier)
            or identifier in ids
        ):
            raise ProjectPolicyError(
                "PROJECT_SCHEMA", f"{item_path}.id is invalid or duplicate"
            )
        ids.add(identifier)
        _planning_text(
            item["criterion"],
            f"{item_path}.criterion",
            questions,
            "What observable outcome must pass?",
        )
        proof_class = item["proof_class"]
        if proof_class == "UNRESOLVED":
            questions.append(
                f"{item_path}.proof_class: Is proof deterministic or a human gate?"
            )
            if item["argv"] is not None or item["human_gate"] is not False:
                raise ProjectPolicyError(
                    "PROJECT_SCHEMA",
                    f"{item_path} unresolved proof must use null argv and false gate",
                )
        elif proof_class == "deterministic":
            _checks([{"id": identifier, "argv": item["argv"]}], item_path)
            if item["human_gate"] is not False:
                raise ProjectPolicyError(
                    "PROJECT_SCHEMA",
                    f"{item_path} deterministic proof cannot be a human gate",
                )
        elif proof_class == "human_gate":
            if item["argv"] is not None or item["human_gate"] is not True:
                raise ProjectPolicyError(
                    "PROJECT_SCHEMA",
                    f"{item_path} human gate requires null argv and true gate",
                )
        else:
            raise ProjectPolicyError(
                "PROJECT_SCHEMA", f"{item_path}.proof_class is invalid"
            )


def _validate_product_contract(
    root: Path, contract: Mapping[str, Any]
) -> tuple[tuple[str, ...], tuple[tuple[Path, str], ...]]:
    _exact_keys(
        contract,
        {"version", "id", "generation", "freeze", "sources", "answers"},
        "product_contract_artifact",
    )
    if contract["version"] != PRODUCT_CONTRACT_VERSION:
        raise ProjectPolicyError(
            "PRODUCT_CONTRACT_VERSION", "unsupported product contract version"
        )
    _bounded_string(contract["id"], "product.id", maximum=128)
    generation = contract["generation"]
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
    ):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", "product.generation must be positive"
        )
    freeze = contract["freeze"]
    if not isinstance(freeze, dict):
        raise ProjectPolicyError("PROJECT_SCHEMA", "product.freeze must be an object")
    _exact_keys(freeze, {"status", "approved_by"}, "product.freeze")
    questions: list[str] = []
    if freeze["status"] == "draft":
        questions.append(
            "product.freeze.status: Which human approves freezing this generation?"
        )
    elif freeze["status"] != "approved":
        raise ProjectPolicyError("PROJECT_SCHEMA", "product.freeze.status is invalid")
    _planning_text(
        freeze["approved_by"],
        "product.freeze.approved_by",
        questions,
        "Who explicitly approved this generation?",
    )
    sources = contract["sources"]
    if not isinstance(sources, dict):
        raise ProjectPolicyError("PROJECT_SCHEMA", "product.sources must be an object")
    _exact_keys(sources, {"brief", "decisions"}, "product.sources")
    source_bindings = tuple(
        _planning_source(root, sources[name], f"product.sources.{name}")
        for name in ("brief", "decisions")
    )
    answers = contract["answers"]
    if not isinstance(answers, dict):
        raise ProjectPolicyError("PROJECT_SCHEMA", "product.answers must be an object")
    _exact_keys(
        answers,
        {
            "problem",
            "target_users",
            "outcomes",
            "scope",
            "journeys",
            "surfaces",
            "data",
            "auth_permissions",
            "invariants",
            "compatibility",
            "failure_recovery",
            "delivery",
            "risks",
            "assumptions_hypotheses",
            "open_decisions",
            "acceptance_criteria",
        },
        "product.answers",
    )
    _planning_text(
        answers["problem"],
        "product.answers.problem",
        questions,
        "What problem are we solving?",
    )
    _planning_list(
        answers["target_users"],
        "product.answers.target_users",
        questions,
        "Who are the target users?",
    )
    _planning_list(
        answers["outcomes"],
        "product.answers.outcomes",
        questions,
        "What measurable outcomes define success?",
    )
    scope = answers["scope"]
    if not isinstance(scope, dict):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", "product.answers.scope must be an object"
        )
    _exact_keys(scope, {"in", "out"}, "product.answers.scope")
    _planning_list(
        scope["in"], "product.answers.scope.in", questions, "What is in scope?"
    )
    _planning_list(
        scope["out"],
        "product.answers.scope.out",
        questions,
        "What is explicitly out of scope?",
    )
    _planning_list(
        answers["journeys"],
        "product.answers.journeys",
        questions,
        "Which end-to-end user journeys must work?",
    )
    for group, names in (
        ("surfaces", ("ui", "api", "events", "jobs", "integrations")),
        ("data", ("tables", "stores", "migrations")),
    ):
        value = answers[group]
        if not isinstance(value, dict):
            raise ProjectPolicyError(
                "PROJECT_SCHEMA", f"product.answers.{group} must be an object"
            )
        _exact_keys(value, set(names), f"product.answers.{group}")
        for name in names:
            _planning_coverage(
                value[name],
                f"product.answers.{group}.{name}",
                questions,
                f"What {name} are affected?",
            )
    for name, prompt in (
        ("auth_permissions", "What authentication and permission rules apply?"),
        ("compatibility", "What compatibility commitments apply?"),
    ):
        _planning_coverage(answers[name], f"product.answers.{name}", questions, prompt)
    _planning_list(
        answers["invariants"],
        "product.answers.invariants",
        questions,
        "What must always remain true?",
    )
    _planning_list(
        answers["failure_recovery"],
        "product.answers.failure_recovery",
        questions,
        "How do failures surface and recover?",
    )
    delivery = answers["delivery"]
    if not isinstance(delivery, dict):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", "product.answers.delivery must be an object"
        )
    _exact_keys(
        delivery, {"rollout", "rollback", "live_proof"}, "product.answers.delivery"
    )
    for name, prompt in (
        ("rollout", "How is this rolled out safely?"),
        ("rollback", "How is this rolled back?"),
        ("live_proof", "What proves the outcome in the live target?"),
    ):
        _planning_list(
            delivery[name], f"product.answers.delivery.{name}", questions, prompt
        )
    _planning_coverage(
        answers["risks"],
        "product.answers.risks",
        questions,
        "What risks need mitigation?",
    )
    _planning_coverage(
        answers["assumptions_hypotheses"],
        "product.answers.assumptions_hypotheses",
        questions,
        "Which assumptions or hypotheses remain?",
        item_kind="assumption",
    )
    _planning_coverage(
        answers["open_decisions"],
        "product.answers.open_decisions",
        questions,
        "Which open decisions need an owner?",
        item_kind="decision",
    )
    _acceptance_questions(answers["acceptance_criteria"], questions)
    return tuple(questions), source_bindings


def load_project_policy(repo: str | Path) -> ProjectPolicy:
    root = discover_repo(repo)
    path = root / PROJECT_MANIFEST
    manifest = _read_object(path)
    required = {
        "version",
        "repository",
        "routing",
        "product_contract",
        "deployment",
        "prohibited_operations",
        "required_checks",
        "live_verification",
        "unresolved",
    }
    _exact_keys(manifest, required, "project")
    if manifest["version"] != PROJECT_VERSION:
        raise ProjectPolicyError(
            "PROJECT_VERSION", "unsupported project manifest version"
        )

    repository = manifest["repository"]
    if not isinstance(repository, dict):
        raise ProjectPolicyError("PROJECT_SCHEMA", "repository must be an object")
    _exact_keys(
        repository, {"canonical_remote", "allowed_roots", "base_branch"}, "repository"
    )
    canonical_remote = _bounded_string(
        repository["canonical_remote"], "repository.canonical_remote"
    )
    allowed_roots = repository["allowed_roots"]
    if (
        not isinstance(allowed_roots, list)
        or not allowed_roots
        or len(allowed_roots) > 64
        or any(
            not isinstance(item, str)
            or (
                item != "."
                and (
                    not _scope_allowed(item, (".",))
                    or any(token in item for token in "*?[")
                )
            )
            for item in allowed_roots
        )
    ):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA",
            "allowed_roots must contain bounded repository-relative roots",
        )
    base_branch = _bounded_string(
        repository["base_branch"], "repository.base_branch", maximum=128
    )

    routing = manifest["routing"]
    if not isinstance(routing, dict):
        raise ProjectPolicyError("PROJECT_SCHEMA", "routing must be an object")
    _exact_keys(routing, {"provider", "project"}, "routing")
    routing_value = {
        "provider": _bounded_string(
            routing["provider"], "routing.provider", maximum=128
        ),
        "project": _bounded_string(routing["project"], "routing.project", maximum=128),
    }

    deployment = manifest["deployment"]
    if not isinstance(deployment, dict):
        raise ProjectPolicyError("PROJECT_SCHEMA", "deployment must be an object")
    _exact_keys(deployment, {"adapter", "targets"}, "deployment")
    adapter = _bounded_string(deployment["adapter"], "deployment.adapter", maximum=128)
    targets = deployment["targets"]
    if (
        not isinstance(targets, list)
        or len(targets) > 32
        or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in targets)
    ):
        raise ProjectPolicyError("PROJECT_SCHEMA", "deployment.targets are invalid")

    prohibited = manifest["prohibited_operations"]
    if not isinstance(prohibited, list) or any(
        not isinstance(item, str) for item in prohibited
    ):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", "prohibited_operations must be strings"
        )
    if not {"direct-scp", "unsanctioned-deploy"}.issubset(prohibited):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", "direct-scp and unsanctioned-deploy must be prohibited"
        )
    required_checks = _checks(manifest["required_checks"], "required_checks")
    live = manifest["live_verification"]
    if not isinstance(live, dict):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", "live_verification must be an object"
        )
    _exact_keys(live, {"required", "checks"}, "live_verification")
    if not isinstance(live["required"], bool):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", "live_verification.required must be boolean"
        )
    live_checks = _checks(live["checks"], "live_verification.checks")
    if (
        live["required"]
        and not live_checks
        and "live_verification.checks" not in manifest["unresolved"]
    ):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA",
            "required live verification needs checks or an unresolved marker",
        )

    contract_binding = manifest["product_contract"]
    if not isinstance(contract_binding, dict):
        raise ProjectPolicyError("PROJECT_SCHEMA", "product_contract must be an object")
    _exact_keys(
        contract_binding,
        {"path", "version", "generation", "digest"},
        "product_contract",
    )
    relative = Path(_bounded_string(contract_binding["path"], "product_contract.path"))
    contract_path = (root / relative).resolve()
    if contract_path != root and not contract_path.is_relative_to(root):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", "product contract escapes repository"
        )
    contract = _read_object(contract_path)
    questions, source_bindings = _validate_product_contract(root, contract)
    version = contract["version"]
    generation = contract["generation"]
    if contract_binding["version"] != version:
        raise ProjectPolicyError(
            "PRODUCT_CONTRACT_VERSION",
            "unsupported or mismatched product contract version",
        )
    if (
        not isinstance(generation, int)
        or generation < 1
        or contract_binding["generation"] != generation
    ):
        raise ProjectPolicyError(
            "PRODUCT_CONTRACT_GENERATION", "product contract generation mismatch"
        )
    digest = hashlib.sha256(canonical_json(contract)).hexdigest()
    if contract_binding["digest"] != digest or not _SHA256.fullmatch(
        str(contract_binding["digest"])
    ):
        raise ProjectPolicyError(
            "PRODUCT_CONTRACT_DIGEST", "product contract digest mismatch"
        )
    unresolved = manifest["unresolved"]
    if (
        not isinstance(unresolved, list)
        or len(unresolved) > 64
        or any(
            not isinstance(item, str) or not item or len(item) > 128
            for item in unresolved
        )
    ):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA", "unresolved must be bounded field names"
        )
    if (
        canonical_remote == "UNRESOLVED"
        and "repository.canonical_remote" not in unresolved
    ):
        raise ProjectPolicyError(
            "PROJECT_SCHEMA",
            "an unresolved canonical repository must remain an explicit preflight blocker",
        )

    return ProjectPolicy(
        root,
        path,
        hashlib.sha256(canonical_json(manifest)).hexdigest(),
        _public_remote_identity(canonical_remote),
        tuple(allowed_roots),
        base_branch,
        routing_value,
        adapter,
        tuple(targets),
        frozenset(prohibited),
        required_checks,
        bool(live["required"]),
        live_checks,
        tuple(dict.fromkeys((*unresolved, *questions))),
        FrozenProductContract(
            version, generation, digest, contract_path, contract, source_bindings
        ),
    )


def load_private_execution_binding(repo: Path) -> PrivateExecutionBinding:
    """Validate the ignored per-project host and checkout boundary."""

    path = repo / PRIVATE_PROJECT_CONFIG
    if not path.is_file():
        raise ProjectPolicyError(
            "PRIVATE_EXECUTION_REQUIRED",
            "reviewed projects require a private execution binding",
        )
    try:
        mode = path.stat().st_mode & 0o777
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ProjectPolicyError(
            "PRIVATE_EXECUTION_READ", "private execution binding is unreadable"
        ) from exc
    if mode & 0o077:
        raise ProjectPolicyError(
            "PRIVATE_EXECUTION_PERMISSIONS",
            "private execution binding must use mode 0600",
        )
    execution = value.get("execution")
    if not isinstance(execution, dict) or set(execution) != {
        "allowed_hosts",
        "allowed_checkout_roots",
    }:
        raise ProjectPolicyError(
            "PRIVATE_EXECUTION_SCHEMA",
            "[execution] must define only allowed_hosts and allowed_checkout_roots",
        )
    hosts = execution["allowed_hosts"]
    roots = execution["allowed_checkout_roots"]
    if (
        not isinstance(hosts, list)
        or not hosts
        or len(hosts) > 32
        or any(
            not isinstance(host, str) or not host.strip() or len(host) > 255
            for host in hosts
        )
    ):
        raise ProjectPolicyError(
            "PRIVATE_EXECUTION_SCHEMA", "allowed_hosts must be a bounded string array"
        )
    if (
        not isinstance(roots, list)
        or not roots
        or len(roots) > 32
        or any(not isinstance(root, str) or not root for root in roots)
    ):
        raise ProjectPolicyError(
            "PRIVATE_EXECUTION_SCHEMA",
            "allowed_checkout_roots must be a bounded string array",
        )
    canonical_hosts = sorted({host.strip().lower() for host in hosts})
    canonical_roots: list[str] = []
    for configured in roots:
        candidate = Path(configured)
        if not candidate.is_absolute():
            raise ProjectPolicyError(
                "PRIVATE_EXECUTION_SCHEMA", "checkout roots must be absolute"
            )
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ProjectPolicyError(
                "PRIVATE_EXECUTION_CHECKOUT", "an allowed checkout root is unavailable"
            ) from exc
        if not resolved.is_dir():
            raise ProjectPolicyError(
                "PRIVATE_EXECUTION_CHECKOUT", "an allowed checkout root is unavailable"
            )
        canonical_roots.append(str(resolved))
    actual_host = socket.gethostname().strip().lower()
    if actual_host not in canonical_hosts:
        raise ProjectPolicyError(
            "PRIVATE_EXECUTION_HOST", "this host is not authorized for the project"
        )
    resolved_repo = repo.resolve(strict=True)
    allowed = [Path(root) for root in canonical_roots]
    if not any(
        resolved_repo == root or resolved_repo.is_relative_to(root) for root in allowed
    ):
        raise ProjectPolicyError(
            "PRIVATE_EXECUTION_CHECKOUT",
            "this checkout is outside the authorized private roots",
        )
    material = {
        "allowed_hosts": canonical_hosts,
        "allowed_checkout_roots": sorted(set(canonical_roots)),
    }
    return PrivateExecutionBinding(hashlib.sha256(canonical_json(material)).hexdigest())


def execution_identity(
    policy: ProjectPolicy, workflow: Mapping[str, Any], *, base_sha: str
) -> ExecutionIdentity:
    return ExecutionIdentity(
        repository_digest(policy.repo),
        base_sha,
        policy.product_contract.digest,
        policy.product_contract.generation,
        hashlib.sha256(canonical_json(workflow)).hexdigest(),
    )


def _checks(value: Any, path: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise ProjectPolicyError("PROJECT_SCHEMA", f"{path} must be a bounded array")
    result: list[Mapping[str, Any]] = []
    ids: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ProjectPolicyError(
                "PROJECT_SCHEMA", f"{path}[{index}] must be an object"
            )
        _exact_keys(item, {"id", "argv"}, f"{path}[{index}]")
        check_id = item["id"]
        argv = item["argv"]
        if (
            not isinstance(check_id, str)
            or not _ID.fullmatch(check_id)
            or check_id in ids
        ):
            raise ProjectPolicyError(
                "PROJECT_SCHEMA", f"{path}[{index}].id is invalid or duplicate"
            )
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 32
            or any(
                not isinstance(part, str) or not part or len(part) > 512
                for part in argv
            )
        ):
            raise ProjectPolicyError(
                "PROJECT_SCHEMA", f"{path}[{index}].argv is invalid"
            )
        ids.add(check_id)
        result.append({"id": check_id, "argv": list(argv)})
    return tuple(result)


def load_assessment(path: str | Path, repo: Path) -> Mapping[str, Any]:
    value = _read_object(Path(path).expanduser().resolve(strict=True))
    return validate_assessment(value, repo)


def validate_assessment(value: Mapping[str, Any], repo: Path) -> Mapping[str, Any]:
    """Validate the one canonical assessment/v1 producer/consumer contract."""

    def reject(message: str) -> None:
        raise ProjectPolicyError("ASSESSMENT_SCHEMA", message)

    def exact(item: Any, keys: set[str], path: str) -> Mapping[str, Any]:
        if not isinstance(item, dict) or set(item) != keys:
            reject(f"{path} fields must be exactly {sorted(keys)}")
        return item

    def bounded_strings(item: Any, path: str, *, maximum: int = 512) -> None:
        if (
            not isinstance(item, list)
            or len(item) > 64
            or any(
                not isinstance(part, str) or not part or len(part) > maximum
                for part in item
            )
        ):
            reject(f"{path} must be a bounded string array")

    required = {
        "version",
        "repo_digest",
        "source",
        "summary",
        "capabilities",
        "gaps",
        "recommended_init",
    }
    exact(value, required, "assessment")
    if value["version"] != ASSESSMENT_VERSION:
        raise ProjectPolicyError("ASSESSMENT_VERSION", "unsupported assessment version")
    if not _SHA256.fullmatch(str(value["repo_digest"])):
        reject("repo_digest must be a sha256 digest")
    if value["repo_digest"] != repository_digest(repo):
        raise ProjectPolicyError(
            "ASSESSMENT_REPOSITORY",
            "assessment belongs to another repository generation",
        )
    source = value["source"]
    exact(source, {"head_sha", "source_digest"}, "source")
    if not re.fullmatch(
        r"[0-9a-f]{40,64}", str(source["head_sha"])
    ) or not _SHA256.fullmatch(str(source["source_digest"])):
        reject("source digests are invalid")
    if source != assessment_source(repo):
        raise ProjectPolicyError(
            "ASSESSMENT_STALE",
            "assessment source no longer matches repository HEAD/tree",
        )
    summary = value["summary"]
    capabilities = value["capabilities"]
    recommended = value["recommended_init"]
    exact(summary, {"ready", "critical", "high", "medium"}, "summary")
    if not isinstance(summary["ready"], bool) or any(
        isinstance(summary[name], bool)
        or not isinstance(summary[name], int)
        or summary[name] < 0
        for name in ("critical", "high", "medium")
    ):
        reject("summary values are invalid")
    expected_capabilities = {
        "planning_capsule",
        "manifest_workflows",
        "deterministic_gates",
        "private_profiles",
        "isolation_integration",
        "bounded_effects",
        "lifecycle_handoff",
        "evidence_runners",
        "transport_need",
    }
    exact(capabilities, expected_capabilities, "capabilities")
    capability_shapes = {
        "planning_capsule": {"ready", "unanswered"},
        "manifest_workflows": {"ready", "count"},
        "deterministic_gates": {
            "ready",
            "test_roots",
            "runner_manifests",
            "proofs",
        },
        "private_profiles": {"ready", "profile_count"},
        "isolation_integration": {"ready"},
        "bounded_effects": {"ready"},
        "lifecycle_handoff": {"ready"},
        "evidence_runners": {"ready", "declarations"},
        "transport_need": {"ready", "recommended"},
    }
    for name, shape in capability_shapes.items():
        capability = exact(capabilities[name], shape, f"capabilities.{name}")
        if not isinstance(capability["ready"], bool):
            reject(f"capabilities.{name}.ready must be boolean")
    for name, field in (
        ("manifest_workflows", "count"),
        ("private_profiles", "profile_count"),
    ):
        count = capabilities[name][field]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            reject(f"capabilities.{name}.{field} must be a non-negative integer")
    for field in ("test_roots", "runner_manifests"):
        bounded_strings(
            capabilities["deterministic_gates"][field],
            f"capabilities.deterministic_gates.{field}",
        )
    proofs = exact(
        capabilities["deterministic_gates"]["proofs"],
        {"tests", "lint", "types", "build"},
        "capabilities.deterministic_gates.proofs",
    )
    if any(not isinstance(proof, bool) for proof in proofs.values()):
        reject("capabilities.deterministic_gates.proofs values must be boolean")
    bounded_strings(
        capabilities["planning_capsule"]["unanswered"],
        "capabilities.planning_capsule.unanswered",
        maximum=2000,
    )
    bounded_strings(
        capabilities["evidence_runners"]["declarations"],
        "capabilities.evidence_runners.declarations",
    )
    transport = capabilities["transport_need"]["recommended"]
    if capabilities["transport_need"]["ready"] is not True or transport not in {
        "subprocess",
        "mcp",
        "a2a",
        "none",
    }:
        reject("capabilities.transport_need is invalid")
    gaps = value["gaps"]
    if not isinstance(gaps, list) or len(gaps) > 128:
        reject("gaps must be bounded")
    for gap in gaps:
        if not isinstance(gap, dict) or set(gap) != {
            "id",
            "priority",
            "area",
            "evidence",
            "remediation",
            "fix_sites",
            "acceptance",
            "verify_cmd",
        }:
            reject("gap envelope is invalid")
        if gap["priority"] not in {"critical", "high", "medium", "low"}:
            reject("gap priority is invalid")
        for key in ("id", "area", "evidence", "remediation"):
            maximum = 2000 if key in {"evidence", "remediation"} else 128
            if not isinstance(gap[key], str) or not gap[key] or len(gap[key]) > maximum:
                reject(f"gap.{key} must be a non-empty string <= {maximum} bytes")
        bounded_strings(gap["fix_sites"], "gap.fix_sites")
        if (
            not isinstance(gap["acceptance"], str)
            or not gap["acceptance"]
            or len(gap["acceptance"]) > 2000
        ):
            reject("gap.acceptance must be a non-empty string <= 2000 bytes")
        verify_cmd = gap["verify_cmd"]
        if verify_cmd is not None and (
            not isinstance(verify_cmd, list)
            or not verify_cmd
            or len(verify_cmd) > 32
            or any(
                not isinstance(part, str) or not part or len(part) > 512
                for part in verify_cmd
            )
        ):
            reject("gap.verify_cmd must be bounded argv or null")
    if not isinstance(recommended, dict) or set(recommended) != {
        "workflow_templates",
        "require_private_config",
        "transport",
    }:
        reject("recommended_init envelope is invalid")
    templates = recommended["workflow_templates"]
    if (
        not isinstance(templates, list)
        or len(templates) > 16
        or any(
            not isinstance(item, str) or not _ID.fullmatch(item) for item in templates
        )
    ):
        reject("workflow templates are invalid")
    if not isinstance(recommended["require_private_config"], bool) or recommended[
        "transport"
    ] not in {"subprocess", "mcp", "a2a", "none"}:
        reject("recommended_init values are invalid")
    if recommended["transport"] != transport:
        reject("recommended_init.transport must match transport_need.recommended")
    if (
        recommended["require_private_config"]
        is capabilities["private_profiles"]["ready"]
    ):
        reject("recommended_init.require_private_config must reflect profile readiness")
    if capabilities["manifest_workflows"]["ready"] is not (
        capabilities["manifest_workflows"]["count"] > 0
    ):
        reject("manifest workflow readiness must match its count")
    if (
        capabilities["private_profiles"]["ready"]
        and not capabilities["private_profiles"]["profile_count"]
    ):
        reject("private profile readiness requires a non-zero profile count")
    if capabilities["deterministic_gates"]["ready"] is not all(proofs.values()):
        reject("deterministic gate readiness must match its proofs")
    if capabilities["evidence_runners"]["ready"] is not bool(
        capabilities["evidence_runners"]["declarations"]
    ):
        reject("evidence runner readiness must match its declarations")
    expected_counts = {
        name: sum(gap["priority"] == name for gap in gaps)
        for name in ("critical", "high", "medium")
    }
    if any(summary[name] != expected_counts[name] for name in expected_counts):
        reject("summary priority counts must match gaps")
    if summary["ready"] is not (not gaps):
        reject("summary.ready must match whether gaps are empty")
    return value


def detect_checks(repo: Path) -> tuple[Mapping[str, Any], ...]:
    checks: list[Mapping[str, Any]] = []
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        if "pytest" in text:
            checks.append({"id": "test", "argv": ["python", "-m", "pytest"]})
        if "ruff" in text:
            checks.append(
                {"id": "lint", "argv": ["python", "-m", "ruff", "check", "."]}
            )
        if "mypy" in text:
            checks.append({"id": "typecheck", "argv": ["python", "-m", "mypy", "."]})
    package = repo / "package.json"
    if package.exists():
        value = _read_object(package)
        scripts = value.get("scripts", {})
        if isinstance(scripts, dict):
            for script, check_id in (
                ("test", "test"),
                ("lint", "lint"),
                ("typecheck", "typecheck"),
                ("build", "build"),
            ):
                if isinstance(scripts.get(script), str) and check_id not in {
                    item["id"] for item in checks
                }:
                    checks.append({"id": check_id, "argv": ["npm", "run", script]})
    return tuple(checks)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def scaffold_project(
    repo: Path, *, assessment: Mapping[str, Any] | None = None
) -> tuple[Path, ...]:
    manifest_path = repo / PROJECT_MANIFEST
    if manifest_path.exists():
        return ()
    checks = detect_checks(repo)
    product_path = repo / ".graph-engineering/product-contract.json"
    workflow_path = repo / WORKFLOW_DIRECTORY / "starter.json"
    brief_path = repo / PROJECT_BRIEF
    decisions_path = repo / DECISION_INDEX
    conflicts = [
        path
        for path in (product_path, workflow_path, brief_path, decisions_path)
        if path.exists()
    ]
    if conflicts:
        raise ProjectPolicyError(
            "INIT_CONFLICT",
            "refusing to overwrite partial project setup: "
            + ", ".join(str(path) for path in conflicts),
        )
    brief_digest = hashlib.sha256(_PROJECT_BRIEF_TEMPLATE.encode()).hexdigest()
    decisions_digest = hashlib.sha256(_DECISION_INDEX_TEMPLATE.encode()).hexdigest()
    unresolved_coverage = {"items": [], "na_reason": "UNRESOLVED"}
    product: dict[str, Any] = {
        "version": PRODUCT_CONTRACT_VERSION,
        "id": repo.name.lower().replace("_", "-")[:128] or "project",
        "generation": 1,
        "freeze": {"status": "draft", "approved_by": "UNRESOLVED"},
        "sources": {
            "brief": {
                "path": str(PROJECT_BRIEF),
                "digest": brief_digest,
            },
            "decisions": {
                "path": str(DECISION_INDEX),
                "digest": decisions_digest,
            },
        },
        "answers": {
            "problem": "UNRESOLVED",
            "target_users": ["UNRESOLVED"],
            "outcomes": ["UNRESOLVED"],
            "scope": {"in": ["UNRESOLVED"], "out": ["UNRESOLVED"]},
            "journeys": ["UNRESOLVED"],
            "surfaces": {
                name: dict(unresolved_coverage)
                for name in ("ui", "api", "events", "jobs", "integrations")
            },
            "data": {
                name: dict(unresolved_coverage)
                for name in ("tables", "stores", "migrations")
            },
            "auth_permissions": dict(unresolved_coverage),
            "invariants": ["UNRESOLVED"],
            "compatibility": dict(unresolved_coverage),
            "failure_recovery": ["UNRESOLVED"],
            "delivery": {
                "rollout": ["UNRESOLVED"],
                "rollback": ["UNRESOLVED"],
                "live_proof": ["UNRESOLVED"],
            },
            "risks": dict(unresolved_coverage),
            "assumptions_hypotheses": dict(unresolved_coverage),
            "open_decisions": dict(unresolved_coverage),
            "acceptance_criteria": [
                {
                    "id": "acceptance-1",
                    "criterion": "UNRESOLVED",
                    "proof_class": "UNRESOLVED",
                    "argv": None,
                    "human_gate": False,
                }
            ],
        },
    }
    product_digest = hashlib.sha256(canonical_json(product)).hexdigest()
    remote = _public_remote_identity(
        _git(repo, "remote", "get-url", "origin", required=False), allow_empty=True
    )
    branch = _git(repo, "branch", "--show-current", required=False) or "main"
    unresolved = [
        "routing.provider",
        "routing.project",
        "deployment.adapter",
        "deployment.targets",
        "live_verification.checks",
        "private_profile",
    ]
    if not remote or remote == "UNRESOLVED":
        unresolved.append("repository.canonical_remote")
        remote = "UNRESOLVED"
    if (
        assessment is not None
        and assessment["recommended_init"]["require_private_config"]
    ):
        unresolved.append("private_profile")
    manifest = {
        "version": PROJECT_VERSION,
        "repository": {
            "canonical_remote": remote,
            "allowed_roots": ["."],
            "base_branch": branch,
        },
        "routing": {"provider": "UNRESOLVED", "project": "UNRESOLVED"},
        "product_contract": {
            "path": str(product_path.relative_to(repo)),
            "version": PRODUCT_CONTRACT_VERSION,
            "generation": 1,
            "digest": product_digest,
        },
        "deployment": {"adapter": "UNRESOLVED", "targets": []},
        "prohibited_operations": ["direct-scp", "unsanctioned-deploy"],
        "required_checks": list(checks),
        "live_verification": {"required": True, "checks": []},
        "unresolved": sorted(set(unresolved)),
    }
    workflow = {
        "version": "graph-engineering/v1alpha1",
        "id": "starter",
        "goal": "Implement the reviewed frozen product contract and prove its acceptance checks.",
        "product_contract": {
            "version": PRODUCT_CONTRACT_VERSION,
            "generation": 1,
            "digest": product_digest,
        },
        "budgets": {
            "max_nodes": 1,
            "max_concurrency": 1,
            "max_attempts_per_node": 1,
            "max_total_attempts": 1,
            "timeout_seconds": 1800,
        },
        "nodes": [
            {
                "id": "implement",
                "kind": "agent",
                "task": "Implement only the reviewed frozen product contract.",
                "needs": [],
                "inputs": {},
                "outputs": {"result": {"schema": {"type": "object"}}},
                "profile": "reviewed_private_profile",
                "workspace": "worktree",
                "permission": "read",
                "checks": list(checks)
                or [{"id": "unresolved_check", "argv": ["false"]}],
                "retry": {"max_attempts": 1, "no_progress_limit": 1},
                "required": True,
            }
        ],
        "outputs": {"result": "implement.result"},
    }
    validate_workflow(workflow)
    _atomic_text(brief_path, _PROJECT_BRIEF_TEMPLATE)
    _atomic_text(decisions_path, _DECISION_INDEX_TEMPLATE)
    _atomic_json(product_path, product)
    _atomic_json(workflow_path, workflow)
    _atomic_json(manifest_path, manifest)
    return (manifest_path, brief_path, product_path, decisions_path, workflow_path)


def discover_workflows(policy: ProjectPolicy) -> tuple[Path, ...]:
    directory = policy.repo / WORKFLOW_DIRECTORY
    if not directory.exists():
        return ()
    return tuple(sorted(path for path in directory.glob("*.json") if path.is_file()))


class RunScopeRegistry:
    """Atomically reserve one durable run ID for an execution identity."""

    def __init__(self, state_root: Path):
        self.path = state_root / ".run-scopes.db"

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS active_scopes ("
            "scope_digest TEXT PRIMARY KEY, identity_json TEXT NOT NULL, "
            "run_id TEXT NOT NULL, state_path TEXT NOT NULL, claimed_at REAL NOT NULL)"
        )
        return connection

    @staticmethod
    def _entry_is_active(row: sqlite3.Row) -> bool:
        state_path = Path(str(row["state_path"]))
        if not state_path.is_file():
            return True
        try:
            uri = f"file:{state_path}?mode=ro"
            with sqlite3.connect(uri, uri=True) as state:
                status = state.execute(
                    "SELECT status FROM runs WHERE id=?", (str(row["run_id"]),)
                ).fetchone()
        except sqlite3.Error:
            return True
        return status is None or str(status[0]) == "running"

    def claim(
        self,
        identity: ExecutionIdentity,
        *,
        run_id: str,
        state_path: Path,
        resume: bool,
    ) -> None:
        encoded = canonical_json(identity.as_dict()).decode("utf-8")
        canonical_state = str(state_path.expanduser().resolve())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT run_id,state_path,identity_json FROM active_scopes "
                "WHERE scope_digest=?",
                (identity.digest,),
            ).fetchone()
            if row is not None and not self._entry_is_active(row):
                connection.execute(
                    "DELETE FROM active_scopes WHERE scope_digest=?", (identity.digest,)
                )
                row = None
            if row is None:
                connection.execute(
                    "INSERT INTO active_scopes VALUES (?,?,?,?,?)",
                    (identity.digest, encoded, run_id, canonical_state, time.time()),
                )
                connection.commit()
                return
            same_run = (
                str(row["run_id"]) == run_id
                and str(row["state_path"]) == canonical_state
                and str(row["identity_json"]) == encoded
            )
            if resume and same_run:
                connection.commit()
                return
            connection.rollback()
            raise ProjectPolicyError(
                "DUPLICATE_ACTIVE_RUN",
                "an active run already owns this repository/base/contract/workflow scope",
            )

    def release_if_inactive(
        self, identity: ExecutionIdentity, *, run_id: str, state_path: Path
    ) -> None:
        canonical_state = state_path.expanduser().resolve()
        active = False
        if canonical_state.is_file():
            try:
                uri = f"file:{canonical_state}?mode=ro"
                with sqlite3.connect(uri, uri=True) as state:
                    row = state.execute(
                        "SELECT status FROM runs WHERE id=?", (run_id,)
                    ).fetchone()
                active = row is not None and str(row[0]) == "running"
            except sqlite3.Error:
                active = True
        if active or not self.path.exists():
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM active_scopes WHERE scope_digest=? AND run_id=? AND state_path=?",
                (identity.digest, run_id, str(canonical_state)),
            )
            connection.commit()

    def matches(self, identity: ExecutionIdentity) -> tuple[Mapping[str, str], ...]:
        if not self.path.exists():
            return ()
        uri = f"file:{self.path}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as connection:
                rows = connection.execute(
                    "SELECT run_id,state_path FROM active_scopes WHERE scope_digest=?",
                    (identity.digest,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ProjectPolicyError(
                "RUN_SCOPE_REGISTRY_INVALID", "active-run registry is unreadable"
            ) from exc
        return tuple(
            {"run_id": str(run_id), "status": "running", "state": str(state)}
            for run_id, state in rows
        )


def planning_capsule_status(repo: Path) -> Mapping[str, Any]:
    """Return bounded, read-only capsule readiness for adoption assessment."""

    try:
        policy = load_project_policy(repo)
        questions, _sources = _validate_product_contract(
            policy.repo, policy.product_contract.value
        )
    except ProjectPolicyError as exc:
        return {"ready": False, "unanswered": [exc.code]}
    return {"ready": not questions, "unanswered": list(questions)}


def matching_active_runs(
    state_root: Path, identity: ExecutionIdentity
) -> tuple[Mapping[str, str], ...]:
    return RunScopeRegistry(state_root).matches(identity)
