from __future__ import annotations

import json
from pathlib import Path

import pytest

from graph_engineering import CheckResult, Scheduler, cli
from graph_engineering.learning import (
    BASELINE_VERSION,
    FEEDBACK_VERSION,
    LearningError,
    benchmark_run,
    compare_benchmark,
    compile_feedback,
    load_baseline,
)
from graph_engineering.lifecycle import LifecycleStore, StaticRunContextProvider

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["value"],
    "properties": {"value": {"type": "integer"}},
}


def workflow() -> dict:
    def node(node_id: str, needs: list[str] | None = None) -> dict:
        return {
            "id": node_id,
            "kind": "transform",
            "task": f"run {node_id}",
            "needs": needs or [],
            "inputs": {"prior": "first.result"} if needs else {},
            "outputs": {"result": {"schema": SCHEMA}},
            "workspace": "read-only",
            "permission": "read",
            "effect": "none",
            "checks": [{"id": "accepted", "argv": ["test", node_id]}],
            "retry": {"max_attempts": 2, "no_progress_limit": 1},
            "required": True,
        }

    return {
        "version": "graph-engineering/v1alpha1",
        "id": "learning-test",
        "goal": "produce durable benchmark evidence",
        "budgets": {
            "max_nodes": 2,
            "max_concurrency": 2,
            "max_attempts_per_node": 2,
            "max_total_attempts": 4,
            "timeout_seconds": 30,
        },
        "nodes": [node("first"), node("second", ["first"])],
        "outputs": {"result": "second.result"},
    }


def completed_run(tmp_path: Path) -> tuple[Path, str]:
    value = workflow()
    state = tmp_path / "state.db"
    engine = Scheduler(
        value,
        state,
        tmp_path / "artifacts",
        {
            "first": lambda _context: {"result": {"value": 1}},
            "second": lambda _context: {"result": {"value": 2}},
        },
        lambda check, _context, _outputs: CheckResult(True, f"{check['id']} passed"),
    )
    run_id = engine.state.create_run(value, "learning-run", lifecycle=True)
    LifecycleStore(state).initialize_context(
        run_id, StaticRunContextProvider({"base_sha": "abc"})
    )
    assert engine.run(run_id, resume=True, lifecycle_resume=False).status == "succeeded"
    return state, run_id


def feedback() -> dict:
    return {
        "version": FEEDBACK_VERSION,
        "id": "feedback-1",
        "submitted_by": "reviewer",
        "summary": "Turn observed failures into enforceable project improvements.",
        "run_id": "learning-run",
        "items": [
            {
                "id": "test-missing-case",
                "source": "human",
                "observation": "The missing-input case was not tested.",
                "evidence": ["run:learning-run", "artifact:abc"],
                "target": "regression_test",
                "verify_cmd": ["pytest", "-q", "tests/test_missing.py"],
            },
            {
                "id": "skill-review",
                "source": "verifier",
                "observation": "Future workflows should ask for this boundary explicitly.",
                "evidence": ["finding:boundary"],
                "target": "skill",
                "verify_cmd": None,
            },
        ],
    }


def test_benchmark_is_bound_to_durable_evidence_and_never_invents_metrics(
    tmp_path: Path,
):
    # intent: performance claims must come from state/lifecycle evidence, not model prose.
    state, run_id = completed_run(tmp_path)
    report = benchmark_run(state, run_id)

    assert report["status"] == "succeeded"
    assert report["metrics"]["accepted_artifact_count"] == 2
    assert report["metrics"]["retry_count"] == 0
    assert report["metrics"]["time_to_first_accepted_artifact_seconds"] is not None
    assert report["metrics"]["verifier_overturn_rate"] is None
    assert report["metrics"]["human_correction_count"] is None
    assert report["evidence"]["terminal_event"] == "run.succeeded"
    assert len(report["digest"]) == 64


def test_baseline_comparison_uses_only_shared_numeric_metrics(tmp_path: Path):
    state, run_id = completed_run(tmp_path)
    report = benchmark_run(state, run_id)
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "version": BASELINE_VERSION,
                "id": "ordinary-session",
                "metrics": {"wall_seconds": 10, "human_correction_count": None},
                "evidence": {"source": "recorded-session"},
            }
        ),
        encoding="utf-8",
    )
    comparison = compare_benchmark(report, load_baseline(baseline_path))
    assert set(comparison["metrics"]) == {"wall_seconds"}
    assert comparison["baseline_id"] == "ordinary-session"


def test_feedback_compiles_to_reviewed_actions_and_never_self_modifies(tmp_path: Path):
    # intent: a learning becomes a test/decision/workflow/skill proposal, never silent mutation.
    source = tmp_path / "feedback.json"
    source.write_text(json.dumps(feedback()), encoding="utf-8")
    proposal = compile_feedback(source)

    assert proposal["policy"] == {
        "auto_apply": False,
        "auto_share_skills": False,
        "tests_are_authoritative": True,
        "named_human_review_required": True,
    }
    test_action, skill_action = proposal["actions"]
    assert test_action["sabotage_required"] is True
    assert test_action["verify_cmd"][0] == "pytest"
    assert skill_action["local_skill_proposal_only"] is True
    assert all(
        action["required_review"] == "named_human" for action in proposal["actions"]
    )


def test_regression_feedback_requires_a_deterministic_verify_command(tmp_path: Path):
    body = feedback()
    body["items"][0]["verify_cmd"] = None
    source = tmp_path / "feedback.json"
    source.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(LearningError, match="requires verify_cmd"):
        compile_feedback(source)


def test_feedback_rejects_credentials_without_echoing_them(tmp_path: Path):
    body = feedback()
    secret = "to" + "ken=do-not-publish-this-value"
    body["items"][0]["observation"] = secret
    source = tmp_path / "feedback.json"
    source.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(LearningError) as captured:
        compile_feedback(source)
    assert captured.value.code == "FEEDBACK_SECRET"
    assert secret not in str(captured.value)


def test_feedback_rejects_credentials_in_verification_argv(tmp_path: Path):
    body = feedback()
    secret = "api_" + "key=never-persist-this-value"
    body["items"][0]["verify_cmd"].append(secret)
    source = tmp_path / "feedback.json"
    source.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(LearningError) as captured:
        compile_feedback(source)
    assert captured.value.code == "FEEDBACK_SECRET"
    assert secret not in str(captured.value)


def test_cli_benchmark_emits_structured_evidence(tmp_path: Path, capsys):
    state, run_id = completed_run(tmp_path)
    assert (
        cli.main(
            [
                "benchmark",
                "--state",
                str(state),
                "--run-id",
                run_id,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["report"]["run_id"] == run_id
    assert payload["report"]["metrics"]["accepted_artifact_count"] == 2


def test_cli_feedback_failure_is_stable_and_secret_free(tmp_path: Path, capsys):
    body = feedback()
    secret = "to" + "ken=never-print-this-value"
    body["summary"] = secret
    source = tmp_path / "feedback.json"
    source.write_text(json.dumps(body), encoding="utf-8")
    assert cli.main(["feedback", "--input", str(source), "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["code"] == "FEEDBACK_SECRET"
    assert secret not in json.dumps(payload)
