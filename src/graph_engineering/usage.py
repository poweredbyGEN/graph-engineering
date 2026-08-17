"""usage — local invocation telemetry, so adoption is a number instead of a feeling.

WHY THIS EXISTS
"Are we actually using this, and is it paying for itself?" cannot be answered from
memory or from any one agent's transcripts: Claude, Codex, Grok, and Gemini all shell
out to the same `graph-engineer` binary, so the binary is the one choke point that
sees every invocation regardless of which tool made it. Each CLI run appends a single
JSON line here; `graph-engineer stats` turns the log into counts by command, by
repository, and by day.

Telemetry must never cost anything: recording is fail-silent (a full disk, a
read-only home, a corrupt log line — none of them may break the command the user
actually ran), stays on THIS machine (nothing is uploaded), and honours
GRAPH_ENGINEERING_NO_USAGE_LOG=1 as an opt-out.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USAGE_VERSION = "2"

_LOG_ENV = "GRAPH_ENGINEERING_USAGE_LOG"
_OPT_OUT_ENV = "GRAPH_ENGINEERING_NO_USAGE_LOG"
_CALLER_ENV = "GRAPH_ENGINEERING_CALLER"


def usage_log_path() -> Path:
    override = os.environ.get(_LOG_ENV)
    if override:
        return Path(override)
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "graph-engineering" / "usage.jsonl"


def _repo_label(start: Path) -> str:
    # The nearest enclosing git checkout names the project; plain directories fall
    # back to their own basename so stats stay meaningful outside a repo.
    current = start
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate.name
    return start.name


def record_invocation(
    command: str,
    exit_code: int,
    duration_ms: int,
    *,
    failure_class: str = "none",
) -> None:
    """Append one usage line. MUST never raise — telemetry cannot break the tool."""
    if os.environ.get(_OPT_OUT_ENV):
        return
    try:
        entry = {
            "version": USAGE_VERSION,
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "command": command,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "repo": _repo_label(Path.cwd()),
            "caller": os.environ.get(_CALLER_ENV) or None,
            "failure_class": failure_class,
        }
        path = usage_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 — intent: fail-silent by contract, see docstring
        return


def summarize(days: int | None = None) -> dict[str, Any]:
    """Aggregate the log into the numbers that quantify adoption.

    Corrupt lines are counted, not fatal: an append-only log shared by concurrent
    processes will eventually hold a torn line, and stats must survive it.
    """
    path = usage_log_path()
    totals: dict[str, int] = {}
    repos: dict[str, int] = {}
    day_counts: dict[str, int] = {}
    callers: dict[str, int] = {}
    failures = 0
    expected_rejections = 0
    operational_failures = 0
    corrupt = 0
    total = 0
    first: str | None = None
    last: str | None = None
    cutoff: str | None = None
    if days is not None:
        from datetime import timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                ts = str(entry["ts"])
                command = str(entry["command"])
            except (ValueError, KeyError, TypeError):
                corrupt += 1
                continue
            if cutoff is not None and ts < cutoff:
                continue
            total += 1
            first = ts if first is None or ts < first else first
            last = ts if last is None or ts > last else last
            totals[command] = totals.get(command, 0) + 1
            repo = str(entry.get("repo") or "unknown")
            repos[repo] = repos.get(repo, 0) + 1
            day_counts[ts[:10]] = day_counts.get(ts[:10], 0) + 1
            caller = entry.get("caller")
            if caller:
                callers[str(caller)] = callers.get(str(caller), 0) + 1
            if entry.get("exit_code") not in (0, None):
                failures += 1
            failure_class = entry.get("failure_class")
            if failure_class == "expected_rejection":
                expected_rejections += 1
            elif failure_class == "operational_failure":
                operational_failures += 1
    from .economics import summarize_outcomes

    return {
        "version": USAGE_VERSION,
        "log": str(path),
        "window_days": days,
        "total_invocations": total,
        "by_command": dict(sorted(totals.items(), key=lambda i: -i[1])),
        "by_repo": dict(sorted(repos.items(), key=lambda i: -i[1])),
        "by_day": dict(sorted(day_counts.items())),
        "by_caller": dict(sorted(callers.items(), key=lambda i: -i[1])),
        "failed_invocations": failures,
        "expected_rejections": expected_rejections,
        "operational_failures": operational_failures,
        "corrupt_lines": corrupt,
        "first": first,
        "last": last,
        "outcomes": summarize_outcomes(),
    }
