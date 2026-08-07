"""site_config — one place that answers "where does this site keep things?".

WHY THIS EXISTS
The ops tools were written against one machine: absolute paths and a hardcoded list of
eight org repos baked into the source. That made them unusable by anyone else and leaked a
private repo inventory into a repo meant to be shared.

Precedence is environment > config.toml > defaults, in that order, because:
  - systemd units set environment variables, and a unit override must win;
  - a config file is the ergonomic default for a human;
  - and a fresh clone with NEITHER must still run rather than crash, so every value has a
    default that is either harmless or empty.

An EMPTY default is deliberate for the lists. A tool that ships with someone else's repo
names pre-filled will silently do the wrong thing on a new machine; a tool with an empty
list does nothing and says so, which is the failure you want.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("AGENT_INFRA_CONFIG", REPO_ROOT / "config.toml"))

_DEFAULTS: dict = {
    "paths": {"projects": "~/projects", "scratch": "/tmp", "mirrors": ""},
    "graph": {
        "externally_managed": [],
        "min_commits_30d": 5,
        "max_graph_age_hours": 20,
        "max_auto_force_shrink": 50,
    },
    "limits": {"min_free_gb": 25, "build_timeout_sec": 1800, "sweep_budget_sec": 21600},
    "adoption": {"watched_skills": [], "stale_weeks": 2},
}


def _load_file() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open("rb") as fh:
            return tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        # Loud, not silent. A malformed config that falls back to defaults would run with
        # someone's settings ignored and report success -- the worst of both.
        raise SystemExit(f"{CONFIG_PATH}: could not parse: {exc}")


_FILE = _load_file()


def get(section: str, key: str, env: str | None = None):
    """Resolve one setting: environment, then config.toml, then the built-in default."""
    if env and (raw := os.environ.get(env)) is not None:
        default = _DEFAULTS[section][key]
        if isinstance(default, list):
            return [x.strip() for x in raw.split(",") if x.strip()]
        if isinstance(default, int) and not isinstance(default, bool):
            try:
                return int(raw)
            except ValueError:
                raise SystemExit(f"{env}={raw!r} is not an integer")
        return raw
    if section in _FILE and key in _FILE[section]:
        return _FILE[section][key]
    return _DEFAULTS[section][key]


def path(section: str, key: str, env: str | None = None) -> Path | None:
    """A path setting, with ~ expanded. Returns None for an empty value so callers can
    distinguish "not configured" from "configured to the current directory"."""
    raw = str(get(section, key, env) or "").strip()
    return Path(raw).expanduser() if raw else None


def describe() -> str:
    src = str(CONFIG_PATH) if CONFIG_PATH.exists() else "(no config.toml — using defaults)"
    return f"config: {src}"
