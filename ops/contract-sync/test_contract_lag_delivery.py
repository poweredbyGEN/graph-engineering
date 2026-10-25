#!/usr/bin/env python3
"""GEN-11056: a rejected six-hour critical escalation must still be delivered.

intent: an incident's ``posted`` flag is written only when a message is accepted,
but the shared alert policy records the severity and the post time at decision
time, before Slack accepts anything. A consumer that first alerted as ``warning``
keeps ``posted=True``; six hours later the ``critical`` escalation is allowed
once by the policy's ``severity_worsened`` branch and records itself, then the
Slack delivery is rejected. The next run asks for the 24-hour cadence (the flag
still says posted) and the policy suppresses the same severity, so the critical
escalation is lost for a day while the state file claims it was posted. The
retry is legal only when the monitor asks for ``cadence_seconds=0`` because no
delivery was ever accepted, so the test also pins that a *delivered*
same-severity repeat stays cadence-suppressed: the fix cannot be "always pass 0".

The policy used here mirrors the box's shared ``alert_policy.AlertPolicy``
judge: record the slot at decision time, allow first sight / severity worsening
/ changed fingerprint / affected growth / cadence expiry, suppress a
same-severity repeat inside the window, and allow anything asked with
``cadence_seconds=0``.

Run: ``uv run --no-project --with 'pytest>=8,<9' pytest ops/contract-sync/test_contract_lag_delivery.py -q``
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
BOUNDARIES = HERE / "test_contract_lag_boundaries.py"
HARNESS = HERE / "test_contract_sync_pr_monitor.py"

SEVERITY_RANK = {"ok": 0, "info": 0, "healthy": 0, "warning": 1, "high": 2,
                 "error": 2, "critical": 3, "fatal": 4, "down": 4}
CADENCE_S = 86400  # contract-sync-pr-monitor.py's CONTRACT_SYNC_PR_MONITOR_CADENCE_S
SIX_HOURS = 21600  # the monitor's critical threshold


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The sibling boundary module already builds contract snapshots offline; reuse
# its builders and its side-effect isolation instead of duplicating them.
B = _load("contract_sync_lag_boundaries_shared", BOUNDARIES)
H = _load("contract_sync_pr_monitor_harness_shared", HARNESS)


@pytest.fixture(scope="module")
def m():
    return H.load_monitor()


class Clock:
    """The policy's clock, advanced by the test in lockstep with each run."""

    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


class SharedLikePolicy:
    """The shared ``alert_policy.AlertPolicy`` judge, minus MiMo and Slack.

    ``evaluate`` records the slot (severity, fingerprint, post time) when it
    decides to post, not when the sender succeeds, which is what makes a
    rejected delivery indistinguishable from a delivered one to the next run.
    """

    def __init__(self, clock: Clock):
        self.clock = clock
        self.entries: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.results: list[tuple[bool, str]] = []

    def should_post(self, key, fp, severity="warning", affected=0, cadence_seconds=21600):
        now = self.clock()
        self.calls.append({"key": key, "fingerprint": fp, "severity": severity,
                           "affected": affected, "cadence_seconds": cadence_seconds})
        entry = self.entries.get(key) or {}
        if not entry:
            reason = "first_seen"
        elif SEVERITY_RANK[severity] > SEVERITY_RANK.get(entry.get("severity", "info"), 1):
            reason = "severity_worsened"
        elif int(affected) > int(entry.get("affected", 0)):
            reason = "affected_count_grew"
        elif entry.get("fingerprint") != fp:
            reason = "fingerprint_changed"
        elif (now - float(entry.get("last_post_ts", 0))) >= cadence_seconds:
            reason = "cadence_expired"
        else:
            self.results.append((False, "duplicate_within_cadence"))
            return False, "duplicate_within_cadence"
        entry.update({"fingerprint": fp, "severity": severity, "affected": int(affected),
                      "last_post_ts": now})
        self.entries[key] = entry
        self.results.append((True, reason))
        return True, reason

    def mimo_rca(self, evidence, timeout=20):
        return "rca: not billed in this case"

    def footer(self, *args, **kwargs):
        return "DECISION: test"


def _install(m, monkeypatch, tmp_path, policy):
    """Reuse the boundary module's isolation, swapping in the shared-like policy."""
    fx = B._install(m, monkeypatch, tmp_path, policy=policy)
    return SimpleNamespace(slack=fx.slack, plane=fx.plane, policy=policy)


def _behind_at(now: float) -> dict:
    """One behind gen-mcp-server, Rails deployed two hours ago (a warning)."""
    return B._behind(now)


def _posted(m) -> dict:
    path = Path(m.INCIDENTS_FILE)
    state = json.loads(path.read_text()) if path.exists() else {}
    return state.get("gen-mcp-server", {})


def test_rejected_critical_escalation_is_retried_and_delivered(m, monkeypatch, tmp_path):
    # intent: a warning delivered at T, then a critical escalation whose Slack
    # delivery is rejected, must retry on the next run with cadence 0 and post
    # exactly one accepted critical -- while the single consumer Plane ticket is
    # never duplicated. Any other outcome leaves a six-hour escalation silently
    # unannounced for the whole cadence window.
    clock = Clock(B.T0 + 7200)
    policy = SharedLikePolicy(clock)
    fx = _install(m, monkeypatch, tmp_path, policy)

    # 1 -- the warning incident is delivered and records a posted incident.
    warning_now = clock.now
    snapshot = _behind_at(warning_now)
    warning = m.lag_evaluations(snapshot, warning_now)
    assert [item["severity"] for item in warning] == ["warning"], warning
    m.reconcile(warning, warning_now)

    assert len(fx.slack) == 1, fx.slack
    assert "SEVERITY: warning" in fx.slack[0], fx.slack[0]
    assert fx.plane == [], "a warning opens no consumer ticket"
    assert _posted(m)["posted"] is True, _posted(m)
    assert policy.results == [(True, "first_seen")], policy.results

    # 2 -- six hours later the escalation is offered once and Slack rejects it.
    clock.now = B.T0 + SIX_HOURS
    critical_now = clock.now
    critical = m.lag_evaluations(snapshot, critical_now)
    assert [item["severity"] for item in critical] == ["critical"], critical

    rejected: list[str] = []

    def rejected_slack(text):
        rejected.append(text)
        raise RuntimeError("slack_delivery_rejected:ratelimited")

    monkeypatch.setattr(m, "slack_post", rejected_slack, raising=False)
    with pytest.raises(RuntimeError, match="slack_delivery_rejected"):
        m.reconcile(critical, critical_now)

    assert len(rejected) == 1, rejected
    assert "SEVERITY: critical" in rejected[0], rejected[0]
    assert policy.results[-1] == (True, "severity_worsened"), policy.results
    # The shared policy already recorded the critical posting the delivery lost.
    assert policy.entries["contract-sync:gen-mcp-server"]["severity"] == "critical"
    # The state file still says "posted" from the warning and keeps the ticket.
    assert _posted(m)["posted"] is True, _posted(m)
    assert _posted(m)["ticket_url"], _posted(m)
    assert len(fx.plane) == 1, fx.plane

    # 3 -- the next run must retry with cadence 0 and deliver the critical once.
    clock.now = critical_now + 600
    retry_now = clock.now
    accepted: list[str] = []
    monkeypatch.setattr(m, "slack_post", lambda text: accepted.append(text), raising=False)
    m.reconcile(m.lag_evaluations(snapshot, retry_now), retry_now)

    assert len(accepted) == 1, accepted
    assert "SEVERITY: critical" in accepted[0], accepted[0]
    assert policy.calls[-1]["cadence_seconds"] == 0, policy.calls[-1]
    assert policy.results[-1] == (True, "cadence_expired"), policy.results

    # 4 -- a delivered same-severity repeat keeps the cadence, so exactly one
    # critical message is accepted and no second retry is attempted.
    clock.now = retry_now + 60
    repeat_now = clock.now
    m.reconcile(m.lag_evaluations(snapshot, repeat_now), repeat_now)

    assert len(accepted) == 1, accepted
    assert policy.calls[-1]["cadence_seconds"] == CADENCE_S, policy.calls[-1]
    assert policy.results[-1] == (False, "duplicate_within_cadence"), policy.results
    assert len(fx.slack) == 1 and len(rejected) == 1 and len(accepted) == 1, (
        fx.slack, rejected, accepted)
    assert len(fx.plane) == 1, fx.plane


def test_delivered_same_severity_repeat_stays_cadence_suppressed(m, monkeypatch, tmp_path):
    # intent: the retry fix must not disable the cadence for delivered alerts.
    # A warning delivered at T stays the only warning for the next run inside
    # the window, so a rejected delivery is the only reason to ask for cadence 0.
    clock = Clock(B.T0 + 7200)
    policy = SharedLikePolicy(clock)
    fx = _install(m, monkeypatch, tmp_path, policy)

    snapshot = _behind_at(clock.now)
    m.reconcile(m.lag_evaluations(snapshot, clock.now), clock.now)
    assert len(fx.slack) == 1 and "SEVERITY: warning" in fx.slack[0], fx.slack

    clock.now += 600
    m.reconcile(m.lag_evaluations(snapshot, clock.now), clock.now)

    assert len(fx.slack) == 1, fx.slack
    assert policy.calls[-1]["cadence_seconds"] == CADENCE_S, policy.calls[-1]
    assert policy.results[-1] == (False, "duplicate_within_cadence"), policy.results
    assert fx.plane == [], fx.plane
