"""Tests for the nightly Graphify sweep.

WHY THIS EXISTS
An audit on 2026-08-07 found 26 of 34 repos with commits in the last 30 days had NO graph
at all. The post-deploy reconciler only refreshes a repo when it DEPLOYS, and its repo list
was a hardcoded 8-entry dict -- so infra, tooling, and docs repos were permanently invisible
to it while agents followed "graph before grep" against nothing.

The sweep's risky parts are discovery (what gets enrolled) and the safety rails (disk,
budget, shrink-guard), because both fail SILENTLY: over-enrolling races two writers onto one
graph, and a missing disk check takes the box down mid-build.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SWEEP = Path("/usr/local/sbin/gen-graphify-nightly")

if os.environ.get("GRAPH_ENGINEERING_PORTABLE_TESTS") == "1" or not SWEEP.exists():
    pytest.skip("site-installed nightly sweep not available in portable suite", allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_loader(
        "gen_graphify_nightly",
        importlib.machinery.SourceFileLoader("gen_graphify_nightly", str(SWEEP)),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_graphify_nightly"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()
M.LOG = Path(tempfile.gettempdir()) / "graph-engineering-nightly-test.log"


# --- remote normalisation: the identity used for dedup --------------------------------

def test_two_checkouts_of_one_repo_normalise_to_the_same_slug():
    # intent: a repo cloned twice under different folder names is ONE GitHub repo. Keying
    # on directory instead of remote would build the same graph twice and let two builds
    # race on one graphify-out.
    a = M.normalize_remote("https://github.com/acme/widget-service.git")
    b = M.normalize_remote("git@github.com:acme/widget-service.git")
    assert a == b == "acme/widget-service"


def test_ssh_https_and_bare_forms_all_normalise():
    for url in ("https://github.com/acme/widget.git",
                "git@github.com:acme/widget.git",
                "ssh://git@github.com/acme/widget.git",
                "https://github.com/acme/widget"):
        assert M.normalize_remote(url) == "acme/widget"


def test_a_non_github_remote_is_ignored_rather_than_guessed():
    # intent: a local path or unknown host has no stable slug. Inventing one would let two
    # unrelated repos collide onto one identity.
    for url in ("/local/path", "", "https://gitlab.com/x/y.git", "   "):
        assert M.normalize_remote(url) is None


# --- ownership boundary: never fight the reconciler -----------------------------------

def test_externally_managed_repos_are_excluded_from_the_sweep(monkeypatch):
    # intent: THE correctness property for enrollment. If the sweep rebuilt a repo another
    # process owns, two writers could hit one graphify-out at once and corrupt a graph that
    # agents then trust.
    #
    # Asserts the INVARIANT against whatever this site has configured, not against specific
    # repo names. Pinning real org/repo strings made this suite pass only on the machine it
    # was written on -- and leaked an org's repo inventory into a shared repo.
    enrolled = {slug for slug, _, _ in M.discover()}
    assert not (enrolled & M.RECONCILER_REMOTES), (
        "a repo marked externally-managed was enrolled anyway")


def test_an_externally_managed_repo_is_never_enrolled(monkeypatch):
    # intent: the mirror, proved rather than assumed. Take a repo discovery ACTUALLY found,
    # mark it externally managed, and confirm it disappears. Without this, the test above
    # passes vacuously whenever the configured list is empty -- which is the default.
    found = M.discover()
    if not found:
        pytest.skip("no active repos discovered on this machine")
    victim = found[0][0]
    monkeypatch.setattr(M, "RECONCILER_REMOTES", {victim})
    assert victim not in {slug for slug, _, _ in M.discover()}


def test_discovery_dedupes_so_each_remote_appears_once():
    # intent: a duplicate entry means the same graph is built twice in one sweep, wasting
    # a slow capped build and racing on the output path.
    slugs = [slug for slug, _, _ in M.discover()]
    assert len(slugs) == len(set(slugs))


def test_discovery_returns_existing_real_checkouts_only():
    # intent: a worktree's .git is a FILE, not a directory. Graphing a worktree duplicates
    # its parent's graph against a different path.
    for _, path, _ in M.discover():
        assert (path / ".git").is_dir()


# --- safety rails ---------------------------------------------------------------------

def test_disk_headroom_floor_is_meaningful():
    # intent: the origin machine's disk was at 84% when this was written. A build that fills it
    # takes the box down with it, so the floor must be real, not decorative.
    assert M.MIN_FREE_GB >= 10


def test_sweep_budget_finishes_before_the_workday():
    # intent: the sweep starts at 22:00. A budget that could run past ~08:00 would put
    # CPU-heavy indexing against a working agent -- the exact thing the 10% slice exists
    # to prevent.
    assert 0 < M.SWEEP_BUDGET <= 10 * 3600


def test_per_repo_timeout_is_smaller_than_the_whole_sweep():
    # intent: if one repo could consume the entire budget, a single pathological repo
    # starves every other repo forever and the gap never closes.
    assert M.BUILD_TIMEOUT < M.SWEEP_BUDGET


def test_shrink_ceiling_matches_the_reconciler():
    # intent: both paths run `graphify update` and hit the same shrink-guard. Divergent
    # ceilings would mean a rebuild is safe on one path and refused on the other, which is
    # the kind of drift nobody notices until a graph silently stops updating.
    assert M.MAX_AUTO_FORCE_SHRINK == 50


def test_freshness_window_prevents_rebuilding_what_the_reconciler_just_built():
    # intent: without a freshness check the sweep would rebuild every enrolled repo every
    # night regardless of need, spending hours of capped CPU to reproduce existing graphs.
    assert 0 < M.MAX_GRAPH_AGE_HOURS <= 24


def test_inactive_repos_are_not_enrolled():
    # intent: a repo with no recent commits has a graph that is still TRUE. Rebuilding it
    # burns the budget that active repos need.
    assert M.MIN_COMMITS_30D >= 1
    for _, path, commits in M.discover():
        assert commits >= M.MIN_COMMITS_30D


# --- systemd wiring -------------------------------------------------------------------

def test_timer_is_capped_by_the_shared_graphify_slice():
    # intent: the CPU cap is the whole reason background indexing is tolerable. A unit that
    # forgets Slice= reintroduces the load-22 stall the slice was created to fix.
    unit = Path("/etc/systemd/system/gen-graphify-nightly.service")
    if not unit.exists():
        pytest.skip("service unit not installed")
    text = unit.read_text()
    assert "Slice=graphify-cap.slice" in text
    assert "CPUQuota=10%" in text


def test_timer_is_persistent_so_a_missed_night_still_runs():
    # intent: without Persistent=true, downtime becomes an unbounded gap in graph freshness
    # that nothing reports -- the exact silent-staleness failure this sweep exists to close.
    unit = Path("/etc/systemd/system/gen-graphify-nightly.timer")
    if not unit.exists():
        pytest.skip("timer unit not installed")
    assert "Persistent=true" in unit.read_text()


def test_units_are_valid_to_systemd():
    # intent: a unit with a typo silently never runs. systemd-analyze parses it the way
    # systemd will.
    rc = subprocess.run(
        ["systemd-analyze", "verify", "/etc/systemd/system/gen-graphify-nightly.timer"],
        capture_output=True, text=True)
    assert "Failed" not in (rc.stderr or ""), rc.stderr
