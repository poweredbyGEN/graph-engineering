"""An in-memory todo store — deliberately incomplete.

This is the worked example for agent-infra. Three capabilities are MISSING on purpose, each
with a failing test that describes what it should do. Point an agent at it and the evidence
loop decides when the work is actually finished.

Do not "fix" this file in the repo — its incompleteness is the fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class TodoError(Exception):
    """Base error for the store."""


class NotFound(TodoError):
    """Raised when a todo id does not exist."""


@dataclass
class Todo:
    id: int
    title: str
    done: bool = False
    tags: list[str] = field(default_factory=list)


class TodoStore:
    def __init__(self) -> None:
        self._items: dict[int, Todo] = {}
        self._next_id = 1

    def add(self, title: str, tags: list[str] | None = None) -> Todo:
        # GAP 1: no input validation. An empty or whitespace-only title is accepted, and a
        # title of unbounded length is accepted. See tests/test_store.py::test_rejects_*.
        todo = Todo(id=self._next_id, title=title, tags=list(tags or []))
        self._items[todo.id] = todo
        self._next_id += 1
        return todo

    def get(self, todo_id: int) -> Todo:
        # GAP 2: raises a bare KeyError, leaking the internal dict as the error surface.
        # Callers cannot distinguish "no such todo" from an internal bug.
        return self._items[todo_id]

    def complete(self, todo_id: int) -> Todo:
        todo = self.get(todo_id)
        todo.done = True
        return todo

    def list(self, tag: str | None = None) -> list[Todo]:
        if tag is None:
            return list(self._items.values())
        return [t for t in self._items.values() if tag in t.tags]

    # GAP 3: no delete. The API is not usable without one.
