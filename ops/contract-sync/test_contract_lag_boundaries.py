#!/usr/bin/env python3
"""Boundary cases for the GEN-11056 contract-lag alarm.

These cases are independent of the C1-C8/E1-E5 acceptance file
(``test_contract_lag.py``): they pin the edges around the same contract, the
places where an alarm can silently stop alarming.

intent: every case here catches a way the lag alarm lies -- checking one hosted
MCP unit instead of both, paging during an in-flight deployd deploy, marking a
failed Slack delivery as posted (so the retry is suppressed by cadence),
recovering an incident because the consumer started deploying, calling MiMo/Slack/
Plane during a dry run, re-alerting on changed PR heads, trusting a future
heartbeat, or recording a sync heartbeat that a dry run never proved.

Every case runs offline: the monitor's side-effect functions are monkeypatched and
all state files live in ``tmp_path``.

Run: ``uv run --no-project --with 'pytest>=8,<9' pytest ops/contract-sync/test_contract_lag_boundaries.py -q``
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
SIBLING = HERE / "test_contract_sync_pr_monitor.py"
MONITOR = HERE / "contract-sync-pr-monitor.py"
SYNC_SH = HERE / "contract-sync.sh"

T0 = 1_790_000_000.0
MAIN_SHA = "a" * 40
DEPLOYED_SHA = "b" * 40
PRODUCER_SHA = "d" * 40
SERVED_HASH = "1" * 64
RAILS_HASH = "2" * 64
# The short sha shape gen-deployd's /status uses for an in-flight deploy.
DEPLOYING_SHA = "1f32461d76df"

_MONITOR = None


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def m():
    """The monitor module, loaded through the existing offline harness so the
    ``alert_policy`` stub and the temp state/log directories are installed the
    same way the GEN-9537 cases install them."""
    global _MONITOR
    if _MONITOR is None:
        spec = importlib.util.spec_from_file_location("contract_sync_pr_monitor_boundaries", SIBLING)
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        _MONITOR = harness.load_monitor()
    return _MONITOR


# --------------------------------------------------------------------------- #
# Fixtures: snapshots shaped like ``collect_snapshot()`` output.
# --------------------------------------------------------------------------- #
def _instance(unit: str = "gen-mcp-hosted@8090", *, contract_version="v9",
              producer_sha=DEPLOYED_SHA, contract_hash=RAILS_HASH,
              deploy_sha=DEPLOYED_SHA, commits_behind=0):
    return {"unit": unit, "contract_version": contract_version, "producer_sha": producer_sha,
            "contract_hash": contract_hash, "deploy_sha": deploy_sha,
            "commits_behind": commits_behind}


def _stale(unit: str = "gen-mcp-hosted@8090", *, commits_behind=6):
    return _instance(unit, contract_version="v8", producer_sha=PRODUCER_SHA,
                     contract_hash=SERVED_HASH, commits_behind=commits_behind)


def _deploy(now: float, *, status="success", deploying=None):
    """A gen-deployd /status target row: ``at`` is the last state update."""
    row = {"last_status": status, "last_sha": MAIN_SHA, "last_deployed_sha": DEPLOYED_SHA,
           "at": _iso(now - 3600)}
    if deploying is not None:
        row["deploying"] = deploying
        row["phase"] = "smoke"
    return row


def _consumer(name: str, instances, *, deploy: dict):
    return {"name": name, "instances": list(instances), "deploy": deploy}


def _snapshot(*, deployed_at: float, consumers, rails_hash=RAILS_HASH, rails_version="v9"):
    return {
        "rails": {"main_sha": MAIN_SHA, "deployed_sha": DEPLOYED_SHA,
                  "contract_version": rails_version, "contract_hash": rails_hash,
                  "deployed_at": _iso(deployed_at)},
        "consumers": list(consumers),
        # A sync that ran after the Rails deploy: sync progress is healthy, so the
        # only incident these cases can produce is the one under test.
        "sync": {"last_success": _iso(deployed_at + 60), "result": "success",
                 "source_sha": MAIN_SHA},
    }


def _behind(now: float, *, rows=None, deploy=None):
    """One behind gen-mcp-server anchored to a real clock."""
    return _snapshot(
        deployed_at=now - 7200,
        consumers=[_consumer("gen-mcp-server", rows or [_stale()],
                             deploy=deploy or _deploy(now))],
    )


def _healthy(now: float, *, deploy=None):
    return _snapshot(
        deployed_at=now - 7200,
        consumers=[_consumer("gen-mcp-server", [_instance()], deploy=deploy or _deploy(now))],
    )


def _pr_entry(number: int, head_sha: str = "e" * 40):
    return {
        "pr": {"number": number, "html_url": f"http://git.invalid/GEN/gen-mcp-server/pulls/{number}",
               "title": "chore: sync backend contracts", "created_at": _iso(T0 - 86400),
               "head": {"ref": "auto/contract-sync-deadbeef", "sha": head_sha}},
        "classification": "RED",
        "failing": [{"context": "ci/woodpecker/push/ci", "status": "failure",
                     "target_url": f"http://ci.invalid/pipeline/{number}"}],
    }


class FakePolicy:
    """Records the dedup slot, the cadence and every billed RCA call."""

    def __init__(self, post: bool = True, reason: str = "test"):
        self.post = post
        self.reason = reason
        self.should_post_calls: list[tuple] = []
        self.rca_calls: list[dict] = []

    def should_post(self, *args, **kwargs):
        self.should_post_calls.append((args, kwargs))
        return self.post, self.reason

    def mimo_rca(self, evidence, timeout=20):
        self.rca_calls.append({"evidence": evidence, "timeout": timeout})
        return "rca: mocked"

    def footer(self, *args, **kwargs):
        return "DECISION: test"


class CadencePolicy(FakePolicy):
    """Behaves like the shipped policy: a slot inside its cadence window is suppressed."""

    def should_post(self, *args, **kwargs):
        self.should_post_calls.append((args, kwargs))
        if kwargs.get("cadence_seconds"):
            return False, "cadence"
        return self.post, self.reason


def _cadences(policy: FakePolicy) -> list[int]:
    return [int((kwargs.get("cadence_seconds") or 0)) for _args, kwargs in policy.should_post_calls]


def _install(m, monkeypatch, tmp_path, policy: FakePolicy | None = None, *,
             dry_run=False, real_senders=False):
    """Isolate every side effect of one case and keep the state files in tmp_path.

    ``real_senders`` keeps the shipped ``slack_post``/``plane_create_issue`` so a
    case can prove what DRY_RUN does with them.
    """
    policy = policy or FakePolicy()
    slack: list[str] = []
    plane: list[dict] = []
    monkeypatch.setattr(m, "DRY_RUN", dry_run, raising=False)
    monkeypatch.setattr(m, "INCIDENTS_FILE", tmp_path / "incidents.json", raising=False)
    monkeypatch.setattr(m, "COMPLETE_FILE", tmp_path / "last-complete.json", raising=False)
    monkeypatch.setattr(m, "POLICY", policy, raising=False)
    if not real_senders:
        monkeypatch.setattr(m, "slack_post", lambda text: slack.append(text), raising=False)
        monkeypatch.setattr(
            m, "plane_create_issue",
            lambda payload: plane.append(payload) or {"id": "plane-test", "sequence_id": 4242},
            raising=False,
        )
    return SimpleNamespace(slack=slack, plane=plane, policy=policy)


def _stub_collectors(m, monkeypatch, snapshot, prs=None):
    monkeypatch.setattr(m, "collect_snapshot", lambda: snapshot, raising=False)
    monkeypatch.setattr(m, "collect_prs", lambda: prs or {}, raising=False)


# --------------------------------------------------------------------------- #
# 1 -- both hosted MCP units are evidence. The 8090 unit being fresh must not
# hide a stale 8092 unit (that is a partial rollout, not a healthy consumer).
# --------------------------------------------------------------------------- #
def test_both_mcp_instances_are_checked(m, monkeypatch, tmp_path):
    # intent: checking only the first hosted MCP unit reports a partially
    # deployed consumer as fresh. With the healthy unit first, the stale second
    # unit must still raise the incident -- in both orderings.
    fx = _install(m, monkeypatch, tmp_path)
    now = T0 + 7200
    healthy = _instance("gen-mcp-hosted@8090")
    stale = _stale("gen-mcp-hosted@8092", commits_behind=3)

    for rows in ([healthy, stale], [stale, healthy]):
        snapshot = _snapshot(deployed_at=T0, consumers=[_consumer("gen-mcp-server", rows, deploy=_deploy(now))])
        evaluations = m.lag_evaluations(snapshot, now)
        assert len(evaluations) == 1, (rows, evaluations)
        incident = evaluations[0]
        assert incident["classification"] == "behind", incident
        assert incident["details"]["served_contract_hash"] == SERVED_HASH, incident

    fx.slack.clear()
    m.reconcile(m.lag_evaluations(snapshot, now), now)
    assert len(fx.slack) == 1, fx.slack
    assert "gen-mcp-hosted@8092" in fx.slack[0], fx.slack[0]


# --------------------------------------------------------------------------- #
# 2 -- deployd's in-flight marker is the `deploying` short-sha field, which
# appears while `last_status` still holds the previous result.
# --------------------------------------------------------------------------- #
def test_deployd_deploying_sha_suppresses_the_page(m, monkeypatch, tmp_path):
    # intent: a deploy in flight is reported by gen-deployd as `deploying: <12-char
    # sha>` with `last_status` still "success" from the previous deploy and an old
    # `at`. Reading only last_status/at pages during every rollout; the control
    # below proves the same lag alerts once the deploy finishes.
    fx = _install(m, monkeypatch, tmp_path)
    now = T0 + 7200
    deploying = _deploy(now, status="success", deploying=DEPLOYING_SHA)

    assert m.lag_evaluations(_behind(now, deploy=deploying), now) == []
    m.reconcile([], now)
    assert fx.slack == [] and fx.plane == [] and fx.policy.rca_calls == []

    settled = _deploy(now)
    assert len(m.lag_evaluations(_behind(now, deploy=settled), now)) == 1


# --------------------------------------------------------------------------- #
# 3 -- a Slack delivery that fails is not a notification.
# --------------------------------------------------------------------------- #
def test_failed_slack_delivery_is_not_recorded_as_posted(m, monkeypatch, tmp_path):
    # intent: marking an incident posted when Slack rejected the message loses it
    # for the whole cadence window. The next run must retry with cadence 0.
    fx = _install(m, monkeypatch, tmp_path, policy=CadencePolicy())
    now = T0 + 7200
    evaluations = m.lag_evaluations(_behind(now), now)
    assert len(evaluations) == 1, evaluations

    def rejected(_text):
        raise RuntimeError("slack_delivery_rejected:ratelimited")

    monkeypatch.setattr(m, "slack_post", rejected, raising=False)
    with contextlib.suppress(Exception):
        m.reconcile(evaluations, now)

    state_path = Path(m.INCIDENTS_FILE)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    assert state.get("gen-mcp-server", {}).get("posted") is not True, state

    retried: list[str] = []
    monkeypatch.setattr(m, "slack_post", lambda text: retried.append(text), raising=False)
    m.reconcile(m.lag_evaluations(_behind(now), now), now)

    assert len(retried) == 1, retried
    assert _cadences(fx.policy)[-1] == 0, fx.policy.should_post_calls


# --------------------------------------------------------------------------- #
# 4 -- deploy suppression silences the alarm without clearing the incident.
# --------------------------------------------------------------------------- #
def test_deploy_suppression_does_not_recover_and_real_recovery_fires_once(m, monkeypatch, tmp_path):
    # intent: a consumer that starts deploying during an open incident must not
    # produce a recovery note, and the real recovery must produce exactly one.
    fx = _install(m, monkeypatch, tmp_path)
    now = time.time()

    _stub_collectors(m, monkeypatch, _behind(now))
    m.cycle()
    assert len(fx.slack) == 1, fx.slack

    _stub_collectors(m, monkeypatch, _behind(now, deploy=_deploy(now, deploying=DEPLOYING_SHA)))
    m.cycle()
    assert len(fx.slack) == 1, "a deploying consumer must not emit a recovery note"

    _stub_collectors(m, monkeypatch, _healthy(now))
    m.cycle()
    assert len(fx.slack) == 2, fx.slack
    assert "recover" in fx.slack[1].lower(), fx.slack[1]

    _stub_collectors(m, monkeypatch, _healthy(now))
    m.cycle()
    assert len(fx.slack) == 2, "recovery is reported once, not every cycle"


# --------------------------------------------------------------------------- #
# 5 -- an unreadable source fails the cycle and leaves the heartbeat stale.
# --------------------------------------------------------------------------- #
def test_unreadable_source_fails_cycle_without_advancing_heartbeat(m, monkeypatch, tmp_path):
    # intent: an incomplete cycle must never look successful. When the snapshot
    # cannot be collected the run fails and last-complete.json keeps its previous
    # value, so the off-box watchman sees the box as stale.
    _install(m, monkeypatch, tmp_path)
    complete = Path(m.COMPLETE_FILE)
    complete.parent.mkdir(parents=True, exist_ok=True)
    previous = {"completed_at": "2026-10-25T00:00:00Z"}
    complete.write_text(json.dumps(previous))

    def unreachable():
        raise RuntimeError("deployd_status_unreachable")

    monkeypatch.setattr(m, "collect_snapshot", unreachable, raising=False)
    monkeypatch.setattr(m, "collect_prs", lambda: {}, raising=False)

    with pytest.raises(RuntimeError):
        m.cycle()
    assert json.loads(complete.read_text()) == previous


# --------------------------------------------------------------------------- #
# 6 -- a dry run costs nothing and consumes nothing.
# --------------------------------------------------------------------------- #
def test_dry_run_skips_rca_slack_plane_and_cadence(m, monkeypatch, tmp_path, capsys):
    # intent: DRY_RUN exists so a new check can be installed without spending MiMo
    # or sending. It must make no HTTP call, spend no RCA, and must not advance the
    # notification cadence -- otherwise the first live run is silently suppressed.
    fx = _install(m, monkeypatch, tmp_path, dry_run=True, real_senders=True)
    monkeypatch.setattr(m, "SLACK_TOKEN", "test-token", raising=False)
    monkeypatch.setattr(m, "PLANE_API_KEY", "test-key", raising=False)

    def http_in_dry_run(*_args, **_kwargs):
        raise AssertionError("DRY_RUN performed an HTTP call")

    monkeypatch.setattr(m.urllib.request, "urlopen", http_in_dry_run)

    now = T0 + 7200
    unreadable = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-agentic", [_instance("gen-agentic@production",
                                                       contract_version=None, producer_sha=None,
                                                       contract_hash=None, commits_behind=None)],
                             deploy=_deploy(now))],
    )
    evaluations = m.lag_evaluations(unreadable, now)
    assert [i["severity"] for i in evaluations] == ["critical"], evaluations

    m.reconcile(evaluations, now)
    m.reconcile(m.lag_evaluations(unreadable, now), now)

    assert fx.policy.rca_calls == [], "DRY_RUN must not spend a MiMo RCA"
    assert "[slack]" in capsys.readouterr().out, "DRY_RUN must print the payload, not send it"
    assert set(_cadences(fx.policy)) == {0}, fx.policy.should_post_calls


# --------------------------------------------------------------------------- #
# 7 -- one incident key per consumer, whatever the PR heads do.
# --------------------------------------------------------------------------- #
def test_cycle_dedup_key_ignores_changed_pr_heads(m, monkeypatch, tmp_path):
    # intent: the blocking PRs and the Rails head are evidence, never keys. A new
    # PR head on the same consumer must reuse the same fingerprint and slot and
    # must not post a second alert.
    fx = _install(m, monkeypatch, tmp_path, policy=CadencePolicy())
    fingerprints: list[tuple] = []
    monkeypatch.setattr(m.ap, "fingerprint", lambda *parts: fingerprints.append(parts) or "stable-fp")
    now = time.time()

    _stub_collectors(m, monkeypatch, _behind(now), {"gen-mcp-server": [_pr_entry(643, "e" * 40)]})
    m.cycle()
    _stub_collectors(m, monkeypatch, _behind(now), {"gen-mcp-server": [_pr_entry(643, "f" * 40)]})
    m.cycle()

    assert len(fx.slack) == 1, fx.slack
    assert fingerprints == [("contract-sync", "gen-mcp-server")] * 2, fingerprints
    slots = [args[0] for args, _kwargs in fx.policy.should_post_calls]
    assert slots == ["contract-sync:gen-mcp-server"] * 2, slots


# --------------------------------------------------------------------------- #
# 8 -- a heartbeat from the future is not a healthy heartbeat.
# --------------------------------------------------------------------------- #
def test_future_heartbeat_is_invalid(m, monkeypatch, tmp_path):
    # intent: a clock-skewed or rewritten last-complete.json with a future
    # timestamp must alert, not satisfy the freshness window forever.
    fx = _install(m, monkeypatch, tmp_path)
    future = {"completed_at": _iso(T0 + 120), "timer_enabled": True, "timer_active": True}

    incidents = m.watchman_evaluations(future, T0)

    assert len(incidents) == 1, incidents
    assert incidents[0]["classification"] == "heartbeat_stale"
    assert incidents[0]["severity"] == "critical"
    assert incidents[0]["details"]["offbox"] is True
    m.reconcile(incidents, T0)
    assert len(fx.slack) == 1, fx.slack

    at_window = {"completed_at": _iso(T0), "timer_enabled": True, "timer_active": True}
    assert m.watchman_evaluations(at_window, T0 + 1800) == []


# --------------------------------------------------------------------------- #
# 9 -- the unit timeout is the cycle budget, and the off-box check reuses the
# same entrypoint instead of installing a second timer.
# --------------------------------------------------------------------------- #
def test_unit_timeout_and_offbox_watchman_reuse_the_monitor(m):
    # intent: a unit timeout below the cycle budget kills runs mid-RCA (the
    # GEN-11056 cause), and a second timer would need its own dead-man check.
    # The shipped unit must allow the documented 600s, and the watchman must run
    # this same script with --watchman from an existing unit's ExecStopPost.
    unit = (HERE / "contract-sync-pr-monitor.service").read_text(encoding="utf-8")
    timeouts = [line for line in unit.splitlines() if line.startswith("TimeoutStartSec=")]
    assert timeouts == ["TimeoutStartSec=600"], timeouts
    assert m.RUN_TIMEOUT_S == 600

    dropin = (HERE / "contract-sync-watchman.conf").read_text(encoding="utf-8")
    stop_posts = [line for line in dropin.splitlines() if line.startswith("ExecStopPost=")]
    assert len(stop_posts) == 1, stop_posts
    assert "--watchman" in stop_posts[0], stop_posts[0]
    assert stop_posts[0].rstrip().endswith(MONITOR.name + " --watchman"), stop_posts[0]

    timers = sorted(path.name for path in HERE.glob("*.timer"))
    assert timers == ["contract-sync.timer"], timers


# --------------------------------------------------------------------------- #
# 10 -- the sync heartbeat is written only by a run that actually synced.
# --------------------------------------------------------------------------- #
def _extract_shell_function(name: str) -> str:
    """The function exactly as ``contract-sync.sh`` defines it."""
    lines = SYNC_SH.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith(f"{name}() {{"))
    end = next(index for index in range(start + 1, len(lines)) if lines[index] == "}")
    return "\n".join(lines[start:end + 1])


def _run_record_sources(tmp_path: Path, dry_run: bool, backend_sha: str, mcp_sha: str) -> Path:
    """Run the real ``record_sources``/``record_state`` with the script's own vars."""
    state_dir = tmp_path / "state"
    tmp_path.mkdir(parents=True, exist_ok=True)
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "\n".join([
            "set -euo pipefail",
            f'STATE_DIR="{state_dir}"',
            f'DRY_RUN="{"1" if dry_run else "0"}"',
            'LAST_BE_SHA_FILE="$STATE_DIR/last-be-sha"',
            'LAST_BE_MANIFEST_FILE="$STATE_DIR/last-be-watched.tsv"',
            'LAST_MCP_SHA_FILE="$STATE_DIR/last-mcp-sha"',
            'LAST_MCP_MANIFEST_FILE="$STATE_DIR/last-mcp-watched.tsv"',
            'MANIFEST_TMP_FILE="$STATE_DIR/.watched-manifest.new"',
            'MCP_MANIFEST_TMP_FILE="$STATE_DIR/.mcp-watched-manifest.new"',
            'log() { printf \'%s %s\\n\' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }',
            'mkdir -p "$STATE_DIR"',
            'printf \'backend\\n\' > "$MANIFEST_TMP_FILE"',
            'printf \'mcp\\n\' > "$MCP_MANIFEST_TMP_FILE"',
            _extract_shell_function("record_state"),
            _extract_shell_function("record_sources"),
            'record_sources "$1" "$2"',
            "",
        ]),
        encoding="utf-8",
    )
    subprocess.run(["bash", str(harness), backend_sha, mcp_sha], check=True, timeout=60,
                   capture_output=True, text=True)
    return state_dir / "last-success.json"


def test_record_sources_writes_source_sha_and_dry_run_does_not(tmp_path):
    # intent: the monitor reads last-success.json to decide whether contract-sync
    # is stalled. A real sync must record the sha it synced; a dry run must leave
    # no heartbeat, because nothing was synced.
    synced = _run_record_sources(tmp_path / "real", False, MAIN_SHA, DEPLOYED_SHA)
    heartbeat = json.loads(synced.read_text(encoding="utf-8"))
    assert heartbeat["source_sha"] == MAIN_SHA, heartbeat
    assert datetime.fromisoformat(heartbeat["last_success"]), heartbeat

    planned = _run_record_sources(tmp_path / "plan", True, MAIN_SHA, DEPLOYED_SHA)
    assert not planned.exists(), planned
