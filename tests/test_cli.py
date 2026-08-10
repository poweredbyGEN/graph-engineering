from __future__ import annotations

import copy
import hashlib
import json
import shutil
import socket
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

from graph_engineering import __version__, cli
from graph_engineering.artifacts import canonical_json
from graph_engineering.cli import main
from graph_engineering.lifecycle import LifecycleStore, StaticRunContextProvider
from graph_engineering.project import (
    PRODUCT_CONTRACT_VERSION,
    PROJECT_VERSION,
    RunScopeRegistry,
    execution_identity,
    load_project_policy,
    repository_digest,
)
from graph_engineering.session_ux import assess_repo, status_projection

RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"const": True}},
}

PROJECT_BRIEF_TEXT = "# CLI project capsule\n\nReviewed generation 1.\n"
DECISION_INDEX_TEXT = "# Decision index\n\nNo open decisions.\n"


def _na(reason: str) -> dict:
    return {"items": [], "na_reason": reason}


PRODUCT_CONTRACT = {
    "version": PRODUCT_CONTRACT_VERSION,
    "id": "cli-test",
    "generation": 1,
    "freeze": {"status": "approved", "approved_by": "CLI Owner"},
    "sources": {
        "brief": {
            "path": ".graph-engineering/PROJECT.md",
            "digest": hashlib.sha256(PROJECT_BRIEF_TEXT.encode()).hexdigest(),
        },
        "decisions": {
            "path": ".graph-engineering/decisions/README.md",
            "digest": hashlib.sha256(DECISION_INDEX_TEXT.encode()).hexdigest(),
        },
    },
    "answers": {
        "problem": "Exercise the portable CLI contract.",
        "target_users": ["CLI maintainers"],
        "outcomes": ["CLI commands return validated evidence"],
        "scope": {"in": ["CLI behavior"], "out": ["Production deployment"]},
        "journeys": ["Maintainer runs a reviewed workflow"],
        "surfaces": {
            "ui": _na("No user interface"),
            "api": _na("No network API"),
            "events": _na("No event stream"),
            "jobs": {"items": ["CLI workflow run"], "na_reason": None},
            "integrations": _na("No external integration"),
        },
        "data": {
            "tables": _na("No tables"),
            "stores": {"items": ["Run state"], "na_reason": None},
            "migrations": _na("No migrations"),
        },
        "auth_permissions": _na("Local invocation only"),
        "invariants": ["Unvalidated workflows do not run"],
        "compatibility": {"items": ["Workflow v1alpha1"], "na_reason": None},
        "failure_recovery": ["Return a stable error without dispatch"],
        "delivery": {
            "rollout": ["Land with CI"],
            "rollback": ["Revert the commit"],
            "live_proof": ["CLI tests pass"],
        },
        "risks": {"items": ["State drift"], "na_reason": None},
        "assumptions_hypotheses": _na("No open assumptions"),
        "open_decisions": _na("No open decisions"),
        "acceptance_criteria": [
            {
                "id": "accepted",
                "criterion": "Accepted check exits zero",
                "proof_class": "deterministic",
                "argv": ["true"],
                "human_gate": False,
            }
        ],
    },
}
PRODUCT_DIGEST = hashlib.sha256(canonical_json(PRODUCT_CONTRACT)).hexdigest()


def _smoke_confinement_available() -> bool:
    """Probe the kernel boundary, not merely whether helper binaries exist."""

    tools = {name: shutil.which(name) for name in ("bwrap", "prlimit", "strace")}
    if any(value is None for value in tools.values()):
        return False
    try:
        result = subprocess.run(
            [
                tools["prlimit"],
                "--fsize=4194304:4194304",
                "--",
                tools["strace"],
                "-qq",
                "-e",
                "trace=execve",
                "-o",
                "/dev/null",
                "--",
                tools["bwrap"],
                "--unshare-all",
                "--unshare-user",
                "--share-net",
                "--die-with-parent",
                "--disable-userns",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--",
                "/bin/true",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


requires_smoke_confinement = pytest.mark.skipif(
    not _smoke_confinement_available(),
    reason="kernel does not permit the nested doctor-smoke namespace",
)


@pytest.fixture(autouse=True)
def _isolated_state_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "CLI Test")
    _git(repo, "config", "user.email", "cli@example.com")
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    _git(repo, "add", "--", "README.md")
    _git(repo, "commit", "-qm", "base")
    branch = _git(repo, "branch", "--show-current")
    remote = "https://example.invalid/graph/cli-test.git"
    _git(repo, "remote", "add", "origin", remote)
    _git(repo, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    project = repo / ".graph-engineering"
    (project / "workflows").mkdir(parents=True)
    (project / "decisions").mkdir()
    (project / "PROJECT.md").write_text(PROJECT_BRIEF_TEXT, encoding="utf-8")
    (project / "decisions/README.md").write_text(DECISION_INDEX_TEXT, encoding="utf-8")
    (project / "product-contract.json").write_text(
        json.dumps(PRODUCT_CONTRACT), encoding="utf-8"
    )
    manifest = {
        "version": PROJECT_VERSION,
        "repository": {
            "canonical_remote": remote,
            "allowed_roots": ["."],
            "base_branch": branch,
        },
        "routing": {"provider": "test", "project": "test"},
        "product_contract": {
            "path": ".graph-engineering/product-contract.json",
            "version": PRODUCT_CONTRACT_VERSION,
            "generation": 1,
            "digest": PRODUCT_DIGEST,
        },
        "deployment": {"adapter": "universal-deploy", "targets": ["staging"]},
        "prohibited_operations": ["direct-scp", "unsanctioned-deploy"],
        "required_checks": [],
        "live_verification": {"required": False, "checks": []},
        "unresolved": [],
    }
    (project / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    local = repo / ".graph-engineering.local.toml"
    local.write_text(
        "[execution]\n"
        f"allowed_hosts = [{json.dumps(socket.gethostname())}]\n"
        f"allowed_checkout_roots = [{json.dumps(str(repo))}]\n",
        encoding="utf-8",
    )
    local.chmod(0o600)
    return repo


def _worker(tmp_path: Path, *, marker: Path | None = None) -> Path:
    script = tmp_path / "worker.py"
    prefix = f"open({str(marker)!r}, 'w').write('spawned')\n" if marker else ""
    script.write_text(
        "#!/usr/bin/env python3\n"
        + prefix
        + "import sys\n"
        + "sys.stdin.read()\n"
        + "print('{\"ok\": true}')\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _config(path: Path, worker: Path) -> Path:
    path.write_text(
        f"""version = 1

[profiles.worker]
adapter = "subprocess"
model = "test-model"
[profiles.worker.capabilities]
read = true
write = false
structured_output = true
worktree = true
resume = true
mcp = false
[profiles.worker.subprocess]
argv = [{json.dumps(str(worker))}]
prompt_transport = "stdin"
output_format = "json"
env_allowlist = []
""",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _node(node_id: str, *, needs: list[str] | None = None) -> dict:
    return {
        "id": node_id,
        "kind": "agent",
        "task": f"Return the accepted result for {node_id}.",
        "needs": needs or [],
        "inputs": {},
        "outputs": {"result": {"schema": copy.deepcopy(RESULT_SCHEMA)}},
        "profile": "worker",
        "workspace": "worktree",
        "permission": "read",
        "checks": [{"id": "accepted", "argv": ["true"]}],
        "retry": {"max_attempts": 1, "no_progress_limit": 1},
        "required": True,
    }


def _workflow(nodes: list[dict] | None = None) -> dict:
    nodes = nodes or [_node("worker")]
    return {
        "version": "graph-engineering/v1alpha1",
        "id": "cli_test",
        "goal": "exercise the thin CLI without a model",
        "product_contract": {
            "version": PRODUCT_CONTRACT_VERSION,
            "generation": 1,
            "digest": PRODUCT_DIGEST,
        },
        "budgets": {
            "max_nodes": 10,
            "max_concurrency": 3,
            "max_attempts_per_node": 1,
            "max_total_attempts": 10,
            "timeout_seconds": 30,
        },
        "nodes": nodes,
        "outputs": {"result": f"{nodes[-1]['id']}.result"},
    }


def _write_workflow(path: Path, workflow: dict) -> Path:
    path.write_text(json.dumps(workflow), encoding="utf-8")
    return path


def _json_stdout(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _completed_cli_run(
    tmp_path: Path, monkeypatch, capsys, run_id: str = "handoff-run"
):
    repo = _repo(tmp_path)
    config = _config(tmp_path / "config.toml", _worker(tmp_path))
    workflow = _write_workflow(tmp_path / "workflow.json", _workflow())
    state = tmp_path / "run" / "state.db"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    argv = [
        "run",
        str(workflow),
        "--repo",
        str(repo),
        "--run-id",
        run_id,
        "--state",
        str(state),
        "--config",
        str(config),
        "--json",
    ]
    assert main(argv) == 0
    _json_stdout(capsys)
    return repo, config, workflow, state, argv


def test_version_flag_reports_the_package_version(capsys):
    # intent: operators must be able to identify the exact installed runtime in receipts.
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    else:  # pragma: no cover - argparse's version action always exits
        raise AssertionError("--version did not terminate after printing")
    assert capsys.readouterr().out.strip() == f"graph-engineer {__version__}"


def test_validate_reports_exact_schema_path_and_json_location(tmp_path: Path, capsys):
    # intent: invalid workflows must identify the broken contract before any worker can run.
    invalid = _workflow()
    invalid["nodes"][0]["workspace"] = "somewhere"
    path = _write_workflow(tmp_path / "invalid.json", invalid)
    assert main(["validate", str(path), "--json"]) == 2
    payload = _json_stdout(capsys)
    assert payload["error"]["issues"][0]["path"] == "$.nodes[0].workspace"

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"version":\n', encoding="utf-8")
    assert main(["validate", str(malformed), "--json"]) == 2
    payload = _json_stdout(capsys)
    assert "line 2, column 1" in payload["error"]["issues"][0]["path"]


def test_validate_deep_json_returns_structured_error_without_traceback(
    tmp_path: Path, capsys
):
    # intent: parser recursion is an operator-facing validation result, never a crash.
    value = _workflow()
    value["nodes"][0]["outputs"]["result"]["schema"] = "__DEEP_SCHEMA__"
    raw = json.dumps(value).replace(
        '"__DEEP_SCHEMA__"', '{"items":' * 2_000 + "{}" + "}" * 2_000
    )
    path = tmp_path / "deep.json"
    path.write_text(raw, encoding="utf-8")

    assert main(["validate", str(path), "--json"]) == 2
    payload = _json_stdout(capsys)
    assert payload["error"]["code"] == "WORKFLOW_INVALID"
    assert payload["error"]["issues"] == [
        {
            "code": "SCHEMA_DEPTH_EXCEEDED",
            "message": "resource nesting exceeds the limit of 64",
            "path": "$.nodes[0].outputs.result.schema",
        }
    ]


def test_validate_reports_external_ref_hidden_under_unevaluated_items(
    tmp_path: Path, capsys
):
    # intent: CLI callers receive one stable preflight result for the exact
    # Draft 2020-12 location that previously bypassed schema-resource policy.
    value = _workflow()
    value["nodes"][0]["outputs"]["result"]["schema"] = {
        "type": "array",
        "unevaluatedItems": {"$ref": "https://example.invalid/hidden.json"},
    }
    path = _write_workflow(tmp_path / "unevaluated-items.json", value)

    assert main(["validate", str(path), "--json"]) == 2

    assert _json_stdout(capsys)["error"] == {
        "code": "WORKFLOW_INVALID",
        "message": "workflow validation failed",
        "issues": [
            {
                "code": "SCHEMA_EXTERNAL_REFERENCE",
                "message": "external and relative schema references are not allowed",
                "path": ("$.nodes[0].outputs.result.schema.unevaluatedItems.$ref"),
            }
        ],
    }


def test_plan_exposes_ready_layers_and_stable_critical_path_without_config(
    tmp_path: Path, capsys
):
    # intent: dependency plumbing must remain deterministic and must not require a model profile.
    first = _node("first")
    second = _node("second")
    join = _node("join", needs=["first", "second"])
    workflow = _workflow([first, second, join])
    path = _write_workflow(tmp_path / "plan.json", workflow)
    assert main(["plan", str(path), "--json"]) == 0
    payload = _json_stdout(capsys)
    assert payload["ready_layers"] == [["first", "second"], ["join"]]
    assert payload["critical_path"] == ["first", "join"]
    assert payload["critical_dependencies"] == [{"from": "first", "to": "join"}]
    assert payload["edges"] == [
        {"from": "first", "to": "join"},
        {"from": "second", "to": "join"},
    ]


def test_plan_status_projection_and_trace_share_output_terminal_slice(
    tmp_path: Path, capsys
):
    # intent: a longer ancillary branch cannot replace the shipment's critical path.
    nodes = [
        _node("root"),
        _node("ship", needs=["root"]),
        _node("research", needs=["root"]),
        _node("research_review", needs=["research"]),
        _node("audit", needs=["research_review"]),
        _node("audit_review", needs=["audit"]),
    ]
    workflow_value = _workflow(nodes)
    workflow_value["outputs"] = {"shipment": "ship.result"}
    for node in workflow_value["nodes"][2:]:
        node["kind"] = "transform"
        node["workspace"] = "read-only"
        node["required"] = False
        node.pop("profile")
    workflow = _write_workflow(tmp_path / "output-slice.json", workflow_value)

    assert main(["plan", str(workflow), "--json"]) == 0
    plan = _json_stdout(capsys)

    state = tmp_path / "run" / "state.db"
    cli.StateStore(state).create_run(workflow_value, "output-slice")
    LifecycleStore(state).initialize_context(
        "output-slice",
        StaticRunContextProvider({"base_sha": "test"}),
        allow_legacy_bootstrap=True,
    )
    assert (
        main(
            [
                "status",
                "--state",
                str(state),
                "--run-id",
                "output-slice",
                "--projection",
                "--json",
            ]
        )
        == 1
    )
    status = _json_stdout(capsys)
    assert (
        main(
            [
                "trace",
                "--state",
                str(state),
                "--run-id",
                "output-slice",
                "--json",
            ]
        )
        == 0
    )
    trace = _json_stdout(capsys)

    expected_path = ["root", "ship"]
    expected_slice = ["root", "ship"]
    ancillary = ["audit", "audit_review", "research", "research_review"]
    assert plan["critical_path"] == expected_path
    assert trace["supervision"]["topology"]["critical_path"] == expected_path
    assert status["projection"]["critical_path"] == expected_path
    assert plan["terminal_slice"] == expected_slice
    assert trace["supervision"]["topology"]["terminal_slice"] == expected_slice
    assert status["projection"]["terminal_slice"] == expected_slice
    assert plan["ancillary_nodes"] == ancillary
    assert trace["supervision"]["topology"]["ancillary_nodes"] == ancillary
    assert status["projection"]["ancillary_nodes"] == ancillary


def test_doctor_rejects_malformed_private_profile_before_probe(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: a malformed worker contract must fail before any executable is considered ready.
    repo = _repo(tmp_path)
    config = tmp_path / "bad.toml"
    config.write_text(
        "version=1\n[profiles.bad]\nadapter='subprocess'\nmodel='x'\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--json",
            ]
        )
        == 2
    )
    assert _json_stdout(capsys)["error"]["code"] == "ADAPTER_CONFIG"


def test_doctor_proves_scratch_is_not_memory_backed(tmp_path: Path, monkeypatch):
    # intent: writable tmpfs must not pass the disk-backed scratch readiness check.
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 25 0:32 / {tmp_path} rw - tmpfs tmpfs rw\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "_MOUNTINFO", mountinfo)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    probe = cli._scratch_storage_check()
    assert not probe["ok"]
    assert probe["detail"] == "memory-backed tmpfs"


def _smoke_worker(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@requires_smoke_confinement
def test_doctor_smoke_is_opt_in_isolated_reduced_and_redacted(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: default doctor never spends, while smoke proves a real strict worker response.
    repo = _repo(tmp_path)
    worker = _smoke_worker(
        tmp_path / "smoke.py",
        "import json, os, pathlib, sys\n"
        "cache = pathlib.Path(os.environ['XDG_CACHE_HOME']) / 'worker' / 'ready'\n"
        "cache.parent.mkdir(parents=True)\n"
        "cache.write_text('initialized')\n"
        "secret = os.environ.get('DOCTOR_UNALLOWLISTED_SECRET')\n"
        "print(json.dumps({'ok': secret is None}))\n",
    )
    config = _config(tmp_path / "config.toml", worker)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    secret = "never-render-this-secret"
    monkeypatch.setenv("DOCTOR_UNALLOWLISTED_SECRET", secret)
    original_execute = cli.execute_profile

    def unexpected_launch(*_args, **_kwargs):
        raise AssertionError("static doctor launched a worker")

    monkeypatch.setattr(cli, "execute_profile", unexpected_launch)

    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--profile",
                "worker",
                "--json",
            ]
        )
        == 1
    )
    static = _json_stdout(capsys)
    assert static["mode"] == "static"
    assert static["profiles"][0]["smoke"] is None
    monkeypatch.setattr(cli, "execute_profile", original_execute)

    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--profile",
                "worker",
                "--smoke",
                "--timeout",
                "2",
                "--json",
            ]
        )
        == 1
    )
    smoked = _json_stdout(capsys)
    smoke = smoked["profiles"][0]["smoke"]
    assert smoke["status"] == "passed"
    assert smoke["code"] == "OK"
    assert set(smoke) == {"status", "code", "duration_ms", "receipt"}
    assert set(smoke["receipt"]) == {
        "command_digest",
        "result_schema_digest",
        "stdout_digest",
        "stderr_digest",
        "stdout_bytes",
        "stderr_bytes",
        "exit_code",
        "duration_ms",
        "transport",
    }
    rendered = json.dumps(smoked)
    assert secret not in rendered
    assert str(worker) not in rendered
    assert "Return exactly" not in rendered


@requires_smoke_confinement
def test_doctor_smoke_fails_closed_for_malformed_timeout_and_write(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: a launched process is not healthy unless schema, time, and no-write gates pass.
    repo = _repo(tmp_path)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    cases = (
        ("malformed", "print('token=smoke-secret')\n", "MALFORMED_OUTPUT", "2"),
        ("timeout", "import time\ntime.sleep(2)\n", "TIMEOUT", "0.1"),
        (
            "write",
            (
                "import pathlib\npathlib.Path('escaped.txt').write_text('x')\n"
                "print('{\"ok\":true}')\n"
            ),
            "WRITE_DETECTED",
            "2",
        ),
    )
    for name, body, expected, timeout in cases:
        worker = _smoke_worker(tmp_path / f"{name}.py", body)
        config = _config(tmp_path / f"{name}.toml", worker)
        assert (
            main(
                [
                    "doctor",
                    "--repo",
                    str(repo),
                    "--config",
                    str(config),
                    "--profile",
                    "worker",
                    "--smoke",
                    "--timeout",
                    timeout,
                    "--json",
                ]
            )
            == 1
        )
        payload = _json_stdout(capsys)
        assert payload["profiles"][0]["smoke"]["status"] == "failed"
        assert payload["profiles"][0]["smoke"]["code"] == expected
        assert "smoke-secret" not in json.dumps(payload)


@requires_smoke_confinement
def test_doctor_smoke_redirects_hardcoded_state_and_avoids_nested_sandbox(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: CLI startup state is disposable, while Grok's inner sandbox is not nested.
    repo = _repo(tmp_path)
    fake_home = tmp_path / "operator-home"
    hardcoded_state = fake_home / ".local" / "state"
    hardcoded_state.mkdir(parents=True)
    hardcoded_data = fake_home / ".local" / "share"
    hardcoded_data.mkdir(parents=True)
    marker = f"smoke-{tmp_path.name}"
    worker = _smoke_worker(
        tmp_path / "grok",
        "import json, os, pathlib, sys\n"
        "assert sys.argv[sys.argv.index('--sandbox') + 1] == 'off'\n"
        "assert sys.argv[sys.argv.index('--tools') + 1] == ''\n"
        f"hard = pathlib.Path({str(hardcoded_state)!r}) / {marker!r}\n"
        "hard.mkdir()\n"
        "(hard / 'state.db').write_text('disposable')\n"
        f"data = pathlib.Path({str(hardcoded_data)!r}) / {marker!r}\n"
        "data.mkdir()\n"
        "(data / 'mcp.db').write_text('disposable')\n"
        "state = pathlib.Path(os.environ['XDG_STATE_HOME'])\n"
        "target = state / 'mcp' / 'shared'\n"
        "link = state / 'mcp' / 'links' / 'current'\n"
        "target.mkdir(parents=True)\n"
        "link.parent.mkdir(parents=True)\n"
        "link.symlink_to('../shared')\n"
        "print(json.dumps({'ok': True}))\n",
    )
    config = _config(tmp_path / "grok.toml", worker)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            f"argv = [{json.dumps(str(worker))}]",
            f"argv = [{json.dumps(str(worker))}, '--tools', 'read_file,grep,list_dir', "
            "'--sandbox', 'strict']",
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--profile",
                "worker",
                "--smoke",
                "--timeout",
                "2",
                "--json",
            ]
        )
        == 1
    )
    smoke = _json_stdout(capsys)["profiles"][0]["smoke"]
    assert smoke["status"] == "passed"
    assert smoke["code"] == "OK"
    assert not (hardcoded_state / marker).exists()
    assert not (hardcoded_data / marker).exists()


@requires_smoke_confinement
def test_doctor_smoke_rejects_symlink_that_really_escapes_disposable_state(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: allowing a normalized in-state MCP link must not allow a true parent escape.
    repo = _repo(tmp_path)
    worker = _smoke_worker(
        tmp_path / "symlink-escape.py",
        "import os, pathlib\n"
        "state = pathlib.Path(os.environ['XDG_STATE_HOME'])\n"
        "link = state / 'nested' / 'escape'\n"
        "link.parent.mkdir(parents=True)\n"
        "link.symlink_to('../../../../outside')\n"
        "print('{\"ok\":true}')\n",
    )
    config = _config(tmp_path / "symlink-escape.toml", worker)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--profile",
                "worker",
                "--smoke",
                "--timeout",
                "2",
                "--json",
            ]
        )
        == 1
    )
    smoke = _json_stdout(capsys)["profiles"][0]["smoke"]
    assert smoke["status"] == "failed"
    assert smoke["code"] == "WRITE_DETECTED"


@requires_smoke_confinement
def test_doctor_codex_smoke_avoids_ptrace_but_keeps_mount_isolation(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: Codex must reach its final JSONL event without gaining host writes.
    repo = _repo(tmp_path)
    outside = tmp_path / "codex-must-not-write"
    worker = _smoke_worker(
        tmp_path / "codex",
        "import json, pathlib\n"
        "tracer = next(line for line in pathlib.Path('/proc/self/status').read_text().splitlines() if line.startswith('TracerPid:'))\n"
        "if tracer.split(':', 1)[1].strip() != '0':\n"
        "    print(json.dumps({'type':'item.completed','item':{'type':'error','message':'ptrace changes Codex behavior'}}))\n"
        "    raise SystemExit(0)\n"
        "state = pathlib.Path(__import__('os').environ['XDG_CACHE_HOME']) / 'codex-smoke-state'\n"
        "state.write_bytes(b'x' * (5 * 1024 * 1024))\n"
        "state.unlink()\n"
        "try:\n"
        f"    pathlib.Path({str(outside)!r}).write_text('must-not-land')\n"
        "except OSError:\n"
        "    pass\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'{\"ok\":true}'}}))\n",
    )
    config = _config(tmp_path / "codex.toml", worker)
    config.write_text(
        config.read_text(encoding="utf-8")
        .replace(
            f"argv = [{json.dumps(str(worker))}]",
            f'argv = [{json.dumps(str(worker))}, "exec"]',
        )
        .replace('output_format = "json"', 'output_format = "jsonl"'),
        encoding="utf-8",
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--profile",
                "worker",
                "--smoke",
                "--timeout",
                "2",
                "--json",
            ]
        )
        == 1
    )
    smoke = _json_stdout(capsys)["profiles"][0]["smoke"]
    assert smoke["status"] == "passed"
    assert smoke["code"] == "OK"
    assert not outside.exists()


@requires_smoke_confinement
@pytest.mark.parametrize("kind", ["parent", "absolute"])
def test_doctor_smoke_blocks_and_reports_every_outside_write(
    tmp_path: Path, monkeypatch, capsys, kind: str
):
    # intent: a worker cannot escape the smoke repo even if it catches the denied write.
    repo = _repo(tmp_path)
    marker = tmp_path / f"{kind}-escape.txt"
    target = "../parent-escape.txt" if kind == "parent" else str(marker)
    worker = _smoke_worker(
        tmp_path / f"{kind}.py",
        "import pathlib\n"
        "try:\n"
        f"    pathlib.Path({target!r}).write_text('must-not-land')\n"
        "except OSError:\n"
        "    pass\n"
        "print('{\"ok\":true}')\n",
    )
    config = _config(tmp_path / f"{kind}.toml", worker)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--profile",
                "worker",
                "--smoke",
                "--timeout",
                "2",
                "--json",
            ]
        )
        == 1
    )
    smoke = _json_stdout(capsys)["profiles"][0]["smoke"]
    assert smoke["status"] == "failed"
    assert smoke["code"] == "WRITE_DETECTED"
    assert not marker.exists()
    assert not (repo.parent / "parent-escape.txt").exists()


@requires_smoke_confinement
def test_doctor_smoke_rejects_disposable_root_parent_escape(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: an allowed HOME/cache prefix cannot disguise a lexical escape.
    repo = _repo(tmp_path)
    outside = tmp_path / "state-parent-escape.txt"
    worker = _smoke_worker(
        tmp_path / "state-parent.py",
        "import os, pathlib\n"
        "target = pathlib.Path(os.environ['HOME']) / '..' / '..' / '..' / "
        f"{outside.name!r}\n"
        "try:\n"
        "    target.write_text('must-not-land')\n"
        "except OSError:\n"
        "    pass\n"
        "print('{\"ok\":true}')\n",
    )
    config = _config(tmp_path / "state-parent.toml", worker)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--profile",
                "worker",
                "--smoke",
                "--timeout",
                "2",
                "--json",
            ]
        )
        == 1
    )
    smoke = _json_stdout(capsys)["profiles"][0]["smoke"]
    assert smoke["code"] == "WRITE_DETECTED"
    assert not outside.exists()


@requires_smoke_confinement
def test_doctor_smoke_audit_cannot_be_unlinked_or_truncated_by_worker(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: audit evidence lives outside the worker mount and survives evasion attempts.
    repo = _repo(tmp_path)
    outside = tmp_path / "after-audit-evasion.txt"
    worker = _smoke_worker(
        tmp_path / "audit-evasion.py",
        "import os, pathlib\n"
        "audit = pathlib.Path(os.environ['GRAPH_ENGINEERING_WRITE_AUDIT'])\n"
        "for operation in ('unlink', 'truncate'):\n"
        "    try:\n"
        "        audit.unlink() if operation == 'unlink' else audit.open('w').truncate()\n"
        "    except OSError:\n"
        "        pass\n"
        "try:\n"
        f"    pathlib.Path({str(outside)!r}).write_text('must-not-land')\n"
        "except OSError:\n"
        "    pass\n"
        "print('{\"ok\":true}')\n",
    )
    config = _config(tmp_path / "audit-evasion.toml", worker)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--profile",
                "worker",
                "--smoke",
                "--timeout",
                "2",
                "--json",
            ]
        )
        == 1
    )
    smoke = _json_stdout(capsys)["profiles"][0]["smoke"]
    assert smoke["code"] == "WRITE_DETECTED"
    assert not outside.exists()


def test_doctor_never_renders_private_environment_identifiers(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: readiness diagnostics reveal only counts, never secret-reference names.
    repo = _repo(tmp_path)
    worker = _smoke_worker(tmp_path / "env-probe.py", "print('{\"ok\":true}')\n")
    config = _config(tmp_path / "env.toml", worker)
    subprocess_name = "DOCTOR_PRIVATE_SUBPROCESS_IDENTIFIER"
    a2a_name = "DOCTOR_PRIVATE_A2A_IDENTIFIER"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "env_allowlist = []", f"env_allowlist = [{subprocess_name!r}]"
        )
        + f"""

[profiles.remote]
adapter = "a2a"
model = "private-model"
[profiles.remote.capabilities]
read = true
write = false
structured_output = true
worktree = false
resume = true
mcp = false
[profiles.remote.a2a]
agent_card_url = "https://private-host.invalid/card"
auth_env = {a2a_name!r}
allowed_skills = ["review"]
expected_identity = "private-worker"
""",
        encoding="utf-8",
    )
    monkeypatch.delenv(subprocess_name, raising=False)
    monkeypatch.delenv(a2a_name, raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--json",
            ]
        )
        == 1
    )
    rendered = json.dumps(_json_stdout(capsys))
    assert subprocess_name not in rendered
    assert a2a_name not in rendered
    assert "private-host.invalid" not in rendered

    assert main(["doctor", "--repo", str(repo), "--config", str(config)]) == 1
    human = capsys.readouterr()
    rendered = human.out + human.err
    assert subprocess_name not in rendered
    assert a2a_name not in rendered
    assert "private-host.invalid" not in rendered


def test_doctor_config_failure_is_opaque(tmp_path: Path, monkeypatch, capsys):
    # intent: malformed private config cannot reflect identifiers, argv, or paths.
    repo = _repo(tmp_path)
    secret_identifier = "DOCTOR_MUST_NOT_RENDER"
    config = tmp_path / "opaque.toml"
    config.write_text(
        f"version=1\n[profiles.bad]\nadapter='subprocess'\nmodel='x'\n"
        f"private_probe={secret_identifier!r}\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--json",
            ]
        )
        == 2
    )
    rendered = json.dumps(_json_stdout(capsys))
    assert secret_identifier not in rendered
    assert str(config) not in rendered
    assert set(json.loads(rendered)["error"]) == {"code", "message"}


def test_doctor_smoke_rejects_mcp_profile_and_argument_abuse(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: smoke cannot inherit MCP authority or accept unbounded selectors/time.
    repo = _repo(tmp_path)
    marker = tmp_path / "must-not-launch"
    worker = _smoke_worker(
        tmp_path / "mcp.py",
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('bad')\n"
        "print('{\"ok\":true}')\n",
    )
    config = _config(tmp_path / "mcp.toml", worker)
    config.write_text(
        config.read_text(encoding="utf-8").replace("mcp = false", "mcp = true"),
        encoding="utf-8",
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert (
        main(
            [
                "doctor",
                "--repo",
                str(repo),
                "--config",
                str(config),
                "--profile",
                "worker",
                "--smoke",
                "--json",
            ]
        )
        == 1
    )
    assert _json_stdout(capsys)["profiles"][0]["smoke"]["code"] == "MCP_NOT_ALLOWED"
    assert not marker.exists()
    with pytest.raises(SystemExit):
        main(["doctor", "--profile", "../escape", "--smoke"])
    with pytest.raises(SystemExit):
        main(["doctor", "--timeout", "121", "--smoke"])


def test_run_and_status_surface_attempts_worktrees_and_bound_receipts(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: a completed run must remain inspectable from durable state alone.
    repo = _repo(tmp_path)
    config = _config(tmp_path / "config.toml", _worker(tmp_path))
    workflow = _write_workflow(tmp_path / "workflow.json", _workflow())
    state = tmp_path / "run" / "state.db"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    argv = [
        "run",
        str(workflow),
        "--repo",
        str(repo),
        "--run-id",
        "durable-run",
        "--state",
        str(state),
        "--config",
        str(config),
        "--json",
    ]
    assert main(argv) == 0
    run = _json_stdout(capsys)
    assert run["status"] == "succeeded"
    assert len(run["run_context"]["values"]["project_policy_digest"]) == 64
    assert len(run["run_context"]["values"]["private_execution_digest"]) == 64
    assert len(run["run_context"]["values"]["repository_digest"]) == 64
    assert run["agent_receipts"]["worker#1"]["profile"] == "worker"
    assert run["worktrees"]["worker#1"].endswith("worker-87eba76e-a1")

    assert (
        main(["status", "--state", str(state), "--run-id", "durable-run", "--json"])
        == 0
    )
    status = _json_stdout(capsys)
    assert status["nodes"]["worker"]["attempt_count"] == 1
    assert status["attempts"][0]["status"] == "succeeded"
    assert status["artifact_receipts"][0]["output_name"] == "result"
    assert status["agent_receipts"]["worker#1"]["exit_code"] == 0
    assert status["check_receipts"][0]["check_id"] == "accepted"
    assert status["worktrees"][0].startswith(".claude/worktrees/graph-runs/")
    assert status["supervision"]["progress"]["worker"]["decision"] == "complete"
    assert status["supervision"]["topology"]["critical_path_remaining"] == []

    assert (
        main(["trace", "--state", str(state), "--run-id", "durable-run", "--json"]) == 0
    )
    trace = _json_stdout(capsys)
    assert trace["context"]["values"]["base_sha"] == _git(repo, "rev-parse", "HEAD")
    assert trace["events"][-1]["event_type"] == "run.succeeded"
    assert any(event["event_type"] == "progress.observed" for event in trace["events"])
    assert trace["supervision"]["progress"]["worker"]["artifact_delta"] == 1

    resume_argv = [*argv[:-1], "--resume", "--json"]
    assert main(resume_argv) == 0
    resumed = _json_stdout(capsys)
    assert resumed["status"] == "succeeded"
    assert resumed["lifecycle"]["event_count"] > trace["event_count"]
    assert (
        main(["trace", "--state", str(state), "--run-id", "durable-run", "--json"]) == 0
    )
    resumed_trace = _json_stdout(capsys)
    assert (
        sum(event["event_type"] == "run.succeeded" for event in resumed_trace["events"])
        == 1
    )

    changed = _workflow()
    changed["goal"] = "silently changed after the durable run"
    changed_path = _write_workflow(tmp_path / "changed.json", changed)
    assert (
        main(
            [
                "run",
                str(changed_path),
                "--repo",
                str(repo),
                "--run-id",
                "durable-run",
                "--state",
                str(state),
                "--config",
                str(config),
                "--resume",
                "--json",
            ]
        )
        == 2
    )
    assert _json_stdout(capsys)["error"]["code"] == "RESUME_MISMATCH"

    receipt = (
        state.parent
        / "artifacts"
        / "receipts"
        / (hashlib.sha256(b"durable-run").hexdigest() + ".json")
    )
    receipt.write_bytes(receipt.read_bytes() + b" ")
    assert (
        main(["status", "--state", str(state), "--run-id", "durable-run", "--json"])
        == 2
    )
    assert _json_stdout(capsys)["error"]["code"] == "CORRUPT_RECEIPT_LEDGER"
    receipt.unlink()
    assert (
        main(
            [
                "status",
                "--state",
                str(state),
                "--run-id",
                "durable-run",
                "--json",
            ]
        )
        == 2
    )
    assert _json_stdout(capsys)["error"]["code"] == "CORRUPT_RECEIPT_LEDGER"


def test_run_rejects_shell_check_before_worker_spawn(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: a checked-in workflow cannot turn the CLI into an arbitrary shell launcher.
    repo = _repo(tmp_path)
    marker = tmp_path / "spawned"
    config = _config(tmp_path / "config.toml", _worker(tmp_path, marker=marker))
    value = _workflow()
    value["nodes"][0]["checks"] = [{"id": "unsafe", "argv": ["bash", "-c", "true"]}]
    workflow = _write_workflow(tmp_path / "workflow.json", value)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    assert (
        main(
            [
                "run",
                str(workflow),
                "--repo",
                str(repo),
                "--run-id",
                "unsafe-check",
                "--state",
                str(tmp_path / "state.db"),
                "--config",
                str(config),
                "--json",
            ]
        )
        == 2
    )
    assert _json_stdout(capsys)["error"]["code"] == "SHELL_CHECK_FORBIDDEN"
    assert not marker.exists()


def test_run_rejects_same_scope_duplicate_before_worker_spawn(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: two run IDs cannot concurrently own one repo/base/contract/workflow scope.
    repo = _repo(tmp_path)
    marker = tmp_path / "spawned"
    config = _config(tmp_path / "config.toml", _worker(tmp_path, marker=marker))
    value = _workflow()
    workflow = _write_workflow(tmp_path / "workflow.json", value)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    state_root = tmp_path / "state-home" / "graph-engineering" / "runs"
    policy = load_project_policy(repo)
    identity = execution_identity(
        policy, value, base_sha=_git(repo, "rev-parse", "HEAD")
    )
    RunScopeRegistry(state_root).claim(
        identity,
        run_id="first-run",
        state_path=tmp_path / "first" / "state.db",
        resume=False,
    )

    assert (
        main(
            [
                "run",
                str(workflow),
                "--repo",
                str(repo),
                "--run-id",
                "second-run",
                "--state",
                str(tmp_path / "second" / "state.db"),
                "--config",
                str(config),
                "--json",
            ]
        )
        == 2
    )
    assert _json_stdout(capsys)["error"]["code"] == "DUPLICATE_ACTIVE_RUN"
    assert not marker.exists()


def test_handoff_accepts_exact_resume_and_rejects_tamper_deletion_and_contract_drift(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: another engine may resume only the exact durable run snapshot it received.
    repo, config, _workflow_path, state, argv = _completed_cli_run(
        tmp_path, monkeypatch, capsys
    )
    secret = "rotated-handoff-credential"
    _git(
        repo,
        "remote",
        "set-url",
        "origin",
        f"https://oauth2:{secret}@example.invalid/graph/cli-test.git",
    )
    handoff = tmp_path / "handoff.json"
    assert (
        main(
            [
                "handoff",
                "--state",
                str(state),
                "--run-id",
                "handoff-run",
                "--output",
                str(handoff),
                "--json",
            ]
        )
        == 0
    )
    exported = _json_stdout(capsys)
    assert exported["version"] == "graph-engineering/handoff/v1"
    body = json.loads(handoff.read_text(encoding="utf-8"))["body"]
    assert len(body["project_policy_sha256"]) == 64
    assert len(body["private_execution_sha256"]) == 64
    assert len(body["repository_sha256"]) == 64
    assert body["repository_sha256"] == repository_digest(repo)
    assert secret not in handoff.read_text(encoding="utf-8")
    assert stat.S_IMODE(handoff.stat().st_mode) == 0o600

    assert main([*argv[:-1], "--resume", "--handoff", str(handoff), "--json"]) == 0
    assert _json_stdout(capsys)["status"] == "succeeded"

    # A resume changes the lifecycle head, so the consumed handoff cannot be replayed.
    assert main([*argv[:-1], "--resume", "--handoff", str(handoff), "--json"]) == 2
    assert _json_stdout(capsys)["error"]["code"] == "HANDOFF_STATE_DRIFT"

    fresh = tmp_path / "fresh-handoff.json"
    assert (
        main(
            [
                "handoff",
                "--state",
                str(state),
                "--run-id",
                "handoff-run",
                "--output",
                str(fresh),
                "--json",
            ]
        )
        == 0
    )
    _json_stdout(capsys)
    tampered = json.loads(fresh.read_text(encoding="utf-8"))
    tampered["body"]["status"] = "failed"
    bad = tmp_path / "tampered.json"
    bad.write_text(json.dumps(tampered), encoding="utf-8")
    assert main([*argv[:-1], "--resume", "--handoff", str(bad), "--json"]) == 2
    assert _json_stdout(capsys)["error"]["code"] == "HANDOFF_TAMPERED"

    changed = _workflow()
    changed["goal"] = "changed contract"
    changed_path = _write_workflow(tmp_path / "changed.json", changed)
    changed_argv = [
        "run",
        str(changed_path),
        "--repo",
        str(repo),
        "--run-id",
        "handoff-run",
        "--state",
        str(state),
        "--config",
        str(config),
        "--resume",
        "--handoff",
        str(fresh),
        "--json",
    ]
    assert main(changed_argv) == 2
    assert _json_stdout(capsys)["error"]["code"] == "HANDOFF_CONTRACT_DRIFT"

    fresh.unlink()
    assert main([*argv[:-1], "--resume", "--handoff", str(fresh), "--json"]) == 2
    assert _json_stdout(capsys)["error"]["code"] == "HANDOFF_READ_ERROR"

    with sqlite3.connect(state) as connection:
        digest = connection.execute(
            "SELECT digest FROM artifacts WHERE run_id=? LIMIT 1", ("handoff-run",)
        ).fetchone()[0]
    (state.parent / "artifacts" / digest[:2] / f"{digest}.json").unlink()
    assert (
        main(["handoff", "--state", str(state), "--run-id", "handoff-run", "--json"])
        == 2
    )
    assert _json_stdout(capsys)["error"]["code"] == "HANDOFF_ARTIFACT_MISSING"


def test_handoff_resume_rejects_profile_and_base_drift(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: a handoff cannot authorize a different model profile or repository base.
    repo, config, _workflow_path, state, argv = _completed_cli_run(
        tmp_path, monkeypatch, capsys, "identity-run"
    )
    handoff = tmp_path / "identity.json"
    assert (
        main(
            [
                "handoff",
                "--state",
                str(state),
                "--run-id",
                "identity-run",
                "--output",
                str(handoff),
                "--json",
            ]
        )
        == 0
    )
    _json_stdout(capsys)

    original = config.read_text(encoding="utf-8")
    config.write_text(original.replace("test-model", "changed-model"), encoding="utf-8")
    assert main([*argv[:-1], "--resume", "--handoff", str(handoff), "--json"]) == 2
    assert _json_stdout(capsys)["error"]["code"] == "PROFILE_MANIFEST_MISMATCH"
    config.write_text(original, encoding="utf-8")

    (repo / "README.md").write_text("new base\n", encoding="utf-8")
    _git(repo, "add", "--", "README.md")
    _git(repo, "commit", "-qm", "new base")
    assert main([*argv[:-1], "--resume", "--handoff", str(handoff), "--json"]) == 2
    assert _json_stdout(capsys)["error"]["code"] == "STALE_BASE"


def test_status_projection_is_bounded_redacted_and_actionable(tmp_path: Path):
    # intent: terminal projection must not become an unbounded secret-bearing process dump.
    state = tmp_path / "state.db"
    nodes = [_node(f"lane_{index}") for index in range(105)]
    workflow = _workflow(nodes)
    workflow["budgets"]["max_nodes"] = 105
    workflow["budgets"]["max_total_attempts"] = 105
    store = cli.StateStore(state)
    store.create_run(workflow, "projection")
    with sqlite3.connect(state) as connection:
        connection.execute(
            "UPDATE nodes SET status='failed',error=? WHERE run_id=? AND node_id=?",
            ("token=super-secret-value\n" + "x" * 2000, "projection", "lane_0"),
        )
        connection.commit()
    projection = status_projection(state, "projection")
    assert len(projection["lanes"]) == 100
    assert projection["lanes_omitted"] == 5
    first = next(item for item in projection["lanes"] if item["node_id"] == "lane_0")
    assert "super-secret-value" not in first["blocker"]
    assert "[REDACTED]" in first["blocker"]
    assert len(first["blocker"].encode()) <= 520
    assert first["next_route"] == "stop_and_review"
    assert projection["critical_path"]


def test_public_adoption_templates_validate_and_stream_classes():
    root = Path(__file__).parents[1]
    mobile = cli._load_json_workflow(
        root / "examples/mobile-automation-vertical-slice.workflow.json"
    )
    matrix = cli._load_json_workflow(
        root / "examples/contract-matrix-class-prove.workflow.json"
    )
    assert mobile["nodes"][0]["id"] == "probe_real_journey"
    alpha = next(node for node in matrix["nodes"] if node["id"] == "class_alpha_prove")
    beta = next(node for node in matrix["nodes"] if node["id"] == "class_beta_prove")
    assert alpha["needs"] == beta["needs"] == ["probe_contract_matrix"]
    assert set(alpha["write_scope"]).isdisjoint(beta["write_scope"])


def test_assess_empty_partial_and_ready_repositories_without_mutation(
    tmp_path: Path, capsys
):
    # intent: adoption guidance reports evidence gaps, not a decorative numeric score.
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    _git(empty, "config", "user.name", "CLI Test")
    _git(empty, "config", "user.email", "cli@example.com")
    (empty / "README.md").write_text("empty adoption\n", encoding="utf-8")
    _git(empty, "add", "--", "README.md")
    _git(empty, "commit", "-qm", "base")
    before = sorted(path.relative_to(empty) for path in empty.rglob("*"))
    empty_result = assess_repo(empty, empty / "missing.toml")
    assert empty_result["version"] == "graph-engineering/assessment/v1"
    assert empty_result["summary"]["critical"] >= 1
    assert "score" not in empty_result["summary"]
    assert all(
        set(gap)
        == {
            "id",
            "priority",
            "area",
            "evidence",
            "fix_sites",
            "remediation",
            "acceptance",
            "verify_cmd",
        }
        for gap in empty_result["gaps"]
    )
    assert before == sorted(path.relative_to(empty) for path in empty.rglob("*"))

    partial = tmp_path / "partial"
    partial.mkdir()
    _git(partial, "init", "-q")
    _git(partial, "config", "user.name", "CLI Test")
    _git(partial, "config", "user.email", "cli@example.com")
    (partial / "README.md").write_text("partial adoption\n", encoding="utf-8")
    _git(partial, "add", "--", "README.md")
    _git(partial, "commit", "-qm", "base")
    (partial / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8"
    )
    unsafe = partial / "config.toml"
    unsafe.write_text("literal_token=do-not-leak", encoding="utf-8")
    unsafe.chmod(0o644)
    partial_result = assess_repo(partial, unsafe)
    assert not partial_result["capabilities"]["private_profiles"]["ready"]
    assert "do-not-leak" not in json.dumps(partial_result)

    ready = tmp_path / "ready"
    ready.mkdir(exist_ok=True)
    _git(ready, "init", "-q")
    _git(ready, "config", "user.name", "CLI Test")
    _git(ready, "config", "user.email", "cli@example.com")
    (ready / "README.md").write_text("ready adoption\n", encoding="utf-8")
    _git(ready, "add", "--", "README.md")
    _git(ready, "commit", "-qm", "base")
    (ready / "tests").mkdir()
    (ready / "pyproject.toml").write_text(
        "[build-system]\nrequires=[]\n"
        "[tool.pytest.ini_options]\n"
        "[tool.ruff]\n"
        "[tool.mypy]\n",
        encoding="utf-8",
    )
    workflow_dir = ready / ".graph-engineering" / "workflows"
    workflow_dir.mkdir(parents=True)
    source = (
        Path(__file__).parents[1]
        / "examples/mobile-automation-vertical-slice.workflow.json"
    )
    (workflow_dir / "slice.json").write_bytes(source.read_bytes())
    worker_root = tmp_path / "ready-worker"
    worker_root.mkdir()
    private = _config(ready / "private.toml", _worker(worker_root))
    ready_result = assess_repo(ready, private)
    assert not ready_result["summary"]["ready"]
    assert not ready_result["capabilities"]["planning_capsule"]["ready"]
    assert any(gap["id"] == "planning-capsule" for gap in ready_result["gaps"])
    assert set(ready_result["source"]) == {"head_sha", "source_digest"}
    prior_digest = ready_result["source"]["source_digest"]
    (ready / "tests" / "test_new.py").write_text("assert True\n", encoding="utf-8")
    changed_result = assess_repo(ready, private)
    assert changed_result["source"]["source_digest"] != prior_digest

    capsule_parent = tmp_path / "capsule-ready"
    capsule_parent.mkdir()
    capsule_result = assess_repo(_repo(capsule_parent))
    assert capsule_result["capabilities"]["planning_capsule"] == {
        "ready": True,
        "unanswered": [],
    }

    artifact = tmp_path / "assessment.json"
    assert (
        main(
            [
                "assess",
                "--repo",
                str(ready),
                "--config",
                str(private),
                "--output",
                str(artifact),
                "--json",
            ]
        )
        == 0
    )
    _json_stdout(capsys)
    assert json.loads(artifact.read_text(encoding="utf-8"))["version"] == (
        "graph-engineering/assessment/v1"
    )
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


def test_assessment_artifact_round_trips_into_init_with_freshness_and_write_bounds(
    tmp_path: Path, capsys
):
    # intent: the public assessment/v1 producer and consumer are one executable contract.
    repo = tmp_path / "adopt"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "CLI Test")
    _git(repo, "config", "user.email", "cli@example.com")
    (repo / "README.md").write_text("before\n", encoding="utf-8")
    _git(repo, "add", "--", "README.md")
    _git(repo, "commit", "-qm", "base")
    artifact = tmp_path / "assessment.json"
    before = {path.relative_to(repo) for path in repo.rglob("*") if path.is_file()}

    assert (
        main(
            [
                "assess",
                "--repo",
                str(repo),
                "--output",
                str(artifact),
                "--json",
            ]
        )
        == 0
    )
    _json_stdout(capsys)
    assert before == {
        path.relative_to(repo) for path in repo.rglob("*") if path.is_file()
    }

    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    assert (
        main(
            [
                "init",
                "--repo",
                str(repo),
                "--from-assessment",
                str(artifact),
                "--json",
            ]
        )
        == 2
    )
    stale = _json_stdout(capsys)
    assert stale["error"]["code"] == "ASSESSMENT_STALE"
    assert not (repo / ".graph-engineering").exists()

    (repo / "README.md").write_text("before\n", encoding="utf-8")
    assessed = json.loads(artifact.read_text(encoding="utf-8"))
    sabotaged = copy.deepcopy(assessed)
    sabotaged["capabilities"]["bounded_effects"]["uncontracted"] = True
    sabotage_path = tmp_path / "sabotaged-assessment.json"
    sabotage_path.write_text(json.dumps(sabotaged), encoding="utf-8")
    assert (
        main(
            [
                "init",
                "--repo",
                str(repo),
                "--from-assessment",
                str(sabotage_path),
                "--json",
            ]
        )
        == 2
    )
    malformed = _json_stdout(capsys)
    assert malformed["error"]["code"] == "ASSESSMENT_SCHEMA"
    assert not (repo / ".graph-engineering").exists()

    assert (
        main(
            [
                "init",
                "--repo",
                str(repo),
                "--from-assessment",
                str(artifact),
                "--json",
            ]
        )
        == 1
    )
    initialized = _json_stdout(capsys)
    assert initialized["assessment_recommendation"] == {
        "workflow_templates": assessed["recommended_init"]["workflow_templates"],
        "require_private_config": assessed["recommended_init"][
            "require_private_config"
        ],
    }
    after = {path.relative_to(repo) for path in repo.rglob("*") if path.is_file()}
    assert after - before == {
        Path(".graph-engineering/product-contract.json"),
        Path(".graph-engineering/project.json"),
        Path(".graph-engineering/PROJECT.md"),
        Path(".graph-engineering/decisions/README.md"),
        Path(".graph-engineering/workflows/starter.json"),
    }


def test_run_rejects_public_private_config_before_worker_spawn(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: executable worker definitions must stay private even when their TOML is valid.
    repo = _repo(tmp_path)
    marker = tmp_path / "spawned"
    config = _config(tmp_path / "config.toml", _worker(tmp_path, marker=marker))
    config.chmod(0o644)
    workflow = _write_workflow(tmp_path / "workflow.json", _workflow())
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    assert (
        main(
            [
                "run",
                str(workflow),
                "--repo",
                str(repo),
                "--run-id",
                "public-config",
                "--state",
                str(tmp_path / "state.db"),
                "--config",
                str(config),
                "--json",
            ]
        )
        == 2
    )
    assert _json_stdout(capsys)["error"]["code"] == "PRIVATE_CONFIG_NOT_READY"
    assert not marker.exists()
