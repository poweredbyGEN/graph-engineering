from __future__ import annotations

from dataclasses import replace

import pytest

from graph_engineering.role_policy import (
    ROLE_POLICY_VERSION,
    AuthorityError,
    CostCeiling,
    WorkAuthority,
    authorize_private_profiles,
    authorize_work,
    bind_role_policy,
    load_role_policy,
    parse_role_policy,
    parse_work_authority,
)


def policy_value() -> dict:
    return {
        "version": ROLE_POLICY_VERSION,
        "generation": 3,
        "approved_by": "Release-Owner",
        "profiles": {"worker": ["read", "structured_output", "write", "worktree"]},
        "tools": ["git", "pytest"],
        "write_scopes": ["src", "tests"],
        "effects": ["repository-write", "deploy"],
        "deployment_targets": ["staging"],
        "approval_boundaries": ["release-owner"],
        "effect_approvals": {"deploy": ["release-owner"]},
        "cost": {
            "max_input_tokens": 1000,
            "max_output_tokens": 500,
            "max_total_tokens": 1200,
            "max_cost_microusd": 250000,
        },
    }


def work() -> WorkAuthority:
    return WorkAuthority(
        profile="worker",
        capabilities=frozenset({"read", "write"}),
        tools=frozenset({"git"}),
        write_scopes=("src/graph_engineering",),
        effects=frozenset({"repository-write"}),
        deployment_targets=frozenset(),
        approval_boundaries=frozenset(),
        cost=CostCeiling(800, 300, 1000, 100000),
    )


def test_work_graph_can_only_narrow_role_policy():
    policy = parse_role_policy(policy_value())
    authorize_work(policy, work())
    assert policy.generation == 3
    assert len(policy.digest) == 64


@pytest.mark.parametrize(
    ("changed", "code"),
    [
        ({"profile": "admin"}, "PROFILE_NOT_AUTHORIZED"),
        ({"capabilities": frozenset({"read", "mcp"})}, "CAPABILITY_EXPANSION"),
        ({"tools": frozenset({"shell"})}, "TOOL_EXPANSION"),
        ({"write_scopes": ("ops",)}, "WRITE_SCOPE_EXPANSION"),
        ({"effects": frozenset({"email"})}, "EFFECT_EXPANSION"),
        ({"deployment_targets": frozenset({"production"})}, "DEPLOYMENT_EXPANSION"),
        ({"approval_boundaries": frozenset({"self"})}, "APPROVAL_EXPANSION"),
        ({"cost": CostCeiling(1001, 500, 1201, 250000)}, "COST_EXPANSION"),
    ],
)
def test_every_authority_dimension_fails_closed_on_expansion(changed, code):
    with pytest.raises(AuthorityError) as caught:
        authorize_work(parse_role_policy(policy_value()), replace(work(), **changed))
    assert caught.value.code == code


def test_role_policy_rejects_unknown_fields_and_capabilities():
    value = policy_value()
    value["future_escape_hatch"] = True
    with pytest.raises(AuthorityError, match="fields must be exactly"):
        parse_role_policy(value)

    value = policy_value()
    value["profiles"]["worker"].append("root")
    with pytest.raises(AuthorityError, match="unknown capabilities"):
        parse_role_policy(value)


def test_effect_cannot_remove_role_policy_approval_obligation():
    requested = replace(
        work(),
        effects=frozenset({"deploy"}),
        deployment_targets=frozenset({"staging"}),
    )
    with pytest.raises(AuthorityError) as caught:
        authorize_work(parse_role_policy(policy_value()), requested)
    assert caught.value.code == "REQUIRED_APPROVAL_MISSING"

    authorize_work(
        parse_role_policy(policy_value()),
        replace(requested, approval_boundaries=frozenset({"release-owner"})),
    )


def test_cost_limits_are_explicit_and_internally_consistent():
    with pytest.raises(AuthorityError, match="directional token ceiling"):
        CostCeiling(100, 50, 99, 1)
    with pytest.raises(AuthorityError, match="non-negative integer"):
        CostCeiling(100, 50, 150, -1)


def test_untrusted_work_authority_is_strictly_parsed():
    value = {
        "profile": "worker",
        "capabilities": ["read"],
        "tools": ["git"],
        "write_scopes": [],
        "effects": [],
        "deployment_targets": [],
        "approval_boundaries": [],
        "cost": {
            "max_input_tokens": 100,
            "max_output_tokens": 50,
            "max_total_tokens": 150,
            "max_cost_microusd": 1000,
        },
    }
    authorize_work(parse_role_policy(policy_value()), parse_work_authority(value))
    value["unknown"] = "authority"
    with pytest.raises(AuthorityError) as caught:
        parse_work_authority(value)
    assert caught.value.code == "WORK_AUTHORITY_SCHEMA"


def test_checked_in_policy_binding_detects_generation_or_content_drift(tmp_path):
    directory = tmp_path / ".graph-engineering"
    directory.mkdir()
    value = policy_value()
    (directory / "role-policy.json").write_text(
        __import__("json").dumps(value), encoding="utf-8"
    )
    policy = load_role_policy(tmp_path)
    binding = bind_role_policy(policy)
    assert load_role_policy(tmp_path, binding=binding).digest == policy.digest

    value["tools"].append("ruff")
    (directory / "role-policy.json").write_text(
        __import__("json").dumps(value), encoding="utf-8"
    )
    with pytest.raises(AuthorityError) as caught:
        load_role_policy(tmp_path, binding=binding)
    assert caught.value.code == "ROLE_POLICY_DRIFT"


def test_private_profile_capability_expansion_is_rejected():
    from graph_engineering.config import parse_agent_config

    value = policy_value()
    config_value = {
        "version": 1,
        "profiles": {
            "worker": {
                "adapter": "subprocess",
                "model": "test-model",
                "capabilities": {
                    "read": True,
                    "write": True,
                    "structured_output": True,
                    "worktree": True,
                    "resume": False,
                    "mcp": False,
                },
                "subprocess": {
                    "argv": ["agent"],
                    "prompt_transport": "stdin",
                    "output_format": "json",
                    "env_allowlist": [],
                },
            }
        },
    }
    config = parse_agent_config(config_value)
    authorize_private_profiles(
        parse_role_policy(value), config, selected_profiles=("worker",)
    )

    config_value["profiles"]["worker"]["capabilities"]["mcp"] = True
    with pytest.raises(AuthorityError) as caught:
        authorize_private_profiles(
            parse_role_policy(value),
            parse_agent_config(config_value),
            selected_profiles=("worker",),
        )
    assert caught.value.code == "PRIVATE_PROFILE_EXPANSION"
