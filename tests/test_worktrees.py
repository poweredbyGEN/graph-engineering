from __future__ import annotations

import concurrent.futures
import subprocess
from pathlib import Path

import pytest

from graph_engineering.worktrees import WorktreeError, WorktreeManager


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Graph Test")
    git(root, "config", "user.email", "graph@example.com")
    (root / "src").mkdir()
    (root / "src" / "base.txt").write_text("base\n")
    git(root, "add", "--", "src/base.txt")
    git(root, "commit", "-qm", "base")
    return root


def test_create_uses_nested_path_and_exact_base(repo: Path):
    manager = WorktreeManager(repo)
    base = git(repo, "rev-parse", "HEAD")
    record = manager.create("run1", "writer", base=base)
    assert record.path == repo / ".claude/worktrees/graph-runs/run1/writer"
    assert git(record.path, "rev-parse", "HEAD") == base == record.base_sha


def test_duplicate_branch_or_path_fails_without_reuse(repo: Path):
    manager = WorktreeManager(repo)
    manager.create("run1", "writer", base="HEAD")
    with pytest.raises(WorktreeError, match="already exists"):
        manager.create("run1", "writer", base="HEAD")


def test_concurrent_creation_is_serialized_and_distinct(repo: Path):
    manager = WorktreeManager(repo)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        records = list(
            pool.map(
                lambda number: manager.create("parallel", f"node{number}", base="HEAD"),
                range(8),
            )
        )
    assert len({record.path for record in records}) == 8
    assert all(record.path.is_dir() for record in records)


def test_capture_includes_tracked_committed_and_untracked_files(repo: Path):
    manager = WorktreeManager(repo)
    record = manager.create("capture", "writer", base="HEAD")
    (record.path / "src/base.txt").write_text("changed\n")
    (record.path / "src/new.bin").write_bytes(b"\x00\xffnew")
    git(record.path, "add", "--", "src/base.txt")
    git(record.path, "commit", "-qm", "change tracked file")

    change = manager.capture(record, write_scope=["src/**"])
    assert change.changed_paths == ("src/base.txt", "src/new.bin")
    assert change.patch
    assert len(change.digest) == 64


def test_capture_rejects_changes_outside_scope(repo: Path):
    manager = WorktreeManager(repo)
    record = manager.create("scope", "writer", base="HEAD")
    (record.path / "README.md").write_text("outside\n")
    with pytest.raises(WorktreeError, match="escaped write scope"):
        manager.capture(record, write_scope=["src/**"])


def test_disjoint_change_sets_apply_to_one_integration_worktree(repo: Path):
    manager = WorktreeManager(repo)
    first = manager.create("merge", "first", base="HEAD")
    second = manager.create("merge", "second", base="HEAD")
    integration = manager.create("merge", "integration", base="HEAD")
    (first.path / "src/first.txt").write_text("first\n")
    (second.path / "src/second.txt").write_text("second\n")
    one = manager.capture(first, write_scope=["src/first.txt"])
    two = manager.capture(second, write_scope=["src/second.txt"])

    manager.apply(integration.path, one, write_scope=["src/first.txt"])
    manager.apply(integration.path, two, write_scope=["src/second.txt"])
    assert (integration.path / "src/first.txt").read_text() == "first\n"
    assert (integration.path / "src/second.txt").read_text() == "second\n"
    assert not (repo / "src/first.txt").exists()


def test_conflicting_tracked_patches_fail_closed(repo: Path):
    manager = WorktreeManager(repo)
    first = manager.create("conflict", "first", base="HEAD")
    second = manager.create("conflict", "second", base="HEAD")
    integration = manager.create("conflict", "integration", base="HEAD")
    (first.path / "src/base.txt").write_text("first\n")
    (second.path / "src/base.txt").write_text("second\n")
    one = manager.capture(first, write_scope=["src/base.txt"])
    two = manager.capture(second, write_scope=["src/base.txt"])
    manager.apply(integration.path, one, write_scope=["src/base.txt"])
    with pytest.raises(WorktreeError, match="command failed"):
        manager.apply(integration.path, two, write_scope=["src/base.txt"])
    assert (integration.path / "src/base.txt").read_text() == "first\n"


@pytest.mark.parametrize("bad", ["../escape", "/absolute", ".git/config"])
def test_apply_rejects_hostile_untracked_paths(repo: Path, bad: str):
    manager = WorktreeManager(repo)
    source = manager.create("unsafe", "source", base="HEAD")
    target = manager.create("unsafe", "target", base="HEAD")
    (source.path / "safe.txt").write_text("safe")
    original = manager.capture(source, write_scope=["safe.txt"])
    forged = type(original)(
        original.base_sha,
        original.patch_b64,
        {bad: "c2FmZQ=="},
        (bad,),
        original.digest,
    )
    with pytest.raises(WorktreeError, match="unsafe repository path"):
        manager.apply(target.path, forged, write_scope=["safe.txt"])


def test_apply_rejects_tampered_payload_even_with_stale_digest(repo: Path):
    manager = WorktreeManager(repo)
    source = manager.create("tamper", "source", base="HEAD")
    target = manager.create("tamper", "target", base="HEAD")
    (source.path / "src/new.txt").write_text("original")
    original = manager.capture(source, write_scope=["src/new.txt"])
    forged = type(original)(
        original.base_sha,
        original.patch_b64,
        {"src/new.txt": "dGFtcGVyZWQ="},
        original.changed_paths,
        original.digest,
    )
    with pytest.raises(WorktreeError, match="digest mismatch"):
        manager.apply(target.path, forged, write_scope=["src/new.txt"])


def test_apply_rejects_malformed_or_oversized_base64_before_git(repo: Path):
    manager = WorktreeManager(repo, max_patch_bytes=8, max_untracked_bytes=8)
    target = manager.create("payload", "target", base="HEAD")
    base = git(repo, "rev-parse", "HEAD")
    change_type = type(manager.capture(target, write_scope=[]))

    malformed = change_type(base, "not-base64!", {}, (), "0" * 64)
    with pytest.raises(WorktreeError, match="invalid tracked patch payload"):
        manager.apply(target.path, malformed, write_scope=[])

    oversized = change_type(base, "YQ==" * 20, {}, (), "0" * 64)
    with pytest.raises(WorktreeError, match="tracked patch exceeds byte limit"):
        manager.apply(target.path, oversized, write_scope=[])


def test_capture_and_apply_tracked_rename(repo: Path):
    manager = WorktreeManager(repo)
    source = manager.create("rename", "source", base="HEAD")
    target = manager.create("rename", "target", base="HEAD")
    git(source.path, "mv", "--", "src/base.txt", "src/renamed.txt")
    change = manager.capture(source, write_scope=["src/**"])

    manager.apply(target.path, change, write_scope=["src/**"])
    assert not (target.path / "src/base.txt").exists()
    assert (target.path / "src/renamed.txt").read_text() == "base\n"


def test_apply_rechecks_scope_at_the_integration_edge(repo: Path):
    manager = WorktreeManager(repo)
    source = manager.create("rescope", "source", base="HEAD")
    target = manager.create("rescope", "target", base="HEAD")
    (source.path / "src/new.txt").write_text("new")
    change = manager.capture(source, write_scope=["src/**"])
    with pytest.raises(WorktreeError, match="escaped write scope"):
        manager.apply(target.path, change, write_scope=["docs/**"])
