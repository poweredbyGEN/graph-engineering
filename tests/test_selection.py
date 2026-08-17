from __future__ import annotations

import json
import subprocess
from pathlib import Path

from graph_engineering import cli
from graph_engineering.selection import (
    TaskBrief,
    choose_execution,
    graphify_dependency_evidence,
)


def brief(**overrides):
    value = {
        "task": "Change one bounded function and run its focused tests.",
        "independent_lanes": 1,
        "estimated_linear_seconds": None,
        "estimated_graph_seconds": None,
        "estimated_linear_cost_usd": None,
        "estimated_graph_cost_usd": None,
    }
    value.update(overrides)
    return TaskBrief.from_mapping(value)


def test_missing_graph_evidence_defaults_to_linear():
    # intent: graph ceremony is never the default merely because a repository exists.
    decision = choose_execution(brief())
    assert decision["mode"] == "LINEAR"
    assert decision["graph_earned"] is False
    assert decision["reasons"][0]["evidence"] == {"independent_lanes": 1}


def test_parallel_frontier_without_forecasted_benefit_stays_linear():
    decision = choose_execution(
        brief(
            independent_lanes=4,
            estimated_linear_seconds=100,
            estimated_graph_seconds=95,
            estimated_linear_cost_usd=1,
            estimated_graph_cost_usd=2,
        )
    )
    assert decision["mode"] == "LINEAR"
    benefit = next(
        item
        for item in decision["reasons"]
        if item["criterion"] == "forecasted_wall_time_benefit"
    )
    assert benefit["met"] is False


def test_real_frontier_benefit_and_reported_cost_choose_transient():
    decision = choose_execution(
        brief(
            independent_lanes=3,
            estimated_linear_seconds=100,
            estimated_graph_seconds=60,
            estimated_linear_cost_usd=1,
            estimated_graph_cost_usd=2,
        )
    )
    assert decision["mode"] == "TRANSIENT_GRAPH"
    assert decision["graph_earned"] is True


def test_inherent_resume_need_upgrades_an_earned_graph_to_durable():
    decision = choose_execution(
        brief(
            independent_lanes=2,
            estimated_linear_seconds=100,
            estimated_graph_seconds=70,
            estimated_linear_cost_usd=1,
            estimated_graph_cost_usd=1.5,
            resumable=True,
        )
    )
    assert decision["mode"] == "DURABLE_GRAPH"


def test_inherent_durability_does_not_require_a_parallel_frontier():
    # intent: a sequential effectful/resumable campaign still needs durable state,
    # fencing, reconciliation, and receipts across session boundaries.
    decision = choose_execution(
        brief(repetitions=5, long_running=True, resumable=True, effectful=True)
    )
    assert decision["mode"] == "DURABLE_GRAPH"
    assert decision["graph_earned"] is False


def test_repetition_is_only_a_promotion_candidate_without_matched_evidence():
    decision = choose_execution(
        brief(
            independent_lanes=2,
            estimated_linear_seconds=100,
            estimated_graph_seconds=70,
            estimated_linear_cost_usd=1,
            estimated_graph_cost_usd=1.5,
            repetitions=12,
        )
    )
    assert decision["mode"] == "TRANSIENT_GRAPH"
    assert decision["durable_promotion_candidate"] is True


def test_eligible_promotion_requires_named_digest_bound_review():
    task = brief(
        independent_lanes=2,
        estimated_linear_seconds=100,
        estimated_graph_seconds=70,
        estimated_linear_cost_usd=1,
        estimated_graph_cost_usd=1.5,
        repetitions=12,
    )
    eligible = {"eligible": True, "review_required": True, "digest": "abc"}
    assert choose_execution(task, promotion=eligible)["mode"] == "TRANSIENT_GRAPH"
    reviewed = {**eligible, "reviewed": True, "reviewed_by": "release-owner"}
    assert choose_execution(task, promotion=reviewed)["mode"] == "DURABLE_GRAPH"


def test_choose_and_execute_need_no_repository_capsule(capsys):
    args = [
        "--task",
        "compare independent surfaces",
        "--independent-lanes",
        "2",
        "--estimated-linear-seconds",
        "100",
        "--estimated-graph-seconds",
        "70",
        "--estimated-linear-cost-usd",
        "1",
        "--estimated-graph-cost-usd",
        "2",
        "--json",
    ]
    assert cli.main(["choose", *args]) == 0
    chosen = json.loads(capsys.readouterr().out)
    assert chosen["decision"]["mode"] == "TRANSIENT_GRAPH"
    assert cli.main(["execute", *args]) == 0
    executed = json.loads(capsys.readouterr().out)
    assert executed["execution"] == {
        "capsule_required": False,
        "durable_runtime_required": False,
        "host_dispatch_authorized": True,
        "instructions": "fan out only the independent lanes; keep one integration owner",
        "mode": "TRANSIENT_GRAPH",
    }


def test_graphify_ingestion_uses_only_fresh_tracked_focus_paths(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "src").mkdir()
    one = repo / "src" / "one.py"
    two = repo / "src" / "two.py"
    scratch = repo / "src" / "scratch.py"
    one.write_text("VALUE = 1\n")
    two.write_text("from .one import VALUE\n")
    scratch.write_text("SECRET_SCRATCH = True\n")
    subprocess.run(["git", "add", "src/one.py", "src/two.py"], cwd=repo, check=True)
    graph_dir = repo / "graphify-out"
    graph_dir.mkdir()
    (graph_dir / "manifest.json").write_text(
        json.dumps(
            {
                "src/one.py": {"mtime": one.stat().st_mtime},
                "src/two.py": {"mtime": two.stat().st_mtime},
                "src/scratch.py": {"mtime": scratch.stat().st_mtime},
            }
        )
    )
    (graph_dir / "graph.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {"id": "one", "source_file": "src/one.py"},
                    {"id": "two", "source_file": "src/two.py"},
                    {"id": "scratch", "source_file": "src/scratch.py"},
                ],
                "links": [
                    {"source": "two", "target": "one", "relation": "imports_from"},
                    {"source": "scratch", "target": "one", "relation": "imports_from"},
                ],
            }
        )
    )
    evidence = graphify_dependency_evidence(
        repo, ["src/one.py", "src/two.py", "src/scratch.py"]
    )
    assert evidence["fresh_tracked_paths"] == ["src/one.py", "src/two.py"]
    assert evidence["ignored_paths"] == ["src/scratch.py"]
    assert evidence["dependency_edges"] == [
        {
            "source_file": "src/two.py",
            "target_file": "src/one.py",
            "relation": "imports_from",
        }
    ]
    assert "node_count" not in evidence
    assert evidence["trusted_for_selection"] is False
