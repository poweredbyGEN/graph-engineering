"""verify-mcp — deterministic verification tools over MCP 2.0.

WHY THIS EXISTS
Agents are usually handed an unrestricted shell, so every verification step is an
arbitrary-code-execution decision the human has to approve. This server inverts that:
it exposes a SMALL, FIXED set of read-only checks (tests, lint, typecheck, git status)
that cannot mutate the repo, so they can be pre-approved once and run unattended.

The security boundary is that no tool takes a command — the caller chooses among
enumerated runners and may only point them at a path INSIDE the configured root. A
compromised or confused model cannot turn `run_tests` into `rm -rf`.

Configure the sandbox root with VERIFY_MCP_ROOT (defaults to the current directory).

SHARED MODE
One process per agent session adds up fast on a machine running dozens of agents. Set
VERIFY_MCP_ALLOWED_ROOTS (os.pathsep-separated parent directories) to run ONE instance
for every session: each call then picks its repo from the client's MCP roots, or from an
explicit absolute `repo` argument, and that repo must sit inside one of the allowed
parents. `path` stays confined to the chosen repo exactly as before. With no repo from
either source the call is refused — a shared server has no meaningful default directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import warnings
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.shared.exceptions import MCPError

mcp = MCPServer(
    "verify-mcp",
    instructions=(
        "Deterministic, read-only verification for a code repository. Use these instead of "
        "shell commands when checking whether work is correct: they cannot modify files, so "
        "their results are trustworthy evidence of the repo's current state. Pass `repo` "
        "(the absolute path of the repository you are working in) on every call."
    ),
)

# Resolved once at import: the single directory tree every tool is confined to.
ROOT = Path(os.environ.get("VERIFY_MCP_ROOT", ".")).resolve()
# Shared mode: parents a per-call repo may live under. Empty = classic single-root mode.
ALLOWED_PARENTS = tuple(
    Path(p).expanduser().resolve()
    for p in os.environ.get("VERIFY_MCP_ALLOWED_ROOTS", "").split(os.pathsep)
    if p.strip()
)
TIMEOUT = int(os.environ.get("VERIFY_MCP_TIMEOUT", "300"))
ROOTS_TIMEOUT = 5  # seconds to wait on a client's roots/list before falling back
MAX_OUTPUT = 20_000  # keep a runaway test log from blowing up the model's context

REPO_ARG_DOC = "absolute path of the repository to check; required on a shared server."


@dataclass(frozen=True)
class Runner:
    """An allow-listed command. `args` is fixed at import time and never model-supplied."""

    name: str
    args: tuple[str, ...]


TEST_RUNNERS = {
    "pytest": Runner("pytest", ("pytest", "-q")),
    "vitest": Runner("vitest", ("npx", "--no-install", "vitest", "run")),
    "jest": Runner("jest", ("npx", "--no-install", "jest")),
    "go": Runner("go", ("go", "test", "./...")),
    "cargo": Runner("cargo", ("cargo", "test")),
}
LINT_RUNNERS = {
    "ruff": Runner("ruff", ("ruff", "check")),
    "eslint": Runner("eslint", ("npx", "--no-install", "eslint", ".")),
    "clippy": Runner("clippy", ("cargo", "clippy")),
}
TYPE_RUNNERS = {
    "mypy": Runner("mypy", ("mypy",)),
    "pyright": Runner("pyright", ("npx", "--no-install", "pyright")),
    "tsc": Runner("tsc", ("npx", "--no-install", "tsc", "--noEmit")),
}


def _within(target: Path, root: Path) -> bool:
    return target == root or root in target.parents


def _safe_path(path: str, root: Path | None = None) -> Path:
    """Resolve `path` inside `root` (default ROOT), or raise. This is the containment boundary.

    Resolve BEFORE comparing so that `../` traversal and symlinks that escape the root are
    both caught — a prefix check on the raw string would pass `root/../../etc`.
    """
    root = ROOT if root is None else root
    target = (root / path).resolve() if path else root
    if not _within(target, root):
        raise ValueError(f"path escapes the configured root: {path!r}")
    if not target.exists():
        raise ValueError(f"path does not exist: {path!r}")
    return target


def _resolve_repo(repo: str, client_roots: list[Path]) -> Path:
    """Pick the repo a call runs against, or raise. The second containment boundary.

    Order: explicit `repo` argument, then the client's first MCP root, then (single-root
    mode only) ROOT. Whatever is chosen must resolve inside ALLOWED_PARENTS — or inside
    ROOT when no parents are configured — so `repo` can never widen the sandbox.
    """
    if repo:
        candidate = Path(repo).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"repo must be an absolute path: {repo!r}")
        candidate = candidate.resolve()
    elif client_roots and ALLOWED_PARENTS:
        candidate = client_roots[0]
    elif not ALLOWED_PARENTS:
        return ROOT
    else:
        raise ValueError(
            "no repo given: this is a shared verify-mcp server, pass repo=<absolute path of "
            "the repository you are working in>"
        )
    parents = ALLOWED_PARENTS or (ROOT,)
    if not any(_within(candidate, p) for p in parents):
        raise ValueError(f"repo is outside the allowed roots: {str(candidate)!r}")
    if not candidate.is_dir():
        raise ValueError(f"repo does not exist: {str(candidate)!r}")
    return candidate


def _file_uri_to_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path)).resolve()


async def _client_roots(ctx: Context | None) -> list[Path]:
    """The client's MCP roots as local paths; empty when unsupported, slow or broken.

    Only consulted in shared mode — a per-session stdio server already runs in its repo.
    Roots are deprecated in the 2026-07-28 protocol but current Claude Code / Codex still
    send them, so they remain the best zero-config signal of which repo a session is in.
    """
    if ctx is None or not ALLOWED_PARENTS:
        return []
    try:
        caps = ctx.client_capabilities
        if caps is None or caps.roots is None:
            return []
        with warnings.catch_warnings(), anyio.fail_after(ROOTS_TIMEOUT):
            warnings.simplefilter("ignore")
            result = await ctx.session.list_roots()
    # No back-channel, client error, timeout or dropped stream: fall back to `repo`.
    except (MCPError, TimeoutError, anyio.ClosedResourceError, anyio.BrokenResourceError):
        return []
    paths = (_file_uri_to_path(str(r.uri)) for r in result.roots)
    return [p for p in paths if p is not None]


async def _target(ctx: Context | None, repo: str, path: str) -> tuple[Path, Path]:
    """(repo root, confined working directory) for one call."""
    roots = [] if repo else await _client_roots(ctx)
    root = _resolve_repo(repo, roots)
    return root, _safe_path(path, root)


def _run(runner: Runner, cwd: Path) -> str:
    """Run an allow-listed command. Never raises on non-zero exit — a failing test suite is
    a RESULT, not an error, and the model needs to read the failure output."""
    if shutil.which(runner.args[0]) is None:
        return f"unavailable: {runner.args[0]!r} is not installed or not on PATH"
    try:
        proc = subprocess.run(
            runner.args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            # No shell: args is a fixed tuple, so there is nothing to interpolate or quote.
        )
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {TIMEOUT}s running {runner.name}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if len(out) > MAX_OUTPUT:
        # Keep BOTH ends: the head has the failure summary, the tail has the exit counts.
        half = MAX_OUTPUT // 2
        out = f"{out[:half]}\n\n…[{len(out) - MAX_OUTPUT} chars truncated]…\n\n{out[-half:]}"
    return f"exit_code={proc.returncode}\n\n{out.strip() or '(no output)'}"


async def _run_async(runner: Runner, cwd: Path) -> str:
    # Off the event loop: on a shared server one session's 5-minute test run must not
    # stall every other session's calls.
    return await anyio.to_thread.run_sync(_run, runner, cwd)


@mcp.tool()
async def run_tests(runner: str = "pytest", path: str = "", repo: str = "", ctx: Context | None = None) -> str:
    """Run the project's test suite and return its exit code and output.

    Args:
        runner: which test runner to use — pytest, vitest, jest, go, or cargo.
        path: subdirectory of the repo to run in; empty means the repo root.
        repo: absolute path of the repository to check; required on a shared server.

    exit_code=0 means the suite passed. Read the output for failures; this tool never
    modifies files, so a green result is real evidence the code works.
    """
    if runner not in TEST_RUNNERS:
        return f"unknown runner {runner!r}; choose one of: {', '.join(sorted(TEST_RUNNERS))}"
    _, cwd = await _target(ctx, repo, path)
    return await _run_async(TEST_RUNNERS[runner], cwd)


@mcp.tool()
async def run_linter(runner: str = "ruff", path: str = "", repo: str = "", ctx: Context | None = None) -> str:
    """Run a linter and return its findings.

    Args:
        runner: ruff, eslint, or clippy.
        path: subdirectory to lint; empty means the repo root.
        repo: absolute path of the repository to check; required on a shared server.
    """
    if runner not in LINT_RUNNERS:
        return f"unknown runner {runner!r}; choose one of: {', '.join(sorted(LINT_RUNNERS))}"
    _, cwd = await _target(ctx, repo, path)
    return await _run_async(LINT_RUNNERS[runner], cwd)


@mcp.tool()
async def run_typecheck(runner: str = "mypy", path: str = "", repo: str = "", ctx: Context | None = None) -> str:
    """Run a static type checker and return its findings.

    Args:
        runner: mypy, pyright, or tsc.
        path: subdirectory to check; empty means the repo root.
        repo: absolute path of the repository to check; required on a shared server.
    """
    if runner not in TYPE_RUNNERS:
        return f"unknown runner {runner!r}; choose one of: {', '.join(sorted(TYPE_RUNNERS))}"
    _, cwd = await _target(ctx, repo, path)
    return await _run_async(TYPE_RUNNERS[runner], cwd)


@mcp.tool()
async def git_status(path: str = "", repo: str = "", ctx: Context | None = None) -> str:
    """Show uncommitted changes as `git status --short` plus a diffstat.

    Read-only: this never stages, commits, or fetches. Use it to confirm what you actually
    changed before claiming work is done. `repo` is the absolute path of the repository;
    required on a shared server.
    """
    _, cwd = await _target(ctx, repo, path)
    status = await _run_async(Runner("git-status", ("git", "status", "--short")), cwd)
    stat = await _run_async(Runner("git-diffstat", ("git", "diff", "--stat")), cwd)
    return f"--- git status --short ---\n{status}\n\n--- git diff --stat ---\n{stat}"


@mcp.tool()
async def list_checks(repo: str = "", ctx: Context | None = None) -> str:
    """List which verification runners are actually installed in this environment.

    Call this first when you don't know the project's stack — it tells you which of the
    tools below will work here instead of guessing and getting 'unavailable'. `repo` is
    the absolute path of the repository; required on a shared server.
    """
    root, _ = await _target(ctx, repo, "")
    lines = [f"root: {root}", f"timeout: {TIMEOUT}s"]
    if ALLOWED_PARENTS:
        lines.append(f"shared: allowed roots {', '.join(map(str, ALLOWED_PARENTS))}")
    lines.append("")
    for label, table in (("tests", TEST_RUNNERS), ("lint", LINT_RUNNERS), ("typecheck", TYPE_RUNNERS)):
        avail = [n for n, r in table.items() if shutil.which(r.args[0])]
        missing = [n for n in table if n not in avail]
        lines.append(f"{label}: available={avail or '(none)'} missing={missing or '(none)'}")
    return "\n".join(lines)


def main() -> None:
    """Entry point. Defaults to stdio (how Claude Code launches it); set
    VERIFY_MCP_TRANSPORT=streamable-http to run it as a network service instead — the
    stateless 2026-07-28 protocol makes that safe behind an ordinary load balancer.
    VERIFY_MCP_HOST / VERIFY_MCP_PORT set the listen address (default 127.0.0.1:8000)."""
    transport = os.environ.get("VERIFY_MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    mcp.run(  # type: ignore[call-overload]
        transport=transport,
        host=os.environ.get("VERIFY_MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("VERIFY_MCP_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
