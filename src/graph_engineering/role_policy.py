"""Slow-changing authority ceilings for fast-changing work graphs.

The workflow schema deliberately does not own authority.  A :class:`RolePolicy`
is reviewed and versioned independently, while each transient work graph declares
the smaller authority it intends to use.  ``authorize_work`` is the fail-closed
join between those two documents.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from .artifacts import canonical_json
from .config import CAPABILITY_NAMES, AgentConfig

if TYPE_CHECKING:  # pragma: no cover
    from .project import ProjectPolicy


ROLE_POLICY_VERSION = "graph-engineering/role-policy/v1"
ROLE_POLICY_PATH = Path(".graph-engineering/role-policy.json")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_COST_FIELDS = (
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "max_cost_microusd",
)


class AuthorityError(ValueError):
    """A role policy or requested work authority is malformed or unsafe."""

    def __init__(self, code: str, path: str, message: str):
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code} at {path}: {message}")


def _error(code: str, path: str, message: str) -> AuthorityError:
    return AuthorityError(code, path, message)


def _name(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise _error("ROLE_POLICY_SCHEMA", path, "must be a portable non-empty name")
    return value


def _names(value: Any, path: str) -> frozenset[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise _error("ROLE_POLICY_SCHEMA", path, "must be a bounded name array")
    result = frozenset(
        _name(item, f"{path}[{index}]") for index, item in enumerate(value)
    )
    if len(result) != len(value):
        raise _error("ROLE_POLICY_SCHEMA", path, "contains duplicates")
    return result


def _scope(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise _error(
            "ROLE_POLICY_SCHEMA", path, "must be a bounded repository-relative path"
        )
    clean = value.replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    if (
        not clean
        or clean.startswith("/")
        or any(part in {"", ".."} for part in clean.split("/"))
        or any(marker in clean for marker in "*?[")
    ):
        raise _error(
            "ROLE_POLICY_SCHEMA", path, "must be a literal repository-relative path"
        )
    return clean


def _scopes(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 256:
        raise _error("ROLE_POLICY_SCHEMA", path, "must be a bounded path array")
    result = tuple(_scope(item, f"{path}[{index}]") for index, item in enumerate(value))
    if len(set(result)) != len(result):
        raise _error("ROLE_POLICY_SCHEMA", path, "contains duplicates")
    return result


def _scope_is_within(requested: str, ceilings: Sequence[str]) -> bool:
    return any(
        ceiling == "."
        or requested == ceiling.rstrip("/")
        or requested.startswith(ceiling.rstrip("/") + "/")
        for ceiling in ceilings
    )


@dataclass(frozen=True)
class CostCeiling:
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int
    max_cost_microusd: int

    def __post_init__(self) -> None:
        for field in _COST_FIELDS:
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise _error(
                    "ROLE_POLICY_SCHEMA",
                    f"cost.{field}",
                    "must be a non-negative integer",
                )
        if (
            self.max_total_tokens < self.max_input_tokens
            or self.max_total_tokens < self.max_output_tokens
        ):
            raise _error(
                "ROLE_POLICY_SCHEMA",
                "cost.max_total_tokens",
                "must not be smaller than either directional token ceiling",
            )

    def as_dict(self) -> dict[str, int]:
        return {field: getattr(self, field) for field in _COST_FIELDS}


@dataclass(frozen=True)
class RolePolicy:
    """A reviewed authority ceiling; work graphs can only select subsets of it."""

    version: str
    generation: int
    approved_by: str
    profile_capabilities: Mapping[str, frozenset[str]]
    tools: frozenset[str]
    write_scopes: tuple[str, ...]
    effects: frozenset[str]
    deployment_targets: frozenset[str]
    approval_boundaries: frozenset[str]
    effect_approvals: Mapping[str, frozenset[str]]
    cost: CostCeiling

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generation": self.generation,
            "approved_by": self.approved_by,
            "profiles": {
                profile: sorted(capabilities)
                for profile, capabilities in sorted(self.profile_capabilities.items())
            },
            "tools": sorted(self.tools),
            "write_scopes": list(self.write_scopes),
            "effects": sorted(self.effects),
            "deployment_targets": sorted(self.deployment_targets),
            "approval_boundaries": sorted(self.approval_boundaries),
            "effect_approvals": {
                effect: sorted(boundaries)
                for effect, boundaries in sorted(self.effect_approvals.items())
            },
            "cost": self.cost.as_dict(),
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.as_dict())).hexdigest()


@dataclass(frozen=True)
class WorkAuthority:
    """Normalized authority requested by one work graph or one of its nodes."""

    profile: str
    capabilities: frozenset[str]
    tools: frozenset[str]
    write_scopes: tuple[str, ...]
    effects: frozenset[str]
    deployment_targets: frozenset[str]
    approval_boundaries: frozenset[str]
    cost: CostCeiling


@dataclass(frozen=True)
class RolePolicyBinding:
    """Immutable work-graph reference to one reviewed policy generation."""

    version: str
    generation: int
    digest: str


def parse_role_policy(value: Mapping[str, Any]) -> RolePolicy:
    """Parse a strict, portable role-policy document.

    Unknown fields fail closed so adding a field to a work graph cannot silently
    create authority that an older runtime does not understand.
    """

    required = {
        "version",
        "generation",
        "approved_by",
        "profiles",
        "tools",
        "write_scopes",
        "effects",
        "deployment_targets",
        "approval_boundaries",
        "effect_approvals",
        "cost",
    }
    if set(value) != required:
        raise _error(
            "ROLE_POLICY_SCHEMA", "$", f"fields must be exactly {sorted(required)}"
        )
    if value["version"] != ROLE_POLICY_VERSION:
        raise _error(
            "ROLE_POLICY_VERSION", "$.version", f"must equal {ROLE_POLICY_VERSION}"
        )
    generation = value["generation"]
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise _error("ROLE_POLICY_SCHEMA", "$.generation", "must be a positive integer")
    approved_by = _name(value["approved_by"], "$.approved_by")

    raw_profiles = value["profiles"]
    if (
        not isinstance(raw_profiles, dict)
        or not raw_profiles
        or len(raw_profiles) > 128
    ):
        raise _error(
            "ROLE_POLICY_SCHEMA", "$.profiles", "must be a non-empty bounded object"
        )
    profiles: dict[str, frozenset[str]] = {}
    for raw_profile, raw_capabilities in raw_profiles.items():
        profile = _name(raw_profile, f"$.profiles.{raw_profile}")
        capabilities = _names(raw_capabilities, f"$.profiles.{profile}")
        unknown = sorted(capabilities - CAPABILITY_NAMES)
        if unknown:
            raise _error(
                "ROLE_POLICY_SCHEMA",
                f"$.profiles.{profile}",
                f"unknown capabilities: {unknown}",
            )
        profiles[profile] = capabilities

    raw_cost = value["cost"]
    if not isinstance(raw_cost, dict) or set(raw_cost) != set(_COST_FIELDS):
        raise _error(
            "ROLE_POLICY_SCHEMA",
            "$.cost",
            f"fields must be exactly {list(_COST_FIELDS)}",
        )
    cost = CostCeiling(**raw_cost)
    raw_effect_approvals = value["effect_approvals"]
    if not isinstance(raw_effect_approvals, dict) or len(raw_effect_approvals) > 256:
        raise _error(
            "ROLE_POLICY_SCHEMA",
            "$.effect_approvals",
            "must be a bounded effect-to-approval object",
        )
    effect_approvals: dict[str, frozenset[str]] = {}
    effects = _names(value["effects"], "$.effects")
    approvals = _names(value["approval_boundaries"], "$.approval_boundaries")
    for raw_effect, raw_approvals in raw_effect_approvals.items():
        effect = _name(raw_effect, f"$.effect_approvals.{raw_effect}")
        if effect not in effects:
            raise _error(
                "ROLE_POLICY_SCHEMA",
                f"$.effect_approvals.{effect}",
                "effect is not in the authority ceiling",
            )
        required_approvals = _names(raw_approvals, f"$.effect_approvals.{effect}")
        unknown_approvals = sorted(required_approvals - approvals)
        if unknown_approvals:
            raise _error(
                "ROLE_POLICY_SCHEMA",
                f"$.effect_approvals.{effect}",
                f"unknown approval boundaries: {unknown_approvals}",
            )
        effect_approvals[effect] = required_approvals
    return RolePolicy(
        ROLE_POLICY_VERSION,
        generation,
        approved_by,
        MappingProxyType(profiles),
        _names(value["tools"], "$.tools"),
        _scopes(value["write_scopes"], "$.write_scopes"),
        effects,
        _names(value["deployment_targets"], "$.deployment_targets"),
        approvals,
        MappingProxyType(effect_approvals),
        cost,
    )


def parse_work_authority(value: Mapping[str, Any]) -> WorkAuthority:
    """Normalize an untrusted work-graph authority declaration."""

    required = {
        "profile",
        "capabilities",
        "tools",
        "write_scopes",
        "effects",
        "deployment_targets",
        "approval_boundaries",
        "cost",
    }
    if set(value) != required:
        raise _error(
            "WORK_AUTHORITY_SCHEMA",
            "work.authority",
            f"fields must be exactly {sorted(required)}",
        )
    raw_cost = value["cost"]
    if not isinstance(raw_cost, dict) or set(raw_cost) != set(_COST_FIELDS):
        raise _error(
            "WORK_AUTHORITY_SCHEMA",
            "work.authority.cost",
            f"fields must be exactly {list(_COST_FIELDS)}",
        )
    capabilities = _names(value["capabilities"], "work.authority.capabilities")
    unknown = sorted(capabilities - CAPABILITY_NAMES)
    if unknown:
        raise _error(
            "WORK_AUTHORITY_SCHEMA",
            "work.authority.capabilities",
            f"unknown capabilities: {unknown}",
        )
    return WorkAuthority(
        _name(value["profile"], "work.authority.profile"),
        capabilities,
        _names(value["tools"], "work.authority.tools"),
        _scopes(value["write_scopes"], "work.authority.write_scopes"),
        _names(value["effects"], "work.authority.effects"),
        _names(value["deployment_targets"], "work.authority.deployment_targets"),
        _names(value["approval_boundaries"], "work.authority.approval_boundaries"),
        CostCeiling(**raw_cost),
    )


def role_policy_from_project(
    project: ProjectPolicy,
    config: AgentConfig,
    *,
    generation: int,
    approved_by: str,
    tools: Iterable[str] = (),
    effects: Iterable[str] = (),
    approval_boundaries: Iterable[str] = (),
    effect_approvals: Mapping[str, Iterable[str]] | None = None,
    cost: CostCeiling,
) -> RolePolicy:
    """Adapt existing checked-in policy and ignored private profiles.

    Adapter commands and credentials never enter the returned public authority
    document.  Only profile names and their already-reviewed capability booleans
    cross the boundary.
    """

    value = {
        "version": ROLE_POLICY_VERSION,
        "generation": generation,
        "approved_by": approved_by,
        "profiles": {
            name: sorted(profile.capabilities.enabled())
            for name, profile in sorted(config.profiles.items())
        },
        "tools": sorted(set(tools)),
        "write_scopes": list(project.allowed_roots),
        "effects": sorted(set(effects)),
        "deployment_targets": list(project.deploy_targets),
        "approval_boundaries": sorted(set(approval_boundaries)),
        "effect_approvals": {
            effect: sorted(set(boundaries))
            for effect, boundaries in sorted((effect_approvals or {}).items())
        },
        "cost": cost.as_dict(),
    }
    return parse_role_policy(value)


def load_role_policy(
    repo: str | Path,
    *,
    binding: RolePolicyBinding | None = None,
) -> RolePolicy:
    """Load the checked-in authority ceiling and optionally prove its binding.

    The binding is intended to be persisted beside a fast-changing work graph.
    It makes an edited role-policy file inert until the graph is explicitly
    re-reviewed against the new generation and digest.
    """

    path = Path(repo).expanduser().resolve() / ROLE_POLICY_PATH
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise _error(
            "ROLE_POLICY_READ", str(path), "role policy is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise _error("ROLE_POLICY_SCHEMA", "$", "role policy must be an object")
    policy = parse_role_policy(value)
    if binding is not None and (
        binding.version != policy.version
        or binding.generation != policy.generation
        or binding.digest != policy.digest
    ):
        raise _error(
            "ROLE_POLICY_DRIFT",
            "work.role_policy",
            "work graph is not bound to the reviewed role-policy generation",
        )
    return policy


def bind_role_policy(policy: RolePolicy) -> RolePolicyBinding:
    return RolePolicyBinding(policy.version, policy.generation, policy.digest)


def authorize_private_profiles(
    policy: RolePolicy,
    config: AgentConfig,
    *,
    selected_profiles: Iterable[str] | None = None,
) -> None:
    """Ensure ignored/private profile changes cannot widen reviewed authority."""

    selected = (
        set(config.profiles) if selected_profiles is None else set(selected_profiles)
    )
    unknown = sorted(selected - set(config.profiles))
    if unknown:
        raise _error(
            "PROFILE_NOT_CONFIGURED",
            "profiles",
            f"private profiles are missing: {unknown}",
        )
    for name in sorted(selected):
        allowed = policy.profile_capabilities.get(name)
        if allowed is None:
            raise _error(
                "PROFILE_NOT_AUTHORIZED",
                f"profiles.{name}",
                "private profile is outside role policy",
            )
        enabled = config.profiles[name].capabilities.enabled()
        excess = sorted(enabled - allowed)
        if excess:
            raise _error(
                "PRIVATE_PROFILE_EXPANSION",
                f"profiles.{name}",
                f"private profile gained capabilities: {excess}",
            )


def authorize_work(policy: RolePolicy, work: WorkAuthority) -> None:
    """Prove that a work graph narrows every dimension of reviewed authority."""

    allowed_capabilities = policy.profile_capabilities.get(work.profile)
    if allowed_capabilities is None:
        raise _error(
            "PROFILE_NOT_AUTHORIZED",
            "work.profile",
            f"profile {work.profile!r} is outside role policy",
        )
    dimensions = (
        (
            work.capabilities,
            allowed_capabilities,
            "CAPABILITY_EXPANSION",
            "work.capabilities",
        ),
        (work.tools, policy.tools, "TOOL_EXPANSION", "work.tools"),
        (work.effects, policy.effects, "EFFECT_EXPANSION", "work.effects"),
        (
            work.deployment_targets,
            policy.deployment_targets,
            "DEPLOYMENT_EXPANSION",
            "work.deployment_targets",
        ),
        (
            work.approval_boundaries,
            policy.approval_boundaries,
            "APPROVAL_EXPANSION",
            "work.approval_boundaries",
        ),
    )
    for requested, allowed, code, path in dimensions:
        excess = sorted(requested - allowed)
        if excess:
            raise _error(code, path, f"not authorized by role policy: {excess}")
    for index, scope in enumerate(work.write_scopes):
        clean = _scope(scope, f"work.write_scopes[{index}]")
        if not _scope_is_within(clean, policy.write_scopes):
            raise _error(
                "WRITE_SCOPE_EXPANSION",
                f"work.write_scopes[{index}]",
                f"{clean!r} is outside role policy",
            )
    required_approvals = frozenset(
        boundary
        for effect in work.effects
        for boundary in policy.effect_approvals.get(effect, ())
    )
    omitted_approvals = sorted(required_approvals - work.approval_boundaries)
    if omitted_approvals:
        raise _error(
            "REQUIRED_APPROVAL_MISSING",
            "work.approval_boundaries",
            f"requested effects require approval boundaries: {omitted_approvals}",
        )
    for field in _COST_FIELDS:
        if getattr(work.cost, field) > getattr(policy.cost, field):
            raise _error(
                "COST_EXPANSION", f"work.cost.{field}", "exceeds role-policy ceiling"
            )
