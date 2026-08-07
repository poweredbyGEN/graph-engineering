#!/usr/bin/env python3
"""check-docs-accurate — fail when the docs claim a test count the suites do not have.

WHY THIS EXISTS
On 2026-08-07 an audit of this repo's own markdown found **6 of 9 test-count claims wrong**
(67%): the root README said loops had 18 tests (23), traces 17 (53), and SETUP.md said 51
total (113). Every one of those numbers was TRUE when it was written. Tests were added and
the prose was never touched.

That is the exact failure this repo argues against -- prose degrades silently and is never
read at the moment it matters. A README that misreports its own test count is a stale graph
in a different costume: confidently specific, and wrong.

So the claim becomes checkable. `N tests` in any markdown file is now an assertion a
subprocess can refute, not a number someone has to remember to update.

WHAT COUNTS AS A CLAIM
Any `<number> test` / `<number> tests` in a tracked .md file, attributed to a suite by the
nearest suite name on the same line or in the file's own path. Claims that name no suite
(e.g. the worked example's "13/13 tests pass", which describes a fixture, not this repo's
suites) are reported as UNATTRIBUTED and do not fail the check -- flagging them would
punish prose that is talking about something else entirely.

Read-only. Reports; changes nothing.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# suite directory -> the names that refer to it in prose
SUITES = {
    "harness/servers/verify-mcp": ("verify-mcp", "harness"),
    "loops": ("loops", "evidence-loop", "evidence_loop"),
    "swarm": ("swarm",),
    "traces": ("traces", "trace-analyze", "trace_analyze"),
    "ops": ("ops", "site_config", "site-config"),
}

# `N tests`, and also a BARE `# N` trailing a pytest command -- SETUP.md writes
# `... pytest tests/ -q           # 17`, which says nothing about "tests" and so slipped
# past a naive pattern while being just as wrong.
CLAIM = re.compile(r"(\d+)\s+tests?\b", re.I)

# Phrases where a number next to "test" counts something OTHER than tests -- "6 of 9
# test-count claims" is a count of CLAIMS. Flagging those trains people to ignore the
# checker, which is worse than not having one.
NOT_A_COUNT = re.compile(r"\btest-count\b|\bof \d+ test\b|\btest count\b", re.I)
BARE_CLAIM = re.compile(r"pytest\b.*#\s*(\d+)\s*$")


def collected(suite: str) -> int | None:
    """How many tests the suite ACTUALLY has, via pytest --collect-only (no execution)."""
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--collect-only"],
            cwd=ROOT / suite, capture_output=True, text=True, timeout=300)
    except Exception:
        return None
    m = re.search(r"(\d+) tests? collected", p.stdout)
    return int(m.group(1)) if m else None


def attribute(line: str, path: Path, context: list[str]) -> str | None:
    """Which suite is this line talking about?

    Checked in order: the line itself, then the PRECEDING LINES, then the file's path.

    The context lookback matters -- SETUP.md writes `cd harness/servers/verify-mcp` on one
    line and `# 16 tests` two lines later inside the same code block. Judging that claim on
    its own line alone attributes it to nothing and silently lets it rot, which is how a
    checker under-reports and gets trusted anyway.
    """
    for hay in [line, *reversed(context)]:
        low = hay.lower()
        for suite, names in SUITES.items():
            if any(n in low for n in names):
                return suite
    parts = str(path).replace("\\", "/")
    for suite in SUITES:
        if parts.startswith(suite + "/"):
            return suite
    return None


def main() -> int:
    truth = {s: collected(s) for s in SUITES}
    total = sum(v for v in truth.values() if v)
    missing = [s for s, v in truth.items() if v is None]
    if missing:
        print(f"FAIL: could not collect tests for {', '.join(missing)} — "
              f"cannot verify any claim against a suite that will not run.", file=sys.stderr)
        return 1

    print("actual:", "  ".join(f"{s.split('/')[-1]}={v}" for s, v in truth.items()),
          f"  TOTAL={total}")

    wrong, unattributed = [], []
    files = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT,
                           capture_output=True, text=True).stdout.split()
    for rel in files:
        p = ROOT / rel
        lines = p.read_text(errors="ignore").splitlines()
        for i, line in enumerate(lines, 1):
            if NOT_A_COUNT.search(line) or "<!-- historical -->" in line:
                # Either the number counts something other than tests, or the prose is
                # deliberately QUOTING a past wrong value to explain an incident. Both are
                # correct writing; flagging them trains people to ignore the checker.
                continue
            claims = [int(m.group(1)) for m in CLAIM.finditer(line)]
            claims += [int(m.group(1)) for m in BARE_CLAIM.finditer(line)]
            for claimed in claims:
                # Look back a few lines: code blocks put `cd <suite>` above the count.
                context = lines[max(0, i - 5):i - 1]
                suite = attribute(line, Path(rel), context)
                # A claim matching the TOTAL is a total, whatever suite name happens to sit
                # above it. Without this, "51 tests" summarising a code block gets blamed on
                # whichever suite the block's last `cd` mentioned, and the report names the
                # wrong file to fix.
                if claimed == total:
                    continue
                if suite is None:
                    # A total-count claim (no suite named) is checked against the SUM -- the
                    # number most likely to rot, since any suite growing invalidates it.
                    if claimed == total:
                        continue
                    unattributed.append((rel, i, claimed, line.strip()[:70]))
                    continue
                if claimed != truth[suite]:
                    wrong.append((rel, i, claimed, truth[suite], suite, line.strip()[:60]))

    for rel, i, claimed, actual, suite, txt in wrong:
        print(f"  STALE {rel}:{i} claims {claimed} for {suite}, actual {actual}", file=sys.stderr)
    for rel, i, claimed, txt in unattributed:
        print(f"  unattributed (not failing): {rel}:{i} '{claimed} tests' — {txt}")

    if wrong:
        print(f"\nFAIL: {len(wrong)} stale test-count claim(s). Update the prose or the tests.",
              file=sys.stderr)
        return 1
    print("PASS: every attributed test-count claim matches the suites.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
