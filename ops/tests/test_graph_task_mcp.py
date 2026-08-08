from __future__ import annotations

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "graph-task-mcp"


def test_wrapper_keeps_state_outside_project(tmp_path: Path):
    # intent: registering the MCP server globally must not litter every source repository with
    # task-graph SQLite files or make agent state look like product code.
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", project], check=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_server = fake_bin / "task-graph-mcp"
    fake_server.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake_server.chmod(0o755)

    state = tmp_path / "state"
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GRAPH_ENGINEERING_STATE_DIR": str(state),
    }
    proc = subprocess.run(
        [SCRIPT], cwd=project, env=env, text=True, capture_output=True, check=True
    )

    args = proc.stdout.splitlines()
    assert "--stdio" in args
    database = Path(args[args.index("--database") + 1])
    assert state in database.parents
    assert project not in database.parents
    assert not (project / "task-graph").exists()


def test_wrapper_separates_projects(tmp_path: Path):
    # intent: two repositories must never share task IDs, claims, or readiness state merely
    # because the same user-level MCP registration launched both servers.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_server = fake_bin / "task-graph-mcp"
    fake_server.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake_server.chmod(0o755)
    state = tmp_path / "state"
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GRAPH_ENGINEERING_STATE_DIR": str(state),
    }

    databases = []
    for name in ("one", "two"):
        project = tmp_path / name
        project.mkdir()
        subprocess.run(["git", "init", "-q", project], check=True)
        proc = subprocess.run(
            [SCRIPT], cwd=project, env=env, text=True, capture_output=True, check=True
        )
        args = proc.stdout.splitlines()
        databases.append(args[args.index("--database") + 1])

    assert databases[0] != databases[1]
