"""Tests for the watch consumer — the Herdr sink must decorate, never endanger.

The load-bearing property: a broken or absent herdr binary degrades the sink with one
warning and the watch keeps following the run. A status surface that can kill the
observation it decorates would be worse than no surface.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from graph_engineering.lifecycle import LifecycleStore, StaticRunContextProvider
from graph_engineering.state import StateStore
from graph_engineering.watch import HerdrSink, watch_run

WORKFLOW = {
    "id": "watch-test",
    "nodes": [{"id": "worker", "kind": "agent", "required": True}],
}


def _run(tmp_path: Path, run_id: str = "run") -> tuple[StateStore, LifecycleStore]:
    state = StateStore(tmp_path / "state.db")
    state.create_run(WORKFLOW, run_id, lifecycle=True)
    lifecycle = LifecycleStore(state.path)
    lifecycle.initialize_context(
        run_id, StaticRunContextProvider({"base_sha": "a" * 40})
    )
    return state, lifecycle


def _finish(state: StateStore, lifecycle: LifecycleStore, run_id: str = "run") -> None:
    lifecycle.append(run_id, "node:a", "node.running", node_id="a")
    lifecycle.append(run_id, "node:a2", "node.succeeded", node_id="a")
    lifecycle.append(run_id, "run:done", "run.succeeded")
    lease = state.acquire_lease(run_id, ttl_seconds=30)
    state.finish_run(run_id, "succeeded", lease)
    state.release_lease(lease)


class RecordingRunner:
    """Stands in for subprocess.run at the sink's real call boundary."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout="", stderr="boom"
        )


def test_watch_follows_run_to_terminal_and_reports_summary(tmp_path: Path):
    # intent: the whole point — a consumer that exits when the run does, with a summary
    # instead of a firehose.
    state, lifecycle = _run(tmp_path)
    _finish(state, lifecycle)
    lines: list[str] = []
    summary = watch_run(state.path, "run", wait_seconds=0, emit=lines.append)
    assert summary["terminal"] is True
    assert summary["status"] == "succeeded"
    assert (
        summary["events_seen"] == 5
    )  # context + started + running + succeeded + run.succeeded
    assert any("node.running" in line for line in lines)


def test_herdr_sink_receives_status_and_terminal_notification(tmp_path: Path):
    # intent: the pane title tracks live status and the terminal event raises exactly one
    # notification — the course's "stream long-running execution" made concrete.
    state, lifecycle = _run(tmp_path)
    _finish(state, lifecycle)
    runner = RecordingRunner()
    sink = HerdrSink(pane_id="w1:p1", runner=runner)
    watch_run(state.path, "run", wait_seconds=0, sink=sink, emit=lambda _s: None)
    renames = [c for c in runner.calls if c[1:3] == ["pane", "rename"]]
    notes = [c for c in runner.calls if c[1:3] == ["notification", "show"]]
    assert renames and renames[-1][3] == "w1:p1"
    assert "succeeded" in renames[-1][4]
    assert len(notes) == 1 and "run succeeded" in notes[0][3]


def test_broken_herdr_degrades_once_and_watch_still_terminates(tmp_path: Path, capsys):
    # intent: THE safety property. A failing sink must warn once and never stop the
    # watch — and must not retry per event, which would drown the stream it decorates.
    state, lifecycle = _run(tmp_path)
    _finish(state, lifecycle)
    runner = RecordingRunner(returncode=1)
    sink = HerdrSink(pane_id="w1:p1", runner=runner)
    summary = watch_run(
        state.path, "run", wait_seconds=0, sink=sink, emit=lambda _s: None
    )
    assert summary["terminal"] is True
    assert len(runner.calls) == 1  # first failure degrades; no further herdr calls
    assert capsys.readouterr().err.count("degraded") == 1


def test_missing_herdr_binary_degrades_instead_of_crashing(tmp_path: Path, capsys):
    # intent: herdr is a sink, not a dependency. A host without it still gets the watch.
    state, lifecycle = _run(tmp_path)
    _finish(state, lifecycle)
    sink = HerdrSink(pane_id="w1:p1", binary=str(tmp_path / "no-such-herdr"))
    summary = watch_run(
        state.path, "run", wait_seconds=0, sink=sink, emit=lambda _s: None
    )
    assert summary["terminal"] is True
    assert "degraded" in capsys.readouterr().err


def test_quiet_budget_stops_a_watch_on_a_silent_run_with_resumable_cursor(
    tmp_path: Path,
):
    # intent: a watch is itself a loop and needs a stop condition. The returned cursor
    # must resume observation exactly where it stopped — no replay, no gap.
    state, lifecycle = _run(tmp_path)  # run stays open: no terminal event
    summary = watch_run(
        state.path, "run", wait_seconds=0, max_quiet_batches=2, emit=lambda _s: None
    )
    assert summary["terminal"] is False
    assert summary["events_seen"] == 1  # run.started only
    _finish(state, lifecycle)
    resumed = watch_run(
        state.path,
        "run",
        cursor=summary["next_cursor"],
        wait_seconds=0,
        emit=lambda _s: None,
    )
    assert resumed["terminal"] is True
    assert resumed["events_seen"] == 4  # the remaining events, none replayed
