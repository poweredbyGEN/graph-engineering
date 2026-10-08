"""Host snapshots contain resource counts, never privileged API payloads."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "snapshot", Path(__file__).parents[1] / "ci-workflow-snapshot.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_snapshot_counts_only_this_runner_and_keeps_credentials_off_ci():
    # intent: another runner's workflows and queue labels cannot resize or leak into PR CI.
    queue = {
        "running": [
            {"agent_id": 1, "labels": {"secret": "private"}},
            {"agent_id": 2},
            {"agent_id": 3},
        ]
    }
    assert (
        module.snapshot(queue, {"1", "2"}, 123)
        == "# generated_at 123\nqa-deploy-box\tqa-deploy-box\t2\n"
    )
    assert module.snapshot({"running": []}, {"1"}, 123).endswith("\t1\n")
