from __future__ import annotations

import copy
from pathlib import Path

import pytest

from graph_engineering.config import (
    CapabilityMismatchError,
    ConfigError,
    get_profile,
    load_agent_config,
    parse_agent_config,
    require_capabilities,
    select_profile,
)


def profile(model: str = "model-a") -> dict:
    return {
        "adapter": "subprocess",
        "model": model,
        "capabilities": {
            "read": True,
            "write": False,
            "structured_output": True,
            "worktree": False,
            "resume": False,
            "mcp": False,
        },
        "subprocess": {
            "argv": ["agent", "--model", "{model}"],
            "prompt_transport": "stdin",
            "output_format": "json",
            "env_allowlist": ["AGENT_API_KEY"],
        },
    }


def config() -> dict:
    return {
        "version": 1,
        "profiles": {"alpha": profile(), "beta": profile("model-b")},
        "pools": {
            "workers": {"profiles": ["alpha", "beta"], "strategy": "stable-hash"}
        },
        "tiers": {"mechanical": {"pool": "workers"}, "judgment": {"profile": "beta"}},
        "routing": {"default_tier": "mechanical"},
    }


def write(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    return path


def test_arbitrary_named_profiles_pools_and_tiers_parse():
    parsed = parse_agent_config(config())
    assert sorted(parsed.profiles) == ["alpha", "beta"]
    assert parsed.tiers["judgment"].profile == "beta"


def test_hostile_argv_placeholder_is_rejected():
    # intent: Formatter attribute/index traversal must never become an argv expansion gadget.
    value = config()
    value["profiles"]["alpha"]["subprocess"]["argv"] = ["agent", "{prompt.__class__}"]
    value["profiles"]["alpha"]["subprocess"]["prompt_transport"] = "argv"
    with pytest.raises(ConfigError) as caught:
        parse_agent_config(value)
    assert caught.value.code == "UNSAFE_PLACEHOLDER"


def test_executable_cannot_be_templated():
    value = config()
    value["profiles"]["alpha"]["subprocess"]["argv"] = ["{model}"]
    with pytest.raises(ConfigError) as caught:
        parse_agent_config(value)
    assert caught.value.code == "UNSAFE_EXECUTABLE"


def test_shell_execution_flag_is_not_part_of_the_contract():
    # intent: adapters are execve-style argv only; config cannot opt into a shell.
    value = config()
    value["profiles"]["alpha"]["subprocess"]["shell"] = True
    with pytest.raises(ConfigError) as caught:
        parse_agent_config(value)
    assert caught.value.code == "UNKNOWN_FIELD"


def test_literal_secret_is_rejected_even_before_unknown_field_check():
    # intent: private config points at secret-bearing env vars; it never stores their values.
    value = config()
    value["profiles"]["alpha"]["api_key"] = "literal-secret"
    with pytest.raises(ConfigError) as caught:
        parse_agent_config(value)
    assert caught.value.code == "LITERAL_SECRET"


def test_environment_entries_are_names_not_assignments():
    value = config()
    value["profiles"]["alpha"]["subprocess"]["env_allowlist"] = ["TOKEN=literal"]
    with pytest.raises(ConfigError) as caught:
        parse_agent_config(value)
    assert caught.value.code == "INVALID_ENV_REFERENCE"


def test_all_capabilities_must_be_explicit():
    value = config()
    del value["profiles"]["alpha"]["capabilities"]["mcp"]
    with pytest.raises(ConfigError) as caught:
        parse_agent_config(value)
    assert caught.value.code == "MISSING_FIELD"


def test_capability_mismatch_fails_closed():
    parsed = parse_agent_config(config())
    with pytest.raises(CapabilityMismatchError) as caught:
        require_capabilities(parsed.profiles["alpha"], {"read", "write", "worktree"})
    assert caught.value.code == "CAPABILITY_MISMATCH"
    assert "worktree" in caught.value.message
    assert "write" in caught.value.message


def test_unknown_pool_profile_is_rejected():
    value = config()
    value["pools"]["workers"]["profiles"].append("ghost")
    with pytest.raises(ConfigError) as caught:
        parse_agent_config(value)
    assert caught.value.code == "UNKNOWN_PROFILE"


def test_unknown_concrete_node_profile_is_rejected():
    parsed = parse_agent_config(config())
    with pytest.raises(ConfigError) as caught:
        get_profile(parsed, "ghost", required={"read"})
    assert caught.value.code == "UNKNOWN_PROFILE"


def test_pool_routing_is_stable_and_keyed():
    parsed = parse_agent_config(config())
    first = select_profile(parsed, pool="workers", routing_key="node-17", environ={})
    repeated = select_profile(parsed, pool="workers", routing_key="node-17", environ={})
    assert first.name == repeated.name
    # intent: this is a compatibility vector; changing the hashing algorithm is a contract change.
    assert first.name == "alpha"


def test_cli_selection_overrides_environment_and_project_default():
    parsed = parse_agent_config(config())
    chosen = select_profile(
        parsed,
        profile="alpha",
        environ={"GRAPH_ENGINEERING_PROFILE": "beta"},
    )
    assert chosen.name == "alpha"


def test_cli_selection_does_not_parse_shadowed_environment_selectors():
    parsed = parse_agent_config(config())
    chosen = select_profile(
        parsed,
        profile="alpha",
        environ={
            "GRAPH_ENGINEERING_PROFILE": "beta",
            "GRAPH_ENGINEERING_POOL": "workers",
        },
    )
    assert chosen.name == "alpha"


def test_environment_selection_overrides_project_default():
    parsed = parse_agent_config(config())
    chosen = select_profile(parsed, environ={"GRAPH_ENGINEERING_PROFILE": "beta"})
    assert chosen.name == "beta"


def test_user_definitions_project_routing_and_local_override_precedence(tmp_path: Path):
    user = write(
        tmp_path / "user.toml",
        """
version = 1
[profiles.alpha]
adapter = "subprocess"
model = "private-model"
[profiles.alpha.capabilities]
read = true
write = false
structured_output = true
worktree = false
resume = false
mcp = false
[profiles.alpha.subprocess]
argv = ["agent", "--model", "{model}"]
prompt_transport = "stdin"
output_format = "json"
env_allowlist = ["AGENT_API_KEY"]
""",
    )
    project = write(
        tmp_path / ".graph-engineering.toml",
        'version = 1\n[routing]\ndefault_profile = "alpha"\n',
    )
    local = write(
        tmp_path / ".graph-engineering.local.toml",
        'version = 1\n[profiles.alpha]\nmodel = "local-model"\n'
        '[execution]\nallowed_hosts = ["private-host"]\n'
        f'allowed_checkout_roots = ["{tmp_path}"]\n',
    )
    parsed = load_agent_config(
        user_path=user, project_path=project, project_local_path=local
    )
    assert parsed.profiles["alpha"].model == "local-model"
    assert select_profile(parsed, environ={}).name == "alpha"


def test_higher_precedence_routing_replaces_lower_selector(tmp_path: Path):
    user = write(
        tmp_path / "user.toml", 'version = 1\n[routing]\ndefault_pool = "workers"\n'
    )
    project = write(
        tmp_path / ".graph-engineering.toml",
        'version = 1\n[routing]\ndefault_profile = "alpha"\n',
    )
    parsed = load_agent_config(
        defaults=config(), user_path=user, project_path=project, project_local_path=None
    )
    assert parsed.routing.default_profile == "alpha"
    assert parsed.routing.default_pool is None


def test_public_example_is_valid():
    import tomllib

    source = Path(__file__).parents[1] / "subagents.example.toml"
    parsed = parse_agent_config(tomllib.loads(source.read_text(encoding="utf-8")))
    assert set(parsed.profiles) == {"claude", "codex", "grok", "kimi-k3", "glm-5.2"}
    assert all(not profile.capabilities.resume for profile in parsed.profiles.values())


def test_checked_in_project_cannot_define_commands(tmp_path: Path):
    # intent: cloning a repository must not authorize it to replace the worker executable.
    project = write(
        tmp_path / ".graph-engineering.toml",
        'version = 1\n[profiles.hostile]\nadapter = "subprocess"\n',
    )
    with pytest.raises(ConfigError) as caught:
        load_agent_config(user_path=None, project_path=project)
    assert caught.value.code == "UNKNOWN_FIELD"


def test_openai_compatible_uses_environment_references_only():
    value = config()
    value["profiles"]["remote"] = {
        "adapter": "openai-compatible",
        "model": "portable-model",
        "capabilities": copy.deepcopy(value["profiles"]["alpha"]["capabilities"]),
        "openai_compatible": {
            "endpoint_env": "REMOTE_BASE_URL",
            "api_key_env": "REMOTE_API_KEY",
        },
    }
    parsed = parse_agent_config(value)
    assert parsed.profiles["remote"].adapter_kind == "openai-compatible"
