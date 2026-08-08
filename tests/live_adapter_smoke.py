"""Opt-in real-worker smoke helper; pytest does not collect this module.

Invoke ``run_live_smoke`` from a private environment after loading a concrete
profile.  The helper contains no provider defaults and never reads credentials.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from graph_engineering.adapters import AdapterRequest, AdapterResult, execute_profile
from graph_engineering.config import Profile


def run_live_smoke(
    profile: Profile,
    *,
    worktree: Path,
    environ: Mapping[str, str],
) -> AdapterResult:
    """Ask a configured worker for one schema-validated, read-only response."""

    schema: Mapping[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"ok": {"const": True}, "profile": {"type": "string"}},
        "required": ["ok", "profile"],
    }
    return execute_profile(
        profile,
        AdapterRequest(
            prompt=(
                "Return only JSON matching the supplied schema. Set ok=true and "
                f"profile={profile.name!r}. Do not read or write project files."
            ),
            cwd=worktree,
            allowed_root=worktree,
            node_id="live-smoke",
            run_id="manual-live-smoke",
            result_schema=schema,
        ),
        environ=environ,
    )
