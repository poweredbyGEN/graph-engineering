"""Runtime join between reviewed role authority, risk topology, and receipts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .adapters import ExecutionReceipt
from .artifacts import canonical_json
from .config import AgentConfig, Profile
from .risk import (
    ApprovalBoundary,
    ProducerIdentity,
    RiskLevel,
    RiskPolicyError,
    VerificationPlan,
    VerifierIdentity,
    validate_risk_assignments,
)
from .role_policy import (
    AuthorityError,
    RolePolicy,
    authorize_private_profiles,
    authorize_work,
    parse_work_authority,
)


class GovernanceError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def validate_runtime_governance(
    workflow: Mapping[str, Any],
    nodes: Mapping[str, Mapping[str, Any]],
    role_policy: RolePolicy,
    config: AgentConfig,
    profiles: Mapping[str, Profile],
    fallback_profiles: Mapping[str, Sequence[tuple[Mapping[str, Any], Profile]]],
    approvals: Mapping[str, Mapping[str, Any]],
) -> None:
    """Authorize concrete profiles/work and construct enforceable risk plans."""

    try:
        selected_profiles = {profile.name for profile in profiles.values()} | {
            profile.name
            for routes in fallback_profiles.values()
            for _route, profile in routes
        }
        authorize_private_profiles(
            role_policy, config, selected_profiles=selected_profiles
        )
        plans: list[VerificationPlan] = []
        declared_effects: dict[str, frozenset[str]] = {}
        for node in nodes.values():
            if node["kind"] != "agent":
                continue
            authority = parse_work_authority(node["authority"])
            authorize_work(role_policy, authority)
            for route in node.get("fallback", {}).get("routes", []):
                authorize_work(role_policy, parse_work_authority(route["authority"]))
            verification = node["verification"]
            producer = ProducerIdentity(
                node["id"],
                profiles[node["id"]].name,
                f"{workflow['id']}:{node['id']}",
                hashlib.sha256(canonical_json(node.get("inputs", {}))).hexdigest(),
            )
            verifiers: list[VerifierIdentity] = []
            for spec in verification["verifiers"]:
                verifier_node = nodes[spec["node"]]
                verifier_inputs = verifier_node.get("inputs", {})
                verifiers.append(
                    VerifierIdentity(
                        spec["node"],
                        profiles[spec["node"]].name,
                        f"{workflow['id']}:{spec['node']}",
                        hashlib.sha256(canonical_json(verifier_inputs)).hexdigest(),
                        spec["lens"],
                        tuple(
                            verifier_inputs[name]
                            for name in spec["raw_evidence_inputs"]
                        ),
                    )
                )
            approval = _approval_boundary(verification, approvals, authority.effects)
            plan = VerificationPlan(
                RiskLevel(verification["risk"]),
                tuple(check["id"] for check in node.get("checks", [])),
                producer,
                tuple(verifiers),
                approval,
            )
            plans.append(plan)
            declared_effects[node["id"]] = (
                authority.effects or frozenset({"acceptance"})
                if plan.risk is RiskLevel.HIGH
                else authority.effects
            )
        validate_risk_assignments(plans, declared_effects=declared_effects)
    except (AuthorityError, RiskPolicyError, KeyError, ValueError) as exc:
        raise GovernanceError(
            getattr(exc, "code", "ROLE_POLICY_INVALID"), str(exc)
        ) from exc


def _approval_boundary(
    verification: Mapping[str, Any],
    approvals: Mapping[str, Mapping[str, Any]],
    effects: frozenset[str],
) -> ApprovalBoundary | None:
    approval_id = verification.get("approval")
    if approval_id is None:
        return None
    approved_by = approvals.get(approval_id, {}).get("approved_by")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise RiskPolicyError(
            "HIGH_RISK_APPROVAL_REQUIRED",
            f"approval {approval_id!r} requires a named human approved_by",
        )
    return ApprovalBoundary(
        approval_id, approved_by.strip(), effects or frozenset({"acceptance"})
    )


def enforce_reported_cost_ceiling(
    node: Mapping[str, Any],
    receipt: ExecutionReceipt,
) -> None:
    """Stop a policy-bound node when provider-reported usage exceeds review."""

    ceiling = parse_work_authority(node["authority"]).cost
    reported = {
        "max_input_tokens": receipt.input_tokens,
        "max_output_tokens": receipt.output_tokens,
        "max_total_tokens": (
            receipt.input_tokens + receipt.output_tokens
            if receipt.input_tokens is not None and receipt.output_tokens is not None
            else None
        ),
        "max_cost_microusd": receipt.cost_microusd,
    }
    missing = sorted(field for field, value in reported.items() if value is None)
    if missing:
        raise GovernanceError(
            "USAGE_REPORT_REQUIRED",
            f"node {node['id']!r} omitted provider usage required by policy: {missing}",
        )
    exceeded = {
        field: value
        for field, value in reported.items()
        if value is not None and value > getattr(ceiling, field)
    }
    if exceeded:
        raise GovernanceError(
            "COST_BUDGET_EXCEEDED",
            f"node {node['id']!r} exceeded its reviewed token/cost ceiling: {exceeded}",
        )


__all__ = [
    "GovernanceError",
    "enforce_reported_cost_ceiling",
    "validate_runtime_governance",
]
