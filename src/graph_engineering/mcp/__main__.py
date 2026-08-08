"""Run the graph-engineering MCP server over stdio."""

from __future__ import annotations

import argparse
from pathlib import Path

from .protocol import ServerProfile
from .server import create_mcp_server
from .store import GraphTaskStore


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve fenced graph tasks over MCP stdio"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--stdio", action="store_true", help="accepted for wrapper compatibility"
    )
    parser.add_argument(
        "--disable-tasks-extension",
        action="store_true",
        help="serve only the portable graph_task_* call/poll tools",
    )
    args = parser.parse_args()
    profile = ServerProfile(tasks_extension=not args.disable_tasks_extension)
    create_mcp_server(GraphTaskStore(args.database), profile=profile).run(
        transport="stdio"
    )


if __name__ == "__main__":
    main()
