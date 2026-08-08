from __future__ import annotations

import copy
import hashlib
import json
import stat
import subprocess
from pathlib import Path

from graph_engineering import __version__, cli
from graph_engineering.cli import main

RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ok"],
    "properties": {"ok": {"const": True}},
}


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
