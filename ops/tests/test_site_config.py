"""Tests for site_config — the boundary between this repo and one machine's setup.

WHY THIS EXISTS
Every ops tool was written against one machine: absolute paths and a hardcoded list of one
org's repos. That made them unrunnable elsewhere and leaked a repo inventory into a shared
repo. These tests pin the properties that keep it portable.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import site_config


def test_a_fresh_clone_with_no_config_still_resolves_every_setting(
    monkeypatch, tmp_path
):
    # intent: THE portability property. A clone with no config.toml must run rather than
    # crash — otherwise the first thing a new user meets is a traceback.
    monkeypatch.setattr(site_config, "_FILE", {})
    for section, key in [
        ("paths", "projects"),
        ("paths", "scratch"),
        ("graph", "min_commits_30d"),
        ("limits", "min_free_gb"),
        ("adoption", "stale_weeks"),
    ]:
        assert site_config.get(section, key) is not None


def test_repo_lists_default_to_EMPTY_not_to_someone_elses_repos(monkeypatch):
    # intent: a tool shipping with another org's repo names pre-filled would silently do the
    # wrong thing on a new machine. Empty means "does nothing and says so", which is the
    # failure you want — and it is why these lists are not baked into the source.
    monkeypatch.setattr(site_config, "_FILE", {})
    assert site_config.get("graph", "externally_managed") == []
    assert site_config.get("adoption", "watched_skills") == []


def test_environment_beats_the_config_file(monkeypatch):
    # intent: systemd units set environment variables, and a unit override must win over a
    # file the operator may not even know is there.
    monkeypatch.setattr(site_config, "_FILE", {"paths": {"projects": "/from/file"}})
    monkeypatch.setenv("AGENT_INFRA_PROJECTS", "/from/env")
    assert site_config.get("paths", "projects", "AGENT_INFRA_PROJECTS") == "/from/env"


def test_config_file_beats_the_built_in_default(monkeypatch):
    monkeypatch.delenv("AGENT_INFRA_PROJECTS", raising=False)
    monkeypatch.setattr(site_config, "_FILE", {"paths": {"projects": "/from/file"}})
    assert site_config.get("paths", "projects", "AGENT_INFRA_PROJECTS") == "/from/file"


def test_a_comma_list_in_the_environment_parses_to_a_list(monkeypatch):
    # intent: environment variables are strings. Without this, a configured list arrives as
    # one string and `x in list` matches SUBSTRINGS — "acme/web" would match "acme/web-api".
    monkeypatch.setenv("AGENT_INFRA_EXTERNALLY_MANAGED", "a/b, c/d ,e/f")
    assert site_config.get(
        "graph", "externally_managed", "AGENT_INFRA_EXTERNALLY_MANAGED"
    ) == ["a/b", "c/d", "e/f"]


def test_an_empty_optional_path_is_None_not_the_current_directory(monkeypatch):
    # intent: `Path("")` is `.` — a mirrors setting left blank would silently scan the CWD
    # and report nonsense drift. None lets the caller say "not configured".
    monkeypatch.delenv("AGENT_INFRA_MIRRORS", raising=False)
    monkeypatch.setattr(site_config, "_FILE", {"paths": {"mirrors": ""}})
    assert site_config.path("paths", "mirrors", "AGENT_INFRA_MIRRORS") is None


def test_a_non_integer_environment_value_fails_loudly(monkeypatch):
    # intent: silently falling back to a default would run with the operator's setting
    # ignored and report success — the worst of both.
    import pytest

    monkeypatch.setenv("AGENT_INFRA_MIN_FREE", "not-a-number")
    with pytest.raises(SystemExit):
        site_config.get("limits", "min_free_gb", "AGENT_INFRA_MIN_FREE")


def test_the_example_config_documents_every_real_setting():
    # intent: a setting that exists in code but not in config.example.toml is undiscoverable
    # — the user would have to read the source to find it.
    example = (Path(__file__).resolve().parents[2] / "config.example.toml").read_text()
    for section, keys in site_config._DEFAULTS.items():
        assert f"[{section}]" in example, f"section [{section}] missing from example"
        for key in keys:
            assert key in example, f"{section}.{key} missing from config.example.toml"
