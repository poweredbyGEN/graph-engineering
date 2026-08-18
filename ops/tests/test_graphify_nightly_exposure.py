"""gen-graphify-nightly reconciler-exposure guard (2026-08-18).

intent: `graph.externally_managed` is a CLAIM that some other process maintains a
repo's graph. dayprotocol/day proved the claim can be false-in-practice: a fresh
57MB mirror graph existed, but the PROJECTS checkout — the only place agents look
("graph before grep" resolves <repo>/graphify-out/graph.json) — had nothing, so
every agent silently fell back to grep. Sibling repos had the inverse: a stale
REAL graphify-out at the checkout shadowing a fresh mirror. These tests pin:

  1. an owned slug WITH a mirror graph gets a checkout symlink created;
  2. a wrong symlink is repointed;
  3. a STALE real dir is moved aside (never deleted) and replaced by a symlink;
  4. a NEWER real dir is left alone;
  5. an owned slug WITHOUT a mirror graph is reported UNCOVERED, and discover()
     therefore does NOT skip it (the day failure mode);
  6. discover() still skips a slug the reconciler actually covers.

Each guard here fails if the exposure logic is removed (sabotage-capable): the
module-level import binds the real functions, and the fixtures are real dirs.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import time

import pytest

OPS = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = OPS / "graphify" / "gen-graphify-nightly"


def _load(monkeypatch, projects: pathlib.Path, mirrors: pathlib.Path):
    """Import the script fresh with PROJECTS/MIRRORS pointed at tmp fixtures."""
    monkeypatch.setenv("AGENT_INFRA_PROJECTS", str(projects))
    monkeypatch.setenv("AGENT_INFRA_MIRRORS", str(mirrors))
    monkeypatch.setenv("GEN_GRAPHIFY_NIGHTLY_LOG", str(projects / "nightly.log"))
    monkeypatch.setenv("GEN_GRAPHIFY_NIGHTLY_STATE", str(projects / "nightly.json"))
    spec = importlib.util.spec_from_loader(
        "gen_graphify_nightly_under_test", loader=None, origin=str(SCRIPT)
    )
    mod = importlib.util.module_from_spec(spec)
    code = SCRIPT.read_text(encoding="utf-8")
    mod.__file__ = str(SCRIPT)
    sys.modules[spec.name] = mod
    exec(compile(code, str(SCRIPT), "exec"), mod.__dict__)
    return mod


def _git_repo(path: pathlib.Path, remote: str) -> None:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", remote], check=True
    )


def _mirror_graph(mirrors: pathlib.Path, slug: str, mtime: float | None = None) -> pathlib.Path:
    out = mirrors / slug.replace("/", "__") / "graphify-out"
    out.mkdir(parents=True)
    g = out / "graph.json"
    g.write_text('{"nodes": [], "edges": []}', encoding="utf-8")
    if mtime is not None:
        os.utime(g, (mtime, mtime))
    return out


@pytest.fixture()
def env(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    mirrors = tmp_path / "mirrors"
    projects.mkdir()
    mirrors.mkdir()
    return projects, mirrors, monkeypatch


def test_missing_checkout_graph_gets_symlink(env):
    projects, mirrors, monkeypatch = env
    _git_repo(projects / "day", "https://github.com/dayprotocol/day.git")
    mirror_out = _mirror_graph(mirrors, "dayprotocol/day")
    mod = _load(monkeypatch, projects, mirrors)
    mod.RECONCILER_REMOTES = {"dayprotocol/day"}

    covered = mod.ensure_reconciler_exposure(mod.remote_map())

    assert covered == {"dayprotocol/day"}
    link = projects / "day" / "graphify-out"
    assert link.is_symlink()
    assert link.resolve() == mirror_out.resolve()
    # the agent-visible path now resolves the mirror graph
    assert (link / "graph.json").exists()


def test_wrong_symlink_is_repointed(env):
    projects, mirrors, monkeypatch = env
    _git_repo(projects / "day", "https://github.com/dayprotocol/day.git")
    mirror_out = _mirror_graph(mirrors, "dayprotocol/day")
    elsewhere = mirrors / "elsewhere"
    elsewhere.mkdir()
    (projects / "day" / "graphify-out").symlink_to(elsewhere)
    mod = _load(monkeypatch, projects, mirrors)
    mod.RECONCILER_REMOTES = {"dayprotocol/day"}

    mod.ensure_reconciler_exposure(mod.remote_map())

    assert (projects / "day" / "graphify-out").resolve() == mirror_out.resolve()


def test_stale_real_dir_is_moved_aside_never_deleted(env):
    projects, mirrors, monkeypatch = env
    _git_repo(projects / "day", "https://github.com/dayprotocol/day.git")
    now = time.time()
    _mirror_graph(mirrors, "dayprotocol/day", mtime=now)
    stale = projects / "day" / "graphify-out"
    stale.mkdir()
    (stale / "graph.json").write_text('{"nodes": ["old"]}', encoding="utf-8")
    os.utime(stale / "graph.json", (now - 8 * 86400, now - 8 * 86400))
    mod = _load(monkeypatch, projects, mirrors)
    mod.RECONCILER_REMOTES = {"dayprotocol/day"}

    mod.ensure_reconciler_exposure(mod.remote_map())

    link = projects / "day" / "graphify-out"
    assert link.is_symlink(), "stale real dir must be replaced by a symlink"
    aside = [p for p in (projects / "day").iterdir()
             if p.name.startswith("graphify-out.superseded-")]
    assert len(aside) == 1, "the stale graph must be moved aside, not deleted"
    assert (aside[0] / "graph.json").read_text(encoding="utf-8") == '{"nodes": ["old"]}'


def test_newer_local_graph_is_left_alone(env):
    projects, mirrors, monkeypatch = env
    _git_repo(projects / "day", "https://github.com/dayprotocol/day.git")
    now = time.time()
    _mirror_graph(mirrors, "dayprotocol/day", mtime=now - 8 * 86400)
    local = projects / "day" / "graphify-out"
    local.mkdir()
    (local / "graph.json").write_text('{"nodes": ["fresh-local"]}', encoding="utf-8")
    mod = _load(monkeypatch, projects, mirrors)
    mod.RECONCILER_REMOTES = {"dayprotocol/day"}

    mod.ensure_reconciler_exposure(mod.remote_map())

    assert not local.is_symlink(), "a NEWER locally built graph must never be clobbered"
    assert (local / "graph.json").read_text(encoding="utf-8") == '{"nodes": ["fresh-local"]}'


def test_owned_slug_without_mirror_graph_is_uncovered_and_discoverable(env):
    """The dayprotocol/day failure mode: owned on paper, no graph anywhere.
    The guard must report it uncovered so discover() builds it like any repo."""
    projects, mirrors, monkeypatch = env
    repo = projects / "day"
    _git_repo(repo, "https://github.com/dayprotocol/day.git")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "x"],
        check=True,
    )
    mod = _load(monkeypatch, projects, mirrors)
    mod.RECONCILER_REMOTES = {"dayprotocol/day"}
    mod.MIN_COMMITS_30D = 1

    remotes = mod.remote_map()
    covered = mod.ensure_reconciler_exposure(remotes)
    assert covered == set(), "no mirror graph -> NOT covered"

    repos = mod.discover(remotes, covered)
    assert [r[0] for r in repos] == ["dayprotocol/day"], (
        "an owned-on-paper slug with no mirror graph must fall through to discovery"
    )


def test_actually_covered_slug_is_skipped_by_discover(env):
    projects, mirrors, monkeypatch = env
    repo = projects / "day"
    _git_repo(repo, "https://github.com/dayprotocol/day.git")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "x"],
        check=True,
    )
    _mirror_graph(mirrors, "dayprotocol/day")
    mod = _load(monkeypatch, projects, mirrors)
    mod.RECONCILER_REMOTES = {"dayprotocol/day"}
    mod.MIN_COMMITS_30D = 1

    remotes = mod.remote_map()
    covered = mod.ensure_reconciler_exposure(remotes)
    assert covered == {"dayprotocol/day"}
    assert mod.discover(remotes, covered) == [], (
        "a slug the reconciler actually covers must not be rebuilt by the nightly"
    )


def test_stale_owned_mirror_is_rebuilt_nightly(env):
    """mav directive 2026-08-18: DAY must update graphify EVERY night. The
    reconciler only fires on deploy, so a covered mirror older than
    MAX_GRAPH_AGE_HOURS must be rebuilt by the nightly (freshness floor)."""
    projects, mirrors, monkeypatch = env
    now = time.time()
    mirror_repo = mirrors / "dayprotocol__day"
    _git_repo(mirror_repo, "https://github.com/dayprotocol/day.git")
    out = mirror_repo / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text('{"nodes": []}', encoding="utf-8")
    os.utime(out / "graph.json", (now - 3 * 86400, now - 3 * 86400))
    mod = _load(monkeypatch, projects, mirrors)
    mod.RECONCILER_REMOTES = {"dayprotocol/day"}

    calls = []
    monkeypatch.setattr(mod, "build", lambda path: (calls.append(path), (True, "ok"))[1])
    monkeypatch.setattr(mod, "sh", lambda *a, **k: (0, "origin/main\n"))
    # the real box may sit under MIN_FREE_GB; the disk floor has its own guard
    monkeypatch.setattr(mod, "free_gb", lambda _p: 999.0)

    rebuilt = mod.refresh_stale_owned_mirrors({"dayprotocol/day"}, time.monotonic())

    assert rebuilt == ["dayprotocol/day"]
    assert calls == [mirror_repo], "the MIRROR clone is the single build location"


def test_fresh_owned_mirror_is_not_rebuilt(env):
    projects, mirrors, monkeypatch = env
    mirror_repo = mirrors / "dayprotocol__day"
    _git_repo(mirror_repo, "https://github.com/dayprotocol/day.git")
    out = mirror_repo / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text('{"nodes": []}', encoding="utf-8")  # mtime = now
    mod = _load(monkeypatch, projects, mirrors)
    mod.RECONCILER_REMOTES = {"dayprotocol/day"}
    monkeypatch.setattr(mod, "build", lambda path: (_ for _ in ()).throw(
        AssertionError("a fresh mirror must not be rebuilt")))

    assert mod.refresh_stale_owned_mirrors({"dayprotocol/day"}, time.monotonic()) == []
