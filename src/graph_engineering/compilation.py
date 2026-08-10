"""Deterministic review boundary for model-proposed workflows."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from importlib.resources import files
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from .artifacts import canonical_json
from .contracts import validate_workflow
from .project import (
    assessment_source,
    load_assessment,
    load_project_policy,
    repository_digest,
)
from .worktrees import WorktreeManager

PROPOSAL_VERSION = "graph-engineering/workflow-proposal/v1"
ACCEPTANCE_VERSION = "graph-engineering/workflow-acceptance/v1"


class CompilationError(RuntimeError):
    """A candidate workflow did not cross the explicit review boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def proposal_schema() -> dict[str, Any]:
    path = files("graph_engineering.schemas").joinpath(
        "workflow-proposal-v1.schema.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _proposal_issues(value: Mapping[str, Any]) -> list[str]:
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or '$'}: {error.message}"
        for error in sorted(
            Draft202012Validator(proposal_schema()).iter_errors(value),
            key=lambda error: list(error.absolute_path),
        )
    ]


def validate_proposal(value: Mapping[str, Any]) -> None:
    issues = _proposal_issues(value)
    if issues:
        raise CompilationError("PROPOSAL_SCHEMA", issues[0])
    expected = _digest({key: item for key, item in value.items() if key != "digest"})
    if value["digest"] != expected:
        raise CompilationError("PROPOSAL_DIGEST_MISMATCH", "proposal content changed")
    workflow = value["workflow"]
    assert isinstance(workflow, dict)
    validate_workflow(workflow)
    if workflow["id"] != value["id"]:
        raise CompilationError(
            "PROPOSAL_ID_MISMATCH", "proposal and workflow IDs differ"
        )


def compile_proposal(
    repo: Path,
    *,
    assessment_path: Path,
    workflow: dict[str, Any],
    proposed_by: str,
) -> dict[str, Any]:
    """Bind an untrusted candidate to current reviewed planning inputs.

    This function validates and packages a proposal. It never writes an executable
    workflow and never grants worker-dispatch authority.
    """

    proposer = proposed_by.strip()
    if not proposer or len(proposer) > 128:
        raise CompilationError("INVALID_PROPOSER", "proposed_by must be 1..128 bytes")
    assessment = load_assessment(assessment_path, repo)
    policy = load_project_policy(repo)
    if policy.unresolved:
        raise CompilationError(
            "PLANNING_UNRESOLVED",
            "the product contract and project policy must be frozen before compilation",
        )
    validate_workflow(workflow)
    base_sha = WorktreeManager(repo).resolve_base("HEAD")
    policy.preflight(workflow, base_sha=base_sha)
    contract = policy.product_contract
    payload: dict[str, Any] = {
        "version": PROPOSAL_VERSION,
        "id": workflow["id"],
        "proposed_by": proposer,
        "repository_digest": repository_digest(repo),
        "assessment_source": dict(assessment["source"]),
        "assessment_digest": _digest(assessment),
        "product_contract": {
            "version": contract.version,
            "generation": contract.generation,
            "digest": contract.digest,
        },
        "workflow": workflow,
    }
    payload["digest"] = _digest(payload)
    validate_proposal(payload)
    return payload


def accept_proposal(
    repo: Path,
    proposal: Mapping[str, Any],
    *,
    expected_digest: str,
    reviewed_by: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Accept a current proposal only through the named-human review boundary."""

    validate_proposal(proposal)
    if proposal["digest"] != expected_digest:
        raise CompilationError(
            "PROPOSAL_DIGEST_MISMATCH", "--proposal-digest does not match the proposal"
        )
    policy = load_project_policy(repo)
    policy._assert_current()
    contract = policy.product_contract
    binding = {
        "version": contract.version,
        "generation": contract.generation,
        "digest": contract.digest,
    }
    if proposal["product_contract"] != binding:
        raise CompilationError(
            "PRODUCT_CONTRACT_DRIFT",
            "proposal belongs to a different contract generation",
        )
    if proposal["repository_digest"] != repository_digest(repo):
        raise CompilationError(
            "REPOSITORY_DRIFT", "proposal belongs to another repository"
        )
    if proposal["assessment_source"] != assessment_source(repo):
        raise CompilationError(
            "ASSESSMENT_DRIFT",
            "repository source changed after the proposal was compiled",
        )
    approved_by = str(contract.value["freeze"]["approved_by"])
    reviewer = reviewed_by.strip()
    if reviewer != approved_by:
        raise CompilationError(
            "REVIEWER_NOT_AUTHORIZED",
            "reviewed_by must exactly match the frozen contract's named approver",
        )
    if reviewer.casefold() == str(proposal["proposed_by"]).strip().casefold():
        raise CompilationError(
            "SELF_APPROVAL_FORBIDDEN", "a proposer cannot self-approve"
        )
    workflow = proposal["workflow"]
    assert isinstance(workflow, dict)
    policy.preflight(workflow, base_sha=WorktreeManager(repo).resolve_base("HEAD"))
    workflow_digest = _digest(workflow)
    acceptance = {
        "version": ACCEPTANCE_VERSION,
        "proposal_digest": proposal["digest"],
        "workflow_digest": workflow_digest,
        "reviewed_by": approved_by,
        "product_contract": binding,
        "assessment_source": dict(proposal["assessment_source"]),
    }
    acceptance["digest"] = _digest(acceptance)
    return dict(workflow), acceptance


__all__ = [
    "ACCEPTANCE_VERSION",
    "PROPOSAL_VERSION",
    "CompilationError",
    "accept_proposal",
    "compile_proposal",
    "proposal_schema",
    "validate_proposal",
]
