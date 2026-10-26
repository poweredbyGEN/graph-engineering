"""Tests for verify-mcp.

Every test names the failure it catches. The containment tests matter most: this server
exists to be pre-approved and run unattended, so a path-traversal regression would hand a
model arbitrary filesystem reach with the human's standing consent already granted.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp.shared.exceptions import NoBackChannelError

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
    out = asyncio.run(getattr(srv, fn)(runner=bad))
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
    out = asyncio.run(srv.run_tests(runner="pytest"))
    assert "exit_code=" in out
    assert "unavailable" in out or "exit_code=0" not in out


def test_missing_binary_reports_unavailable(srv):
    # intent: an uninstalled runner must say so plainly. Returning empty output would read
    # as "no problems found" — a false green, the worst failure mode for a verifier.
    srv.TEST_RUNNERS["ghost"] = srv.Runner("ghost", ("definitely-not-a-real-binary-xyz",))
    assert "unavailable" in asyncio.run(srv.run_tests(runner="ghost"))


def test_output_is_truncated_from_both_ends(srv, monkeypatch, tmp_path):
    # intent: a runaway log must not blow the model's context, but truncating only the tail
    # would drop pytest's summary line (where the pass/fail counts live).
    monkeypatch.setattr(srv, "MAX_OUTPUT", 200)
    big = "A" * 5000 + "SUMMARY_MARKER"
    monkeypatch.setattr(
        srv.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, big, ""),
    )
    out = asyncio.run(srv.run_tests(runner="pytest"))
    assert "truncated" in out
    assert "SUMMARY_MARKER" in out, "tail was dropped — pass/fail counts would be lost"


def test_list_checks_reports_root_and_availability(srv, tmp_path):
    # intent: list_checks is how an agent discovers the stack instead of guessing. It must
    # name the real root so a misconfigured VERIFY_MCP_ROOT is visible immediately.
    out = asyncio.run(srv.list_checks())
    assert str(tmp_path.resolve()) in out
    assert "tests:" in out and "lint:" in out and "typecheck:" in out


def test_tools_are_registered_with_descriptions(srv):
    # intent: the description IS the model's routing signal. An undescribed tool is dead
    # weight in the listing — it costs context and never gets called.
    tools = asyncio.run(srv.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"run_tests", "run_linter", "run_typecheck", "git_status", "list_checks"} <= names
    for t in tools:
        assert t.description and len(t.description) > 40, f"{t.name} has a thin description"


def test_context_parameter_is_hidden_from_the_tool_schema(srv):
    # intent: `ctx` is injected by the server. If it leaked into the input schema the model
    # would be asked to fill it in, and `repo` must be visible for shared mode to work.
    tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
    props = tools["run_tests"].input_schema["properties"]
    assert "ctx" not in props
    assert {"runner", "path", "repo"} <= set(props)


# --- single-root mode: `repo` cannot widen the sandbox ------------------------------

def test_repo_inside_root_is_accepted(srv, tmp_path):
    # intent: a per-session server may still be told the repo explicitly (agents are told
    # to always pass it) — a repo equal to or under ROOT must keep working.
    (tmp_path / "sub").mkdir()
    assert srv._resolve_repo(str(tmp_path), []) == tmp_path.resolve()
    assert srv._resolve_repo(str(tmp_path / "sub"), []) == (tmp_path / "sub").resolve()


def test_repo_outside_root_is_refused(srv):
    # intent: without shared mode configured, `repo` must not become a way out of ROOT.
    with pytest.raises(ValueError, match="outside the allowed roots"):
        srv._resolve_repo("/etc", [])


def test_client_roots_ignored_in_single_root_mode(srv, tmp_path):
    # intent: a stdio server already runs in its session's repo; a client root pointing
    # elsewhere must not silently retarget it.
    assert srv._resolve_repo("", [Path("/somewhere/else")]) == tmp_path.resolve()


# --- shared mode ---------------------------------------------------------------------

@pytest.fixture()
def shared(tmp_path, monkeypatch):
    """Server in shared mode: repos may live under tmp_path/allowed only."""
    allowed = tmp_path / "allowed"
    (allowed / "repo-a" / "pkg").mkdir(parents=True)
    (allowed / "repo-b").mkdir()
    (tmp_path / "outside").mkdir()
    monkeypatch.setenv("VERIFY_MCP_ROOT", str(tmp_path / "outside"))
    monkeypatch.setenv("VERIFY_MCP_ALLOWED_ROOTS", str(allowed))
    for mod in [m for m in list(sys.modules) if m.startswith("verify_mcp")]:
        del sys.modules[mod]
    import verify_mcp.server as s

    return s, allowed


def test_shared_mode_requires_a_repo(shared):
    # intent: a shared server has no meaningful cwd. Falling back to it would run one
    # agent's tests in some other directory and report a false result.
    srv, _ = shared
    with pytest.raises(ValueError, match="no repo given"):
        srv._resolve_repo("", [])


def test_shared_mode_explicit_repo_wins_over_client_roots(shared):
    # intent: an explicit argument is the caller's deliberate choice; roots are only a hint.
    srv, allowed = shared
    got = srv._resolve_repo(str(allowed / "repo-b"), [(allowed / "repo-a").resolve()])
    assert got == (allowed / "repo-b").resolve()


def test_shared_mode_uses_first_client_root(shared):
    # intent: clients that send MCP roots must work with zero configuration.
    srv, allowed = shared
    got = srv._resolve_repo("", [(allowed / "repo-a").resolve(), (allowed / "repo-b").resolve()])
    assert got == (allowed / "repo-a").resolve()


@pytest.mark.parametrize("bad", ["/etc", "{allowed}/../outside", "{allowed}/repo-a/../../outside"])
def test_shared_mode_refuses_repos_outside_allowed_parents(shared, bad):
    # intent: the allowed parents ARE the sandbox in shared mode; traversal in `repo` must
    # be resolved before the containment check, same as `path`.
    srv, allowed = shared
    with pytest.raises(ValueError, match="outside the allowed roots"):
        srv._resolve_repo(bad.format(allowed=allowed), [])


def test_shared_mode_refuses_client_root_outside_allowed_parents(shared, tmp_path):
    # intent: a client root is untrusted input too — it must pass the same containment.
    srv, _ = shared
    with pytest.raises(ValueError, match="outside the allowed roots"):
        srv._resolve_repo("", [(tmp_path / "outside").resolve()])


def test_shared_mode_refuses_symlinked_repo_escape(shared, tmp_path):
    # intent: a symlink inside an allowed parent that points out of it must not pass.
    srv, allowed = shared
    (allowed / "sneaky").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="outside the allowed roots"):
        srv._resolve_repo(str(allowed / "sneaky"), [])


def test_shared_mode_refuses_relative_repo(shared):
    # intent: a relative repo would resolve against the server's cwd, which in shared mode
    # is nobody's repo.
    srv, _ = shared
    with pytest.raises(ValueError, match="absolute"):
        srv._resolve_repo("repo-a", [])


def test_shared_mode_path_is_confined_to_the_chosen_repo(shared):
    # intent: picking repo-a must not let `path` reach a sibling repo under the same parent.
    srv, allowed = shared
    with pytest.raises(ValueError, match="escapes"):
        asyncio.run(srv._target(None, str(allowed / "repo-a"), "../repo-b"))
    root, cwd = asyncio.run(srv._target(None, str(allowed / "repo-a"), "pkg"))
    assert root == (allowed / "repo-a").resolve()
    assert cwd == (allowed / "repo-a" / "pkg").resolve()


def test_shared_mode_runs_in_the_requested_repo(shared, monkeypatch):
    # intent: the whole point of the change — the runner's cwd is the caller's repo.
    srv, allowed = shared
    seen = {}

    def fake_run(args, cwd, **_):
        seen["cwd"] = cwd
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(srv.subprocess, "run", fake_run)
    monkeypatch.setattr(srv.shutil, "which", lambda _: "/usr/bin/true")
    out = asyncio.run(srv.run_tests(runner="pytest", repo=str(allowed / "repo-b")))
    assert "exit_code=0" in out
    assert seen["cwd"] == (allowed / "repo-b").resolve()


def test_client_roots_parse_file_uris_and_skip_others(shared):
    # intent: roots arrive as URIs; percent-encoding must be decoded and non-file roots
    # ignored rather than turned into bogus paths.
    srv, allowed = shared

    class Root:
        def __init__(self, uri):
            self.uri = uri

    class Result:
        roots = (Root("https://example.com/x"), Root(f"file://{allowed}/repo%2Da"))

    class Session:
        async def list_roots(self):
            return Result()

    class Caps:
        roots = object()

    class Ctx:
        client_capabilities = Caps()
        session = Session()

    assert asyncio.run(srv._client_roots(Ctx())) == [(allowed / "repo-a").resolve()]


def test_client_roots_failure_falls_back_to_empty(shared):
    # intent: a client without a back-channel (stateless HTTP) raises on roots/list; that
    # must degrade to "pass repo explicitly", not crash the tool call.
    srv, _ = shared

    class Session:
        async def list_roots(self):
            raise NoBackChannelError("roots/list")

    class Caps:
        roots = object()

    class Ctx:
        client_capabilities = Caps()
        session = Session()

    assert asyncio.run(srv._client_roots(Ctx())) == []
