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
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mcp.server import MCPServer

mcp = MCPServer(
    "verify-mcp",
    instructions=(
        "Deterministic, read-only verification for a code repository. Use these instead of "
        "shell commands when checking whether work is correct: they cannot modify files, so "
        "their results are trustworthy evidence of the repo's current state."
    ),
)

# Resolved once at import: the single directory tree every tool is confined to.
ROOT = Path(os.environ.get("VERIFY_MCP_ROOT", ".")).resolve()
TIMEOUT = int(os.environ.get("VERIFY_MCP_TIMEOUT", "300"))
MAX_OUTPUT = 20_000  # keep a runaway test log from blowing up the model's context


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


def _safe_path(path: str) -> Path:
    """Resolve `path` inside ROOT, or raise. This is the containment boundary.

    Resolve BEFORE comparing so that `../` traversal and symlinks that escape the root are
    both caught — a prefix check on the raw string would pass `root/../../etc`.
    """
    target = (ROOT / path).resolve() if path else ROOT
    if target != ROOT and ROOT not in target.parents:
        raise ValueError(f"path escapes the configured root: {path!r}")
    if not target.exists():
        raise ValueError(f"path does not exist: {path!r}")
    return target


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


@mcp.tool()
def run_tests(runner: str = "pytest", path: str = "") -> str:
    """Run the project's test suite and return its exit code and output.

    Args:
        runner: which test runner to use — pytest, vitest, jest, go, or cargo.
        path: subdirectory of the repo to run in; empty means the repo root.

    exit_code=0 means the suite passed. Read the output for failures; this tool never
    modifies files, so a green result is real evidence the code works.
    """
    if runner not in TEST_RUNNERS:
        return f"unknown runner {runner!r}; choose one of: {', '.join(sorted(TEST_RUNNERS))}"
    return _run(TEST_RUNNERS[runner], _safe_path(path))


@mcp.tool()
def run_linter(runner: str = "ruff", path: str = "") -> str:
    """Run a linter and return its findings.

    Args:
        runner: ruff, eslint, or clippy.
        path: subdirectory to lint; empty means the repo root.
    """
    if runner not in LINT_RUNNERS:
        return f"unknown runner {runner!r}; choose one of: {', '.join(sorted(LINT_RUNNERS))}"
    return _run(LINT_RUNNERS[runner], _safe_path(path))


@mcp.tool()
def run_typecheck(runner: str = "mypy", path: str = "") -> str:
    """Run a static type checker and return its findings.

    Args:
        runner: mypy, pyright, or tsc.
        path: subdirectory to check; empty means the repo root.
    """
    if runner not in TYPE_RUNNERS:
        return f"unknown runner {runner!r}; choose one of: {', '.join(sorted(TYPE_RUNNERS))}"
    return _run(TYPE_RUNNERS[runner], _safe_path(path))


@mcp.tool()
def git_status(path: str = "") -> str:
    """Show uncommitted changes as `git status --short` plus a diffstat.

    Read-only: this never stages, commits, or fetches. Use it to confirm what you actually
    changed before claiming work is done.
    """
    cwd = _safe_path(path)
    status = _run(Runner("git-status", ("git", "status", "--short")), cwd)
    stat = _run(Runner("git-diffstat", ("git", "diff", "--stat")), cwd)
    return f"--- git status --short ---\n{status}\n\n--- git diff --stat ---\n{stat}"


@mcp.tool()
def list_checks() -> str:
    """List which verification runners are actually installed in this environment.

    Call this first when you don't know the project's stack — it tells you which of the
    tools below will work here instead of guessing and getting 'unavailable'.
    """
    lines = [f"root: {ROOT}", f"timeout: {TIMEOUT}s", ""]
    for label, table in (("tests", TEST_RUNNERS), ("lint", LINT_RUNNERS), ("typecheck", TYPE_RUNNERS)):
        avail = [n for n, r in table.items() if shutil.which(r.args[0])]
        missing = [n for n in table if n not in avail]
        lines.append(f"{label}: available={avail or '(none)'} missing={missing or '(none)'}")
    return "\n".join(lines)


def main() -> None:
    """Entry point. Defaults to stdio (how Claude Code launches it); set
    VERIFY_MCP_TRANSPORT=streamable-http to run it as a network service instead — the
    stateless 2026-07-28 protocol makes that safe behind an ordinary load balancer."""
    transport = os.environ.get("VERIFY_MCP_TRANSPORT", "stdio")
    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
