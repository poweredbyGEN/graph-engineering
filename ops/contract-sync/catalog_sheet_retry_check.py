#!/usr/bin/env python3
"""Decide whether a re-generated catalog mirror differs only in its stamp cells.

contract-sync retries a consumer whose previous tick opened a PR that merged.
The consumer's mirror generator stamps ``served_sha`` with the consumer HEAD it
was cut from (``scripts/sheet_mirror.py``), so re-running it against the same
backend input reproduces every real cell and moves only that declared stamp.
Deciding that needs the consumer's own catalog-sheet schema, parse and render --
never a second TSV reader in agent-infra (GEN-10539).

The consumer module is imported normally, never re-compiled: in the consumer's
own runtime (the image ``regen_gen_mcp_server`` already uses, where the package
is installed) the canonical ``gen_mcp_server.catalog_sheet`` imports its
``provider_catalog``/``relay_probe``/``server``/``families`` dependencies. A
checkout whose catalogue lives in a standalone module (the pinned fixture
module, pure stdlib) is loaded by file location instead. Both are the
consumer's bytes; a module whose API cannot be imported refuses.

Accepted only when, for both the committed sheet and the regenerated one,
``render(parse(text))`` reproduces the file byte for byte -- which rejects a
duplicated, missing or reordered declared column and any short or long row --
and the two files then render identically with the declared ``served_sha`` cell
blanked. Every other difference, and any doubt about the import, refuses.

Bytecode writing is off before any consumer import: a ``__pycache__`` next to
the consumer's module would surface as a changed generated path and make this
comparison mutate the very tree it is judging.

# intent: a retry that treated the volatile stamp as contract data would either
# refuse a consumer that already shipped the input, or rewrite a generated cell
# by hand; a retry that compared loosely would let a real catalog change through
# as a stamp-only difference.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

STAMP_COLUMN = "served_sha"
PACKAGE_MODULE = "gen_mcp_server.catalog_sheet"
CANDIDATES = (
    "src/gen_mcp_server/catalog_sheet.py",
    "src/gen_mcp_server/catalog_sheet_pinned.py",
    "scripts/catalog_sheet.py",
    "scripts/catalog_sheet_pinned.py",
)
API_NAMES = ("COLUMNS", "KEYS", "parse", "render")


def has_api(module) -> bool:
    return all(hasattr(module, name) for name in API_NAMES)


def import_package_module(clone: Path):
    """The clone's own installed package module, imported as the consumer does."""
    source_root = str(clone / "src")
    added = source_root not in sys.path
    if added:
        sys.path.insert(0, source_root)
    try:
        module = importlib.import_module(PACKAGE_MODULE)
    except Exception:  # noqa: BLE001 - an import the runtime cannot make refuses
        return None
    finally:
        if added:
            sys.path.remove(source_root)
    return module if has_api(module) else None


def import_file_module(clone: Path):
    """The one standalone module in the clone that declares the sheet API."""
    found = []
    for relative in CANDIDATES:
        module_path = clone / relative
        if not module_path.is_file():
            continue
        spec = importlib.util.spec_from_file_location("consumer_catalog_sheet", module_path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - a module that cannot load as itself refuses
            continue
        if has_api(module):
            found.append(module)
    if len(found) != 1:
        return None
    return found[0]


def load_api(clone: Path):
    return import_package_module(clone) or import_file_module(clone)


def blank_stamps(api, sections: dict) -> dict:
    for title, rows in sections.items():
        if STAMP_COLUMN not in api.COLUMNS.get(title, ()):
            continue
        for row in rows:
            if STAMP_COLUMN in row:
                row[STAMP_COLUMN] = ""
    return sections


def compares(api, committed: str, regenerated: str) -> bool:
    try:
        if api.render(api.parse(committed)) != committed:
            return False
        if api.render(api.parse(regenerated)) != regenerated:
            return False
        return api.render(blank_stamps(api, api.parse(committed))) == api.render(
            blank_stamps(api, api.parse(regenerated))
        )
    except (KeyError, TypeError, ValueError):
        return False


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 2
    clone, relative = Path(argv[1]), argv[2]
    api = load_api(clone)
    if api is None:
        return 1
    try:
        committed = subprocess.run(
            ["git", "-C", str(clone), "show", f"HEAD:{relative}"],
            capture_output=True, text=True, check=True,
        ).stdout
        regenerated = (clone / relative).read_text()
    except (OSError, subprocess.CalledProcessError):
        return 1
    return 0 if compares(api, committed, regenerated) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
