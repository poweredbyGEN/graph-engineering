#!/usr/bin/env python3
"""Decide whether a re-generated catalog mirror differs only in its stamp cells.

contract-sync retries a consumer whose previous tick opened a PR that merged.
The consumer's mirror generator stamps ``served_sha`` with the consumer HEAD it
was cut from (``scripts/sheet_mirror.py``), so re-running it against the same
backend input reproduces every real cell and moves only that declared stamp.
Deciding that needs the consumer's own catalog-sheet schema, parse and render --
never a second TSV reader in agent-infra (GEN-10539).

The consumer module is loaded from the clone's own bytes: the canonical paths
(``src/gen_mcp_server/catalog_sheet.py``, ``scripts/catalog_sheet*.py``) are
searched, and only the ``import csv/io/json`` lines, ``COLUMNS``, ``KEYS``,
``parse`` and ``render`` definitions are compiled. Nothing else from the module
runs, so no consumer runtime, engine or package dependency is required.

Accepted only when, for both the committed sheet and the regenerated one,
``render(parse(text))`` reproduces the file byte for byte -- which rejects a
duplicated, missing or reordered declared column and any short or long row --
and the two files then render identically with the declared ``served_sha`` cell
blanked. Every other difference, and any doubt about the loader, refuses.

# intent: a retry that treated the volatile stamp as contract data would either
# refuse a consumer that already shipped the input, or rewrite a generated cell
# by hand; a retry that compared loosely would let a real catalog change through
# as a stamp-only difference.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

STAMP_COLUMN = "served_sha"
STDLIB_IMPORTS = {"csv", "io", "json"}
CANDIDATES = (
    "src/gen_mcp_server/catalog_sheet.py",
    "src/gen_mcp_server/catalog_sheet_pinned.py",
    "scripts/catalog_sheet.py",
    "scripts/catalog_sheet_pinned.py",
)
API_NAMES = ("COLUMNS", "KEYS", "parse", "render")


def module_source(path: Path) -> str | None:
    """The consumer's own definitions, compiled on their own."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    kept: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__" or node.module in STDLIB_IMPORTS:
                kept.append(node)
        elif isinstance(node, ast.Import):
            if all(alias.name.split(".")[0] in STDLIB_IMPORTS for alias in node.names):
                kept.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names and names <= {"COLUMNS", "KEYS"}:
                kept.append(node)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name in ("parse", "render"):
                kept.append(node)
    if not kept:
        return None
    source = ast.Module(body=kept, type_ignores=[])
    try:
        return ast.unparse(source)
    except (AttributeError, ValueError):
        return None


def load_api(clone: Path):
    """The one module in the clone that declares the catalog-sheet API."""
    found = []
    for relative in CANDIDATES:
        module_path = clone / relative
        if not module_path.is_file():
            continue
        source = module_source(module_path)
        if source is None:
            continue
        namespace: dict = {"__name__": "consumer_catalog_sheet"}
        try:
            exec(compile(source, str(module_path), "exec"), namespace)
        except Exception:  # noqa: BLE001 - a loader that cannot run its own code refuses
            continue
        if all(name in namespace for name in API_NAMES):
            found.append(namespace)
    if len(found) != 1:
        return None
    return found[0]


def blank_stamps(api, sections: dict) -> dict:
    for title, rows in sections.items():
        if STAMP_COLUMN not in api["COLUMNS"].get(title, ()):
            continue
        for row in rows:
            if STAMP_COLUMN in row:
                row[STAMP_COLUMN] = ""
    return sections


def compares(api, committed: str, regenerated: str) -> bool:
    parse, render = api["parse"], api["render"]
    try:
        if render(parse(committed)) != committed:
            return False
        if render(parse(regenerated)) != regenerated:
            return False
        return render(blank_stamps(api, parse(committed))) == render(
            blank_stamps(api, parse(regenerated))
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
