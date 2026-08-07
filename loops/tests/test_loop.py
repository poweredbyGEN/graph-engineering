"""Tests for evidence-loop.

The load-bearing ones are the termination and evidence-integrity tests: a loop that can't stop
burns budget forever, and a loop that passes without real evidence is worse than no loop at all
because it manufactures false confidence.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_loop.loop import (  # noqa: E402
    Attempt,
    CheckResult,
    build_feedback,
    load_config,
    run_check,
)


def ok(name="tests", out="fine"):
    return CheckResult(name, ["true"], 0, 0.1, out)


def bad(name="tests", out="boom", code=1):
    return CheckResult(name, ["false"], code, 0.1, out)


# --- evidence integrity -------------------------------------------------------------

def test_attempt_passes_only_when_every_check_passes():
    # intent: partial success must not read as success. If `any` ever replaces `all`, a repo
    # with a green linter and a red test suite would report done.
    assert Attempt(1, [ok("a"), ok("b")]).passed
    assert not Attempt(1, [ok("a"), bad("b")]).passed


def test_empty_checks_never_passes():
    # intent: a config with no checks, or a run where every check errored out of existence,
    # must NOT be treated as success. Vacuous truth here means a rubber stamp.
    assert not Attempt(1, []).passed


def test_timed_out_check_is_a_failure_even_with_exit_zero():
    # intent: a timeout is not a pass. Some runners exit 0 when killed; keying only on
    # exit_code would let a hung suite report green.
    assert not CheckResult("t", ["x"], 0, 9.0, "", timed_out=True).passed


def test_missing_binary_fails_loudly(tmp_path):
    # intent: an uninstalled checker must fail, never silently contribute nothing. An absent
    # checker that "passes" turns the whole loop into a rubber stamp.
    r = run_check("ghost", ["definitely-not-a-real-binary-xyz"], tmp_path, 5)
    assert not r.passed and r.exit_code == 127 and "not found" in r.output


def test_nonzero_exit_is_captured_not_raised(tmp_path):
    # intent: a failing check is the loop's INPUT. If run_check raises, the runner dies instead
    # of feeding the failure back to the agent.
    r = run_check("fail", [sys.executable, "-c", "import sys; sys.exit(3)"], tmp_path, 10)
    assert r.exit_code == 3 and not r.passed


def test_no_phantom_failure_from_stale_bytecode(tmp_path):
    # intent: caught LIVE in e2e — after the agent fixed calc.py, attempt 2 still failed
    # because a cached .pyc served the OLD module. The loop reported a failure that no longer
    # existed, which is the exact false signal this layer exists to prevent. Checks must run
    # with bytecode caching disabled.
    (tmp_path / "m.py").write_text("VALUE = 1\n")
    probe = [sys.executable, "-c", "import m; import sys; sys.exit(0 if m.VALUE == 2 else 1)"]

    assert run_check("before", probe, tmp_path, 30).exit_code == 1  # populates any cache
    (tmp_path / "m.py").write_text("VALUE = 2\n")  # the "agent" fixes it
    r = run_check("after", probe, tmp_path, 30)

    assert r.passed, "stale bytecode made a fixed file still report as failing"
    assert not list(tmp_path.glob("__pycache__/*")), "check wrote bytecode despite the guard"


def test_check_env_is_passed_through(tmp_path):
    # intent: src/-layout repos need PYTHONPATH (or NODE_ENV, etc.) or the check fails on an
    # import error that LOOKS like a code problem but is a config problem — sending the agent
    # chasing a bug that does not exist.
    probe = [sys.executable, "-c", "import os,sys; sys.exit(0 if os.environ.get('MARKER')=='yes' else 1)"]
    assert not run_check("no-env", probe, tmp_path, 10).passed
    assert run_check("with-env", probe, tmp_path, 10, {"MARKER": "yes"}).passed


def test_check_timeout_is_enforced(tmp_path):
    # intent: without a per-check timeout a hung command blocks the loop forever, which is the
    # failure mode that makes unattended runs unsafe.
    r = run_check("slow", [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 1)
    assert r.timed_out and not r.passed


# --- termination --------------------------------------------------------------------

def test_identical_failures_produce_identical_digests():
    # intent: the no-progress guard keys on this. If digests differ for identical failures the
    # guard never fires and a stuck agent burns every retry.
    a1, a2 = Attempt(1, [bad("tests", "same error")]), Attempt(2, [bad("tests", "same error")])
    assert a1.digest() == a2.digest()


def test_different_failures_produce_different_digests():
    # intent: the mirror case — if digests collide across DIFFERENT failures the loop stops
    # early on an agent that is actually making progress.
    assert Attempt(1, [bad("t", "error A")]).digest() != Attempt(2, [bad("t", "error B")]).digest()


def test_digest_ignores_passing_checks():
    # intent: progress must be measured on what still fails. Including passes would change the
    # digest when an unrelated check flips, masking a stuck failure.
    assert Attempt(1, [ok("lint"), bad("t", "x")]).digest() == Attempt(2, [bad("t", "x")]).digest()


# --- feedback quality ---------------------------------------------------------------

def test_feedback_contains_goal_command_and_output():
    # intent: feedback the agent cannot act on wastes an attempt. It must carry what failed,
    # how it was invoked, and what the goal was.
    fb = build_feedback("all green", Attempt(1, [bad("tests", "AssertionError: x != y")]), [])
    assert "all green" in fb and "AssertionError" in fb and "false" in fb


def test_feedback_includes_prior_attempts():
    # intent: without history the agent cycles between two wrong fixes forever. This is the
    # 'state' element of the loop.
    h = [Attempt(1, [bad("tests")]), Attempt(2, [bad("lint")])]
    fb = build_feedback("g", h[-1], h)
    assert "Previous attempts" in fb and "attempt 1" in fb


def test_feedback_forbids_editing_the_checks():
    # intent: the cheapest way to make evidence pass is to delete the test. The instruction
    # must be present in every feedback message, not just the docs.
    fb = build_feedback("g", Attempt(1, [bad()]), [])
    assert "not modify the checks" in fb


def test_long_output_truncates_from_both_ends(tmp_path):
    # intent: tail-only truncation drops pytest's summary line (the pass/fail counts);
    # head-only drops the exit status. Both ends must survive.
    from evidence_loop.loop import _truncate

    out = _truncate("HEAD" + "x" * 50_000 + "TAIL", limit=1000)
    assert "HEAD" in out and "TAIL" in out and "truncated" in out


# --- config -------------------------------------------------------------------------

def test_config_without_checks_is_rejected(tmp_path):
    # intent: "a loop with no evidence is not a loop" — it would pass instantly and always.
    p = tmp_path / "e.toml"
    p.write_text('goal = "x"\n')
    with pytest.raises(SystemExit, match="no \\[\\[checks\\]\\]"):
        load_config(p)


def test_config_with_string_cmd_is_rejected(tmp_path):
    # intent: cmd must be an argv list. A string implies shell=True somewhere downstream,
    # which reintroduces injection through a config file.
    p = tmp_path / "e.toml"
    p.write_text('goal = "x"\n[[checks]]\nname = "t"\ncmd = "pytest -q"\n')
    with pytest.raises(SystemExit, match="argv"):
        load_config(p)


# --- end to end ---------------------------------------------------------------------

def test_check_only_run_reports_failure_and_exits_nonzero(tmp_path):
    # intent: the runner must exit non-zero when evidence fails, so CI and callers can gate on
    # it. Exiting 0 on failure would make it useless in a pipeline.
    (tmp_path / ".evidence.toml").write_text(
        'goal = "g"\n[[checks]]\nname = "t"\ncmd = ["python3", "-c", "import sys; sys.exit(1)"]\n'
    )
    r = subprocess.run(
        [sys.executable, "-m", "evidence_loop.loop", "--config",
         str(tmp_path / ".evidence.toml"), "--cwd", str(tmp_path), "--check-only"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]), timeout=60,
    )
    assert r.returncode == 1 and "FAIL" in r.stdout


def test_passing_evidence_exits_zero(tmp_path):
    # intent: the happy path must actually be reachable — a runner that can only fail is as
    # useless as one that can only pass.
    (tmp_path / ".evidence.toml").write_text(
        'goal = "g"\n[[checks]]\nname = "t"\ncmd = ["python3", "-c", "pass"]\n'
    )
    r = subprocess.run(
        [sys.executable, "-m", "evidence_loop.loop", "--config",
         str(tmp_path / ".evidence.toml"), "--cwd", str(tmp_path), "--check-only"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]), timeout=60,
    )
    assert r.returncode == 0 and "evidence passed" in r.stdout


# --- is a loop the right shape? (dry-run advisories) ----------------------------------

def _dry_run(tmp_path, toml: str) -> str:
    cfg = tmp_path / "e.toml"
    cfg.write_text(toml)
    r = subprocess.run(
        [sys.executable, "-m", "evidence_loop.loop", "--config", str(cfg), "--dry-run"],
        capture_output=True, text=True,
        cwd=str(Path(__file__).resolve().parents[1]))
    return r.stdout + r.stderr


def test_a_single_attempt_config_is_flagged_as_not_a_loop(tmp_path):
    # intent: max_attempts=1 runs once and stops — that is a command, not a loop. It cannot
    # react to its own feedback, so every cost of the loop machinery buys nothing.
    out = _dry_run(tmp_path, 'goal = "g"\n[limits]\nmax_attempts = 1\n'
                             '[agent]\ncmd = ["true"]\n'
                             '[[checks]]\nname = "t"\ncmd = ["pytest", "tests/"]\n')
    assert "not a loop" in out


def test_a_check_that_cannot_fail_is_flagged(tmp_path):
    # intent: THE criterion people talk themselves past. A check that always passes means
    # the loop stops on attempt 1 every time while looking like it verified something.
    out = _dry_run(tmp_path, 'goal = "g"\n[agent]\ncmd = ["true"]\n'
                             '[[checks]]\nname = "vacuous"\ncmd = ["true"]\n')
    assert "cannot fail" in out and "vacuous" in out


def test_a_healthy_config_produces_no_warnings(tmp_path):
    # intent: the mirror. Warning on a good config trains people to ignore the warnings,
    # which is worse than not having them.
    out = _dry_run(tmp_path, 'goal = "g"\n[limits]\nmax_attempts = 4\n'
                             '[agent]\ncmd = ["claude", "-p", "{feedback}"]\n'
                             '[[checks]]\nname = "tests"\ncmd = ["pytest", "tests/", "-q"]\n')
    assert "before you run this" not in out


def test_check_only_config_is_named_as_the_right_first_step(tmp_path):
    # intent: check-only is step 1 of the build order, not a mistake. The warning must say
    # so rather than reading as "your config is broken".
    out = _dry_run(tmp_path, 'goal = "g"\n'
                             '[[checks]]\nname = "t"\ncmd = ["pytest", "tests/"]\n')
    assert "FIRST step" in out
