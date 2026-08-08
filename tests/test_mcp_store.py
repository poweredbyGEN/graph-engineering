from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from graph_engineering.mcp.store import (
    ClaimConflict,
    GraphTaskStore,
    StaleClaim,
    TaskStoreError,
)


class Clock:
    def __init__(self, now: int = 10_000):
        self.now = now

    def __call__(self) -> int:
        return self.now


def test_only_one_concurrent_claim_wins(tmp_path):
    # intent: two model workers racing for one node must never both believe they own it.
    store = GraphTaskStore(tmp_path / "tasks.db")
    task = store.create("workflow", "node", {"work": 1})

    def attempt(owner: str):
        try:
            return store.claim(task.task_id, owner, 30_000)
        except ClaimConflict:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(attempt, [f"worker-{index}" for index in range(8)]))
    assert sum(claim is not None for claim in claims) == 1


def test_expired_lease_increments_generation_and_fences_old_worker(tmp_path):
    # intent: a resumed worker cannot commit over the replacement that reclaimed its expired lease.
    clock = Clock()
    store = GraphTaskStore(tmp_path / "tasks.db", clock_ms=clock)
    task = store.create("workflow", "node", {})
    old = store.claim(task.task_id, "worker", 1_000)
    clock.now += 1_001
    new = store.claim(task.task_id, "worker", 1_000)
    assert new.generation == old.generation + 1
    with pytest.raises(StaleClaim):
        store.complete(task.task_id, old.owner, old.generation, {"wrong": True})
    completed = store.complete(task.task_id, new.owner, new.generation, {"right": True})
    assert completed.state == "completed"
    assert completed.result == {"right": True}


def test_heartbeat_and_terminal_writes_require_live_exact_claim(tmp_path):
    clock = Clock()
    store = GraphTaskStore(tmp_path / "tasks.db", clock_ms=clock)
    task = store.create("workflow", "node", {})
    claim = store.claim(task.task_id, "worker", 1_000)
    renewed = store.heartbeat(task.task_id, "worker", claim.generation, 2_000)
    assert renewed.lease_expires_ms == clock.now + 2_000
    with pytest.raises(StaleClaim):
        store.fail(task.task_id, "other", claim.generation, "no")
    failed = store.fail(
        task.task_id, "worker", claim.generation, "deterministic failure"
    )
    assert failed.state == "failed"


def test_bounds_payload_identifier_result_and_lease(tmp_path):
    store = GraphTaskStore(tmp_path / "tasks.db")
    with pytest.raises(TaskStoreError):
        store.create("bad workflow", "node", {})
    with pytest.raises(TaskStoreError):
        store.create("workflow", "node", {"blob": "x" * 70_000})
    task = store.create("workflow", "node", {})
    with pytest.raises(TaskStoreError):
        store.claim(task.task_id, "worker", 999)
    claim = store.claim(task.task_id, "worker", 1_000)
    with pytest.raises(TaskStoreError):
        store.complete(task.task_id, claim.owner, claim.generation, "x" * 300_000)


def test_cancel_is_fenced_and_protocol_cancel_cannot_reopen_terminal_task(tmp_path):
    store = GraphTaskStore(tmp_path / "tasks.db")
    task = store.create("workflow", "node", {})
    claim = store.claim(task.task_id, "worker", 30_000)
    with pytest.raises(StaleClaim):
        store.cancel(task.task_id, "intruder", claim.generation)
    cancelled = store.cancel(task.task_id, claim.owner, claim.generation)
    assert cancelled.state == "cancelled"
    assert store.request_protocol_cancel(task.task_id).state == "cancelled"
