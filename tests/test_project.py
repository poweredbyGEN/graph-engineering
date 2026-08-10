from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import graph_engineering.orchestrator as orchestrator_module
from graph_engineering import cli
from graph_engineering.artifacts import canonical_json
from graph_engineering.compilation import (
    CompilationError,
    accept_proposal,
    compile_proposal,
)
from graph_engineering.config import parse_agent_config
from graph_engineering.orchestrator import OrchestrationError, PortableRuntime
from graph_engineering.project import (
    PRODUCT_CONTRACT_VERSION,
    PROJECT_VERSION,
    ProjectPolicyError,
    RunScopeRegistry,
    discover_repo,
    execution_identity,
    load_private_execution_binding,
    load_project_policy,
    repository_digest,
    scaffold_project,
)
from graph_engineering.session_ux import assess_repo


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repo), *args),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def bare_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Policy Test")
    git(repo, "config", "user.email", "policy@example.invalid")
    (repo / "pyproject.toml").write_text(
        "[project]\nname='policy-test'\nversion='0.1'\n[tool.pytest.ini_options]\ntestpaths=['tests']\n",
        encoding="utf-8",
    )
    git(repo, "add", "--", "pyproject.toml")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", "https://example.invalid/acme/policy-test.git")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def test_repository_digest_is_portable_public_and_credential_free(tmp_path: Path):
    repo = bare_repo(tmp_path)
    secret = "raw-operator-token"
    variants = [
        f"https://oauth2:{secret}@Example.Invalid/acme/policy-test.git",
        "https://another-user:another-secret@example.invalid/acme/policy-test",
        "ssh://git@example.invalid/acme/policy-test.git",
        "git@example.invalid:acme/policy-test.git",
    ]
    digests = []
    assessments = []
    for remote in variants:
        git(repo, "remote", "set-url", "origin", remote)
        digests.append(repository_digest(repo))
        assessments.append(assess_repo(repo))

    assert len(set(digests)) == 1
    assert {assessment["repo_digest"] for assessment in assessments} == {digests[0]}
    assert secret not in json.dumps(assessments)
    assert "another-secret" not in json.dumps(assessments)

    renamed = tmp_path / "checkout-renamed-with-private-local-label"
    repo.rename(renamed)
    assert repository_digest(renamed) == digests[0]

    git(
        renamed,
        "remote",
        "set-url",
        "origin",
        "https://example.invalid/other/policy-test.git",
    )
    assert repository_digest(renamed) != digests[0]


def test_repository_digest_no_origin_fallback_is_path_free_and_execution_unresolved(
    tmp_path: Path,
):
    repo = bare_repo(tmp_path)
    git(repo, "remote", "remove", "origin")
    digest = repository_digest(repo)
    assert assess_repo(repo)["repo_digest"] == digest

    renamed = tmp_path / "renamed-local-checkout"
    repo.rename(renamed)
    assert repository_digest(renamed) == digest
    git(renamed, "remote", "add", "origin", "UNRESOLVED")
    assert repository_digest(renamed) == digest

    paths = scaffold_project(renamed)
    manifest = json.loads(paths[0].read_text(encoding="utf-8"))
    assert manifest["repository"]["canonical_remote"] == "UNRESOLVED"
    assert "repository.canonical_remote" in manifest["unresolved"]

    other = tmp_path / "different-repository"
    other.mkdir()
    git(other, "init", "-q", "-b", "main")
    git(other, "config", "user.name", "Policy Test")
    git(other, "config", "user.email", "policy@example.invalid")
    (other / "README.md").write_text("different root\n", encoding="utf-8")
    git(other, "add", "--", "README.md")
    git(other, "commit", "-qm", "different root")
    assert repository_digest(other) != digest


def test_repository_digest_rejects_ambiguous_origin_without_echoing_it(tmp_path: Path):
    repo = bare_repo(tmp_path)
    secret = "operator-secret-token"
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        f"https://example.invalid/acme/repo.git?token={secret}",
    )

    with pytest.raises(ProjectPolicyError) as caught:
        repository_digest(repo)
    assert caught.value.code == "REMOTE_REVIEW_REQUIRED"
    assert secret not in str(caught.value)
    with pytest.raises(ProjectPolicyError, match="REMOTE_REVIEW_REQUIRED"):
        assess_repo(repo)


def workflow(contract_digest: str, *, generation: int = 1) -> dict:
    return {
        "version": "graph-engineering/v1alpha1",
        "id": "starter",
        "goal": "Implement the frozen policy test contract.",
        "product_contract": {
            "version": PRODUCT_CONTRACT_VERSION,
            "generation": generation,
            "digest": contract_digest,
        },
        "budgets": {
            "max_nodes": 1,
            "max_concurrency": 1,
            "max_attempts_per_node": 1,
            "max_total_attempts": 1,
            "timeout_seconds": 30,
        },
        "nodes": [
            {
                "id": "implement",
                "kind": "agent",
                "task": "Return the reviewed result.",
                "needs": [],
                "inputs": {},
                "outputs": {
                    "result": {
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["ok"],
                            "properties": {"ok": {"const": True}},
                        }
                    }
                },
                "profile": "worker",
                "workspace": "worktree",
                "permission": "read",
                "checks": [{"id": "test", "argv": ["python", "-m", "pytest"]}],
                "retry": {"max_attempts": 1, "no_progress_limit": 1},
                "required": True,
            }
        ],
        "outputs": {"result": "implement.result"},
    }


def reviewed_contract(directory: Path) -> dict:
    brief = (
        "# Reviewed project\n\nBuild the policy outcome described by generation 1.\n"
    )
    decisions = "# Decision index\n\nNo open decisions.\n"
    (directory / "decisions").mkdir(parents=True, exist_ok=True)
    (directory / "PROJECT.md").write_text(brief, encoding="utf-8")
    (directory / "decisions/README.md").write_text(decisions, encoding="utf-8")

    def na(reason: str) -> dict:
        return {"items": [], "na_reason": reason}

    return {
        "version": PRODUCT_CONTRACT_VERSION,
        "id": "policy-test",
        "generation": 1,
        "freeze": {"status": "approved", "approved_by": "Policy Owner"},
        "sources": {
            "brief": {
                "path": ".graph-engineering/PROJECT.md",
                "digest": hashlib.sha256(brief.encode()).hexdigest(),
            },
            "decisions": {
                "path": ".graph-engineering/decisions/README.md",
                "digest": hashlib.sha256(decisions.encode()).hexdigest(),
            },
        },
        "answers": {
            "problem": "Prove project policy before worker dispatch.",
            "target_users": ["Repository maintainers"],
            "outcomes": ["Unsafe work is rejected before dispatch"],
            "scope": {"in": ["Runtime preflight"], "out": ["Deployment"]},
            "journeys": ["Maintainer validates and runs a reviewed workflow"],
            "surfaces": {
                "ui": na("No user interface"),
                "api": na("No network API"),
                "events": na("No event stream"),
                "jobs": {"items": ["Portable graph run"], "na_reason": None},
                "integrations": na("No external integration"),
            },
            "data": {
                "tables": na("No database tables"),
                "stores": {"items": ["Run state database"], "na_reason": None},
                "migrations": na("No data migration"),
            },
            "auth_permissions": na("Local repository execution only"),
            "invariants": ["No worker starts before policy passes"],
            "compatibility": {"items": ["Workflow v1alpha1"], "na_reason": None},
            "failure_recovery": [
                "Return a stable preflight error and dispatch nothing"
            ],
            "delivery": {
                "rollout": ["Land through reviewed CI"],
                "rollback": ["Revert the commit"],
                "live_proof": ["Focused project tests pass"],
            },
            "risks": {"items": ["Stale contract generation"], "na_reason": None},
            "assumptions_hypotheses": {
                "items": [
                    {
                        "id": "local-git",
                        "statement": "The repository is a local git checkout",
                        "status": "validated",
                        "evidence": ["git rev-parse succeeds"],
                    }
                ],
                "na_reason": None,
            },
            "open_decisions": na("No open product decisions"),
            "acceptance_criteria": [
                {
                    "id": "test",
                    "criterion": "Project tests pass",
                    "proof_class": "deterministic",
                    "argv": ["python", "-m", "pytest"],
                    "human_gate": False,
                }
            ],
        },
    }


def reviewed_repo(tmp_path: Path) -> tuple[Path, dict, dict]:
    repo = bare_repo(tmp_path)
    directory = repo / ".graph-engineering"
    (directory / "workflows").mkdir(parents=True)
    contract = reviewed_contract(directory)
    digest = hashlib.sha256(canonical_json(contract)).hexdigest()
    value = workflow(digest)
    (directory / "product-contract.json").write_text(
        json.dumps(contract), encoding="utf-8"
    )
    manifest = {
        "version": PROJECT_VERSION,
        "repository": {
            "canonical_remote": "https://example.invalid/acme/policy-test.git",
            "allowed_roots": ["."],
            "base_branch": "main",
        },
        "routing": {"provider": "plane", "project": "example"},
        "product_contract": {
            "path": ".graph-engineering/product-contract.json",
            "version": PRODUCT_CONTRACT_VERSION,
            "generation": 1,
            "digest": digest,
        },
        "deployment": {
            "adapter": "universal-deploy",
            "targets": ["staging"],
        },
        "prohibited_operations": ["direct-scp", "unsanctioned-deploy"],
        "required_checks": [{"id": "test", "argv": ["python", "-m", "pytest"]}],
        "live_verification": {"required": False, "checks": []},
        "unresolved": [],
    }
    (directory / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "workflows" / "starter.json").write_text(
        json.dumps(value), encoding="utf-8"
    )
    local = repo / ".graph-engineering.local.toml"
    local.write_text(
        "[execution]\n"
        f"allowed_hosts = [{json.dumps(socket.gethostname())}]\n"
        f"allowed_checkout_roots = [{json.dumps(str(repo))}]\n",
        encoding="utf-8",
    )
    local.chmod(0o600)
    return repo, contract, value


def agent_config(marker: Path):
    return parse_agent_config(
        {
            "version": 1,
            "profiles": {
                "worker": {
                    "adapter": "subprocess",
                    "model": "test-model",
                    "capabilities": {
                        "read": True,
                        "write": False,
                        "structured_output": True,
                        "worktree": True,
                        "resume": False,
                        "mcp": False,
                    },
                    "subprocess": {
                        "argv": [
                            sys.executable,
                            "-c",
                            f"from pathlib import Path; import json; Path({str(marker)!r}).write_text('spawned'); print(json.dumps(dict(ok=True)))",
                        ],
                        "prompt_transport": "stdin",
                        "output_format": "json",
                        "env_allowlist": [],
                    },
                }
            },
        }
    )


def _assessment_file(repo: Path, tmp_path: Path) -> Path:
    path = tmp_path / "assessment.json"
    path.write_text(json.dumps(assess_repo(repo)), encoding="utf-8")
    return path


def test_compiled_workflow_requires_distinct_named_human_acceptance(tmp_path: Path):
    # intent: a model proposal is inert until the frozen contract approver accepts
    # the exact digest; schema validation alone must never grant dispatch authority.
    repo, _contract, value = reviewed_repo(tmp_path)
    assessment = _assessment_file(repo, tmp_path)
    proposal = compile_proposal(
        repo,
        assessment_path=assessment,
        workflow=value,
        proposed_by="planning-model",
    )

    with pytest.raises(CompilationError, match="REVIEWER_NOT_AUTHORIZED"):
        accept_proposal(
            repo,
            proposal,
            expected_digest=proposal["digest"],
            reviewed_by="another-model",
        )

    workflow_value, receipt = accept_proposal(
        repo,
        proposal,
        expected_digest=proposal["digest"],
        reviewed_by="Policy Owner",
    )
    assert workflow_value == value
    assert receipt["proposal_digest"] == proposal["digest"]
    assert receipt["reviewed_by"] == "Policy Owner"


def test_compiled_workflow_rejects_self_approval_and_digest_drift(tmp_path: Path):
    repo, _contract, value = reviewed_repo(tmp_path)
    assessment = _assessment_file(repo, tmp_path)
    proposal = compile_proposal(
        repo,
        assessment_path=assessment,
        workflow=value,
        proposed_by="  policy owner  ",
    )
    with pytest.raises(CompilationError, match="SELF_APPROVAL_FORBIDDEN"):
        accept_proposal(
            repo,
            proposal,
            expected_digest=proposal["digest"],
            reviewed_by="Policy Owner",
        )

    proposal["workflow"]["goal"] = "silently changed after review"
    with pytest.raises(CompilationError, match="PROPOSAL_DIGEST_MISMATCH"):
        accept_proposal(
            repo,
            proposal,
            expected_digest=proposal["digest"],
            reviewed_by="Policy Owner",
        )


def test_compiled_workflow_rejects_repository_source_drift(tmp_path: Path):
    repo, _contract, value = reviewed_repo(tmp_path)
    assessment = _assessment_file(repo, tmp_path)
    proposal = compile_proposal(
        repo,
        assessment_path=assessment,
        workflow=value,
        proposed_by="planning-model",
    )
    (repo / "new-untracked-input.txt").write_text("drift\n", encoding="utf-8")

    with pytest.raises(CompilationError, match="ASSESSMENT_DRIFT"):
        accept_proposal(
            repo,
            proposal,
            expected_digest=proposal["digest"],
            reviewed_by="Policy Owner",
        )


def test_compile_accept_cli_keeps_unreviewed_proposal_outside_repo(
    tmp_path: Path, capsys
):
    repo, _contract, value = reviewed_repo(tmp_path)
    assessment = _assessment_file(repo, tmp_path)
    candidate = tmp_path / "candidate.json"
    proposal = tmp_path / "proposal.json"
    candidate.write_text(json.dumps(value), encoding="utf-8")
    assert (
        cli.main(
            [
                "compile",
                "--repo",
                str(repo),
                "--assessment",
                str(assessment),
                "--candidate",
                str(candidate),
                "--proposed-by",
                "planning-model",
                "--output",
                str(proposal),
                "--json",
            ]
        )
        == 0
    )
    compiled = json.loads(capsys.readouterr().out)
    workflow_output = repo / ".graph-engineering/workflows/accepted.json"
    acceptance_output = repo / ".graph-engineering/reviews/accepted.json"
    assert (
        cli.main(
            [
                "accept",
                "--repo",
                str(repo),
                "--proposal",
                str(proposal),
                "--proposal-digest",
                compiled["proposal_digest"],
                "--reviewed-by",
                "Policy Owner",
                "--workflow-output",
                str(workflow_output),
                "--acceptance-output",
                str(acceptance_output),
                "--json",
            ]
        )
        == 0
    )
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["dispatch_authorized"] is True
    assert workflow_output.is_file()
    assert acceptance_output.is_file()


def test_policy_rejects_wrong_root_remote_and_stale_base(tmp_path: Path):
    repo, _contract, value = reviewed_repo(tmp_path)
    nested = repo / "nested"
    nested.mkdir()
    with pytest.raises(ProjectPolicyError, match="WRONG_REPOSITORY_ROOT"):
        discover_repo(nested)

    policy = load_project_policy(repo)
    git(repo, "remote", "set-url", "origin", "https://example.invalid/evil/repo.git")
    with pytest.raises(ProjectPolicyError, match="WRONG_REPOSITORY"):
        policy.preflight(value, base_sha=git(repo, "rev-parse", "HEAD"))

    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://example.invalid/acme/policy-test.git",
    )
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    git(repo, "add", "--", "new.txt")
    git(repo, "commit", "-qm", "new local base")
    with pytest.raises(ProjectPolicyError, match="STALE_BASE"):
        policy.preflight(value, base_sha=git(repo, "rev-parse", "HEAD"))


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda value: value["nodes"][0].update(task="scp build host:/srv/app"),
            "DIRECT_SCP_FORBIDDEN",
        ),
        (
            lambda value: value["nodes"][0].update(
                task="deploy to production",
            ),
            "UNSANCTIONED_DEPLOY",
        ),
    ],
)
def test_runtime_policy_fails_before_worker_dispatch(tmp_path: Path, mutate, code: str):
    repo, _contract, value = reviewed_repo(tmp_path)
    marker = tmp_path / "spawned"
    mutate(value)
    with pytest.raises(OrchestrationError, match=code):
        PortableRuntime(
            value,
            agent_config(marker),
            repo=repo,
            state_path=tmp_path / "state.db",
            artifact_root=tmp_path / "artifacts",
            approvals={"operator": {"approved": True}},
            environ=os.environ | {"TMPDIR": str(tmp_path)},
        )
    assert not marker.exists()


def test_product_contract_generation_and_digest_never_mix(tmp_path: Path):
    repo, contract, value = reviewed_repo(tmp_path)
    policy = load_project_policy(repo)
    wrong_generation = copy.deepcopy(value)
    wrong_generation["product_contract"]["generation"] = 2
    with pytest.raises(ProjectPolicyError, match="PRODUCT_CONTRACT_MISMATCH"):
        policy.preflight(wrong_generation, base_sha=git(repo, "rev-parse", "HEAD"))

    marker = tmp_path / "spawned"
    runtime = PortableRuntime(
        value,
        agent_config(marker),
        repo=repo,
        state_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        environ=os.environ | {"TMPDIR": str(tmp_path)},
    )
    assert (
        runtime.context_provider.provide()["product_contract_digest"]
        == policy.product_contract.digest
    )
    contract["generation"] = 2
    (repo / ".graph-engineering/product-contract.json").write_text(
        json.dumps(contract), encoding="utf-8"
    )
    with pytest.raises(OrchestrationError, match="PRODUCT_CONTRACT_DRIFT"):
        runtime.run(run_id="contract-drift")
    assert not marker.exists()


def test_approved_capsule_rejects_missing_axis_and_brief_drift(tmp_path: Path):
    repo, contract, value = reviewed_repo(tmp_path)
    contract_path = repo / ".graph-engineering/product-contract.json"
    manifest_path = repo / ".graph-engineering/project.json"
    contract["answers"]["surfaces"]["api"] = {
        "items": [],
        "na_reason": "UNRESOLVED",
    }
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["product_contract"]["digest"] = hashlib.sha256(
        canonical_json(contract)
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    policy = load_project_policy(repo)
    assert any("surfaces.api" in question for question in policy.unresolved)
    with pytest.raises(ProjectPolicyError, match="PROJECT_POLICY_UNRESOLVED"):
        policy.preflight(value, base_sha=git(repo, "rev-parse", "HEAD"))

    drift_root = tmp_path / "drift"
    drift_root.mkdir()
    repo, _contract, value = reviewed_repo(drift_root)
    policy = load_project_policy(repo)
    (repo / ".graph-engineering/PROJECT.md").write_text(
        "# silently changed brief\n", encoding="utf-8"
    )
    with pytest.raises(ProjectPolicyError, match="PRODUCT_SOURCE_DRIFT"):
        policy.preflight(value, base_sha=git(repo, "rev-parse", "HEAD"))


def test_init_scaffolds_without_private_authority_and_is_idempotent(
    tmp_path: Path, capsys
):
    repo = bare_repo(tmp_path)
    assessment = assess_repo(repo)
    assessment_path = tmp_path / "assessment.json"
    assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
    argv = [
        "init",
        "--repo",
        str(repo),
        "--from-assessment",
        str(assessment_path),
        "--json",
    ]
    idempotent_argv = ["init", "--repo", str(repo), "--json"]
    assert cli.main(argv) == 1
    first = json.loads(capsys.readouterr().out)
    assert len(first["created"]) == 5
    manifest = json.loads((repo / ".graph-engineering/project.json").read_text())
    starter = json.loads(
        (repo / ".graph-engineering/workflows/starter.json").read_text()
    )
    assert manifest["deployment"] == {"adapter": "UNRESOLVED", "targets": []}
    assert "private_profile" in manifest["unresolved"]
    assert any(
        question.startswith("product.answers.problem:")
        for question in first["unresolved"]
    )
    assert first["assessment_recommendation"] == {
        "workflow_templates": [
            "mobile-automation-vertical-slice",
            "contract-matrix-class-prove",
        ],
        "require_private_config": True,
    }
    assert not (repo / ".graph-engineering.local.toml").exists()
    assert all(
        "deployment" not in node and "approval" not in node for node in starter["nodes"]
    )
    assert (
        assessment["recommended_init"]["transport"] not in json.dumps(starter).lower()
    )
    marker = tmp_path / "draft-dispatched"
    starter["nodes"][0]["profile"] = "worker"
    with pytest.raises(OrchestrationError, match="PROJECT_POLICY_UNRESOLVED"):
        PortableRuntime(
            starter,
            agent_config(marker),
            repo=repo,
            state_path=tmp_path / "draft-state.db",
            artifact_root=tmp_path / "draft-artifacts",
            environ=os.environ | {"TMPDIR": str(tmp_path)},
        )
    assert not marker.exists()

    assert cli.main(argv) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "ASSESSMENT_STALE"
    assert cli.main(idempotent_argv) == 1
    second = json.loads(capsys.readouterr().out)
    assert second["reused"]
    assert second["created"] == []


def test_init_redacts_remote_userinfo_from_every_generated_surface(
    tmp_path: Path, capsys
):
    repo = bare_repo(tmp_path)
    secret = "operator-secret-token"
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        f"https://oauth2:{secret}@example.invalid/acme/policy-test.git",
    )

    assert cli.main(["init", "--repo", str(repo), "--json"]) == 1
    output = capsys.readouterr()
    generated = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((repo / ".graph-engineering").rglob("*"))
        if path.is_file()
    )
    manifest = json.loads((repo / ".graph-engineering/project.json").read_text())

    assert manifest["repository"]["canonical_remote"] == (
        "https://example.invalid/acme/policy-test"
    )
    assert secret not in output.out + output.err + generated
    assert "oauth2" not in output.out + output.err + generated


@pytest.mark.parametrize(
    "remote",
    [
        "https://example.invalid/acme/repo.git?token=operator-secret-token",
        "https://example.invalid/acme/repo.git#operator-secret-token",
        "https://operator-secret-token@[/repo.git",
        "https://example.invalid/acme/\x01repo.git",
        "file:///srv/private/repo.git",
        "/srv/private/repo.git",
        "../private/repo.git",
    ],
)
def test_scaffold_rejects_ambiguous_or_local_remote_before_any_write(
    tmp_path: Path, remote: str
):
    repo = bare_repo(tmp_path)
    git(repo, "remote", "set-url", "origin", remote)

    with pytest.raises(ProjectPolicyError) as caught:
        scaffold_project(repo)

    assert caught.value.code == "REMOTE_REVIEW_REQUIRED"
    assert "operator-secret-token" not in str(caught.value)
    assert not (repo / ".graph-engineering").exists()


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        (
            "https://example.invalid/acme/policy-test.git",
            "https://example.invalid/acme/policy-test",
        ),
        (
            "ssh://git@example.invalid/acme/policy-test.git",
            "ssh://example.invalid/acme/policy-test",
        ),
        (
            "git@example.invalid:acme/policy-test.git",
            "example.invalid:acme/policy-test",
        ),
    ],
)
def test_scaffold_preserves_supported_public_repository_identity(
    tmp_path: Path, remote: str, expected: str
):
    repo = bare_repo(tmp_path)
    git(repo, "remote", "set-url", "origin", remote)

    scaffold_project(repo)

    manifest = json.loads((repo / ".graph-engineering/project.json").read_text())
    assert manifest["repository"]["canonical_remote"] == expected


def test_init_reuses_reviewed_project_and_detects_matching_active_run(
    tmp_path: Path, capsys
):
    repo, _contract, value = reviewed_repo(tmp_path)
    state_root = tmp_path / "runs"
    state = state_root / "active" / "state.db"
    policy = load_project_policy(repo)
    identity = execution_identity(
        policy, value, base_sha=git(repo, "rev-parse", "HEAD")
    )
    RunScopeRegistry(state_root).claim(
        identity, run_id="already-running", state_path=state, resume=False
    )

    assert (
        cli.main(
            [
                "init",
                "--repo",
                str(repo),
                "--state-root",
                str(state_root),
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["reused"]
    assert payload["created"] == []
    assert payload["launch_blocked_by_active_run"]
    assert payload["matching_active_runs"][0]["run_id"] == "already-running"


def test_private_execution_binding_fails_closed_without_leaking_host_or_root(
    tmp_path: Path,
):
    repo, _contract, value = reviewed_repo(tmp_path)
    local = repo / ".graph-engineering.local.toml"
    assert len(load_private_execution_binding(repo).digest) == 64

    local.unlink()
    marker = tmp_path / "missing-spawned"
    with pytest.raises(OrchestrationError, match="PRIVATE_EXECUTION_REQUIRED"):
        PortableRuntime(
            value,
            agent_config(marker),
            repo=repo,
            state_path=tmp_path / "missing.db",
            artifact_root=tmp_path / "missing-artifacts",
            environ=os.environ | {"TMPDIR": str(tmp_path)},
        )
    assert not marker.exists()

    secret_root = tmp_path / "private-checkout-name"
    secret_root.mkdir()
    local.write_text(
        "[execution]\n"
        'allowed_hosts = ["not-this-host"]\n'
        f"allowed_checkout_roots = [{json.dumps(str(secret_root))}]\n",
        encoding="utf-8",
    )
    local.chmod(0o600)
    with pytest.raises(ProjectPolicyError) as caught:
        load_private_execution_binding(repo)
    assert caught.value.code == "PRIVATE_EXECUTION_HOST"
    assert socket.gethostname() not in str(caught.value)
    assert str(secret_root) not in str(caught.value)


def test_policy_and_private_binding_drift_stop_before_worker_dispatch(tmp_path: Path):
    repo, _contract, value = reviewed_repo(tmp_path)
    marker = tmp_path / "spawned"
    runtime = PortableRuntime(
        value,
        agent_config(marker),
        repo=repo,
        state_path=tmp_path / "state.db",
        artifact_root=tmp_path / "artifacts",
        environ=os.environ | {"TMPDIR": str(tmp_path)},
    )
    manifest_path = repo / ".graph-engineering/project.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unresolved"] = ["security-review-required"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(OrchestrationError, match="PROJECT_POLICY_DRIFT"):
        runtime.run(run_id="policy-drift")
    assert not marker.exists()

    private_parent = tmp_path / "private"
    private_parent.mkdir()
    repo, _contract, value = reviewed_repo(private_parent)
    marker = tmp_path / "private-spawned"
    runtime = PortableRuntime(
        value,
        agent_config(marker),
        repo=repo,
        state_path=tmp_path / "private-state.db",
        artifact_root=tmp_path / "private-artifacts",
        environ=os.environ | {"TMPDIR": str(tmp_path)},
    )
    local = repo / ".graph-engineering.local.toml"
    local.write_text(
        "[execution]\n"
        f"allowed_hosts = [{json.dumps(socket.gethostname())}]\n"
        f"allowed_checkout_roots = [{json.dumps(str(repo))}, {json.dumps(str(tmp_path))}]\n",
        encoding="utf-8",
    )
    local.chmod(0o600)
    with pytest.raises(OrchestrationError, match="PRIVATE_EXECUTION_DRIFT"):
        runtime.run(run_id="private-drift")
    assert not marker.exists()


@pytest.mark.parametrize(
    ("boundary", "code"),
    [
        ("project", "PROJECT_POLICY_DRIFT"),
        ("private", "PRIVATE_EXECUTION_DRIFT"),
    ],
)
def test_dispatch_boundary_revalidates_policy_before_worker_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    code: str,
):
    """A mutation after scheduler setup must still dispatch zero workers."""

    repo, _contract, value = reviewed_repo(tmp_path)
    marker = tmp_path / "worker-spawned"
    runtime = PortableRuntime(
        value,
        agent_config(marker),
        repo=repo,
        state_path=tmp_path / "dispatch-state.db",
        artifact_root=tmp_path / "dispatch-artifacts",
        environ=os.environ | {"TMPDIR": str(tmp_path)},
    )
    original_run = orchestrator_module.Scheduler.run

    def mutate_after_scheduler_setup(scheduler, *args, **kwargs):
        if boundary == "project":
            path = repo / ".graph-engineering/project.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            manifest["unresolved"] = ["late-policy-mutation"]
            path.write_text(json.dumps(manifest), encoding="utf-8")
        else:
            path = repo / ".graph-engineering.local.toml"
            path.write_text(
                "[execution]\n"
                f"allowed_hosts = [{json.dumps(socket.gethostname())}]\n"
                f"allowed_checkout_roots = [{json.dumps(str(repo))}, {json.dumps(str(tmp_path))}]\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
        return original_run(scheduler, *args, **kwargs)

    monkeypatch.setattr(
        orchestrator_module.Scheduler, "run", mutate_after_scheduler_setup
    )
    result = runtime.run(run_id=f"dispatch-fence-{boundary}")

    assert result.run.status == "failed"
    error = result.run.nodes["implement"]["error"]
    assert code in error
    assert str(tmp_path) not in error
    assert not marker.exists()


def test_run_scope_registry_is_atomic_repo_bound_and_resume_exact(tmp_path: Path):
    repo, _contract, value = reviewed_repo(tmp_path)
    policy = load_project_policy(repo)
    identity = execution_identity(
        policy, value, base_sha=git(repo, "rev-parse", "HEAD")
    )
    registry = RunScopeRegistry(tmp_path / "registry")
    state = tmp_path / "one" / "state.db"
    registry.claim(identity, run_id="one", state_path=state, resume=False)
    git(
        repo,
        "remote",
        "set-url",
        "origin",
        "https://credential:rotated-secret@example.invalid/acme/policy-test.git",
    )
    credential_variant = execution_identity(
        policy, value, base_sha=git(repo, "rev-parse", "HEAD")
    )
    assert credential_variant == identity
    with pytest.raises(ProjectPolicyError, match="DUPLICATE_ACTIVE_RUN"):
        registry.claim(
            credential_variant,
            run_id="two",
            state_path=tmp_path / "two" / "state.db",
            resume=False,
        )
    registry.claim(identity, run_id="one", state_path=state, resume=True)

    other = type(identity)(
        "f" * 64,
        identity.base_sha,
        identity.product_contract_digest,
        identity.product_contract_generation,
        identity.workflow_digest,
    )
    registry.claim(
        other,
        run_id="other-repository",
        state_path=tmp_path / "other" / "state.db",
        resume=False,
    )
    assert registry.matches(other)[0]["run_id"] == "other-repository"
