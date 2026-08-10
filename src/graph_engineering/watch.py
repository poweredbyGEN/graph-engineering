"""watch — consume the bounded lifecycle event stream into a live Herdr surface.

WHY THIS EXISTS
`events` made runtime state QUERYABLE; nothing consumed it, so a long run was still a
black box unless someone polled by hand. This is the consumer: it follows one run to a
terminal state, renders each event as a compact line, and — when a Herdr pane id is
given — keeps that pane's title showing the live run status and raises a desktop
notification on the terminal event.

Herdr is a SINK, never a dependency. If the `herdr` binary is missing or a call fails,
the watch degrades to stdout with a single warning and keeps streaming — a broken
status surface must not take down observation of the run it was surfacing.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .lifecycle import LifecycleStore

WATCH_VERSION = "1"

# One stream call blocks at most this long server-side; the loop re-arms until terminal.
DEFAULT_WAIT_SECONDS = 20.0
DEFAULT_LIMIT = 200

_TERMINAL_EVENTS = {"run.succeeded", "run.failed", "run.cancelled"}


class WatchError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class HerdrSink:
    """Pushes run status into a Herdr pane title and terminal notifications.

    The binary is injectable so tests exercise the real subprocess boundary with a stub
    on PATH instead of monkeypatching internals.
    """

    pane_id: str
    notify: bool = True
    binary: str = "herdr"
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
    _degraded: bool = field(default=False, init=False)

    def _call(self, *args: str) -> None:
        if self._degraded:
            return
        try:
            result = self.runner(
                [self.binary, *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._degrade(str(exc))
            return
        if result.returncode != 0:
            self._degrade(result.stderr.strip()[:200] or f"exit {result.returncode}")

    def _degrade(self, reason: str) -> None:
        # intent: warn ONCE, then stay silent. A dead herdr repeated per event would
        # drown the stream it was decorating.
        self._degraded = True
        print(
            f"watch: herdr sink degraded ({reason}); continuing on stdout",
            file=sys.stderr,
        )

    def status(self, run_id: str, status: str, node: str | None) -> None:
        label = f"ge:{run_id} {status}" + (f" @{node}" if node else "")
        self._call("pane", "rename", self.pane_id, label)

    def terminal(self, run_id: str, status: str) -> None:
        if not self.notify:
            return
        self._call(
            "notification",
            "show",
            f"graph-engineering run {status}",
            "--body",
            f"run {run_id} reached {status}",
            "--position",
            "top-right",
        )


def _summarise(event: dict[str, Any]) -> str:
    node = event.get("node_id") or "-"
    return f"[{event['sequence']:>4}] {event['event_type']:<24} {node}"


def watch_run(
    state_path: Path,
    run_id: str,
    *,
    cursor: str | None = None,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    limit: int = DEFAULT_LIMIT,
    max_quiet_batches: int = 0,
    sink: HerdrSink | None = None,
    emit: Callable[[str], None] = print,
    json_lines: bool = False,
) -> dict[str, Any]:
    """Follow one run to a terminal state. Returns a summary, never the raw firehose.

    ``max_quiet_batches`` bounds a watch on a run that has gone silent: 0 means wait
    forever (an interactive pane), N>0 means give up after N consecutive empty batches
    — the stop condition every loop needs, applied to the observer itself.
    """

    store = LifecycleStore(state_path)
    seen = 0
    quiet = 0
    last_status = "running"
    current_node: str | None = None
    while True:
        batch = store.stream(
            run_id, cursor=cursor, limit=limit, wait_seconds=wait_seconds
        )
        cursor = batch["next_cursor"]
        events = batch["events"]
        if events:
            quiet = 0
        for event in events:
            seen += 1
            etype = event["event_type"]
            if etype == "node.running":
                current_node = event.get("node_id")
            if etype in _TERMINAL_EVENTS:
                last_status = etype.removeprefix("run.")
            emit(json.dumps(event) if json_lines else _summarise(event))
        if sink is not None:
            sink.status(run_id, last_status, current_node)
        if batch["terminal"]:
            if sink is not None:
                sink.terminal(run_id, last_status)
            return {
                "version": WATCH_VERSION,
                "run_id": run_id,
                "events_seen": seen,
                "status": last_status,
                "next_cursor": cursor,
                "terminal": True,
            }
        if not events:
            quiet += 1
            if max_quiet_batches and quiet >= max_quiet_batches:
                # intent: a watch is itself a loop and needs a budget. Exiting with the
                # cursor lets the caller resume exactly where observation stopped.
                return {
                    "version": WATCH_VERSION,
                    "run_id": run_id,
                    "events_seen": seen,
                    "status": last_status,
                    "next_cursor": cursor,
                    "terminal": False,
                }
            if wait_seconds == 0:
                time.sleep(0.2)  # avoid a hot loop when the store never blocks
