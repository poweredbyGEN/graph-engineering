"""Offline api-docs surface-command fixture, without provider or network access."""

import json
import pathlib
import subprocess
import sys

arguments = sys.argv[1:]
if "--derive-servers" in arguments:
    assert "--mcp-src" in arguments
    source = arguments[arguments.index("--mcp-src") + 1]
    sha = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
    pathlib.Path("scripts/mcp-surface.json").write_text(json.dumps({"mcp": sha}) + "\n")
else:
    assert not arguments
    assert pathlib.Path("scripts/mcp-surface.json").is_file()
