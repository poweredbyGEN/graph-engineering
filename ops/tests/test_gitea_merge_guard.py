"""Tests for the GEN-6025 merge gate in ``ops/gitea``.

WHY THIS EXISTS
``.woodpecker/ci.yml`` sets ``concurrency: 1``, so merging to main CANCELS the pipeline
still running for the previous main sha. A rapid series of merges therefore leaves every
main sha without a terminal verdict, and gen-deployd's gate can never pass: on 2026-08-28
production sat 10 commits behind for a day while every PR page showed green. The gate in
``gitea pr merge`` refuses the merge while main's own CI is non-terminal. These tests pin
that refusal, because a gate nobody can see failing is a gate that silently stops working.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

GITEA = Path(__file__).resolve().parents[1] / "gitea"


def _run(tmp_path, statuses, *, env=None, main_sha="deadbeef"):
    """Drive the real script with a stub ``curl`` so no network is touched.

    The stub answers the two endpoints the gate calls: the main branch head and that
    sha's statuses. A merge POST is echoed so a test can tell "merged" from "refused".
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"main_sha = {main_sha!r}\n"
        f"statuses = {json.dumps(statuses)!r}\n"
        "url = [a for a in sys.argv[1:] if a.startswith('http')][-1]\n"
        "if '/branches/main' in url:\n"
        "    print(json.dumps({'commit': {'id': main_sha}}))\n"
        "elif '/statuses' in url:\n"
        "    print(json.loads(statuses) and json.dumps(json.loads(statuses)))\n"
        "elif '/merge' in url:\n"
        "    print('MERGE-POSTED')\n"
        "else:\n"
        "    print(json.dumps({'number': 1, 'title': 't', 'body': ''}))\n"
    )
    (bin_dir / "curl").chmod(0o755)

    env_file = tmp_path / "gen-gitea.env"
    env_file.write_text("GITEA_TOKEN=x\nGITEA_URL=https://git.example\n")
    script = GITEA.read_text().replace("/etc/gen-gitea.env", str(env_file))
    patched = tmp_path / "gitea"
    patched.write_text(script)
    patched.chmod(0o755)

    import os

    run_env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    run_env.update(env or {})
    return subprocess.run(
        [str(patched), "pr", "merge", "o/r", "1"],
        capture_output=True, text=True, env=run_env,
    )


def _status(context, state, sid):
    return {"context": context, "status": state, "id": sid}


def test_merge_is_refused_while_mains_own_ci_is_still_running(tmp_path):
    # intent: THE property. A pending push status on main means merging now cancels that
    # pipeline and strands main with no verdict — the exact 2026-08-28 outage.
    r = _run(tmp_path, [_status("ci/woodpecker/push/ci", "pending", 1)])
    assert r.returncode == 3, r.stdout
    assert "REFUSED" in r.stderr
    assert "MERGE-POSTED" not in r.stdout


def test_merge_proceeds_when_mains_ci_is_terminal(tmp_path):
    # intent: the gate must not block the normal case, or it teaches people to bypass it.
    r = _run(tmp_path, [_status("ci/woodpecker/push/ci", "success", 1)])
    assert r.returncode == 0, r.stderr
    assert "MERGE-POSTED" in r.stdout


def test_a_failure_on_main_is_terminal_and_does_not_block(tmp_path):
    # intent: the gate protects the DEPLOY signal, not code health. A red main already has
    # its verdict; blocking on it would stop the very fix that repairs main.
    r = _run(tmp_path, [_status("ci/woodpecker/push/ci", "failure", 1)])
    assert r.returncode == 0, r.stderr
    assert "MERGE-POSTED" in r.stdout


def test_newest_status_per_context_wins_over_a_superseded_row(tmp_path):
    # intent: a restarted pipeline lands statuses whose created_at ties the superseded run,
    # so the gate ranks by max status id. Ranking by API order would read the stale
    # success and merge into a pipeline that is still running (caught live 2026-08-25).
    r = _run(tmp_path, [
        _status("ci/woodpecker/push/ci", "success", 1),
        _status("ci/woodpecker/push/ci", "pending", 2),
    ])
    assert r.returncode == 3, r.stdout
    assert "REFUSED" in r.stderr


def test_a_main_with_no_ci_at_all_blocks_until_explicitly_overridden(tmp_path):
    # intent: no rows is ambiguous, not safe — it is equally "CI never ran" and "the
    # statuses call failed". Fail closed, with a named escape hatch in the message.
    r = _run(tmp_path, [])
    assert r.returncode == 3
    assert "GEN_MERGE_SKIP_MAIN_GATE=1" in r.stderr


def test_the_override_lets_a_deliberate_first_merge_through(tmp_path):
    # intent: the escape hatch must actually work, or the gate gets deleted instead of used.
    r = _run(tmp_path, [], env={"GEN_MERGE_SKIP_MAIN_GATE": "1"})
    assert r.returncode == 0, r.stderr
    assert "MERGE-POSTED" in r.stdout


def test_only_push_pipelines_gate_the_merge(tmp_path):
    # intent: a pull_request pipeline runs against the PR, not main, and is not subject to
    # the concurrency cancellation. Gating on it would block every merge forever.
    r = _run(tmp_path, [
        _status("ci/woodpecker/push/ci", "success", 1),
        _status("ci/woodpecker/pr/ci", "pending", 2),
    ])
    assert r.returncode == 0, r.stderr
    assert "MERGE-POSTED" in r.stdout
