from __future__ import annotations

import json

import pytest

from graph_engineering import cli
from graph_engineering.economics import (
    OUTCOME_VERSION,
    EconomicsError,
    promotion_evidence,
    summarize_outcomes,
    validate_outcome,
)


def outcome(match: str, mode: str, *, wall: float, cost: float | None = 1.0):
    return {
        "version": OUTCOME_VERSION,
        "id": f"{match}-{mode}",
        "match_id": match,
        "task_id": "repeatable-audit",
        "mode": mode,
        "objective": "latency",
        "acceptance_suite": "pytest:contract-v3",
        "accepted": True,
        "wall_seconds": wall,
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_usd": cost,
        "model": "test-model",
        "verifier_decisions": 2,
        "verifier_overturns": 1,
        "cold_adoption_seconds": 0 if mode == "LINEAR" else 4,
        "integration_failures": 0,
        "escaped_defects": 0,
        "failure_class": "none",
        "guard_metrics": [],
        "merged_proof_seconds": 8,
        "deployed_proof_seconds": 10,
        "live_proof_seconds": 12,
        "evidence": [f"receipt:{match}"],
    }


def matched(count: int = 3):
    values = []
    for index in range(count):
        values.extend(
            [
                validate_outcome(outcome(str(index), "LINEAR", wall=100)),
                validate_outcome(outcome(str(index), "TRANSIENT_GRAPH", wall=60)),
            ]
        )
    return values


def test_outcome_requires_all_cost_quality_and_live_proof_fields():
    record = validate_outcome(outcome("a", "LINEAR", wall=10))
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 20
    assert record["verifier_overturns"] == 1
    assert record["live_proof_seconds"] == 12.0
    assert len(record["digest"]) == 64


def test_expected_rejection_is_not_an_operational_failure():
    value = outcome("a", "LINEAR", wall=10)
    value["accepted"] = False
    value["failure_class"] = "expected_rejection"
    assert validate_outcome(value)["failure_class"] == "expected_rejection"


def test_promotion_requires_three_equal_acceptance_matched_wins():
    report = promotion_evidence(matched())
    assert report["eligible"] is True
    assert report["decision"] == "ELIGIBLE_FOR_NAMED_REVIEW"
    assert report["matched_wins"] == 3
    # Cold-adoption cost is charged to the graph's end-to-end latency.
    assert report["wall_speedup_percent"] == 36.0
    assert report["review_required"] is True


def test_promotion_rejects_costless_or_slower_pairs():
    records = matched(2)
    records.extend(
        [
            validate_outcome(outcome("costless", "LINEAR", wall=100)),
            validate_outcome(
                outcome("costless", "TRANSIENT_GRAPH", wall=60, cost=None)
            ),
            validate_outcome(outcome("slower", "LINEAR", wall=100)),
            validate_outcome(outcome("slower", "TRANSIENT_GRAPH", wall=110)),
        ]
    )
    report = promotion_evidence(records)
    assert report["eligible"] is False
    assert {item["reason"] for item in report["rejected"]} == {
        "both costs must be reported",
        "transient graph did not win declared latency objective",
    }


def test_promotion_accepts_repeated_quality_wins_and_rejects_escaped_defect_regression():
    records = matched()
    for record in records:
        record["objective"] = "quality"
        record["escaped_defects"] = 2 if record["mode"] == "LINEAR" else 0
    report = promotion_evidence(records)
    assert report["eligible"] is True
    assert report["declared_objective"] == "quality"
    broken = [dict(record) for record in records]
    broken[1]["escaped_defects"] = 3
    rejected = promotion_evidence(broken)
    assert rejected["eligible"] is False
    assert any(
        item["reason"] == "transient graph worsened escaped defects"
        for item in rejected["rejected"]
    )


def test_verifier_overturns_cannot_exceed_decisions():
    value = outcome("a", "LINEAR", wall=10)
    value["verifier_overturns"] = 3
    with pytest.raises(EconomicsError, match="exceeds"):
        validate_outcome(value)


def test_cli_promote_reports_evidence_instead_of_auto_promoting(tmp_path, capsys):
    source = tmp_path / "outcomes.json"
    # Input records are source documents, so remove computed digests rather than trust them.
    source.write_text(
        json.dumps(
            [
                {key: value for key, value in item.items() if key != "digest"}
                for item in matched()
            ]
        )
    )
    assert cli.main(["promote", "--evidence", str(source), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["promotion"]["eligible"] is True
    assert payload["promotion"]["review_required"] is True


def test_cli_outcome_records_and_stats_reports_economics(tmp_path, monkeypatch, capsys):
    source = tmp_path / "outcome.json"
    source.write_text(json.dumps(outcome("reported", "TRANSIENT_GRAPH", wall=10)))
    log = tmp_path / "outcomes.jsonl"
    monkeypatch.setenv("GRAPH_ENGINEERING_OUTCOME_LOG", str(log))
    assert cli.main(["outcome", "--input", str(source), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["recorded"] == 1
    report = summarize_outcomes()
    assert report["input_tokens"] == 100
    assert report["output_tokens"] == 20
    assert report["cost_usd"] == 1.0
    assert report["verifier_overturns"] == 1
    assert report["cold_adoption_seconds"] == 4.0
    assert report["integration_failures"] == 0
    assert report["escaped_defects"] == 0
    assert report["merged_proofs"] == 1
    assert report["deployed_proofs"] == 1
    assert report["live_proofs"] == 1


def test_legacy_v1_outcome_still_validates_without_guard_metrics():
    value = outcome("a", "LINEAR", wall=10)
    value["version"] = "graph-engineering/outcome/v1"
    del value["guard_metrics"]
    record = validate_outcome(value)
    assert record["version"] == "graph-engineering/outcome/v1"
    assert "guard_metrics" not in record


def test_accepted_outcome_cannot_carry_a_regressed_guard_metric():
    # intent: Goodhart guard. "Resolution rate up while churn doubled" must be
    # unrepresentable as an accepted outcome.
    value = outcome("a", "TRANSIENT_GRAPH", wall=10)
    value["guard_metrics"] = [{"name": "churn_rate", "regressed": True}]
    with pytest.raises(EconomicsError, match="guard metric is regressed"):
        validate_outcome(value)
    value["accepted"] = False
    value["failure_class"] = "expected_rejection"
    record = validate_outcome(value)
    assert record["guard_metrics"] == [{"name": "churn_rate", "regressed": True}]


def test_guard_metric_entries_are_closed_and_bounded():
    value = outcome("a", "LINEAR", wall=10)
    value["guard_metrics"] = [{"name": "churn_rate", "regressed": False, "extra": 1}]
    with pytest.raises(EconomicsError, match="guard_metrics entries"):
        validate_outcome(value)
    value["guard_metrics"] = [{"name": "", "regressed": False}]
    with pytest.raises(EconomicsError, match="guard_metrics entries"):
        validate_outcome(value)
    value["guard_metrics"] = "churn"
    with pytest.raises(EconomicsError, match="guard_metrics must be a list"):
        validate_outcome(value)


def test_promotion_rejects_pairs_with_regressed_guard_metrics():
    records = [dict(record) for record in matched()]
    # Bypass validate_outcome deliberately: promotion must reject a regressed
    # guard metric even on records that dodged the acceptance-time guard.
    records[1]["guard_metrics"] = [{"name": "churn_rate", "regressed": True}]
    report = promotion_evidence(records)
    assert report["eligible"] is False
    assert any(
        item["reason"] == "a run reports a regressed guard metric"
        for item in report["rejected"]
    )


def test_summary_counts_guard_metric_regressions(tmp_path, monkeypatch):
    log = tmp_path / "outcomes.jsonl"
    monkeypatch.setenv("GRAPH_ENGINEERING_OUTCOME_LOG", str(log))
    value = outcome("regressed", "TRANSIENT_GRAPH", wall=10)
    value["accepted"] = False
    value["failure_class"] = "expected_rejection"
    value["guard_metrics"] = [{"name": "churn_rate", "regressed": True}]
    from graph_engineering.economics import record_outcomes

    record_outcomes([validate_outcome(value)])
    record_outcomes([validate_outcome(outcome("clean", "LINEAR", wall=10))])
    report = summarize_outcomes()
    assert report["total"] == 2
    assert report["guard_metric_regressions"] == 1
