"""Tests for usage telemetry — the log must count everything and break nothing.

The load-bearing property: record_invocation is fail-silent by contract. A telemetry
write that can crash the CLI would make every command less reliable in exchange for a
counter, which is exactly backwards.
"""

from __future__ import annotations

import json
from pathlib import Path

from graph_engineering import cli
from graph_engineering.usage import record_invocation, summarize, usage_log_path


def test_record_appends_one_line_with_the_fields_stats_needs(
    tmp_path: Path, monkeypatch
):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("GRAPH_ENGINEERING_USAGE_LOG", str(log))
    monkeypatch.setenv("GRAPH_ENGINEERING_CALLER", "claude")
    record_invocation("plan", 0, 42)
    entry = json.loads(log.read_text().strip())
    assert entry["command"] == "plan"
    assert entry["exit_code"] == 0
    assert entry["duration_ms"] == 42
    assert entry["caller"] == "claude"
    assert entry["failure_class"] == "none"
    assert entry["ts"].endswith("+00:00")


def test_opt_out_env_writes_nothing(tmp_path: Path, monkeypatch):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("GRAPH_ENGINEERING_USAGE_LOG", str(log))
    monkeypatch.setenv("GRAPH_ENGINEERING_NO_USAGE_LOG", "1")
    record_invocation("plan", 0, 1)
    assert not log.exists()


def test_record_is_fail_silent_when_the_log_cannot_be_written(
    tmp_path: Path, monkeypatch
):
    # intent: THE safety property — a directory where the file should be makes every
    # open() fail, and the command must still succeed.
    blocked = tmp_path / "usage.jsonl"
    blocked.mkdir()
    monkeypatch.setenv("GRAPH_ENGINEERING_USAGE_LOG", str(blocked))
    record_invocation("plan", 0, 1)  # must not raise


def test_summarize_counts_by_command_repo_day_and_survives_corrupt_lines(
    tmp_path: Path, monkeypatch
):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("GRAPH_ENGINEERING_USAGE_LOG", str(log))
    lines = [
        {
            "version": "1",
            "ts": "2026-08-09T10:00:00+00:00",
            "command": "run",
            "exit_code": 0,
            "duration_ms": 5,
            "repo": "alpha",
            "caller": "claude",
        },
        {
            "version": "1",
            "ts": "2026-08-10T11:00:00+00:00",
            "command": "run",
            "exit_code": 2,
            "duration_ms": 5,
            "repo": "alpha",
            "caller": "codex",
        },
        {
            "version": "1",
            "ts": "2026-08-10T12:00:00+00:00",
            "command": "stats",
            "exit_code": 0,
            "duration_ms": 5,
            "repo": "beta",
            "caller": None,
        },
    ]
    log.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\nnot json at all\n"
    )
    result = summarize()
    assert result["total_invocations"] == 3
    assert result["by_command"] == {"run": 2, "stats": 1}
    assert result["by_repo"] == {"alpha": 2, "beta": 1}
    assert result["by_day"] == {"2026-08-09": 1, "2026-08-10": 2}
    assert result["by_caller"] == {"claude": 1, "codex": 1}
    assert result["failed_invocations"] == 1
    # Legacy v1 entries remain readable but cannot be retroactively classified.
    assert result["expected_rejections"] == 0
    assert result["operational_failures"] == 0
    assert result["corrupt_lines"] == 1
    assert result["first"] == "2026-08-09T10:00:00+00:00"
    assert result["last"] == "2026-08-10T12:00:00+00:00"


def test_window_filter_drops_entries_older_than_the_cutoff(tmp_path: Path, monkeypatch):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("GRAPH_ENGINEERING_USAGE_LOG", str(log))
    log.write_text(
        json.dumps(
            {
                "version": "1",
                "ts": "2001-01-01T00:00:00+00:00",
                "command": "run",
                "exit_code": 0,
                "duration_ms": 1,
                "repo": "old",
            }
        )
        + "\n"
    )
    assert summarize()["total_invocations"] == 1
    assert summarize(days=30)["total_invocations"] == 0


def test_every_cli_invocation_is_recorded_and_stats_reads_it_back(
    tmp_path: Path, monkeypatch, capsys
):
    # intent: the end-to-end loop — running ANY command logs it, and `stats` turns the
    # log into the adoption numbers. Cross-agent tracking rests on this hook firing.
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("GRAPH_ENGINEERING_USAGE_LOG", str(log))
    monkeypatch.delenv("GRAPH_ENGINEERING_NO_USAGE_LOG", raising=False)
    assert cli.main(["capabilities", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["by_command"]["capabilities"] == 1
    # stats records itself too — the log now holds both invocations
    assert summarize()["by_command"] == {"capabilities": 1, "stats": 1}


def test_default_log_path_is_under_xdg_data_home(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("GRAPH_ENGINEERING_USAGE_LOG", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert usage_log_path() == tmp_path / "graph-engineering" / "usage.jsonl"


def test_failure_classes_are_reported_separately(tmp_path: Path, monkeypatch):
    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("GRAPH_ENGINEERING_USAGE_LOG", str(log))
    record_invocation("validate", 2, 1, failure_class="expected_rejection")
    record_invocation("run", 1, 1, failure_class="operational_failure")
    report = summarize()
    assert report["failed_invocations"] == 2
    assert report["expected_rejections"] == 1
    assert report["operational_failures"] == 1
