from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from graph_engineering.cli import main
from graph_engineering.lifecycle import (
    LifecycleError,
    LifecycleStore,
    StaticRunContextProvider,
)
from graph_engineering.state import StateStore

WORKFLOW = {
    "id": "stream-test",
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


def test_stream_cursor_resumes_without_duplicates_and_redacts(tmp_path: Path):
    # intent: a consumer reconnects from a stable cursor without replaying delivered facts.
    _state, lifecycle = _run(tmp_path)
    first = lifecycle.stream("run", limit=1)
    assert [event["event_type"] for event in first["events"]] == ["run.started"]
    assert first["has_more"] is False
    lifecycle.append(
        "run",
        "check:redacted",
        "check.completed",
        payload={"authorization": "Bearer abcdefghijklmnop"},
    )
    second = lifecycle.stream("run", cursor=first["next_cursor"], limit=1)
    assert [event["sequence"] for event in second["events"]] == [2]
    assert second["events"][0]["payload"]["authorization"] == "[REDACTED]"
    empty = lifecycle.stream("run", cursor=second["next_cursor"])
    assert empty["events"] == []
    assert empty["next_cursor"] == second["next_cursor"]


def test_stream_long_poll_wakes_and_terminal_batch_stays_bounded(tmp_path: Path):
    # intent: Prime/Herdr can wait briefly without unbounded buffering or an infinite stream.
    state, lifecycle = _run(tmp_path)
    initial = lifecycle.stream("run")

    def append_later() -> None:
        time.sleep(0.05)
        lifecycle.append(
            "run", "join:ready", "join.decided", payload={"decision": "ready"}
        )

    thread = threading.Thread(target=append_later)
    thread.start()
    waited = lifecycle.stream(
        "run", cursor=initial["next_cursor"], wait_seconds=1, limit=1
    )
    thread.join()
    assert waited["timed_out"] is False
    assert waited["events"][0]["event_type"] == "join.decided"

    lease = state.acquire_lease("run", ttl_seconds=30)
    state.finish_run("run", "succeeded", lease)
    state.release_lease(lease)
    terminal = lifecycle.stream("run", cursor=waited["next_cursor"], limit=1)
    assert len(terminal["events"]) == 1
    assert terminal["events"][0]["event_type"] == "run.succeeded"
    assert terminal["terminal"] is True


def test_stream_rejects_cross_run_stale_and_unbounded_cursors(tmp_path: Path):
    # intent: a cursor cannot cross identity, skip tampered history, or defeat backpressure.
    _state, lifecycle = _run(tmp_path, "one")
    state = StateStore(lifecycle.path)
    state.create_run(WORKFLOW, "two", lifecycle=True)
    lifecycle.initialize_context(
        "two", StaticRunContextProvider({"base_sha": "b" * 40})
    )
    cursor = lifecycle.stream("one")["next_cursor"]
    with pytest.raises(LifecycleError, match="STREAM_CURSOR_INVALID"):
        lifecycle.stream("two", cursor=cursor)
    with pytest.raises(LifecycleError, match="STREAM_CURSOR_INVALID"):
        lifecycle.stream("one", cursor=cursor[:-1] + "A")
    with pytest.raises(LifecycleError, match="STREAM_LIMIT_INVALID"):
        lifecycle.stream("one", limit=257)
    with pytest.raises(LifecycleError, match="STREAM_WAIT_INVALID"):
        lifecycle.stream("one", wait_seconds=31)


def test_stream_timeout_returns_same_reconnect_cursor(tmp_path: Path):
    # intent: an idle bounded wait is a normal empty batch, not a lost position or error.
    _state, lifecycle = _run(tmp_path)
    first = lifecycle.stream("run")
    idle = lifecycle.stream("run", cursor=first["next_cursor"], wait_seconds=0.01)
    assert idle["timed_out"] is True
    assert idle["events"] == []
    assert idle["next_cursor"] == first["next_cursor"]


def test_event_cli_emits_generic_consumer_batch(tmp_path: Path, capsys):
    # intent: Prime and Herdr consume one neutral typed contract without gaining authority.
    state, _lifecycle = _run(tmp_path)
    assert (
        main(
            [
                "events",
                "--state",
                str(state.path),
                "--run-id",
                "run",
                "--limit",
                "1",
                "--json",
            ]
        )
        == 0
    )
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["command"] == "events"
    assert payload["events"][0]["event_type"] == "run.started"
    assert isinstance(payload["next_cursor"], str)
