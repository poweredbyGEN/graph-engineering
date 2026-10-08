"""Run deterministic shell fixtures through the bounded QA pytest runner."""

import subprocess
from pathlib import Path


def test_portable_worker_and_shared_shard_plan_cases():
    subprocess.run(
        ["sh", str(Path(__file__).with_name("test_ci_workers.sh"))],
        check=True,
        timeout=60,
    )
