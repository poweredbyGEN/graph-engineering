from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from graph_engineering import CheckResult, RunResult, Scheduler
from graph_engineering.contracts import WorkflowValidationError, validate_workflow

EXAMPLE = Path(__file__).parents[1] / "examples/frontend-evidence.workflow.json"


def _workflow() -> dict:
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def _evidence_schema(value: dict) -> dict:
    node = next(node for node in value["nodes"] if node["id"] == "run_ui_suite")
    return node["outputs"]["evidence"]["schema"]


def _descriptor(path: str = "run/checkpoint.png") -> dict:
    return {
        "path": path,
        "sha256": "a" * 64,
        "bytes": 1,
        "media_type": "image/png",
    }


def _evidence(status: str, checkpoints: dict) -> dict:
    return {
        "run_id": f"ui-{status}",
        "suite": "focused-ui",
        "tier": "live",
        "status": status,
        "checkpoints": checkpoints,
        "evidence_digest": "b" * 64,
    }


def _run_example(
    tmp_path: Path, evidence: dict
) -> tuple[Scheduler, RunResult, list[str]]:
    value = _workflow()
    checks: list[str] = []

    def passing_check(check, _context, _outputs):
        checks.append(check["id"])
        return CheckResult(True, "project check exited 0")

    engine = Scheduler(
        value,
        tmp_path / "state.db",
        tmp_path / "artifacts",
        {
            "authorize_live": lambda _context: {
                "decision": {
                    "approved": True,
                    "suite": "focused-ui",
                    "target": "test-target",
                }
            },
            "run_ui_suite": lambda _context: {"evidence": evidence},
        },
        passing_check,
    )
    return engine, engine.run("frontend-evidence"), checks


def test_frontend_evidence_example_is_a_valid_bounded_workflow():
    value = _workflow()
    validate_workflow(value)
    approval, runner = value["nodes"]
    assert runner["needs"] == [approval["id"]]
    assert runner["approval"] == approval["id"]
    assert runner["effect"] == "non_idempotent_write"
    assert runner["retry"]["max_attempts"] == 1
    assert runner["checks"][0]["argv"][0] == "python"
    assert not {"endpoint", "token", "secret", "cdp_url"} & set(runner)


def test_invalid_acceptance_schema_is_rejected_before_execution():
    value = _workflow()
    runner = next(node for node in value["nodes"] if node["id"] == "run_ui_suite")
    runner["outputs"]["evidence"]["acceptance_schema"] = {"type": "not-a-type"}

    with pytest.raises(WorkflowValidationError, match="INVALID_ACCEPTANCE_SCHEMA"):
        validate_workflow(value)


def test_success_artifact_requires_all_visual_steps_and_reload_persistence():
    schema = _evidence_schema(_workflow())
    evidence = _evidence(
        "succeeded",
        {
            name: copy.deepcopy(_descriptor())
            for name in ("prepared", "triggered", "waiting", "result", "reload")
        },
    )
    assert not list(Draft202012Validator(schema).iter_errors(evidence))

    # intent: sabotage any mandatory success checkpoint and the contract must reject it.
    for checkpoint in ("prepared", "triggered", "waiting", "result", "reload"):
        sabotaged = copy.deepcopy(evidence)
        del sabotaged["checkpoints"][checkpoint]
        assert list(Draft202012Validator(schema).iter_errors(sabotaged)), checkpoint


def test_checked_in_workflow_accepts_only_check_green_complete_success(tmp_path: Path):
    evidence = _evidence(
        "succeeded",
        {
            name: copy.deepcopy(_descriptor())
            for name in ("prepared", "triggered", "waiting", "result", "reload")
        },
    )

    engine, result, checks = _run_example(tmp_path, evidence)

    assert result.status == "succeeded"
    assert result.nodes["run_ui_suite"]["status"] == "succeeded"
    assert checks == ["authorization_recorded", "evidence_contract"]
    assert engine.state.artifact(result.run_id, "run_ui_suite", "evidence") is not None


def test_check_green_failed_evidence_is_durable_but_never_accepted(tmp_path: Path):
    # intent: schema-valid partial failure evidence must never become graph success merely
    # because the external project check exits zero.
    evidence = _evidence("failed", {"partial": _descriptor("run/partial.png")})

    engine, result, checks = _run_example(tmp_path, evidence)

    assert result.status == "failed"
    assert result.nodes["run_ui_suite"]["status"] == "failed"
    assert checks == ["authorization_recorded", "evidence_contract"]
    assert engine.state.artifact(result.run_id, "run_ui_suite", "evidence") is None
    failure = engine.state.last_attempt_failure(result.run_id, "run_ui_suite")
    assert failure is not None
    assert failure["code"] == "OUTPUT_NOT_ACCEPTED"
    digest = failure["artifact_receipts"]["evidence"]
    artifact_path = tmp_path / "artifacts" / digest[:2] / f"{digest}.json"
    assert json.loads(artifact_path.read_text(encoding="utf-8")) == evidence


def test_resume_rechecks_output_acceptance_before_trusting_prior_success(
    tmp_path: Path,
):
    complete = _evidence(
        "succeeded",
        {
            name: copy.deepcopy(_descriptor())
            for name in ("prepared", "triggered", "waiting", "result", "reload")
        },
    )
    engine, first, _checks = _run_example(tmp_path, complete)
    broad_schema = _evidence_schema(_workflow())
    rejected = _evidence("crashed", {"partial": _descriptor("run/crash.png")})
    rejected_artifact = engine.artifacts.put(rejected, broad_schema)
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute(
            "UPDATE artifacts SET digest=? WHERE run_id=? AND node_id=? AND output_name=?",
            (rejected_artifact.digest, first.run_id, "run_ui_suite", "evidence"),
        )

    resumed = engine.run(first.run_id, resume=True, lifecycle_resume=False)

    assert resumed.status == "failed"
    assert resumed.nodes["run_ui_suite"]["status"] == "failed"


@pytest.mark.parametrize("status", ["failed", "crashed", "blocked"])
@pytest.mark.parametrize("checkpoint", ["prepared", "triggered", "partial"])
def test_non_success_artifact_accepts_genuine_partial_evidence(
    status: str, checkpoint: str
):
    schema = _evidence_schema(_workflow())
    evidence = _evidence(status, {checkpoint: _descriptor()})
    assert not list(Draft202012Validator(schema).iter_errors(evidence))


@pytest.mark.parametrize("status", ["failed", "crashed", "blocked"])
def test_non_success_artifact_cannot_be_empty(status: str):
    schema = _evidence_schema(_workflow())
    evidence = _evidence(status, {})

    assert list(Draft202012Validator(schema).iter_errors(evidence))


@pytest.mark.parametrize("status", ["failed", "crashed", "blocked"])
def test_non_success_status_never_satisfies_success_discriminator(status: str):
    schema = _evidence_schema(_workflow())
    checkpoints = {
        name: copy.deepcopy(_descriptor())
        for name in ("prepared", "triggered", "waiting", "result", "reload")
    }
    evidence = _evidence(status, checkpoints)

    assert not list(Draft202012Validator(schema).iter_errors(evidence))
    success_branch = schema["oneOf"][0]
    assert list(Draft202012Validator(success_branch).iter_errors(evidence))


def test_partial_evidence_still_requires_a_bounded_file_descriptor():
    schema = _evidence_schema(_workflow())
    evidence = _evidence("crashed", {"partial": _descriptor("../escaped.trace.zip")})

    assert list(Draft202012Validator(schema).iter_errors(evidence))


@pytest.mark.parametrize("field", ["approval", "effect"])
def test_live_browser_node_cannot_drop_authority_or_replay_boundary(field: str):
    value = _workflow()
    runner = next(node for node in value["nodes"] if node["id"] == "run_ui_suite")
    del runner[field]
    with pytest.raises(WorkflowValidationError):
        validate_workflow(value)
