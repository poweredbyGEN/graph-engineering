"""Safe, portable configuration for graph worker profiles.

The public project file is deliberately a routing surface, not a command injection
surface.  Adapter definitions belong in the user's private configuration or an
ignored local override.
"""

from __future__ import annotations

import hashlib
import os
import re
import string
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

CONFIG_VERSION = 1
DEFAULT_USER_CONFIG = Path("~/.config/graph-engineering/config.toml").expanduser()
DEFAULT_PROJECT_LOCAL = ".graph-engineering.local.toml"
CAPABILITY_NAMES = frozenset(
    {"read", "write", "structured_output", "worktree", "resume", "mcp"}
)
ARGV_PLACEHOLDERS = frozenset(
    {
        "cwd",
        "idempotency_key",
        "model",
        "node_id",
        "prompt",
        "prompt_file",
        "run_id",
        "schema_file",
    }
)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|credential|token)", re.IGNORECASE
)
_DEFAULT_USER_SENTINEL = object()


class ConfigError(ValueError):
    """A deterministic configuration error with a machine-readable code."""

    def __init__(self, code: str, path: str, message: str):
        self.code = code
        self.path = path
        self.message = message
        super().__init__(f"{code} at {path}: {message}")


class CapabilityMismatchError(ConfigError):
    """Raised when a selected worker cannot satisfy a node's requirements."""


@dataclass(frozen=True)
class Capabilities:
    read: bool
    write: bool
    structured_output: bool
    worktree: bool
    resume: bool
    mcp: bool

    def enabled(self) -> frozenset[str]:
        return frozenset(name for name in CAPABILITY_NAMES if getattr(self, name))


@dataclass(frozen=True)
class SubprocessAdapter:
    argv: tuple[str, ...]
    prompt_transport: Literal["stdin", "argv", "file"]
    output_format: Literal["text", "json", "jsonl"]
    env_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class OpenAICompatibleAdapter:
    endpoint_env: str
    api_key_env: str
    organization_env: str | None = None


@dataclass(frozen=True)
class A2AAdapter:
    """Private configuration for an independently operated A2A worker."""

    agent_card_url: str
    auth_env: str
    allowed_skills: tuple[str, ...]
    expected_identity: str


Adapter = SubprocessAdapter | OpenAICompatibleAdapter | A2AAdapter


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    adapter: Adapter
    capabilities: Capabilities

    @property
    def adapter_kind(self) -> str:
        if isinstance(self.adapter, SubprocessAdapter):
            return "subprocess"
        if isinstance(self.adapter, OpenAICompatibleAdapter):
            return "openai-compatible"
        return "a2a"


@dataclass(frozen=True)
class Pool:
    name: str
    profiles: tuple[str, ...]
    strategy: Literal["first", "stable-hash"]


@dataclass(frozen=True)
class Tier:
    name: str
    profile: str | None = None
    pool: str | None = None


@dataclass(frozen=True)
class Routing:
    default_profile: str | None = None
    default_pool: str | None = None
    default_tier: str | None = None


@dataclass(frozen=True)
class AgentConfig:
    profiles: Mapping[str, Profile]
    pools: Mapping[str, Pool]
    tiers: Mapping[str, Tier]
    routing: Routing


def _error(code: str, path: str, message: str) -> ConfigError:
    return ConfigError(code, path, message)


def _table(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error("TYPE_ERROR", path, "must be a table")
    return value


def _strict_keys(table: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise _error("UNKNOWN_FIELD", path, f"unknown fields: {unknown}")


def _name(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise _error("INVALID_NAME", path, "must be a portable non-empty name")
    return value


def _nonempty(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error("TYPE_ERROR", path, "must be a non-empty string")
    return value


def _env_name(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
        raise _error("INVALID_ENV_REFERENCE", path, "must name an environment variable")
    return value


def _reject_literal_secrets(value: Any, path: str = "$") -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        child = f"{path}.{key}"
        if (
            _SECRET_KEY.search(str(key))
            and not str(key).endswith("_env")
            and not isinstance(item, (dict, list))
        ):
            raise _error(
                "LITERAL_SECRET",
                child,
                "secret values are forbidden; reference an environment variable instead",
            )
        if isinstance(item, dict):
            _reject_literal_secrets(item, child)


def _merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        # Routing is a tagged union. Replacing a route must not retain the losing
        # layer's selector and accidentally produce a multi-selection.
        if key == "routing" and isinstance(value, dict):
            merged[key] = dict(value)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path, *, project_surface: bool = False) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _error("PARSE_ERROR", str(path), f"cannot read TOML: {exc}") from exc
    _reject_literal_secrets(data)
    if project_surface:
        _strict_keys(data, {"version", "routing"}, "$")
    return data


def _parse_placeholders(argv: tuple[str, ...], path: str) -> list[str]:
    formatter = string.Formatter()
    found: list[str] = []
    for index, argument in enumerate(argv):
        try:
            parts = formatter.parse(argument)
            for _, field, format_spec, conversion in parts:
                if field is None:
                    continue
                if field not in ARGV_PLACEHOLDERS or format_spec or conversion:
                    raise _error(
                        "UNSAFE_PLACEHOLDER",
                        f"{path}[{index}]",
                        f"placeholder {field!r} is not allowed",
                    )
                found.append(field)
        except ValueError as exc:
            raise _error("UNSAFE_PLACEHOLDER", f"{path}[{index}]", str(exc)) from exc
    return found


def _parse_capabilities(value: Any, path: str) -> Capabilities:
    table = _table(value, path)
    _strict_keys(table, set(CAPABILITY_NAMES), path)
    missing = sorted(CAPABILITY_NAMES - set(table))
    if missing:
        raise _error(
            "MISSING_FIELD", path, f"capabilities must be explicit; missing {missing}"
        )
    for key, item in table.items():
        if not isinstance(item, bool):
            raise _error("TYPE_ERROR", f"{path}.{key}", "must be a boolean")
    return Capabilities(**{name: table[name] for name in CAPABILITY_NAMES})


def _parse_subprocess(value: Any, path: str) -> SubprocessAdapter:
    table = _table(value, path)
    _strict_keys(
        table, {"argv", "prompt_transport", "output_format", "env_allowlist"}, path
    )
    argv_value = table.get("argv")
    if (
        not isinstance(argv_value, list)
        or not argv_value
        or not all(isinstance(item, str) and item for item in argv_value)
    ):
        raise _error("TYPE_ERROR", f"{path}.argv", "must be a non-empty string array")
    argv = tuple(argv_value)
    placeholders = _parse_placeholders(argv, f"{path}.argv")
    if any(marker in argv[0] for marker in ("{", "}")):
        raise _error(
            "UNSAFE_EXECUTABLE", f"{path}.argv[0]", "executable may not be templated"
        )

    transport = table.get("prompt_transport", "stdin")
    if transport not in {"stdin", "argv", "file"}:
        raise _error(
            "INVALID_VALUE", f"{path}.prompt_transport", "must be stdin, argv, or file"
        )
    expected = {"stdin": None, "argv": "prompt", "file": "prompt_file"}[transport]
    prompt_fields = [
        field for field in placeholders if field in {"prompt", "prompt_file"}
    ]
    if expected is None and prompt_fields:
        raise _error(
            "PROMPT_TRANSPORT",
            f"{path}.argv",
            "stdin transport forbids prompt placeholders",
        )
    if expected is not None and prompt_fields != [expected]:
        raise _error(
            "PROMPT_TRANSPORT",
            f"{path}.argv",
            f"{transport} transport requires exactly one {{{expected}}} placeholder",
        )

    output_format = table.get("output_format", "text")
    if output_format not in {"text", "json", "jsonl"}:
        raise _error(
            "INVALID_VALUE", f"{path}.output_format", "must be text, json, or jsonl"
        )
    env_value = table.get("env_allowlist", [])
    if not isinstance(env_value, list):
        raise _error("TYPE_ERROR", f"{path}.env_allowlist", "must be an array")
    env_allowlist = tuple(
        _env_name(item, f"{path}.env_allowlist[{index}]")
        for index, item in enumerate(env_value)
    )
    if len(set(env_allowlist)) != len(env_allowlist):
        raise _error("DUPLICATE_VALUE", f"{path}.env_allowlist", "contains duplicates")
    return SubprocessAdapter(argv, transport, output_format, env_allowlist)


def _parse_openai(value: Any, path: str) -> OpenAICompatibleAdapter:
    table = _table(value, path)
    _strict_keys(table, {"endpoint_env", "api_key_env", "organization_env"}, path)
    endpoint_env = _env_name(table.get("endpoint_env"), f"{path}.endpoint_env")
    api_key_env = _env_name(table.get("api_key_env"), f"{path}.api_key_env")
    organization = table.get("organization_env")
    organization_env = (
        _env_name(organization, f"{path}.organization_env")
        if organization is not None
        else None
    )
    return OpenAICompatibleAdapter(endpoint_env, api_key_env, organization_env)


def _parse_a2a(value: Any, path: str) -> A2AAdapter:
    table = _table(value, path)
    _strict_keys(
        table,
        {"agent_card_url", "auth_env", "allowed_skills", "expected_identity"},
        path,
    )
    card_url = _nonempty(table.get("agent_card_url"), f"{path}.agent_card_url")
    if not card_url.startswith(("https://", "http://127.0.0.1:", "http://localhost:")):
        raise _error(
            "INSECURE_A2A_URL",
            f"{path}.agent_card_url",
            "must use HTTPS (loopback HTTP is allowed for conformance tests)",
        )
    auth_env = _env_name(table.get("auth_env"), f"{path}.auth_env")
    raw_skills = table.get("allowed_skills")
    if (
        not isinstance(raw_skills, list)
        or not raw_skills
        or not all(
            isinstance(item, str) and _NAME.fullmatch(item) for item in raw_skills
        )
    ):
        raise _error(
            "TYPE_ERROR",
            f"{path}.allowed_skills",
            "must be a non-empty portable-name array",
        )
    if len(set(raw_skills)) != len(raw_skills):
        raise _error("DUPLICATE_VALUE", f"{path}.allowed_skills", "contains duplicates")
    identity = _nonempty(table.get("expected_identity"), f"{path}.expected_identity")
    return A2AAdapter(card_url, auth_env, tuple(raw_skills), identity)


def parse_agent_config(data: Mapping[str, Any]) -> AgentConfig:
    """Parse and validate a merged configuration mapping."""

    raw = dict(data)
    _reject_literal_secrets(raw)
    _strict_keys(raw, {"version", "profiles", "pools", "tiers", "routing"}, "$")
    if raw.get("version") != CONFIG_VERSION:
        raise _error("VERSION_ERROR", "$.version", f"must equal {CONFIG_VERSION}")

    profiles_table = _table(raw.get("profiles", {}), "$.profiles")
    profiles: dict[str, Profile] = {}
    for raw_name, raw_profile in sorted(profiles_table.items()):
        name = _name(raw_name, f"$.profiles.{raw_name}")
        path = f"$.profiles.{name}"
        table = _table(raw_profile, path)
        _strict_keys(
            table,
            {
                "adapter",
                "model",
                "capabilities",
                "subprocess",
                "openai_compatible",
                "a2a",
            },
            path,
        )
        adapter_kind = table.get("adapter")
        if adapter_kind not in {"subprocess", "openai-compatible", "a2a"}:
            raise _error(
                "INVALID_VALUE",
                f"{path}.adapter",
                "must be subprocess, openai-compatible, or a2a",
            )
        adapter_key = {
            "subprocess": "subprocess",
            "openai-compatible": "openai_compatible",
            "a2a": "a2a",
        }[adapter_kind]
        other_keys = {"subprocess", "openai_compatible", "a2a"} - {adapter_key}
        if adapter_key not in table or any(key in table for key in other_keys):
            raise _error(
                "ADAPTER_CONFIG", path, f"must define only [{path[2:]}.{adapter_key}]"
            )
        adapter: Adapter
        if adapter_kind == "subprocess":
            adapter = _parse_subprocess(table[adapter_key], f"{path}.{adapter_key}")
        elif adapter_kind == "openai-compatible":
            adapter = _parse_openai(table[adapter_key], f"{path}.{adapter_key}")
        else:
            adapter = _parse_a2a(table[adapter_key], f"{path}.{adapter_key}")
        profiles[name] = Profile(
            name=name,
            model=_nonempty(table.get("model"), f"{path}.model"),
            adapter=adapter,
            capabilities=_parse_capabilities(
                table.get("capabilities"), f"{path}.capabilities"
            ),
        )

    pools_table = _table(raw.get("pools", {}), "$.pools")
    pools: dict[str, Pool] = {}
    for raw_name, raw_pool in sorted(pools_table.items()):
        name = _name(raw_name, f"$.pools.{raw_name}")
        path = f"$.pools.{name}"
        table = _table(raw_pool, path)
        _strict_keys(table, {"profiles", "strategy"}, path)
        members = table.get("profiles")
        if not isinstance(members, list) or not members:
            raise _error("TYPE_ERROR", f"{path}.profiles", "must be a non-empty array")
        member_names = tuple(_name(item, f"{path}.profiles") for item in members)
        if len(set(member_names)) != len(member_names):
            raise _error("DUPLICATE_VALUE", f"{path}.profiles", "contains duplicates")
        unknown = sorted(set(member_names) - set(profiles))
        if unknown:
            raise _error(
                "UNKNOWN_PROFILE", f"{path}.profiles", f"unknown profiles: {unknown}"
            )
        strategy = table.get("strategy", "stable-hash")
        if strategy not in {"first", "stable-hash"}:
            raise _error(
                "INVALID_VALUE", f"{path}.strategy", "must be first or stable-hash"
            )
        pools[name] = Pool(name, member_names, strategy)

    tiers_table = _table(raw.get("tiers", {}), "$.tiers")
    tiers: dict[str, Tier] = {}
    for raw_name, raw_tier in sorted(tiers_table.items()):
        name = _name(raw_name, f"$.tiers.{raw_name}")
        path = f"$.tiers.{name}"
        table = _table(raw_tier, path)
        _strict_keys(table, {"profile", "pool"}, path)
        if ("profile" in table) == ("pool" in table):
            raise _error(
                "SELECTION_ERROR", path, "must set exactly one of profile or pool"
            )
        profile = (
            _name(table["profile"], f"{path}.profile") if "profile" in table else None
        )
        pool = _name(table["pool"], f"{path}.pool") if "pool" in table else None
        if profile is not None and profile not in profiles:
            raise _error(
                "UNKNOWN_PROFILE", f"{path}.profile", f"unknown profile {profile!r}"
            )
        if pool is not None and pool not in pools:
            raise _error("UNKNOWN_POOL", f"{path}.pool", f"unknown pool {pool!r}")
        tiers[name] = Tier(name, profile, pool)

    routing_table = _table(raw.get("routing", {}), "$.routing")
    _strict_keys(
        routing_table, {"default_profile", "default_pool", "default_tier"}, "$.routing"
    )
    selected = [key for key in routing_table if routing_table[key] is not None]
    if len(selected) > 1:
        raise _error("SELECTION_ERROR", "$.routing", "set at most one default selector")
    routing = Routing(
        default_profile=_name(
            routing_table["default_profile"], "$.routing.default_profile"
        )
        if "default_profile" in routing_table
        else None,
        default_pool=_name(routing_table["default_pool"], "$.routing.default_pool")
        if "default_pool" in routing_table
        else None,
        default_tier=_name(routing_table["default_tier"], "$.routing.default_tier")
        if "default_tier" in routing_table
        else None,
    )
    if routing.default_profile and routing.default_profile not in profiles:
        raise _error(
            "UNKNOWN_PROFILE", "$.routing.default_profile", "profile is not configured"
        )
    if routing.default_pool and routing.default_pool not in pools:
        raise _error("UNKNOWN_POOL", "$.routing.default_pool", "pool is not configured")
    if routing.default_tier and routing.default_tier not in tiers:
        raise _error("UNKNOWN_TIER", "$.routing.default_tier", "tier is not configured")
    return AgentConfig(
        MappingProxyType(profiles),
        MappingProxyType(pools),
        MappingProxyType(tiers),
        routing,
    )


def load_agent_config(
    *,
    project_path: str | Path | None = None,
    project_local_path: str | Path | None = None,
    user_path: str | Path | None | object = _DEFAULT_USER_SENTINEL,
    defaults: Mapping[str, Any] | None = None,
) -> AgentConfig:
    """Load defaults -> private user -> checked-in project -> ignored local config.

    Passing ``user_path=None`` disables the implicit user config, which is useful for
    hermetic tests.  A checked-in project file is restricted to routing selection.
    """

    merged: dict[str, Any] = {"version": CONFIG_VERSION}
    if defaults is not None:
        merged = _merge(merged, defaults)

    resolved_user = (
        DEFAULT_USER_CONFIG if user_path is _DEFAULT_USER_SENTINEL else user_path
    )
    if resolved_user is not None:
        path = Path(resolved_user).expanduser()
        if path.is_file():
            merged = _merge(merged, _read_toml(path))

    resolved_project: Path | None = None
    if project_path is not None:
        resolved_project = Path(project_path)
        if resolved_project.is_file():
            merged = _merge(merged, _read_toml(resolved_project, project_surface=True))

    if project_local_path is not None:
        local = Path(project_local_path)
    elif resolved_project is not None:
        local = resolved_project.with_name(DEFAULT_PROJECT_LOCAL)
    else:
        local = None
    if local is not None and local.is_file():
        local_values = _read_toml(local)
        # The ignored local file also owns the per-project host/checkout boundary.
        # Project policy validates that section; worker parsing must not treat it as
        # an executable profile surface or merge it into the public configuration.
        local_values.pop("execution", None)
        merged = _merge(merged, local_values)
    return parse_agent_config(merged)


def require_capabilities(profile: Profile, required: Iterable[str]) -> None:
    """Fail closed when a node asks for capabilities a profile did not declare."""

    requested = frozenset(required)
    unknown = sorted(requested - CAPABILITY_NAMES)
    if unknown:
        raise _error(
            "UNKNOWN_CAPABILITY", f"profiles.{profile.name}", f"unknown: {unknown}"
        )
    missing = sorted(requested - profile.capabilities.enabled())
    if missing:
        raise CapabilityMismatchError(
            "CAPABILITY_MISMATCH",
            f"profiles.{profile.name}",
            f"profile lacks required capabilities: {missing}",
        )


def get_profile(
    config: AgentConfig, name: str, *, required: Iterable[str] = ()
) -> Profile:
    """Resolve a concrete ``node.profile`` and enforce its requirements.

    Pool/tier selection belongs in graph compilation. Persisted runs pin this
    concrete profile name so resume cannot silently switch worker or model.
    """

    _name(name, "node.profile")
    profile = config.profiles.get(name)
    if profile is None:
        raise _error("UNKNOWN_PROFILE", "node.profile", f"unknown profile {name!r}")
    require_capabilities(profile, required)
    return profile


def _selection_from_mapping(
    values: Mapping[str, str | None], path: str
) -> tuple[str, str] | None:
    present = [(kind, value) for kind, value in values.items() if value]
    if len(present) > 1:
        raise _error(
            "SELECTION_ERROR", path, "set at most one of profile, pool, or tier"
        )
    return present[0] if present else None


def select_profile(
    config: AgentConfig,
    *,
    profile: str | None = None,
    pool: str | None = None,
    tier: str | None = None,
    routing_key: str = "default",
    environ: Mapping[str, str] | None = None,
) -> Profile:
    """Resolve CLI -> environment -> configured default, then route deterministically."""

    cli = _selection_from_mapping(
        {"profile": profile, "pool": pool, "tier": tier}, "cli"
    )
    selection = cli
    if selection is None:
        environment = os.environ if environ is None else environ
        selection = _selection_from_mapping(
            {
                "profile": environment.get("GRAPH_ENGINEERING_PROFILE"),
                "pool": environment.get("GRAPH_ENGINEERING_POOL"),
                "tier": environment.get("GRAPH_ENGINEERING_TIER"),
            },
            "environment",
        )
    if selection is None:
        selection = _selection_from_mapping(
            {
                "profile": config.routing.default_profile,
                "pool": config.routing.default_pool,
                "tier": config.routing.default_tier,
            },
            "routing",
        )
    if selection is None:
        raise _error(
            "NO_PROFILE_SELECTED", "routing", "select a profile, pool, or tier"
        )
    kind, name = selection
    _name(name, kind)
    if kind == "tier":
        selected_tier = config.tiers.get(name)
        if selected_tier is None:
            raise _error("UNKNOWN_TIER", "tier", f"unknown tier {name!r}")
        kind, name = (
            ("profile", selected_tier.profile)
            if selected_tier.profile is not None
            else ("pool", selected_tier.pool)
        )
    if kind == "profile":
        selected = config.profiles.get(name)
        if selected is None:
            raise _error("UNKNOWN_PROFILE", "profile", f"unknown profile {name!r}")
        return selected

    selected_pool = config.pools.get(name)
    if selected_pool is None:
        raise _error("UNKNOWN_POOL", "pool", f"unknown pool {name!r}")
    if selected_pool.strategy == "first":
        selected_name = selected_pool.profiles[0]
    else:
        digest = hashlib.sha256(
            f"{selected_pool.name}\0{routing_key}".encode()
        ).digest()
        selected_name = selected_pool.profiles[
            int.from_bytes(digest[:8], "big") % len(selected_pool.profiles)
        ]
    return config.profiles[selected_name]
