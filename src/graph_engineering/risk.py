"""Mechanically enforced verification requirements by declared work risk."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskPolicyError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ProducerIdentity:
    node_id: str
    profile: str
    lineage: str
    context_digest: str


@dataclass(frozen=True)
class VerifierIdentity:
    node_id: str
    profile: str
    lineage: str
    context_digest: str
    lens: str
    raw_evidence_refs: tuple[str, ...]
    receives_producer_summary: bool = False


@dataclass(frozen=True)
class ApprovalBoundary:
    boundary_id: str
    approved_by: str
    effects: frozenset[str]


@dataclass(frozen=True)
class VerificationPlan:
    risk: RiskLevel
    deterministic_checks: tuple[str, ...]
    producer: ProducerIdentity
    verifiers: tuple[VerifierIdentity, ...] = ()
    approval: ApprovalBoundary | None = None


def _present(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise RiskPolicyError(
            "RISK_SCHEMA", f"{field} must be a bounded non-empty string"
        )


def _check_identity(identity: ProducerIdentity | VerifierIdentity, label: str) -> None:
    for field in ("node_id", "profile", "lineage", "context_digest"):
        _present(getattr(identity, field), f"{label}.{field}")


def validate_verification_plan(plan: VerificationPlan) -> None:
    """Reject a plan unless its proof topology matches its risk.

    Independence is structural, not prompt prose: verifier profile, lineage and
    context must all differ from the producer.  Verifiers consume named raw
    evidence references and may not receive the producer's narrative summary.
    """

    if not isinstance(plan.risk, RiskLevel):
        raise RiskPolicyError("RISK_SCHEMA", "risk must be a RiskLevel")
    _check_identity(plan.producer, "producer")
    if not plan.deterministic_checks or any(
        not isinstance(item, str) or not item.strip()
        for item in plan.deterministic_checks
    ):
        raise RiskPolicyError(
            "DETERMINISTIC_CHECK_REQUIRED",
            "every risk level requires a named deterministic check",
        )
    if len(set(plan.deterministic_checks)) != len(plan.deterministic_checks):
        raise RiskPolicyError("RISK_SCHEMA", "deterministic checks contain duplicates")

    if plan.risk is RiskLevel.LOW:
        if plan.verifiers or plan.approval is not None:
            raise RiskPolicyError(
                "LOW_RISK_OVERREACH", "low risk uses deterministic checks only"
            )
        return

    if plan.risk is RiskLevel.MEDIUM and len(plan.verifiers) != 1:
        raise RiskPolicyError(
            "INDEPENDENT_VERIFIER_REQUIRED",
            "medium risk requires exactly one independent verifier",
        )
    if plan.risk is RiskLevel.HIGH and len(plan.verifiers) < 2:
        raise RiskPolicyError(
            "INDEPENDENT_VERIFIER_REQUIRED",
            "high risk requires at least two independent verifier lenses",
        )
    seen_nodes: set[str] = set()
    seen_profiles: set[str] = set()
    seen_lineages: set[str] = set()
    seen_contexts: set[str] = set()
    seen_lenses: set[str] = set()
    for index, verifier in enumerate(plan.verifiers):
        label = f"verifiers[{index}]"
        _check_identity(verifier, label)
        _present(verifier.lens, f"{label}.lens")
        if (
            verifier.profile == plan.producer.profile
            or verifier.lineage == plan.producer.lineage
            or verifier.context_digest == plan.producer.context_digest
        ):
            raise RiskPolicyError(
                "VERIFIER_NOT_INDEPENDENT",
                f"{label} must use a distinct profile, lineage, and context",
            )
        if not isinstance(verifier.receives_producer_summary, bool):
            raise RiskPolicyError(
                "RISK_SCHEMA", f"{label}.receives_producer_summary must be boolean"
            )
        if verifier.receives_producer_summary:
            raise RiskPolicyError(
                "SUMMARY_CONTAMINATION",
                f"{label} may consume raw evidence, not the producer summary",
            )
        if not verifier.raw_evidence_refs or any(
            not isinstance(ref, str) or not ref.strip()
            for ref in verifier.raw_evidence_refs
        ):
            raise RiskPolicyError(
                "RAW_EVIDENCE_REQUIRED",
                f"{label} requires named raw evidence references",
            )
        for value, seen, field in (
            (verifier.node_id, seen_nodes, "node"),
            (verifier.profile, seen_profiles, "profile"),
            (verifier.lineage, seen_lineages, "lineage"),
            (verifier.context_digest, seen_contexts, "context"),
            (verifier.lens, seen_lenses, "lens"),
        ):
            if value in seen:
                raise RiskPolicyError(
                    "VERIFIER_COLLUSION", f"verifiers must have distinct {field}s"
                )
            seen.add(value)

    if plan.risk is RiskLevel.MEDIUM:
        if plan.approval is not None:
            raise RiskPolicyError(
                "MEDIUM_RISK_OVERREACH",
                "medium risk requires independent verification, not an effect approval boundary",
            )
        return

    if plan.approval is None:
        raise RiskPolicyError(
            "HIGH_RISK_APPROVAL_REQUIRED",
            "high risk requires a named human/effect approval boundary",
        )
    _present(plan.approval.boundary_id, "approval.boundary_id")
    _present(plan.approval.approved_by, "approval.approved_by")
    if not plan.approval.effects or any(
        not isinstance(effect, str) or not effect.strip()
        for effect in plan.approval.effects
    ):
        raise RiskPolicyError(
            "HIGH_RISK_APPROVAL_REQUIRED",
            "approval must bind at least one named effect",
        )


def validate_risk_assignments(
    plans: Sequence[VerificationPlan],
    *,
    declared_effects: Mapping[str, frozenset[str]] | None = None,
) -> None:
    """Validate a graph's plans and bind high-risk approvals to real effects."""

    if not plans:
        raise RiskPolicyError(
            "RISK_PLAN_REQUIRED",
            "work graph must declare at least one verification plan",
        )
    effects = {} if declared_effects is None else declared_effects
    seen_producers: set[str] = set()
    for plan in plans:
        if plan.producer.node_id in seen_producers:
            raise RiskPolicyError(
                "RISK_SCHEMA", f"duplicate producer plan {plan.producer.node_id!r}"
            )
        seen_producers.add(plan.producer.node_id)
        validate_verification_plan(plan)
        if plan.risk is RiskLevel.HIGH:
            actual = effects.get(plan.producer.node_id, frozenset())
            assert plan.approval is not None
            missing = sorted(actual - plan.approval.effects)
            if missing:
                raise RiskPolicyError(
                    "UNAPPROVED_EFFECT",
                    f"approval boundary omits producer effects: {missing}",
                )
