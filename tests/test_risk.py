from __future__ import annotations

from dataclasses import replace

import pytest

from graph_engineering.risk import (
    ApprovalBoundary,
    ProducerIdentity,
    RiskLevel,
    RiskPolicyError,
    VerificationPlan,
    VerifierIdentity,
    validate_risk_assignments,
    validate_verification_plan,
)

PRODUCER = ProducerIdentity("build", "builder", "implementation", "ctx-build")


def verifier(number: int, *, summary: bool = False) -> VerifierIdentity:
    return VerifierIdentity(
        f"verify-{number}",
        f"reviewer-{number}",
        f"review-lineage-{number}",
        f"ctx-review-{number}",
        f"lens-{number}",
        ("checks/test-results.json", "artifacts/change.diff"),
        summary,
    )


def test_low_risk_is_lightweight_deterministic_only():
    validate_verification_plan(VerificationPlan(RiskLevel.LOW, ("schema",), PRODUCER))
    with pytest.raises(RiskPolicyError) as caught:
        validate_verification_plan(
            VerificationPlan(RiskLevel.LOW, ("schema",), PRODUCER, (verifier(1),))
        )
    assert caught.value.code == "LOW_RISK_OVERREACH"


def test_medium_requires_independent_raw_evidence_verifier():
    validate_verification_plan(
        VerificationPlan(RiskLevel.MEDIUM, ("pytest",), PRODUCER, (verifier(1),))
    )

    for same_identity in (
        replace(verifier(1), profile=PRODUCER.profile),
        replace(verifier(1), lineage=PRODUCER.lineage),
        replace(verifier(1), context_digest=PRODUCER.context_digest),
    ):
        with pytest.raises(RiskPolicyError) as caught:
            validate_verification_plan(
                VerificationPlan(
                    RiskLevel.MEDIUM, ("pytest",), PRODUCER, (same_identity,)
                )
            )
        assert caught.value.code == "VERIFIER_NOT_INDEPENDENT"

    with pytest.raises(RiskPolicyError) as caught:
        validate_verification_plan(
            VerificationPlan(
                RiskLevel.MEDIUM,
                ("pytest",),
                PRODUCER,
                (replace(verifier(1), raw_evidence_refs=()),),
            )
        )
    assert caught.value.code == "RAW_EVIDENCE_REQUIRED"

    with pytest.raises(RiskPolicyError) as caught:
        validate_verification_plan(
            VerificationPlan(
                RiskLevel.MEDIUM, ("pytest",), PRODUCER, (verifier(1, summary=True),)
            )
        )
    assert caught.value.code == "SUMMARY_CONTAMINATION"

    with pytest.raises(RiskPolicyError) as caught:
        validate_verification_plan(
            VerificationPlan(
                RiskLevel.MEDIUM, ("pytest",), PRODUCER, (verifier(1), verifier(2))
            )
        )
    assert caught.value.code == "INDEPENDENT_VERIFIER_REQUIRED"


def test_high_requires_distinct_lenses_and_named_effect_approval():
    approval = ApprovalBoundary(
        "production-release", "Mav", frozenset({"deploy-production"})
    )
    plan = VerificationPlan(
        RiskLevel.HIGH, ("pytest",), PRODUCER, (verifier(1), verifier(2)), approval
    )
    validate_risk_assignments(
        (plan,), declared_effects={"build": frozenset({"deploy-production"})}
    )

    for colluding in (
        replace(verifier(2), profile=verifier(1).profile),
        replace(verifier(2), lens=verifier(1).lens),
    ):
        with pytest.raises(RiskPolicyError) as caught:
            validate_verification_plan(
                replace(plan, verifiers=(verifier(1), colluding))
            )
        assert caught.value.code == "VERIFIER_COLLUSION"

    with pytest.raises(RiskPolicyError) as caught:
        validate_verification_plan(replace(plan, approval=None))
    assert caught.value.code == "HIGH_RISK_APPROVAL_REQUIRED"

    with pytest.raises(RiskPolicyError) as caught:
        validate_risk_assignments(
            (plan,), declared_effects={"build": frozenset({"delete-data"})}
        )
    assert caught.value.code == "UNAPPROVED_EFFECT"


def test_every_risk_level_requires_deterministic_anchor():
    with pytest.raises(RiskPolicyError) as caught:
        validate_verification_plan(VerificationPlan(RiskLevel.LOW, (), PRODUCER))
    assert caught.value.code == "DETERMINISTIC_CHECK_REQUIRED"
