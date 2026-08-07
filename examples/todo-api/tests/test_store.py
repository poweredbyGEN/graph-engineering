"""Tests for the todo store.

The first block passes today. The three below it FAIL on purpose — they are the evidence
contract an agent has to satisfy. Each describes the behaviour it wants precisely enough that
an agent can implement it without guessing, which is the difference between a check that
drives work and one that just complains.
"""

from __future__ import annotations

import pytest

from todo_api.store import NotFound, TodoStore


@pytest.fixture()
def store():
    return TodoStore()


# --- already passing -----------------------------------------------------------------

def test_add_and_get(store):
    t = store.add("write docs")
    assert store.get(t.id).title == "write docs"
    assert not t.done


def test_complete(store):
    t = store.add("ship it")
    assert store.complete(t.id).done


def test_list_by_tag(store):
    store.add("a", tags=["work"])
    store.add("b", tags=["home"])
    assert [t.title for t in store.list(tag="work")] == ["a"]
    assert len(store.list()) == 2


# --- GAP 1: input validation ---------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "\n\t"])
def test_rejects_empty_title(store, bad):
    # intent: an empty or whitespace-only title creates a todo nobody can identify. It must be
    # rejected at the boundary, not stored and discovered later.
    with pytest.raises(ValueError, match="title"):
        store.add(bad)


def test_rejects_overlong_title(store):
    # intent: unbounded input is a memory and display hazard. Cap it and say so.
    with pytest.raises(ValueError, match="title"):
        store.add("x" * 501)


def test_accepts_title_at_the_limit(store):
    # intent: the boundary must be inclusive — an off-by-one here rejects legitimate input,
    # which is the failure mode a naive `> 500` check introduces.
    assert store.add("x" * 500).title == "x" * 500


# --- GAP 2: a real error type --------------------------------------------------------

def test_get_missing_raises_notfound(store):
    # intent: a bare KeyError leaks the internal dict as the error surface and cannot be told
    # apart from a genuine bug. Callers need a domain error they can catch.
    with pytest.raises(NotFound):
        store.get(999)


def test_complete_missing_raises_notfound(store):
    # intent: every path that resolves an id must fail the same way, or callers end up
    # catching two different exception types for one condition.
    with pytest.raises(NotFound):
        store.complete(999)


# --- GAP 3: delete -------------------------------------------------------------------

def test_delete_removes_the_todo(store):
    # intent: the API is not usable without a delete.
    t = store.add("temporary")
    store.delete(t.id)
    with pytest.raises(NotFound):
        store.get(t.id)


def test_delete_missing_raises_notfound(store):
    # intent: deleting something that is not there is an error, not a silent no-op — a silent
    # no-op hides typos and double-deletes.
    with pytest.raises(NotFound):
        store.delete(999)


def test_delete_does_not_reuse_ids(store):
    # intent: reusing an id after delete makes a stale reference silently point at a DIFFERENT
    # todo. This is the subtle one an agent is most likely to get wrong.
    a = store.add("first")
    store.delete(a.id)
    b = store.add("second")
    assert b.id != a.id
