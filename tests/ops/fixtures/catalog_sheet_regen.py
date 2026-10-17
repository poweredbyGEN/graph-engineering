#!/usr/bin/env python3
"""Fixture consumer regeneration command for the GEN-10539 retry cases.

The contract-sync orchestration runs the consumer's own regeneration command, so
these cases need one that produces the real vendored ``catalog-sheet.tsv`` shape.
This script stands in for that consumer command: it stamps every row's declared
``served_sha`` cell with the input provenance it read and preserves every other
cell, exactly as the mirror's upsert does. Rendering and parsing go through the
pinned consumer functions in ``catalog_sheet_pinned.py`` -- never a second TSV
schema written for the test.

# intent: a fixture that hand-wrote the TSV, or re-derived cells, would let a
# product comparison pass on bytes the consumer would never produce.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import catalog_sheet_pinned as pinned  # noqa: E402

SHEET = pathlib.Path("src/gen_mcp_server/contracts/catalog-sheet.tsv")


def backend_cell(backend: pathlib.Path) -> str:
    """One ordinary (non-stamp) cell derived from the watched backend input."""
    try:
        first = (backend / "config/model_capabilities.yml").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        first = ""
    return f"mode: {first} | manual"


def baseline(stamp: str, backend: pathlib.Path) -> dict[str, list[dict[str, str]]]:
    """Sections in the pinned COLUMNS shape, for a sheet that is not committed yet."""
    return {
        "Tool catalog": [
            {
                "tool_name": "gen_agent_action", "kind": "action", "replaces_legacy_count": "25",
                "readOnlyHint": "false", "destructiveHint": "true", "idempotentHint": "false",
                "openWorldHint": "true", "served_sha": stamp,
            },
            {
                "tool_name": "gen_analyze", "kind": "action", "replaces_legacy_count": "26",
                "readOnlyHint": "false", "destructiveHint": "true", "idempotentHint": "false",
                "openWorldHint": "true", "served_sha": stamp,
            },
        ],
        "Branches": [
            {
                "tool_name": "gen_analyze", "branch_kind": "op", "branch_key": "search",
                "required_fields": "query", "enum_fields": backend_cell(backend),
                "served_idempotency_key": "optional", "served_confirm_token": "—",
                "source_fn": "search", "lifecycle": "", "scheduled_task_ok": "YES",
                "served_sha": stamp,
            },
        ],
        "Discover views": [
            {
                "domain": "vidsheet", "view": "search", "required_args": "agent_id + query",
                "maps_legacy_tools": "gen_analyze", "scheduled_task_ok": "YES",
                "served_sha": stamp,
            },
        ],
        "Migration": [
            {
                "legacy_tool": "gen_analyze", "fate": "KEEP", "new_tool": "gen_analyze",
                "new_branch_key": "search", "served_now": "yes", "served_sha": stamp,
            },
        ],
        "Provider routing": [
            {
                "capability": "video", "mcp_capability": "generate_video", "platform": "tiktok",
                "provider": "kling", "fallback_provider": "", "fallback_trigger": "",
                "unit_cost": "1", "limits": "", "evidence": "GEN-9383", "served_sha": stamp,
            },
        ],
    }


def malformed(text: str, mode: str) -> str:
    """Emit the artifact the consumer produced when its generator is broken."""
    lines = text.split("\n")
    if mode == "dup-header":
        # A duplicated declared column with no matching cells in the body.
        return "\n".join(
            line.replace("\tserved_sha", "\tserved_sha\tserved_sha") if line.startswith("tool_name\t") else line
            for line in lines
        )
    if mode == "missing-header":
        return "\n".join(
            line.replace("\tserved_sha", "") if line.startswith("tool_name\t") else line
            for line in lines
        )
    if mode == "truncated-row":
        out, in_body = [], False
        for line in lines:
            if line.startswith("# "):
                in_body = False
            elif in_body and line and not line.startswith("tool_name\t"):
                line = line.rsplit("\t", 1)[0]  # drop the served_sha cell from one data row
                in_body = False
            elif line.startswith("tool_name\t"):
                in_body = True
            out.append(line)
        return "\n".join(out)
    raise SystemExit(f"unknown FIXTURE_CATALOG_MODE {mode!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-path", required=True)
    parser.add_argument("--ref", required=True)
    args = parser.parse_args()

    backend = pathlib.Path(args.backend_path)
    stamp = subprocess.check_output(
        ["git", "-C", str(backend), "rev-parse", "--short", args.ref], text=True
    ).strip()

    if SHEET.exists():
        sections = pinned.parse(SHEET.read_text())
        for rows in sections.values():
            for row in rows:
                row["served_sha"] = stamp
    else:
        sections = baseline(stamp, backend)

    text = pinned.render(sections)
    mode = os.environ.get("FIXTURE_CATALOG_MODE", "")
    if mode:
        text = malformed(text, mode)
    SHEET.parent.mkdir(parents=True, exist_ok=True)
    SHEET.write_text(text)


if __name__ == "__main__":
    main()
