"""Run real Git transport cases through the existing CI and release pytest gate."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.parametrize("case", ["T1", "T2", "T3", "T4", "T5", "T6", "T8", "T9"])
def test_ci_fetch_pinned(case):
    # intent: a broken transport contract must fail the same pytest command used by both gates.
    subprocess.run(
        ["bash", str(Path(__file__).with_suffix(".sh")), case],
        check=True,
        timeout=185,
    )


@pytest.mark.parametrize("case", ["progress", "stall"])
def test_ci_fetch_pinned_low_speed_window(tmp_path, case):
    # intent: progressing fetches outlive 30 seconds; zero-byte windows retry to a typed refusal.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sha = "a" * 40
    attempts = tmp_path / "attempts"
    fake_git = bindir / "git"
    fake_git.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "command = sys.argv[3]\n"
        "if command == 'fetch':\n"
        "    assert os.environ['GIT_HTTP_LOW_SPEED_LIMIT'] == '1'\n"
        "    assert os.environ['GIT_HTTP_LOW_SPEED_TIME'] == '30'\n"
        "    with open(os.environ['ATTEMPTS'], 'a') as log:\n"
        "        log.write(str(time.monotonic()) + '\\n')\n"
        "    if os.environ['CASE'] == 'stall':\n"
        "        time.sleep(int(os.environ['GIT_HTTP_LOW_SPEED_TIME']))\n"
        "        print('fatal: Operation too slow', file=sys.stderr)\n"
        "        sys.exit(128)\n"
        "    for _ in range(31):\n"
        "        print('receiving objects', file=sys.stderr, flush=True)\n"
        "        time.sleep(1)\n"
        "elif command == 'rev-parse':\n"
        f"    print({sha!r})\n"
        "else:\n"
        "    assert command in ('init', 'checkout'), sys.argv\n"
    )
    fake_git.chmod(0o755)
    # Backoff is separate from transport timing; only the fake Git sleeps in these cases.
    (bindir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "sleep").chmod(0o755)
    started = time.monotonic()
    result = subprocess.run(
        [
            str(Path(__file__).resolve().parents[1] / "ci-fetch-pinned"),
            "gen-workflow",
            sha,
            str(tmp_path / "dest"),
        ],
        env=os.environ
        | {
            "PATH": f"{bindir}:{os.environ['PATH']}",
            "GEN_GITEA_READ_TOKEN": "fixture-token",
            "ATTEMPTS": str(attempts),
            "CASE": case,
        },
        text=True,
        capture_output=True,
        timeout=110,
        check=False,
    )
    elapsed = time.monotonic() - started
    calls = attempts.read_text().splitlines()
    assert elapsed > 30
    if case == "progress":
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert len(calls) == 1
        assert "attempt 1/3 ok" in result.stdout
    else:
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert len(calls) == 3
        assert elapsed >= 90
        assert (
            result.stderr.strip()
            == f"PINNED_FETCH_STALLED gen-workflow {sha} after 3 attempts"
        )
    print(f"{case} PASS attempts={len(calls)} elapsed={elapsed:.2f}s")
