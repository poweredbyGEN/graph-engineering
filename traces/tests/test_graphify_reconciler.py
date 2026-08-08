"""Tests for the Graphify post-deploy reconciler's failure handling.

WHY THESE LIVE HERE
The reconciler is deployed at /usr/local/sbin/gen-graphify-postdeploy, outside any repo.
On 2026-08-07 it was found looping on an indexed repo: the same sha failing every ~7 minutes
since ~10:00, 5h31m of CPU and a 9.7G memory peak spent re-deriving one identical error.
Two defects combined:

  1. A shrink-guard in graphify refuses to overwrite a graph that has FEWER nodes. Node
     IDs embed line numbers, so moved code renames nodes rather than deleting them --
     an indexed repo's revert was 22 removed / 20 added = net -2, read as data loss.
  2. The retry had a 300s cooldown and NO failure counter, so a deterministic failure
     retried forever.

The fix is prose in a shell script unless something can fail on it, so the logic lives in
functions this file imports directly from the installed script.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

RECONCILER = Path("/usr/local/sbin/gen-graphify-postdeploy")

if os.environ.get("GRAPH_ENGINEERING_PORTABLE_TESTS") == "1" or not RECONCILER.exists():
    pytest.skip("site-installed reconciler not available in portable suite", allow_module_level=True)


def _load():
    """Import the reconciler as a module despite having no .py extension."""
    spec = importlib.util.spec_from_loader(
        "gen_graphify_postdeploy",
        importlib.machinery.SourceFileLoader("gen_graphify_postdeploy", str(RECONCILER)),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_graphify_postdeploy"] = mod
    spec.loader.exec_module(mod)
    return mod


import importlib.machinery  # noqa: E402

M = _load()

# The module logs to /var/log/gen-graphify.log at import. run_capture() logs failures, and
# a test asserting on failure handling must not write to a PRODUCTION ops log -- doing so
# put fake "command failed" lines into the real incident log on first run. Redirect to a
# temp path for the whole session.
_TEST_LOG = Path(tempfile.gettempdir()) / "graph-engineering-reconciler-test.log"
_TEST_LOG.parent.mkdir(parents=True, exist_ok=True)
M.LOG = _TEST_LOG

# The exact stderr that wedged an indexed repo, reproduced verbatim.
REAL_SHRINK_OUTPUT = (
    "[graphify] WARNING: new graph has 5072 nodes but existing\n"
    "graph.json has 5074. Refusing to overwrite — you may be missing chunk files "
    "from a previous session. Pass --force to override.\n"
    "Nothing to update or rebuild failed — check output above.\n"
)


# --- recognising the recoverable failure ----------------------------------------------

def test_the_real_indexed_repo_failure_is_recognised_as_a_shrink_refusal():
    # intent: this exact text looped every 7 minutes for hours. If the matcher stops
    # recognising it, the loop silently comes back.
    assert M._is_shrink_refusal(REAL_SHRINK_OUTPUT) is True
    assert M._shrink_amount(REAL_SHRINK_OUTPUT) == 2


def test_an_unrelated_failure_is_not_treated_as_a_shrink_refusal():
    # intent: --force must never be applied to a failure it cannot fix. Forcing past a
    # genuine build error would write a corrupt graph that agents then trust.
    for other in ("fatal: unable to read tree abc123",
                  "ModuleNotFoundError: no module named 'graphify'",
                  "Killed",
                  ""):
        assert M._is_shrink_refusal(other) is False


def test_export_py_wording_is_also_recognised():
    # intent: TWO code paths emit this guard (export.py and watch.py) with different
    # wording. Matching only one leaves the other looping exactly as before.
    export_style = (
        "[graphify] WARNING: new graph has 900 nodes but existing\n"
        "graph.json has 950 (net -50). Refusing to overwrite. Possible causes: "
        "missing chunk files from a previous session, or fuzzy dedup collapsed "
        "same-named symbols across files during an --update.\n"
    )
    assert M._is_shrink_refusal(export_style) is True
    assert M._shrink_amount(export_style) == 50


# --- the ceiling: force small shrinks, never large ones -------------------------------

def test_a_large_shrink_is_above_the_auto_force_ceiling():
    # intent: THE safety property. The guard exists to catch truncated graphs. A big drop
    # is the real thing it protects against -- auto-forcing that would overwrite a good
    # graph with a broken one and every agent doing graph-before-grep would trust it.
    huge = ("[graphify] WARNING: new graph has 12 nodes but existing\n"
            "graph.json has 5074. Refusing to overwrite.\n")
    assert M._shrink_amount(huge) == 5062
    assert M._shrink_amount(huge) > M.MAX_AUTO_FORCE_SHRINK


def test_the_observed_churn_is_below_the_ceiling():
    # intent: the fix must actually resolve the case that caused the incident, or it is
    # just a differently-shaped hang.
    assert M._shrink_amount(REAL_SHRINK_OUTPUT) <= M.MAX_AUTO_FORCE_SHRINK


def test_unparseable_counts_return_none_so_the_caller_refuses_to_force():
    # intent: if the numbers cannot be read we do NOT know how big the drop is, and an
    # unknown drop must be treated as dangerous. None must never be coerced to 0.
    partial = "Refusing to overwrite — new graph has some nodes but existing graph.json\n"
    assert M._shrink_amount(partial) is None


def test_a_graph_that_grew_is_not_reported_as_a_shrink():
    # intent: growth is normal. Reporting it as a shrink would spend a --force on a build
    # that never needed one, defeating the guard for no reason.
    grew = ("[graphify] WARNING: new graph has 5074 nodes but existing\n"
            "graph.json has 5072.\n")
    assert M._shrink_amount(grew) is None


# --- the poison guard: bounding a deterministic failure -------------------------------

def test_poison_threshold_is_small_enough_to_bound_the_incident():
    # intent: the incident ran ~47 retries over 5.5h. Any threshold that still allows
    # unbounded retries reproduces it; this pins the bound itself, not just the flag.
    assert 1 < M.MAX_SHA_FAILURES <= 5


def test_run_capture_returns_output_so_failures_can_be_told_apart():
    # intent: the original run() returned a bare bool, so every failure looked identical
    # and the only possible response was "retry". Distinguishing a recoverable refusal
    # from a real error REQUIRES the output -- losing it re-creates the original bug.
    ok, out = M.run_capture(["sh", "-c", "echo marker-text >&2; exit 3"], timeout=30)
    assert ok is False
    assert "marker-text" in out


def test_run_capture_reports_success_with_output():
    ok, out = M.run_capture(["sh", "-c", "echo fine"], timeout=30)
    assert ok is True and "fine" in out
