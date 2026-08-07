"""evidence-loop — run an agent until deterministic evidence says it is done.

WHY THIS EXISTS
The default agent loop stops when the model says it is finished. That is a self-report, and
self-reports are the single most common source of "it looked done but wasn't". This runner
moves the stop condition OUT of the model: evidence is gathered by a subprocess the agent
cannot influence, and the loop continues until that subprocess is happy or the budget runs out.

Language-agnostic on purpose. Every command is an argv list from `.evidence.toml`, so the same
runner drives a Python, TypeScript, Go, or Rust repo without changes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path

MAX_FEEDBACK = 8_000  # per check, so one noisy suite cannot crowd out the others


@dataclass
class CheckResult:
    name: str
    cmd: list[str]
    exit_code: int
    duration_sec: float
    output: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@dataclass
class Attempt:
    n: int
    checks: list[CheckResult] = field(default_factory=list)
    agent_ran: bool = False
    agent_exit: int | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def digest(self) -> str:
        """Fingerprint of THIS attempt's failures, used to detect a stuck loop.

        Keyed on check name + exit code + output so that "same error twice" is detectable.
        Two identical consecutive digests mean the agent is not converging.
        """
        blob = "".join(f"{c.name}:{c.exit_code}:{c.output}" for c in self.failures())
        return sha256(blob.encode()).hexdigest()[:16]


def _truncate(text: str, limit: int = MAX_FEEDBACK) -> str:
    """Keep both ends. Tail-only truncation drops the summary line where pass/fail counts live;
    head-only drops the exit status. The middle is the least informative part of a long log."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n…[{len(text) - limit} chars truncated]…\n\n{text[-half:]}"


def run_check(name: str, cmd: list[str], cwd: Path, timeout: int,
              extra_env: dict[str, str] | None = None) -> CheckResult:
    """Run one evidence command. A non-zero exit is a RESULT, not an exception — the whole
    point is to capture and feed back what failed.

    `extra_env` covers the common src/-layout case where a check needs PYTHONPATH (or a repo
    needs NODE_ENV, etc.) to run at all. Without it those checks fail on an import error that
    looks like a code problem but is really a config problem.
    """
    start = time.monotonic()
    # Stale bytecode caches produce PHANTOM FAILURES: after the agent edits a source file, a
    # cached .pyc can make the very next check re-report the error that was just fixed. Caught
    # live in e2e — attempt 2 failed on an already-corrected file. A verifier that reports
    # failures which no longer exist is worse than no verifier, so caching is disabled for
    # every check (harmless for non-Python commands, which ignore the variable).
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(extra_env or {})}
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env
        )  # shell=False: cmd is an argv list from config, never a composed string
        out = (proc.stdout or "") + (proc.stderr or "")
        return CheckResult(name, cmd, proc.returncode, time.monotonic() - start, _truncate(out))
    except subprocess.TimeoutExpired:
        return CheckResult(
            name, cmd, -1, time.monotonic() - start,
            f"TIMEOUT after {timeout}s", timed_out=True,
        )
    except FileNotFoundError:
        # A missing binary must be loud. Treated as a failure, never as "nothing to report",
        # because an absent checker silently turns the loop into a rubber stamp.
        return CheckResult(name, cmd, 127, time.monotonic() - start, f"command not found: {cmd[0]!r}")


def build_feedback(goal: str, attempt: Attempt, history: list[Attempt]) -> str:
    """Compose what the agent is told. Specific and actionable beats complete.

    Includes prior attempts so the agent does not retry an approach that already failed — the
    'state' element of the loop. Without it, agents cycle between two wrong fixes.
    """
    parts = [f"GOAL: {goal}", "", "The following checks are still failing:"]
    for c in attempt.failures():
        parts.append(f"\n--- {c.name} (exit {c.exit_code}, {' '.join(c.cmd)}) ---\n{c.output}")
    if len(history) > 1:
        prior = [f"  attempt {a.n}: {', '.join(c.name for c in a.failures()) or 'passed'}"
                 for a in history[:-1]]
        parts += ["", "Previous attempts (do not repeat an approach that already failed):", *prior]
    parts += ["", "Fix the cause, not the symptom. Do not modify the checks themselves."]
    return "\n".join(parts)


def load_config(path: Path) -> dict:
    with path.open("rb") as fh:
        cfg = tomllib.load(fh)
    if not cfg.get("checks"):
        raise SystemExit(f"{path}: no [[checks]] defined — a loop with no evidence is not a loop")
    for c in cfg["checks"]:
        if not isinstance(c.get("cmd"), list) or not c["cmd"]:
            raise SystemExit(f"{path}: check {c.get('name')!r} needs cmd = [\"argv\", \"list\"]")
    return cfg


def main() -> int:
    ap = argparse.ArgumentParser(description="Run an agent until evidence passes.")
    ap.add_argument("--config", default=".evidence.toml")
    ap.add_argument("--cwd", default=".")
    ap.add_argument("--check-only", action="store_true", help="gather evidence, never run the agent")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    ap.add_argument("--trace", help="write a JSON trace of every attempt to this path")
    a = ap.parse_args()

    cwd = Path(a.cwd).resolve()
    cfg = load_config(Path(a.config))
    goal = cfg.get("goal", "(no goal set)")
    checks = cfg["checks"]
    limits = cfg.get("limits", {})
    max_attempts = int(limits.get("max_attempts", 3))
    timeout = int(limits.get("timeout_sec", 900))
    agent_cmd = (cfg.get("agent") or {}).get("cmd")

    if a.dry_run:
        print(f"goal: {goal}\ncwd:  {cwd}\nmax_attempts: {max_attempts}  timeout: {timeout}s")
        for c in checks:
            print(f"  check {c['name']}: {' '.join(c['cmd'])}")
        print(f"  agent: {' '.join(agent_cmd) if agent_cmd else '(none — check-only)'}")

        # Is a loop even the right shape here? A loop pays only when the work can grade
        # itself and the finish line is a fact rather than a feeling. These are the two
        # criteria a CONFIG can actually be checked against -- "will you run it again" and
        # "does a human step in mid-run" are answerable only by the person reading this.
        warnings = []
        if max_attempts <= 1:
            warnings.append(
                "max_attempts = 1 — this runs once and stops, which is a command, not a loop. "
                "A loop needs room to react to its own feedback.")
        soft = [c["name"] for c in checks
                if any(t in " ".join(c["cmd"]).lower() for t in ("echo", "true", ":"))
                and len(c["cmd"]) <= 2]
        if soft:
            warnings.append(
                f"check(s) {', '.join(soft)} look like they cannot fail. A check that never "
                "fails is not evidence — the loop will stop on the first attempt every time.")
        if not agent_cmd:
            warnings.append(
                "no [agent] cmd — this is check-only. That is the right FIRST step (see the "
                "build order in README), but it will never fix anything on its own.")
        if warnings:
            print("\nbefore you run this:")
            for w in warnings:
                print(f"  ⚠ {w}")
        return 0

    history: list[Attempt] = []
    for n in range(1, max_attempts + 1):
        attempt = Attempt(n=n)
        for c in checks:
            r = run_check(c["name"], c["cmd"], cwd, timeout, c.get("env"))
            attempt.checks.append(r)
            icon = "PASS" if r.passed else "FAIL"
            print(f"[attempt {n}] {icon} {r.name} ({r.duration_sec:.1f}s)", flush=True)
        history.append(attempt)

        if attempt.passed:
            print(f"\n✅ evidence passed on attempt {n}/{max_attempts}")
            break

        if a.check_only or not agent_cmd:
            print("\n❌ evidence failed (check-only; no agent run)")
            break

        # No-progress guard: identical failures twice running means the agent is not
        # converging. Continuing spends budget to reproduce the same error.
        if len(history) >= 2 and history[-1].digest() == history[-2].digest():
            print(f"\n⛔ no progress — attempt {n} failed identically to {n - 1}; stopping early")
            break

        if n == max_attempts:
            print(f"\n❌ evidence still failing after {max_attempts} attempts — escalate")
            break

        feedback = build_feedback(goal, attempt, history)
        cmd = [arg.replace("{feedback}", feedback) for arg in agent_cmd]
        print(f"\n[attempt {n}] running agent…", flush=True)
        try:
            proc = subprocess.run(cmd, cwd=cwd, timeout=timeout)
            attempt.agent_ran, attempt.agent_exit = True, proc.returncode
        except subprocess.TimeoutExpired:
            attempt.agent_ran, attempt.agent_exit = True, -1
            print(f"[attempt {n}] agent TIMED OUT after {timeout}s")

    if a.trace:
        Path(a.trace).write_text(json.dumps(
            {"goal": goal, "cwd": str(cwd), "attempts": [asdict(x) for x in history]},
            indent=2, default=str) + "\n")
        print(f"trace: {a.trace}")

    return 0 if history and history[-1].passed else 1


if __name__ == "__main__":
    sys.exit(main())
