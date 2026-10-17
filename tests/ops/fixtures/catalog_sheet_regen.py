#!/usr/bin/env python3
"""Fixture consumer regeneration command for the GEN-10539 retry cases.

contract-sync runs the consumer's own regeneration command, so these cases need
one that produces the real vendored ``catalog-sheet.tsv`` shape. This script
stands in for that consumer command, and it follows the mirror's semantics:

* the declared ``served_sha`` cell records the **consumer HEAD at the revision
  the mirror was cut from** (``scripts/sheet_mirror.py``), never the backend sha
  and never the future commit that carries the sheet;
* every other cell is re-derived from the watched backend input, so a backend
  change lands as a real cell change, not as a stamp change;
* a second generated artifact (the provenance sidecar) is written from the same
  backend-derived input and carries no stamp, so it is byte-stable between runs.

Parsing and rendering go through the pinned consumer functions in
``catalog_sheet_pinned.py`` -- never a second TSV schema written for the test.

``FIXTURE_STAMP_MODE=content`` turns the mirror into a content-only generator
(no stamp cell is written or refreshed), which is the "regeneration is
identical" corner case.

# intent: a fixture that hand-wrote the TSV, stamped the backend sha, or never
# re-derived a cell on update would let a non-converged consumer look converged
# and would let a real contract change masquerade as a stamp-only difference.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

# The consumer's own diff detection reads `git status`, so an interpreter cache
# file next to the pinned module would surface as a changed generated path and
# make every refusal host- and timing-dependent. Pin bytecode off before the
# import so the regenerated tree holds only the artifacts the generator writes.
sys.dont_write_bytecode = True

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import catalog_sheet_pinned as pinned  # noqa: E402

SHEET = pathlib.Path("src/gen_mcp_server/contracts/catalog-sheet.tsv")
SIDECAR = pathlib.Path("src/gen_mcp_server/contracts/catalog-sheet.source.json")
STAMP_MODE = os.environ.get("FIXTURE_STAMP_MODE", "consumer-head")


def backend_cell(backend: pathlib.Path) -> str:
    """One ordinary (non-stamp) cell derived from the watched backend input."""
    try:
        first = (backend / "config/model_capabilities.yml").read_text().splitlines()[0].strip()
    except (OSError, IndexError):
        first = ""
    return f"mode: {first} | manual"


def consumer_stamp() -> str | None:
    """Consumer HEAD at mirror-cut time; ``None`` for a content-only generator."""
    if STAMP_MODE == "content":
        return None
    return subprocess.check_output(["git", "-C", ".", "rev-parse", "--short", "HEAD"], text=True).strip()


def baseline(stamp: str | None, backend: pathlib.Path) -> dict[str, list[dict[str, str]]]:
    """Sections in the pinned COLUMNS shape, for a sheet that is not committed yet."""
    sha = stamp or ""
    # The evidence cell is an ordinary cell whose text may repeat the old stamp;
    # only declared served_sha cells are volatile.
    evidence = f"GEN-9383; mirror cut at {stamp}" if stamp else "GEN-9383"
    return {
        "Tool catalog": [
            {
                "tool_name": "gen_agent_action", "kind": "action", "replaces_legacy_count": "25",
                "readOnlyHint": "false", "destructiveHint": "true", "idempotentHint": "false",
                "openWorldHint": "true", "served_sha": sha,
            },
            {
                "tool_name": "gen_analyze", "kind": "action", "replaces_legacy_count": "26",
                "readOnlyHint": "false", "destructiveHint": "true", "idempotentHint": "false",
                "openWorldHint": "true", "served_sha": sha,
            },
        ],
        "Branches": [
            {
                "tool_name": "gen_analyze", "branch_kind": "op", "branch_key": "search",
                "required_fields": "query", "enum_fields": backend_cell(backend),
                "served_idempotency_key": "optional", "served_confirm_token": "—",
                "source_fn": "search", "lifecycle": "", "scheduled_task_ok": "YES",
                "served_sha": sha,
            },
        ],
        "Discover views": [
            {
                "domain": "vidsheet", "view": "search", "required_args": "agent_id + query",
                "maps_legacy_tools": "gen_analyze", "scheduled_task_ok": "YES",
                "served_sha": sha,
            },
        ],
        "Migration": [
            {
                "legacy_tool": "gen_analyze", "fate": "KEEP", "new_tool": "gen_analyze",
                "new_branch_key": "search", "served_now": "yes", "served_sha": sha,
            },
        ],
        "Provider routing": [
            {
                "capability": "video", "mcp_capability": "generate_video", "platform": "tiktok",
                "provider": "kling", "fallback_provider": "", "fallback_trigger": "",
                "unit_cost": "1", "limits": "", "evidence": evidence, "served_sha": sha,
            },
        ],
    }


def update(sections: dict[str, list[dict[str, str]]], stamp: str | None,
           backend: pathlib.Path) -> dict[str, list[dict[str, str]]]:
    """Re-mirror an existing sheet: refresh stamps and re-derive real cells."""
    derived = backend_cell(backend)
    for rows in sections.values():
        for row in rows:
            if stamp is not None and "served_sha" in row:
                row["served_sha"] = stamp
    for row in sections.get("Branches", []):
        if row.get("branch_key") == "search":
            row["enum_fields"] = derived
    return sections


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
    stamp = consumer_stamp()

    if SHEET.exists():
        sections = update(pinned.parse(SHEET.read_text()), stamp, backend)
    else:
        sections = baseline(stamp, backend)

    text = pinned.render(sections)
    mode = os.environ.get("FIXTURE_CATALOG_MODE", "")
    if mode:
        text = malformed(text, mode)
    SHEET.parent.mkdir(parents=True, exist_ok=True)
    SHEET.write_text(text)

    SIDECAR.write_text(json.dumps(
        {"enum_fields": backend_cell(backend), "origin": "gen-backend-v2"}, indent=2
    ) + "\n")


if __name__ == "__main__":
    main()
