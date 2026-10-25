#!/usr/bin/env python3
"""Independent acceptance tests for the GEN-11056 contract-lag drift alarm.

Scope: this file tests only the served-contract lag extension of the existing
``contract-sync-pr-monitor.py`` (one monitor, no second path). It writes nothing
and touches no network: every side-effect function is monkeypatched.

Test contract -- the implementation must match it exactly; every case below
asserts it::

    lag_evaluations(snapshot, now)          -> list[incident]
    reconcile(evaluations, now)             -> list[finding]
    watchman_evaluations(observation, now)  -> list[incident]
    watchman_cycle()                        -> runs read_watchman() then reconcile()
    cycle()                                 -> {'findings': reconcile(...)}

* ``now`` is epoch seconds (float). Timestamps inside a snapshot or an
  observation are ISO-8601 UTC strings (``...Z``) read through ``parse_time``.
* The alarm's clock is ``snapshot['rails']['deployed_at']``; ``age_s`` is
  ``now - deployed_at`` in seconds.
* ``reconcile`` returns one finding per incident it evaluated: the incident
  itself (so ``consumer``/``classification``/``details`` are readable on the
  finding) plus ``posted`` (bool) saying whether the policy let it through.
* An incident is ``{consumer, severity, classification, details, age_s}``.
  ``details`` carries ``served_contract_version``, ``served_contract_hash``,
  ``expected_contract_version``, ``expected_contract_hash``, ``producer_sha``,
  ``deployed_sha``, ``main_sha``, ``commits_behind``, ``prs`` (filled in by
  ``cycle()``), ``offbox`` (watchman) and ``identity_error`` (bool).
* ``behind`` is decided by the contract hash: a matching version with a
  different hash is still behind, and a consumer serving an older hash is never
  treated as fresh.
* Grace is 3600s. An incident exists only while ``age_s > grace`` (exactly at
  grace is silent). Severity is ``warning`` below 21600s and ``critical`` at or
  above it, and only ``critical`` opens a Plane issue -- exactly one per
  consumer incident.
* Fail closed: a consumer with no instance, or an instance whose identity cannot
  be read (``contract_hash is None``), is ``critical`` immediately, because
  freshness cannot be verified. Silence is never the answer for an unreadable
  consumer.
* Deploy suppression: ``deploy.last_status == 'deploying'`` or
  ``now - deploy.at < 300`` yields NO incident for that consumer, and therefore
  no Slack, no RCA, no ticket.
* ``contract-sync`` is described by the ``sync`` block, not by ``consumers[]``:
  it is ``sync_stalled`` only when the sync hash lags AND ``last_success`` is
  older than ``rails.deployed_at`` AND ``age_s > grace``.
* ``contract-sync-monitor`` (the watchman) is ``heartbeat_stale`` when
  ``now - completed_at > 1800`` or ``timer_enabled``/``timer_active`` is false,
  and its incident carries ``details['offbox'] is True`` because the heartbeat
  is observed from off the monitored box. Its alert names the timer state, since
  a disabled timer is exactly what hid GEN-11056.

Cases C1-C8 mirror the ticket's acceptance cases; E1-E4 cover the validation
edges the implementation must not paper over. The cases guard deployment evidence and consumer-level alert delivery.

Run: ``uv run --no-project --with 'pytest>=8,<9' pytest ops/contract-sync/test_contract_lag.py -q``
"""
from __future__ import annotations

import importlib.util
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
SIBLING = HERE / "test_contract_sync_pr_monitor.py"

# Fixed clock for the direct-evaluation cases: rails deployed at T0, the test
# then hands lag_evaluations its own "now", so nothing depends on wall time.
T0 = 1_790_000_000.0
MAIN_SHA = "a" * 40
DEPLOYED_SHA = "b" * 40
SYNC_SHA = "c" * 40
PRODUCER_SHA = "d" * 40
SERVED_HASH = "1" * 64  # SHA256 of the older Rails operations schema bytes
RAILS_HASH = "2" * 64   # SHA256 of the deployed/main Rails operations schema bytes
GRACE_S = 3600
CRITICAL_S = 21600

_MONITOR = None


def _iso(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(scope="module")
def m():
    """The monitor module, loaded through the sibling test's loader so the
    shared alert_policy stub and the state/log dirs are set up the same way the
    existing monitor tests set them up."""
    global _MONITOR
    if _MONITOR is None:
        spec = importlib.util.spec_from_file_location("contract_sync_pr_monitor_tests", SIBLING)
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        _MONITOR = harness.load_monitor()
    return _MONITOR


# --------------------------------------------------------------------------- #
# Snapshot builders -- exact inputs for every case.
# --------------------------------------------------------------------------- #
def _instance(unit: str, *, contract_version="v9", producer_sha=DEPLOYED_SHA,
              contract_hash=RAILS_HASH, deploy_sha=DEPLOYED_SHA, commits_behind=0):
    return {
        "unit": unit,
        "contract_version": contract_version,
        "producer_sha": producer_sha,
        "contract_hash": contract_hash,
        "deploy_sha": deploy_sha,
        "commits_behind": commits_behind,
    }


def _consumer(name: str, instances, *, deploy_status="success", deploy_at=None):
    return {
        "name": name,
        "instances": instances,
        "deploy": {"last_status": deploy_status, "at": _iso(T0 if deploy_at is None else deploy_at)},
    }


def _snapshot(*, deployed_at, consumers=None, rails_hash=RAILS_HASH,
              rails_version="v9", sync=None):
    return {
        "rails": {
            "main_sha": MAIN_SHA,
            "deployed_sha": DEPLOYED_SHA,
            "contract_version": rails_version,
            "contract_hash": rails_hash,
            "deployed_at": _iso(deployed_at),
        },
        "consumers": list(consumers or []),
        # The sync bot ran successfully after this Rails deploy yet the consumer
        # still serves old bytes -- the real shape of the GEN-11056 incident. It is
        # anchored to deployed_at so a case with a moving clock stays consistent.
        "sync": sync or {
            "last_success": _iso(deployed_at + 60),
            "result": "success",
            "source_sha": MAIN_SHA,
        },
    }


def _pr(number: int, **overrides):
    pr = {
        "number": number,
        "html_url": f"http://git.invalid/GEN/gen-mcp-server/pulls/{number}",
        "title": "chore: sync backend contracts",
        "created_at": _iso(T0 - 86400),
        "head": {"ref": "auto/contract-sync-deadbeef", "sha": "e" * 40},
    }
    pr.update(overrides)
    return pr


def _pr_entry(number: int, *, classification="RED", failing=None):
    """One `collect_prs()` entry: the existing per-PR finding plus its classification."""
    return {
        "pr": _pr(number),
        "classification": classification,
        "failing": failing if failing is not None else [
            {"id": number, "context": "ci/woodpecker/push/ci", "status": "failure",
             "target_url": f"http://ci.invalid/pipeline/{number}"},
        ],
    }


class FakePolicy:
    """Stands in for ``alert_policy.AlertPolicy``: records the dedup slot, the
    RCA calls (``mimo_rca`` is billed and must be at most one per incident) and
    the optional RCA latency used by the timeout case."""

    def __init__(self, post: bool = True, reason: str = "test", latency: float = 0.0):
        self.post = post
        self.reason = reason
        self.latency = latency
        self.should_post_calls = []
        self.rca_calls = []
        self.footer_calls = []

    def should_post(self, *args, **kwargs):
        self.should_post_calls.append((args, kwargs))
        return self.post, self.reason

    def mimo_rca(self, evidence, timeout=20):
        self.rca_calls.append({"evidence": evidence, "timeout": timeout})
        if self.latency:
            time.sleep(self.latency)
        return "rca: mocked"

    def footer(self, *args, **kwargs):
        self.footer_calls.append((args, kwargs))
        return "DECISION: test"


def _call_slot(call) -> str:
    """The dedup slot a should_post() call used, whatever its call shape."""
    args, kwargs = call
    if args:
        return str(args[0])
    for key in ("slot", "key", "dedup_key", "consumer"):
        if key in kwargs:
            return str(kwargs[key])
    return json.dumps(kwargs, sort_keys=True, default=str)


def _install(m, monkeypatch, tmp_path, policy: FakePolicy | None = None) -> SimpleNamespace:
    """Isolate every side effect of one case: no Slack, no Plane, no per-PR
    ticket, no systemd read, DRY_RUN off (so the reconciler's real branches run)
    and an incidents state file inside the case's own tmp_path."""
    policy = policy or FakePolicy()
    slack: list[str] = []
    plane: list[dict] = []
    tickets: list[tuple] = []
    monkeypatch.setattr(m, "DRY_RUN", False, raising=False)
    monkeypatch.setattr(m, "INCIDENTS_FILE", tmp_path / "incidents.json", raising=False)
    monkeypatch.setattr(m, "POLICY", policy, raising=False)
    monkeypatch.setattr(m, "slack_post", lambda text: slack.append(text), raising=False)
    monkeypatch.setattr(
        m, "plane_create_issue",
        lambda payload: plane.append(payload) or {"id": "plane-test", "sequence_id": 4242},
        raising=False,
    )
    monkeypatch.setattr(
        m, "ensure_ticket",
        lambda repo, pr, classification, head_sha: tickets.append(
            (repo, pr.get("number"), classification, head_sha)
        ) or "http://plane.invalid/gen/issues/1",
        raising=False,
    )
    # contract-sync.service state is an unrelated input here: the sync block of
    # the snapshot carries it, so a systemd probe must not add incidents.
    monkeypatch.setattr(m, "systemctl_show", lambda: "", raising=False)
    return SimpleNamespace(slack=slack, plane=plane, tickets=tickets, policy=policy)


def _mentions(message: str, *needles: str) -> None:
    for needle in needles:
        assert needle in message, f"{needle!r} missing from alert:\n{message}"


def _mentions_sha(message: str, sha: str) -> None:
    assert sha in message or sha[:8] in message, f"sha {sha[:8]} missing from alert:\n{message}"


def _mentions_age(message: str, seconds: int) -> None:
    hours = seconds / 3600.0
    renderings = {f"{hours:g}h", f"{hours:.1f}h", f"{hours:.2f}h", str(seconds), f"{seconds // 60}m"}
    assert any(r in message for r in renderings), f"age {seconds}s missing from alert:\n{message}"


def _findings(result):
    return result["findings"] if isinstance(result, dict) else result


# --------------------------------------------------------------------------- #
# C1 -- the load-bearing case: a served contract older than deployed Rails,
# past the grace window, must page with everything an operator needs.
# --------------------------------------------------------------------------- #
def test_c1_mcp_behind_past_grace_alerts_once(m, monkeypatch, tmp_path):
    # intent: the hosted MCP serving an older contract than deployed Rails for
    # longer than the grace window produces exactly one consumer incident and
    # exactly one Slack alert naming served/expected contract, producer,
    # deployed and main SHAs, age, commits behind and the blocking PR. Silence
    # here is the GEN-11056 incident that nobody was told about.
    fx = _install(m, monkeypatch, tmp_path)
    now = T0 + 7200  # two hours past the Rails deploy: past grace, below 6h
    snapshot = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-mcp-server", [
            _instance("gen-mcp-hosted@8090", contract_version="v8", producer_sha=PRODUCER_SHA,
                      contract_hash=SERVED_HASH, commits_behind=12),
        ])],
    )

    evaluations = m.lag_evaluations(snapshot, now)

    assert len(evaluations) == 1, evaluations
    incident = evaluations[0]
    assert incident["consumer"] == "gen-mcp-server"
    assert incident["classification"] == "behind"
    assert incident["severity"] == "warning"
    assert incident["age_s"] == 7200
    details = incident["details"]
    assert details["served_contract_version"] == "v8"
    assert details["served_contract_hash"] == SERVED_HASH
    assert details["expected_contract_version"] == "v9"
    assert details["expected_contract_hash"] == RAILS_HASH
    assert details["producer_sha"] == PRODUCER_SHA
    assert details["deployed_sha"] == DEPLOYED_SHA
    assert details["main_sha"] == MAIN_SHA
    assert details["commits_behind"] == 12

    # cycle() attaches the blocking sync PRs to the incident; the reconciler reads
    # them from the incident so no second Gitea read happens per evaluation.
    incident["details"]["prs"] = [_pr_entry(643)]

    findings = m.reconcile(evaluations, now)

    assert len(findings) == 1, findings
    assert findings[0]["consumer"] == "gen-mcp-server"
    assert len(fx.slack) == 1, fx.slack
    message = fx.slack[0]
    _mentions(message, "gen-mcp-server", "v8", "v9", "#643", "12")
    _mentions_sha(message, PRODUCER_SHA)
    _mentions_sha(message, DEPLOYED_SHA)
    _mentions_sha(message, MAIN_SHA)
    _mentions_age(message, 7200)

    # One billed RCA per incident, time-boxed; a warning severity never files.
    assert len(fx.policy.rca_calls) == 1, fx.policy.rca_calls
    assert fx.policy.rca_calls[0]["timeout"] == 20
    assert fx.plane == [], fx.plane

    # The posted incident is remembered (C6's recovery note depends on it) and
    # the dedup slot is keyed by the consumer, not the PR.
    assert Path(m.INCIDENTS_FILE).exists(), m.INCIDENTS_FILE
    assert "gen-mcp-server" in Path(m.INCIDENTS_FILE).read_text(encoding="utf-8")
    first_slot = _call_slot(fx.policy.should_post_calls[0])
    assert "gen-mcp-server" in first_slot, first_slot
    m.reconcile(m.lag_evaluations(snapshot, now), now)
    assert _call_slot(fx.policy.should_post_calls[-1]) == first_slot


# --------------------------------------------------------------------------- #
# C2 -- inside the grace window the alarm is silent, and it opens exactly at
# grace + 1s (a normal sync + CI + deploy must not page).
# --------------------------------------------------------------------------- #
def test_c2_behind_inside_grace_is_silent(m, monkeypatch, tmp_path):
    # intent: a consumer that is behind but still inside the 3600s grace window
    # raises no incident, no Slack, no RCA and no ticket; one second past grace
    # the same snapshot does alert, so the silence is the grace window and not a
    # permanently dead check.
    fx = _install(m, monkeypatch, tmp_path)
    snapshot = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-mcp-server", [
            _instance("gen-mcp-hosted@8090", contract_version="v8", contract_hash=SERVED_HASH,
                      commits_behind=4),
        ])],
    )

    inside = m.lag_evaluations(snapshot, T0 + 1800)
    assert inside == [], inside
    m.reconcile(inside, T0 + 1800)
    assert fx.slack == [] and fx.plane == [] and fx.policy.rca_calls == []

    at_grace = m.lag_evaluations(snapshot, T0 + GRACE_S)
    assert at_grace == [], at_grace

    past = m.lag_evaluations(snapshot, T0 + GRACE_S + 1)
    assert len(past) == 1, past
    assert past[0]["age_s"] == GRACE_S + 1
    assert past[0]["severity"] == "warning"


# --------------------------------------------------------------------------- #
# C3 -- twelve red PRs for one consumer are one incident, never twelve.
# --------------------------------------------------------------------------- #
def test_c3_twelve_red_prs_collapse_to_one_incident(m, monkeypatch, tmp_path):
    # intent: one incident per consumer, not per PR. A dozen red sync PRs for
    # gen-mcp-server produce one incident whose details carry all twelve PRs,
    # one billed RCA and one Slack message, while every PR keeps its own Plane
    # ticket mapping.
    fx = _install(m, monkeypatch, tmp_path)
    now = time.time()
    snapshot = _snapshot(
        deployed_at=now - 7200,
        consumers=[_consumer("gen-mcp-server", [
            _instance("gen-mcp-hosted@8090", contract_version="v8", contract_hash=SERVED_HASH,
                      commits_behind=12),
        ], deploy_at=now - 3600)],
    )
    entries = [_pr_entry(643 + i) for i in range(12)]
    monkeypatch.setattr(m, "collect_snapshot", lambda: snapshot, raising=False)
    monkeypatch.setattr(m, "collect_prs", lambda: {"gen-mcp-server": entries}, raising=False)

    findings = _findings(m.cycle())

    assert len(findings) == 1, findings
    assert findings[0]["consumer"] == "gen-mcp-server"
    assert len(findings[0]["details"]["prs"]) == 12
    assert [entry["pr"]["number"] for entry in findings[0]["details"]["prs"]] == list(range(643, 655))

    assert len(fx.tickets) == 12, fx.tickets
    assert [ticket[1] for ticket in fx.tickets] == list(range(643, 655))
    assert len(fx.policy.rca_calls) == 1, fx.policy.rca_calls
    assert len(fx.slack) == 1, fx.slack
    _mentions(fx.slack[0], "#643", "#654")
    assert fx.plane == [], fx.plane


# --------------------------------------------------------------------------- #
# C4 -- the watchman: a stale heartbeat or a dead timer is itself an alert,
# observed from off the monitored box.
# --------------------------------------------------------------------------- #
def test_c4_watchman_stale_heartbeat_or_dead_timer_alerts_offbox(m, monkeypatch, tmp_path):
    # intent: a disabled timer on the monitoring box is exactly the failure that
    # hid GEN-11056 for five days. A heartbeat older than 1800s, or a timer that
    # is not enabled/active, raises one contract-sync-monitor incident (offbox,
    # via the existing entrypoint) and one alert; a healthy heartbeat is silent.
    fx = _install(m, monkeypatch, tmp_path)
    now = T0 + 1801
    stale = {"completed_at": _iso(T0), "timer_enabled": True, "timer_active": True}

    evaluations = m.watchman_evaluations(stale, now)
    assert len(evaluations) == 1, evaluations
    incident = evaluations[0]
    assert incident["consumer"] == "contract-sync-monitor"
    assert incident["classification"] == "heartbeat_stale"
    assert incident["age_s"] == 1801
    assert incident["details"]["offbox"] is True

    healthy = m.watchman_evaluations(
        {"completed_at": _iso(T0), "timer_enabled": True, "timer_active": True}, T0 + 900)
    assert healthy == [], healthy

    disabled = m.watchman_evaluations(
        {"completed_at": _iso(T0 + 60), "timer_enabled": False, "timer_active": True}, T0 + 120)
    assert len(disabled) == 1, disabled
    assert disabled[0]["classification"] == "heartbeat_stale"

    reads: list[int] = []
    monkeypatch.setattr(m, "read_watchman", lambda: reads.append(1) or stale, raising=False)
    m.watchman_cycle()
    assert len(reads) == 1
    assert len(fx.slack) == 1, fx.slack
    _mentions(fx.slack[0], "contract-sync-monitor")
    assert "timer" in fx.slack[0].lower(), fx.slack[0]

    monkeypatch.setattr(
        m, "read_watchman",
        lambda: {"completed_at": _iso(time.time()), "timer_enabled": True, "timer_active": True},
        raising=False,
    )
    m.watchman_cycle()
    # A healed heartbeat pages nothing new: it closes the incident with exactly
    # one recovery note (STUCK-then-heal is one alert plus one recovery).
    assert len(fx.slack) == 2, fx.slack
    assert "recover" in fx.slack[1].lower(), fx.slack[1]


# --------------------------------------------------------------------------- #
# C5 -- contract-sync itself stalled while a newer Rails contract waits.
# --------------------------------------------------------------------------- #
def test_c5_contract_sync_stalled_alerts_only_when_newer_contract_waits(m, monkeypatch, tmp_path):
    # intent: the sync bot exits 1 on every tick without alerting. It is one
    # contract-sync incident only while a newer Rails contract waits, its last
    # success is older than the Rails deploy and the lag is past grace; a sync
    # that succeeded after the deploy, or a lag inside grace, is silent.
    fx = _install(m, monkeypatch, tmp_path)
    now = T0 + 7200
    in_sync = [_consumer("gen-mcp-server", [_instance("gen-mcp-hosted@8090")])]
    stalled = _snapshot(
        deployed_at=T0, consumers=in_sync,
        sync={"last_success": _iso(T0 - 60), "result": "failed", "source_sha": SYNC_SHA},
    )

    evaluations = m.lag_evaluations(stalled, now)

    assert [incident["consumer"] for incident in evaluations] == ["contract-sync"], evaluations
    incident = evaluations[0]
    assert incident["classification"] == "sync_stalled"
    assert incident["age_s"] == 7200
    assert incident["details"]["last_success"] == _iso(T0 - 60)
    assert incident["details"]["result"] == "failed"

    findings = m.reconcile(evaluations, now)
    assert len(findings) == 1, findings
    assert len(fx.slack) == 1, fx.slack
    assert len(fx.policy.rca_calls) == 1
    _mentions(fx.slack[0], "contract-sync", "failed", _iso(T0 - 60))
    _mentions_age(fx.slack[0], 7200)

    fresh_sync = _snapshot(
        deployed_at=T0, consumers=in_sync,
        sync={"last_success": _iso(T0 + 60), "result": "success", "source_sha": MAIN_SHA},
    )
    assert m.lag_evaluations(fresh_sync, now) == []

    assert m.lag_evaluations(stalled, T0 + 1800) == []


# --------------------------------------------------------------------------- #
# C6 -- one recovery note for an incident that was actually posted.
# --------------------------------------------------------------------------- #
def test_c6_recovery_note_only_for_a_posted_incident(m, monkeypatch, tmp_path):
    # intent: a real alert gets exactly one recovery note, and a run that never
    # posted (policy declined) stays silent when it heals -- a recovery note for
    # something nobody was told about is noise that trains people to ignore it.
    fx = _install(m, monkeypatch, tmp_path)
    now = T0 + 7200
    behind = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-mcp-server", [
            _instance("gen-mcp-hosted@8090", contract_version="v8", contract_hash=SERVED_HASH,
                      commits_behind=3),
        ])],
    )
    healthy = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-mcp-server", [_instance("gen-mcp-hosted@8090")])],
    )

    m.reconcile(m.lag_evaluations(behind, now), now)
    assert len(fx.slack) == 1, fx.slack

    m.reconcile(m.lag_evaluations(healthy, now), now)
    assert len(fx.slack) == 2, fx.slack
    _mentions(fx.slack[1], "gen-mcp-server")
    assert "recover" in fx.slack[1].lower(), fx.slack[1]
    assert len(fx.policy.rca_calls) == 1, "a recovery note must not spend an RCA call"
    assert fx.plane == [], fx.plane

    m.reconcile(m.lag_evaluations(healthy, now), now)
    assert len(fx.slack) == 2, fx.slack

    # A policy-suppressed incident was never posted, so its disappearance is silent.
    quiet_dir = tmp_path / "quiet"
    quiet_dir.mkdir()
    fx2 = _install(m, monkeypatch, quiet_dir, policy=FakePolicy(post=False, reason="cadence"))
    m.reconcile(m.lag_evaluations(behind, now), now)
    assert fx2.slack == [], fx2.slack
    m.reconcile(m.lag_evaluations(healthy, now), now)
    assert fx2.slack == [], fx2.slack


# --------------------------------------------------------------------------- #
# C7 -- deploy-aware: a consumer that is deploying, or just deployed, is quiet.
# --------------------------------------------------------------------------- #
def test_c7_deploying_consumer_is_suppressed(m, monkeypatch, tmp_path):
    # intent: gen-deployd owning a consumer means the lag is expected. A
    # consumer that reports deploying, or whose deploy finished less than 300s
    # ago, raises no incident and therefore no Slack, RCA, ticket or Plane
    # issue -- while the same lag past that window still alerts, proving the
    # suppression is the deploy state and not a dead check.
    fx = _install(m, monkeypatch, tmp_path)
    now = T0 + 7200
    stale_instances = [
        _instance("gen-mcp-hosted@8090", contract_version="v8", contract_hash=SERVED_HASH,
                  commits_behind=9),
    ]

    deploying = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-mcp-server", stale_instances,
                             deploy_status="deploying", deploy_at=now - 7100)],
    )
    evaluations = m.lag_evaluations(deploying, now)
    assert [i for i in evaluations if i["consumer"] == "gen-mcp-server"] == [], evaluations
    m.reconcile(evaluations, now)
    assert fx.slack == [] and fx.plane == [] and fx.tickets == []
    assert fx.policy.rca_calls == []

    just_deployed = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-mcp-server", stale_instances,
                             deploy_status="success", deploy_at=now - 120)],
    )
    assert [i for i in m.lag_evaluations(just_deployed, now)
            if i["consumer"] == "gen-mcp-server"] == []

    settled = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-mcp-server", stale_instances,
                             deploy_status="success", deploy_at=now - 400)],
    )
    assert len(m.lag_evaluations(settled, now)) == 1
    m.reconcile(m.lag_evaluations(settled, now), now)
    assert len(fx.slack) == 1, fx.slack


# --------------------------------------------------------------------------- #
# C8 -- a worst-case run of 15 red PRs stays far inside the unit timeout.
# --------------------------------------------------------------------------- #
def test_c8_fifteen_red_prs_finish_inside_run_timeout(m, monkeypatch, tmp_path):
    # intent: the unit was killed by TimeoutSec=120 because a run wrote one MiMo
    # RCA per red PR. Fifteen red PRs for one consumer must cost exactly one
    # time-boxed RCA and one Slack message, measured with a realistic mocked RCA
    # latency, and finish well inside the documented RUN_TIMEOUT_S budget.
    fx = _install(m, monkeypatch, tmp_path, policy=FakePolicy(latency=0.05))
    assert m.RUN_TIMEOUT_S == 600
    now = time.time()
    snapshot = _snapshot(
        deployed_at=now - 7200,
        consumers=[_consumer("gen-mcp-server", [
            _instance("gen-mcp-hosted@8090", contract_version="v8", contract_hash=SERVED_HASH,
                      commits_behind=15),
        ], deploy_at=now - 3600)],
    )
    entries = [_pr_entry(700 + i) for i in range(15)]
    monkeypatch.setattr(m, "collect_snapshot", lambda: snapshot, raising=False)
    monkeypatch.setattr(m, "collect_prs", lambda: {"gen-mcp-server": entries}, raising=False)

    started = time.monotonic()
    findings = _findings(m.cycle())
    elapsed = time.monotonic() - started

    assert len(findings) == 1, findings
    assert len(findings[0]["details"]["prs"]) == 15
    assert len(fx.policy.rca_calls) == 1, fx.policy.rca_calls
    assert len(fx.slack) == 1, fx.slack
    assert elapsed < m.RUN_TIMEOUT_S, elapsed


# --------------------------------------------------------------------------- #
# E1 -- an unreadable consumer snapshot fails closed, not open.
# --------------------------------------------------------------------------- #
def test_e1_missing_instances_fail_closed(m, monkeypatch, tmp_path):
    # intent: a consumer whose instances cannot be read is never assumed fresh.
    # It is critical immediately, even inside the grace window, and files exactly
    # one Plane issue per consumer.
    fx = _install(m, monkeypatch, tmp_path)
    now = T0 + 60  # inside grace: an ordinary lag would be silent
    snapshot = _snapshot(
        deployed_at=T0,
        consumers=[
            _consumer("gen-agentic", [], deploy_at=T0 - 600),
            _consumer("gen-mcp-server", [], deploy_at=T0 - 600),
        ],
    )

    evaluations = m.lag_evaluations(snapshot, now)

    assert len(evaluations) == 2, evaluations
    by_consumer = {incident["consumer"]: incident for incident in evaluations}
    assert set(by_consumer) == {"gen-agentic", "gen-mcp-server"}
    for incident in evaluations:
        assert incident["classification"] == "missing_instances"
        assert incident["severity"] == "critical"
        assert incident["age_s"] == 60

    m.reconcile(evaluations, now)
    assert len(fx.slack) == 2, fx.slack
    assert len(fx.plane) == 2, fx.plane
    assert "gen-agentic" in json.dumps(fx.plane[0])
    assert "gen-mcp-server" in json.dumps(fx.plane[1])


# --------------------------------------------------------------------------- #
# E2 -- gen-agentic's unavailable identity is reported, never fabricated.
# --------------------------------------------------------------------------- #
def test_e2_agentic_identity_error_never_fabricates_version_or_sha(m, monkeypatch, tmp_path):
    # intent: gen-agentic cannot expose contract_version or producer_sha. The
    # shared contract hash still proves it is behind, so the incident must say
    # behind and carry None for the version/SHA it does not have -- a made-up
    # version in an alert sends the operator to the wrong place.
    fx = _install(m, monkeypatch, tmp_path)
    now = T0 + 7200
    snapshot = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-agentic", [
            _instance("gen-agentic@production", contract_version=None, producer_sha=None,
                      contract_hash=SERVED_HASH, commits_behind=None),
        ])],
    )

    evaluations = m.lag_evaluations(snapshot, now)

    assert len(evaluations) == 1, evaluations
    incident = evaluations[0]
    assert incident["consumer"] == "gen-agentic"
    assert incident["classification"] == "behind"
    assert incident["details"]["identity_error"] is True
    assert incident["details"]["served_contract_version"] is None
    assert incident["details"]["producer_sha"] is None
    assert incident["details"]["served_contract_hash"] == SERVED_HASH
    assert incident["details"]["commits_behind"] is None

    m.reconcile(evaluations, now)
    assert len(fx.slack) == 1, fx.slack
    _mentions(fx.slack[0], "gen-agentic", "identity_error")


# --------------------------------------------------------------------------- #
# E3 -- nothing comparable means critical, not silence.
# --------------------------------------------------------------------------- #
def test_e3_unverifiable_identity_is_critical(m, monkeypatch, tmp_path):
    # intent: an instance that reports no hash at all cannot be proven fresh, so
    # it is a critical incident (one consumer Plane issue) rather than the
    # healthy status an empty comparison would produce.
    fx = _install(m, monkeypatch, tmp_path)
    now = T0 + 7200
    snapshot = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-agentic", [
            _instance("gen-agentic@production", contract_version=None, producer_sha=None,
                      contract_hash=None, commits_behind=None),
        ], deploy_at=T0 - 7200)],
    )

    evaluations = m.lag_evaluations(snapshot, now)

    assert len(evaluations) == 1, evaluations
    incident = evaluations[0]
    assert incident["consumer"] == "gen-agentic"
    assert incident["classification"] == "identity_error"
    assert incident["severity"] == "critical"
    assert incident["details"]["identity_error"] is True
    assert incident["details"]["served_contract_hash"] is None

    m.reconcile(evaluations, now)
    assert len(fx.slack) == 1, fx.slack
    assert len(fx.plane) == 1, fx.plane
    assert "gen-agentic" in json.dumps(fx.plane[0])


# --------------------------------------------------------------------------- #
# E4 -- the hash decides; a matching version is not freshness.
# --------------------------------------------------------------------------- #
def test_e4_matching_version_with_stale_hash_is_behind(m, monkeypatch, tmp_path):
    # intent: identical contract_version strings with different operations-schema
    # bytes is a real drift state (the version is a label, the hash is the
    # content). Comparing versions alone would call this healthy and stay silent.
    now = T0 + 7200
    snapshot = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-mcp-server", [
            _instance("gen-mcp-hosted@8090", contract_version="v9", contract_hash=SERVED_HASH,
                      commits_behind=1),
        ])],
    )

    evaluations = m.lag_evaluations(snapshot, now)

    assert len(evaluations) == 1, evaluations
    incident = evaluations[0]
    assert incident["classification"] == "behind"
    assert incident["details"]["served_contract_version"] == "v9"
    assert incident["details"]["served_contract_hash"] == SERVED_HASH
    assert incident["details"]["expected_contract_hash"] == RAILS_HASH


# --------------------------------------------------------------------------- #
# E5 -- the 6h severity edge is where the Plane issue appears.
# --------------------------------------------------------------------------- #
def test_e5_critical_at_six_hours_files_one_plane_issue(m, monkeypatch, tmp_path):
    # intent: escalation happens at 6h and not a second earlier. 21599s is a
    # warning with no Plane issue; 21600s is critical with exactly one issue
    # naming the consumer, its served/expected contract and the blocking PR.
    fx = _install(m, monkeypatch, tmp_path)
    stale_instances = [
        _instance("gen-mcp-hosted@8090", contract_version="v8", contract_hash=SERVED_HASH,
                  commits_behind=7),
    ]
    snapshot = _snapshot(
        deployed_at=T0,
        consumers=[_consumer("gen-mcp-server", stale_instances, deploy_at=T0 - 7200)],
    )

    just_below = m.lag_evaluations(snapshot, T0 + CRITICAL_S - 1)
    assert len(just_below) == 1, just_below
    assert just_below[0]["severity"] == "warning"
    m.reconcile(just_below, T0 + CRITICAL_S - 1)
    assert fx.plane == [], fx.plane

    at_critical = m.lag_evaluations(snapshot, T0 + CRITICAL_S)
    assert len(at_critical) == 1, at_critical
    assert at_critical[0]["severity"] == "critical"
    assert at_critical[0]["age_s"] == CRITICAL_S
    at_critical[0]["details"]["prs"] = [_pr_entry(643)]
    m.reconcile(at_critical, T0 + CRITICAL_S)
    assert len(fx.plane) == 1, fx.plane
    payload = json.dumps(fx.plane[0])
    assert "gen-mcp-server" in payload
    assert ("v8" in payload) and ("v9" in payload)
    assert "#643" in payload or "643" in payload
