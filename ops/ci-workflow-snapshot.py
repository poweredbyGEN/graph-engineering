#!/usr/bin/env python3
"""Publish queue counts without exposing queue credentials to PR workloads."""

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path


def snapshot(queue, agent_ids, now):
    # Count whole live workflows, including their setup and database services.
    count = sum(str(job["agent_id"]) in agent_ids for job in queue["running"])
    return f"# generated_at {now}\nqa-deploy-box\tqa-deploy-box\t{max(1, count)}\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-ids", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    ids = set(args.agent_ids.split(","))
    if not ids or any(not value.isdecimal() for value in ids):
        parser.error("agent IDs must be comma-separated integers")
    request = urllib.request.Request(
        os.environ["WOODPECKER_SERVER"].rstrip("/") + "/api/queue/info",
        headers={"Authorization": "Bearer " + os.environ["WOODPECKER_TOKEN"]},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        value = snapshot(json.load(response), ids, int(time.time()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".new")
    temporary.write_text(value)
    temporary.chmod(0o644)
    temporary.replace(args.output)


if __name__ == "__main__":
    main()
