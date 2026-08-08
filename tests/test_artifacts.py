from __future__ import annotations

import sqlite3
import stat

import pytest

from graph_engineering.artifacts import ArtifactError, ArtifactStore
from graph_engineering.state import MIGRATIONS, StateStore


def test_artifacts_are_content_addressed_immutable_and_validated(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    schema = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
    }
    first = store.put({"value": 7}, schema)
    second = store.put({"value": 7}, schema)
    assert first.digest == second.digest
    assert first.path == second.path
    assert store.get(first.digest, schema).value == {"value": 7}

    with pytest.raises(ArtifactError, match="schema validation"):
        store.put({"value": "seven"}, schema)

    first.path.write_text('{"value":8}', encoding="utf-8")
    with pytest.raises(ArtifactError, match="digest mismatch"):
        store.get(first.digest, schema)


def test_state_database_uses_wal_and_versioned_migration(tmp_path):
    path = tmp_path / "state.db"
    StateStore(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == len(
            MIGRATIONS
        )


def test_artifact_publish_fsyncs_containing_directory(tmp_path, monkeypatch):
    directory_syncs = []
    real_fsync = __import__("os").fsync

    def record_fsync(descriptor):
        directory_syncs.append(stat.S_ISDIR(__import__("os").fstat(descriptor).st_mode))
        real_fsync(descriptor)

    monkeypatch.setattr("graph_engineering.artifacts.os.fsync", record_fsync)
    ArtifactStore(tmp_path / "artifacts").put({"value": 1}, {"type": "object"})
    assert any(directory_syncs)
