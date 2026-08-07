"""Tests for verify-mcp.

Every test names the failure it catches. The containment tests matter most: this server
exists to be pre-approved and run unattended, so a path-traversal regression would hand a
model arbitrary filesystem reach with the human's standing consent already granted.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "src"))


@pytest.fixture()
def srv(tmp_path, monkeypatch):
    """Import the server fresh with ROOT pinned to a temp dir.

    ROOT is resolved at import time, so the module must be reloaded per-test — reusing a
    cached import would silently test the previous test's root.
    """
    monkeypatch.setenv("VERIFY_MCP_ROOT", str(tmp_path))
    for mod in [m for m in list(sys.modules) if m.startswith("verify_mcp")]:
        del sys.modules[mod]
    import verify_mcp.server as s

    return s


# --- containment -------------------------------------------------------------------

@pytest.mark.parametrize("escape", ["..", "../..", "/etc", "../../../etc/passwd"])
def test_safe_path_refuses_escapes(srv, escape):
    # intent: the whole security model is "the model may only name paths inside ROOT".
    # If traversal ever resolves, a pre-approved run_tests becomes arbitrary filesystem
    # reach — the exact thing this server exists to prevent.
    with pytest.raises(ValueError, match="escapes"):
        srv._safe_path(escape)


def test_safe_path_refuses_symlink_out_of_root(srv, tmp_path):
    # intent: a prefix-string check would PASS a symlink pointing outside root. Resolution
    # must happen before comparison, so this catches a "simplification" of _safe_path into
    # a startswith() test.
    (tmp_path / "escape").symlink_to("/etc")
    with pytest.raises(ValueError, match="escapes"):
        srv._safe_path("escape")


def test_safe_path_allows_real_subdir(srv, tmp_path):
    # intent: containment must not be so strict it rejects legitimate work — a guard that
    # refuses everything would "pass" the tests above while making the server useless.
    (tmp_path / "pkg").mkdir()
    assert srv._safe_path("pkg") == (tmp_path / "pkg").resolve()
    assert srv._safe_path("") == tmp_path.resolve()


def test_safe_path_refuses_missing_path(srv):
    # intent: a nonexistent path must fail loudly, not silently fall back to ROOT and run
    # the whole suite when the caller asked for one subdirectory.
    with pytest.raises(ValueError, match="does not exist"):
        srv._safe_path("nope")


# --- runner allow-list -------------------------------------------------------------

@pytest.mark.parametrize(
    "fn,bad",
    [("run_tests", "rm"), ("run_linter", "curl"), ("run_typecheck", "bash")],
)
def test_unknown_runner_is_refused(srv, fn, bad):
    # intent: `runner` is the only model-controlled value that reaches command selection.
    # It must be a lookup key into a fixed table, never a command. If this ever returns
    # output instead of "unknown runner", the allow-list has been bypassed.
    out = getattr(srv, fn)(runner=bad)
    assert "unknown runner" in out
    assert bad not in out.split("choose one of")[0].replace(repr(bad), "")


def test_runner_args_contain_no_shell_metacharacters(srv):
    # intent: commands run with shell=False and fixed tuples. A future edit that folds a
    # path or flag into a single string ("pytest -q {path}") would reintroduce injection.
    for table in (srv.TEST_RUNNERS, srv.LINT_RUNNERS, srv.TYPE_RUNNERS):
        for runner in table.values():
            assert isinstance(runner.args, tuple)
            for arg in runner.args:
                assert not any(c in arg for c in ";|&$`><"), f"{runner.name}: {arg!r}"


# --- behaviour ---------------------------------------------------------------------

def test_failing_suite_returns_output_not_exception(srv, tmp_path):
    # intent: a failing test suite is a RESULT the model must read, not an error that
    # aborts the tool call. If this raises, agents lose the failure output they need.
    (tmp_path / "test_x.py").write_text("def test_fail():\n    assert False\n")
    out = srv.run_tests(runner="pytest")
    assert "exit_code=" in out
    assert "unavailable" in out or "exit_code=0" not in out


def test_missing_binary_reports_unavailable(srv):
    # intent: an uninstalled runner must say so plainly. Returning empty output would read
    # as "no problems found" — a false green, the worst failure mode for a verifier.
    srv.TEST_RUNNERS["ghost"] = srv.Runner("ghost", ("definitely-not-a-real-binary-xyz",))
    assert "unavailable" in srv.run_tests(runner="ghost")


def test_output_is_truncated_from_both_ends(srv, monkeypatch, tmp_path):
    # intent: a runaway log must not blow the model's context, but truncating only the tail
    # would drop pytest's summary line (where the pass/fail counts live).
    monkeypatch.setattr(srv, "MAX_OUTPUT", 200)
    big = "A" * 5000 + "SUMMARY_MARKER"
    monkeypatch.setattr(
        srv.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, big, ""),
    )
    out = srv.run_tests(runner="pytest")
    assert "truncated" in out
    assert "SUMMARY_MARKER" in out, "tail was dropped — pass/fail counts would be lost"


def test_list_checks_reports_root_and_availability(srv, tmp_path):
    # intent: list_checks is how an agent discovers the stack instead of guessing. It must
    # name the real root so a misconfigured VERIFY_MCP_ROOT is visible immediately.
    out = srv.list_checks()
    assert str(tmp_path.resolve()) in out
    assert "tests:" in out and "lint:" in out and "typecheck:" in out


def test_tools_are_registered_with_descriptions(srv):
    # intent: the description IS the model's routing signal. An undescribed tool is dead
    # weight in the listing — it costs context and never gets called.
    import asyncio

    tools = asyncio.run(srv.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"run_tests", "run_linter", "run_typecheck", "git_status", "list_checks"} <= names
    for t in tools:
        assert t.description and len(t.description) > 40, f"{t.name} has a thin description"
