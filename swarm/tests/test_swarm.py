"""Tests for the swarm runner.

The load-bearing tests are the ones that stop a swarm from LAUNDERING a guess into a result:
fail-closed with no checks, evidence-over-exit-code, and the refutation majority rule. Speed is
not the point of a swarm; unverifiable output produced faster is worse than none.
"""

from __future__ import annotations

import subprocess
import time
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swarm_run.swarm import LaneResult, load_config, run_lane  # noqa: E402


def lane_result(**kw) -> LaneResult:
    base = dict(name="l", passed=True, exit_code=0, duration_sec=1.0)
    return LaneResult(**{**base, **kw})


@pytest.fixture()
def repo(tmp_path):
    """A real git repo — worktree isolation cannot be faked."""
    r = tmp_path / "repo"
    r.mkdir()
    for cmd in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                ["git", "config", "user.name", "t"]):
        subprocess.run(cmd, cwd=r, check=True, capture_output=True)
    (r / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "f.txt"], cwd=r, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, check=True, capture_output=True)
    return r


# --- fail closed --------------------------------------------------------------------

def test_lane_with_no_checks_fails_closed(repo, tmp_path):
    # intent: THE most important test. With no evidence configured nothing can be proven, so a
    # lane must FAIL, not pass. A swarm that reports success without checks manufactures
    # confidence at scale — worse than not running at all.
    cfg = {"agent": {"cmd": ["python3", "-c", "pass"]}, "isolate": False}
    r = run_lane({"name": "x", "task": "t"}, cfg, repo, tmp_path, keep=False)
    assert not r.passed
    assert "cannot verify" in r.output


def test_agent_exit_zero_does_not_pass_a_failing_check(repo, tmp_path):
    # intent: the agent's own exit code must never be the verdict. An agent that does nothing
    # and exits 0 must still fail when the evidence says the work is not done.
    cfg = {
        "agent": {"cmd": ["python3", "-c", "pass"]},          # succeeds, changes nothing
        "checks": [{"name": "c", "cmd": ["python3", "-c", "import sys; sys.exit(1)"]}],
        "isolate": False,
    }
    r = run_lane({"name": "x", "task": "t"}, cfg, repo, tmp_path, keep=False)
    assert not r.passed and r.exit_code == 1


def test_failing_agent_can_still_pass_if_evidence_passes(repo, tmp_path):
    # intent: the mirror. A noisy agent that exits non-zero but left the repo in a good state
    # is a PASS — evidence is the authority in both directions, not a second opinion.
    cfg = {
        "agent": {"cmd": ["python3", "-c", "import sys; sys.exit(3)"]},
        "checks": [{"name": "c", "cmd": ["python3", "-c", "pass"]}],
        "isolate": False,
    }
    assert run_lane({"name": "x", "task": "t"}, cfg, repo, tmp_path, keep=False).passed


def test_missing_check_binary_fails_the_lane(repo, tmp_path):
    # intent: an uninstalled checker must fail the lane, never be skipped. A silently absent
    # check turns the swarm into a rubber stamp.
    cfg = {
        "agent": {"cmd": ["python3", "-c", "pass"]},
        "checks": [{"name": "ghost", "cmd": ["definitely-not-a-real-binary-xyz"]}],
        "isolate": False,
    }
    r = run_lane({"name": "x", "task": "t"}, cfg, repo, tmp_path, keep=False)
    assert not r.passed and "not found" in r.output


# --- isolation ----------------------------------------------------------------------

def test_lane_runs_in_its_own_worktree(repo, tmp_path):
    # intent: two agents in one checkout clobber each other and make the evidence meaningless
    # (you cannot tell whose change broke what). Each lane must get its own tree and branch.
    cfg = {
        "agent": {"cmd": ["python3", "-c", "open('made.txt','w').write('x')"]},
        "checks": [{"name": "c", "cmd": ["python3", "-c", "open('made.txt')"]}],
        "isolate": True, "base": "HEAD",
    }
    r = run_lane({"name": "alpha", "task": "t"}, cfg, repo, tmp_path, keep=True)
    assert r.passed and r.branch == "swarm/alpha"
    assert (Path(r.worktree) / "made.txt").exists()
    assert not (repo / "made.txt").exists(), "lane leaked into the main checkout"


def test_concurrent_worktree_creation_does_not_race(repo, tmp_path):
    # intent: caught LIVE in e2e — `git worktree add` mutates shared state under
    # .git/worktrees/ and is NOT thread-safe. Four concurrent lanes produced
    # "fatal: failed to read .git/worktrees/beta/commondir" as one lane read another's
    # half-written metadata, and that lane failed for a reason unrelated to its work.
    #
    # Asserting on a plain concurrent run does NOT catch this: on a small repo the calls
    # finish too fast to interleave, so it passes with the lock removed (verified — 3/3).
    # Instead assert the invariant directly: creation must be serialized. We wrap _run to
    # record overlap, so removing the lock fails deterministically rather than flakily.
    import concurrent.futures as cf

    import swarm_run.swarm as sw

    active, overlapped, guard = 0, [], __import__("threading").Lock()
    real_run = sw._run

    def counting_run(cmd, cwd, timeout, env=None):
        nonlocal active
        is_add = cmd[:3] == ["git", "worktree", "add"]
        if is_add:
            with guard:
                active += 1
                if active > 1:
                    overlapped.append(True)
            time.sleep(0.02)  # widen the window a real `git worktree add` occupies
        try:
            return real_run(cmd, cwd, timeout, env)
        finally:
            if is_add:
                with guard:
                    active -= 1

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sw, "_run", counting_run)
    try:
        names = [f"lane{i}" for i in range(8)]
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            made = list(ex.map(lambda n: sw.make_worktree(repo, n, "HEAD", tmp_path), names))
    finally:
        monkey.undo()

    assert not overlapped, "two `git worktree add` calls ran concurrently — the lock is gone"
    assert all(m is not None for m in made), "a concurrent worktree creation failed"
    assert len({m[1] for m in made}) == len(names), "branches collided"


def test_two_lanes_do_not_share_a_worktree(repo, tmp_path):
    # intent: proves the isolation is per-lane, not merely "somewhere else". Both lanes write
    # the same filename; neither may see the other's file.
    cfg = {
        "agent": {"cmd": ["python3", "-c", "open('out.txt','w').write('{name}')"]},
        "checks": [{"name": "c", "cmd": ["python3", "-c", "pass"]}],
        "isolate": True, "base": "HEAD",
    }
    a = run_lane({"name": "one", "task": "t"}, cfg, repo, tmp_path, keep=True)
    b = run_lane({"name": "two", "task": "t"}, cfg, repo, tmp_path, keep=True)
    assert a.worktree != b.worktree
    assert (Path(a.worktree) / "out.txt").read_text() == "one"
    assert (Path(b.worktree) / "out.txt").read_text() == "two"


# --- adversarial verification -------------------------------------------------------

def test_unverified_pass_is_not_confirmed():
    # intent: `passed` and `confirmed` must stay distinct. A lane nobody challenged has not
    # earned the stronger word, and collapsing the two is how unverified work gets shipped.
    r = lane_result(verdicts=[])
    assert r.passed and not r.confirmed


def test_majority_refutation_kills_the_claim():
    # intent: the refuters are the quality mechanism. If a majority find a problem the lane
    # must not be confirmed no matter how green its own evidence was.
    r = lane_result(verdicts=[{"refuted": True}, {"refuted": True}, {"refuted": False}])
    assert r.passed and not r.confirmed


def test_minority_refutation_survives():
    # intent: one dissenting verifier must not veto. Refuters are prompted adversarially, so
    # some false positives are expected; requiring unanimity would confirm nothing.
    r = lane_result(verdicts=[{"refuted": True}, {"refuted": False}, {"refuted": False}])
    assert r.confirmed


def test_tie_survives_but_a_failing_lane_never_confirms():
    # intent: pin the tie rule explicitly so a refactor cannot silently flip it, and check that
    # refutation can never rescue a lane whose evidence already failed.
    assert lane_result(verdicts=[{"refuted": True}, {"refuted": False}]).confirmed
    assert not lane_result(passed=False, verdicts=[{"refuted": False}] * 3).confirmed


# --- config -------------------------------------------------------------------------

def test_duplicate_lane_names_are_rejected(tmp_path):
    # intent: duplicate names collide on branch AND worktree path — the one-agent-per-unit rule
    # that keeps a swarm from thrashing. Must fail at config load, before anything spawns.
    p = tmp_path / "s.toml"
    p.write_text('[agent]\ncmd=["true"]\n[[lanes]]\nname="a"\ntask="x"\n'
                 '[[lanes]]\nname="a"\ntask="y"\n')
    with pytest.raises(SystemExit, match="duplicate lane"):
        load_config(p)


@pytest.mark.parametrize("bad", ["a/b", "a b", "a..b", "a~1"])
def test_lane_names_must_be_valid_branch_names(tmp_path, bad):
    # intent: an invalid name fails deep inside `git worktree add` with an opaque error after
    # the swarm has already started. Reject it up front.
    p = tmp_path / "s.toml"
    p.write_text(f'[agent]\ncmd=["true"]\n[[lanes]]\nname="{bad}"\ntask="x"\n')
    with pytest.raises(SystemExit, match="branch"):
        load_config(p)


def test_config_without_lanes_or_agent_is_rejected(tmp_path):
    # intent: fail fast on an unusable config rather than reporting "0/0 lanes passed", which
    # reads as success.
    p = tmp_path / "s.toml"
    p.write_text('[agent]\ncmd=["true"]\n')
    with pytest.raises(SystemExit, match="no \\[\\[lanes\\]\\]"):
        load_config(p)
    p.write_text('[[lanes]]\nname="a"\ntask="x"\n')
    with pytest.raises(SystemExit, match="agent"):
        load_config(p)


# --- data contracts and the fake edge test --------------------------------------------

def test_a_consumes_with_no_producer_is_rejected_as_a_fake_edge(tmp_path):
    # intent: THE fake edge test, enforced at config time. A lane declaring it consumes
    # something nothing produces means either a lane is missing or the dependency was
    # assumed rather than real. Both are worth failing on -- a fake edge silently
    # serializes work that could have run in parallel.
    cfg = tmp_path / "s.toml"
    cfg.write_text(
        '[agent]\ncmd = ["true"]\n'
        '[[checks]]\nname = "t"\ncmd = ["true"]\n'
        '[[lanes]]\nname = "a"\ntask = "x"\nproduces = ["report.json"]\n'
        '[[lanes]]\nname = "b"\ntask = "y"\nconsumes = ["nonexistent.json"]\n')
    with pytest.raises(SystemExit, match="FAKE EDGE"):
        load_config(cfg)


def test_a_real_producer_consumer_pair_is_accepted(tmp_path):
    # intent: the mirror. Rejecting a REAL dependency would push people to drop the
    # declarations entirely, which is how the data goes missing in the first place.
    cfg = tmp_path / "s.toml"
    cfg.write_text(
        '[agent]\ncmd = ["true"]\n'
        '[[checks]]\nname = "t"\ncmd = ["true"]\n'
        '[[lanes]]\nname = "a"\ntask = "x"\nproduces = ["report.json"]\n'
        '[[lanes]]\nname = "b"\ntask = "y"\nconsumes = ["report.json"]\n')
    assert len(load_config(cfg)["lanes"]) == 2


def test_lanes_without_declarations_still_load(tmp_path):
    # intent: contracts are optional. Making them mandatory would break every existing
    # config and teach people to write `produces = []` to shut the validator up.
    cfg = tmp_path / "s.toml"
    cfg.write_text(
        '[agent]\ncmd = ["true"]\n'
        '[[checks]]\nname = "t"\ncmd = ["true"]\n'
        '[[lanes]]\nname = "a"\ntask = "x"\n')
    assert len(load_config(cfg)["lanes"]) == 1


def test_a_non_list_contract_is_rejected(tmp_path):
    # intent: `produces = "report.json"` is a natural typo, and a bare string iterates as
    # CHARACTERS -- silently producing an artifact named "r", then "e", then "p".
    cfg = tmp_path / "s.toml"
    cfg.write_text(
        '[agent]\ncmd = ["true"]\n'
        '[[checks]]\nname = "t"\ncmd = ["true"]\n'
        '[[lanes]]\nname = "a"\ntask = "x"\nproduces = "report.json"\n')
    with pytest.raises(SystemExit, match="must be a list"):
        load_config(cfg)
