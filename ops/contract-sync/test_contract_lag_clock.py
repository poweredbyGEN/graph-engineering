"""Consumer PR backlog clocks and ticket cardinality, entirely offline."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("lag_clock_delivery_harness", HERE / "test_contract_lag_delivery.py")
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)
NOW = 1_800_000_000.0
DAY = 86400


@pytest.fixture
def fx(monkeypatch, tmp_path):
    m = D.H.load_monitor()
    clock = D.Clock(NOW)
    policy = D.SharedLikePolicy(clock)
    slack, issues, comments = [], [], []
    for name, value in {
        "STATE_DIR": tmp_path, "LOG_DIR": tmp_path,
        "TICKETS_FILE": tmp_path / "tickets.json", "INCIDENTS_FILE": tmp_path / "incidents.json",
        "COMPLETE_FILE": tmp_path / "last-complete.json", "PROBE_LOG": tmp_path / "probe.log",
        "ALERT_LOG": tmp_path / "alerts.log", "DRY_RUN": False, "POLICY": policy,
        "REPOS": ["GEN/api-docs"], "GRACE_S": 3600, "CADENCE_S": DAY,
    }.items():
        monkeypatch.setattr(m, name, value)
    monkeypatch.setattr(m.time, "time", clock)
    monkeypatch.setattr(m, "slack_post", slack.append)
    monkeypatch.setattr(m, "plane_create_issue", lambda payload: issues.append(payload) or {
        "id": "consumer-issue", "sequence_id": 12345,
    })
    monkeypatch.setattr(m, "plane_create_comment", lambda issue, payload: comments.append((issue, payload)) or {})
    snapshot = D.B._snapshot(deployed_at=NOW - 300, consumers=[])
    monkeypatch.setattr(m, "collect_snapshot", lambda: snapshot)
    prs, states = [], {}
    monkeypatch.setattr(m, "gitea_list_open_prs", lambda repo: list(prs))
    monkeypatch.setattr(m, "gitea_statuses", lambda repo, sha: copy.deepcopy(states[sha]))
    return SimpleNamespace(m=m, clock=clock, policy=policy, slack=slack, issues=issues,
                           comments=comments, prs=prs, states=states, snapshot=snapshot)


def add_pr(fx, number, age, status="failure", *, mergeable=True, base="main"):
    sha = f"{number:040x}"
    pr = {
        "number": number, "html_url": f"https://git.invalid/GEN/api-docs/pulls/{number}",
        "title": "chore: sync backend contracts", "created_at": D.B._iso(NOW - age),
        "head": {"ref": "auto/contract-sync-" + sha[:8], "sha": sha},
        "base": {"ref": base}, "mergeable": mergeable,
    }
    fx.prs.append(pr)
    fx.states[sha] = [] if status is None else [{
        "context": "ci/woodpecker/push/test", "status": status,
        "target_url": f"https://ci.invalid/pipeline/{number}", "created_at": D.B._iso(NOW - 60),
    }]
    return pr


def backlog(fx):
    for number in range(312, 324):
        add_pr(fx, number, 4 * DAY - (number - 312) * 3600)
    newest = fx.prs[-1]
    newest["created_at"] = D.B._iso(NOW - 600)
    fx.states[newest["head"]["sha"]][0]["status"] = "pending"
    # Newest-first API order must not determine the incident's age or worst state.
    fx.prs.reverse()


def test_four_day_backlog_survives_recent_rails_and_pending_newest(fx):
    # intent: a new Rails deploy and pending newest PR cannot hide four days of red consumer backlog.
    backlog(fx)
    result = fx.m.cycle()["findings"]
    assert len(result) == 1
    item = result[0]
    assert (item["consumer"], item["classification"], item["severity"]) == ("api-docs", "RED", "critical")
    assert item["age_s"] == pytest.approx(4 * DAY)
    assert len(item["details"]["prs"]) == 12
    assert len(fx.slack) == len(fx.issues) == 1
    assert "SEVERITY: critical" in fx.slack[0]
    assert "96.00h" in fx.slack[0]
    assert "checked:no_safe_auto_fix_mapped" in fx.slack[0]
    for pr in fx.prs:
        assert f"#{pr['number']} " in fx.slack[0]
    assert "PENDING" in fx.slack[0]
    assert not any("RECOVERED" in message for message in fx.slack)


def test_pending_newest_turns_red_without_repost_or_new_ticket(fx):
    # intent: changing a non-worst PR state keeps the same delivered consumer incident and cadence.
    backlog(fx)
    fx.m.cycle()
    assert len(fx.issues) == 1
    fx.clock.now += 600
    fx.states[fx.prs[0]["head"]["sha"]][0]["status"] = "failure"
    fx.m.cycle()
    assert len(fx.slack) == len(fx.issues) == 1
    assert fx.comments == []
    assert fx.policy.results[-1] == (False, "duplicate_within_cadence")


@pytest.mark.parametrize("posted", [True, False])
def test_recovery_requires_no_open_pr_and_prior_accepted_alert(fx, posted):
    # intent: pending open PRs hold an incident; only an empty backlog resolves a posted incident once.
    backlog(fx)
    fx.m.cycle()
    state = json.loads(fx.m.INCIDENTS_FILE.read_text())
    state["api-docs"]["posted"] = posted
    fx.m.INCIDENTS_FILE.write_text(json.dumps(state))
    fx.slack.clear()
    fx.prs[:] = [fx.prs[0]]
    fx.clock.now += 600
    fx.m.cycle()
    assert not any("RECOVERED" in message for message in fx.slack)
    fx.slack.clear()
    fx.prs.clear()
    fx.m.cycle()
    assert sum("RECOVERED" in message for message in fx.slack) == int(posted)
    fx.m.cycle()
    assert sum("RECOVERED" in message for message in fx.slack) == int(posted)


def test_three_hour_warning_escalates_at_24h_once(fx):
    # intent: PR age creates warning at three hours and exactly one critical repost/ticket at 24 hours.
    add_pr(fx, 312, 3 * 3600)
    item = fx.m.cycle()["findings"][0]
    assert (item["age_s"], item["severity"]) == (3 * 3600, "warning")
    assert len(fx.slack) == 1 and fx.issues == []
    fx.clock.now = NOW + 21 * 3600
    item = fx.m.cycle()["findings"][0]
    assert (item["age_s"], item["severity"]) == (DAY, "critical")
    assert len(fx.slack) == 2 and len(fx.issues) == 1
    fx.clock.now += 600
    fx.m.cycle()
    assert len(fx.slack) == 2 and len(fx.issues) == 1


def test_twelve_red_prs_create_one_consumer_ticket_and_preserve_legacy_rows(fx):
    # intent: the ticket map retains old PR rows while twelve blocked PRs create only one consumer ticket.
    legacy = {"https://git.invalid/GEN/api-docs/pulls/1": {
        "issue_id": "legacy", "head_sha": "a" * 40, "url": "https://plane.invalid/legacy",
    }}
    fx.m.TICKETS_FILE.write_text(json.dumps(legacy))
    for number in range(312, 324):
        add_pr(fx, number, 4 * DAY)
    fx.m.cycle()
    fx.clock.now += 600
    fx.m.cycle()
    assert len(fx.issues) == 1
    assert fx.issues[0]["name"] == "Contract consumer incident: api-docs"
    assert json.loads(fx.m.TICKETS_FILE.read_text())[next(iter(legacy))] == next(iter(legacy.values()))


def test_ticket_comments_track_only_pr_set_and_worst_state_even_inside_cadence(fx):
    # intent: changed backlog membership or worst state updates the consumer ticket, without per-cycle comments.
    red = add_pr(fx, 312, 4 * DAY)
    add_pr(fx, 313, 3 * DAY, "success", mergeable=False)
    fx.m.cycle()
    assert len(fx.issues) == 1 and fx.comments == []
    fx.clock.now += 600
    add_pr(fx, 314, 600, "pending")
    fx.m.cycle()
    assert len(fx.comments) == 1
    assert "314" in fx.comments[-1][1]["comment_html"]
    fx.m.cycle()
    assert len(fx.comments) == 1
    fx.prs.remove(red)
    fx.m.cycle()
    assert len(fx.comments) == 2
    assert "CONFLICT" in fx.comments[-1][1]["comment_html"]
    fx.m.cycle()
    assert len(fx.comments) == 2 and len(fx.issues) == 1
    assert len(fx.slack) == 1


@pytest.mark.parametrize("age,severity", [(7199, None), (7200, "warning"), (86399, "warning"), (86400, "critical")])
def test_pr_warning_and_critical_age_boundaries(fx, age, severity):
    # intent: open PR age, independent of Rails age, controls the two-hour and 24-hour thresholds.
    add_pr(fx, 312, age)
    result = fx.m.cycle()["findings"]
    if severity is None:
        assert result == [] and fx.slack == [] and fx.issues == []
    else:
        assert len(result) == 1 and result[0]["severity"] == severity
        assert len(fx.issues) == int(severity == "critical")


@pytest.mark.parametrize("status,mergeable,expected", [
    ("failure", False, "RED"), ("success", False, "CONFLICT"),
    ("pending", True, "STALE"), ("success", True, "PENDING"),
])
def test_worst_state_is_order_independent_and_green_open_pr_still_lags(fx, status, mergeable, expected):
    # intent: every open sync PR keeps main behind, and the worst state wins regardless of API ordering.
    add_pr(fx, 312, 4 * DAY, status, mergeable=mergeable)
    add_pr(fx, 313, 600, "pending")
    fx.prs.reverse()
    result = fx.m.cycle()["findings"]
    assert len(result) == 1
    assert result[0]["classification"] == expected
    assert len(result[0]["details"]["prs"]) == 2


def test_only_sync_prs_targeting_main_count(fx):
    # intent: unrelated branches and sync PRs for release branches cannot create consumer-main lag.
    fx.snapshot["rails"]["deployed_at"] = D.B._iso(NOW - 7200)
    add_pr(fx, 312, 4 * DAY, base="release")
    other = add_pr(fx, 313, 4 * DAY)
    other["head"]["ref"] = "feature/contracts"
    assert fx.m.cycle()["findings"] == []
    assert fx.slack == [] and fx.issues == []


def test_ticket_comment_failure_retries_same_snapshot_without_new_ticket(fx, monkeypatch):
    # intent: a rejected comment cannot advance the successful snapshot or lose the update inside Slack cadence.
    add_pr(fx, 312, 4 * DAY)
    fx.m.cycle()
    previous = json.loads(fx.m.INCIDENTS_FILE.read_text())["api-docs"]["ticket_summary"]
    add_pr(fx, 313, 600, "pending")
    attempts = []

    def send_comment(issue_id, payload):
        attempts.append((issue_id, payload))
        if len(attempts) == 1:
            raise RuntimeError("plane_comment_rejected")
        fx.comments.append((issue_id, payload))
        return {}

    monkeypatch.setattr(fx.m, "plane_create_comment", send_comment)
    with pytest.raises(RuntimeError, match="plane_comment_rejected"):
        fx.m.cycle()
    assert json.loads(fx.m.INCIDENTS_FILE.read_text())["api-docs"]["ticket_summary"] == previous
    fx.m.cycle()
    assert attempts[0] == attempts[1]
    assert len(fx.comments) == 1 and len(fx.issues) == 1 and len(fx.slack) == 1
    fx.m.cycle()
    assert len(attempts) == 2


def test_removing_oldest_pr_uses_next_oldest_without_recovery(fx):
    # intent: merging the oldest PR moves the backlog clock to the next oldest rather than the Rails deploy.
    oldest = add_pr(fx, 312, 4 * DAY)
    add_pr(fx, 313, 3 * DAY)
    fx.m.cycle()
    fx.prs.remove(oldest)
    fx.clock.now += 600
    fx.snapshot["rails"]["deployed_at"] = D.B._iso(fx.clock.now - 30)
    result = fx.m.cycle()["findings"]
    assert len(result) == 1 and result[0]["age_s"] == 3 * DAY + 600
    assert result[0]["severity"] == "critical"
    assert len(fx.slack) == 1 and len(fx.issues) == 1
    assert len(fx.comments) == 1
    assert not any("RECOVERED" in message for message in fx.slack)


def test_pr_threshold_environment_overrides_control_cycle(fx, monkeypatch):
    # intent: deployment configuration overrides both PR age thresholds without changing the deployed-contract clock.
    monkeypatch.setenv("WARNING_HOURS", "1")
    monkeypatch.setenv("CRITICAL_HOURS", "4")
    configured_spec = importlib.util.spec_from_file_location("configured_lag_clock_monitor", fx.m.__file__)
    configured = importlib.util.module_from_spec(configured_spec)
    configured_spec.loader.exec_module(configured)
    assert (configured.WARNING_HOURS, configured.CRITICAL_HOURS) == (1, 4)
    monkeypatch.setattr(fx.m, "WARNING_HOURS", configured.WARNING_HOURS)
    monkeypatch.setattr(fx.m, "CRITICAL_HOURS", configured.CRITICAL_HOURS)
    fx.snapshot["rails"]["deployed_at"] = D.B._iso(NOW - 7200)
    add_pr(fx, 312, 3599)
    assert fx.m.cycle()["findings"] == []
    fx.clock.now += 1
    assert fx.m.cycle()["findings"][0]["severity"] == "warning"
    assert fx.issues == []
    fx.clock.now += 3 * 3600
    assert fx.m.cycle()["findings"][0]["severity"] == "critical"
    assert len(fx.slack) == 2 and len(fx.issues) == 1


def seed_legacy_incident(fx):
    legacy = {"posted": True, "posted_severity": "critical",
              "ticket_url": "https://plane.invalid/gen/projects/legacy-project/issues/11145"}
    fx.m.INCIDENTS_FILE.write_text(json.dumps({"api-docs": legacy}))
    return legacy


def test_legacy_consumer_ticket_resolves_once_and_comments_existing_issue(fx, monkeypatch):
    # intent: previously stored consumer URLs retain their ticket and gain only the identity needed for comments.
    add_pr(fx, 312, 4 * DAY)
    legacy = seed_legacy_incident(fx)
    resolved = []
    monkeypatch.setattr(fx.m, "plane_ticket_issue_id", lambda url: resolved.append(url) or "legacy-consumer-id", raising=False)
    fx.m.cycle()
    assert resolved == [legacy["ticket_url"]]
    assert fx.issues == [] and len(fx.comments) == 1
    assert fx.comments[0][0] == "legacy-consumer-id"
    state = json.loads(fx.m.INCIDENTS_FILE.read_text())["api-docs"]
    assert state["ticket_url"] == legacy["ticket_url"]
    assert state["ticket_issue_id"] == "legacy-consumer-id"
    fx.m.cycle()
    assert len(resolved) == len(fx.comments) == 1
    assert fx.issues == []


@pytest.mark.parametrize("failure", ["lookup", "comment"])
def test_legacy_ticket_migration_failure_preserves_url_and_pending_summary(fx, monkeypatch, failure):
    # intent: failed legacy resolution or commenting never creates a replacement or claims its snapshot was delivered.
    add_pr(fx, 312, 4 * DAY)
    legacy = seed_legacy_incident(fx)
    resolved = []
    attempts = []

    def resolve(url):
        resolved.append(url)
        if failure == "lookup" and len(resolved) == 1:
            raise RuntimeError("legacy_lookup_failed")
        return "legacy-consumer-id"

    def comment(issue_id, payload):
        attempts.append((issue_id, payload))
        if failure == "comment" and len(attempts) == 1:
            raise RuntimeError("legacy_comment_failed")
        fx.comments.append((issue_id, payload))
        return {}

    monkeypatch.setattr(fx.m, "plane_ticket_issue_id", resolve, raising=False)
    monkeypatch.setattr(fx.m, "plane_create_comment", comment)
    with pytest.raises(RuntimeError, match="legacy_.*_failed"):
        fx.m.cycle()
    pending = json.loads(fx.m.INCIDENTS_FILE.read_text())["api-docs"]
    assert pending["ticket_url"] == legacy["ticket_url"]
    assert "ticket_summary" not in pending
    assert fx.issues == [] and fx.comments == []
    fx.m.cycle()
    assert len(fx.comments) == 1 and fx.comments[0][0] == "legacy-consumer-id"
    assert fx.issues == []
    fx.m.cycle()
    assert len(fx.comments) == 1
    assert len(resolved) == (2 if failure == "lookup" else 1)


def test_held_young_pending_backlog_updates_existing_ticket_without_alert(fx):
    # intent: a held young PR still updates the existing ticket's changed set and worst state without recovery or paging.
    backlog(fx)
    fx.m.cycle()
    newest = fx.prs[0]
    fx.prs[:] = [newest]
    fx.clock.now += 600
    assert fx.m.cycle()["findings"] == []
    assert len(fx.slack) == len(fx.issues) == 1
    assert len(fx.comments) == 1
    assert "PENDING" in fx.comments[0][1]["comment_html"]
    assert str(newest["number"]) in fx.comments[0][1]["comment_html"]
    fx.m.cycle()
    assert len(fx.comments) == 1


def test_empty_backlog_comments_existing_ticket_once_before_recovery(fx):
    # intent: clearing the final PR records the empty backlog on its consumer ticket and recovers only once.
    import html

    backlog(fx)
    fx.m.cycle()
    fx.prs.clear()
    fx.m.cycle()
    assert len(fx.comments) == 1
    assert fx.comments[0][0] == "consumer-issue"
    assert "no open sync PRs remain" in html.unescape(fx.comments[0][1]["comment_html"])
    assert sum("RECOVERED" in message for message in fx.slack) == 1
    fx.m.cycle()
    assert len(fx.comments) == 1 and len(fx.issues) == 1
    assert sum("RECOVERED" in message for message in fx.slack) == 1


def fresh_monitor():
    D.H._stub_alert_policy()
    spec = importlib.util.spec_from_file_location("lag_clock_page_monitor", HERE / "contract-sync-pr-monitor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_open_pr_collection_reads_every_page(monkeypatch):
    # intent: a backlog beyond Gitea's first page cannot hide its oldest open PR or omit evidence.
    from urllib.parse import parse_qs, urlsplit

    m = fresh_monitor()
    pages = []

    def get(path):
        query = parse_qs(urlsplit(path).query)
        assert query["state"] == ["open"]
        page = int(query.get("page", ["1"])[0])
        pages.append(page)
        return {1: [{"number": i} for i in range(1, 51)], 2: [{"number": 51}]}.get(page, [])

    monkeypatch.setattr(m, "gitea_get", get)
    assert [pr["number"] for pr in m.gitea_list_open_prs("GEN/api-docs")] == list(range(1, 52))
    assert pages == [1, 2, 3]


def test_malformed_open_pr_response_fails_instead_of_recovering(monkeypatch):
    # intent: an invalid Gitea PR response cannot masquerade as an empty backlog and recover a consumer.
    m = fresh_monitor()
    monkeypatch.setattr(m, "gitea_get", lambda path: {"message": "invalid_response"})
    with pytest.raises(ValueError, match="invalid_open_pr_list:GEN/api-docs"):
        m.gitea_list_open_prs("GEN/api-docs")


def test_large_backlog_alert_fits_slack_and_keeps_each_pr_and_full_investigation(fx, monkeypatch):
    # intent: a real-sized backlog fits one Slack message without truncating the PR list, footer or investigation evidence.
    rca = []
    footer = "\n_probe: offline:contract-sync-pr-monitor.py · tail:alerts.log · state:isolated_"
    monkeypatch.setattr(fx.policy, "footer", lambda *args: footer)
    monkeypatch.setattr(fx.policy, "mimo_rca", lambda evidence, timeout=20: rca.append(evidence) or "offline RCA")
    for number in range(312, 358):
        pr = add_pr(fx, number, 4 * DAY)
        pr["title"] = "chore: sync backend contracts with validated consumer schema " + "contract-field " * 10
        fx.states[pr["head"]["sha"]] = [{
            "context": f"ci/woodpecker/pr/shard-{shard}", "status": "failure",
            "target_url": f"https://ci.invalid/repos/48/pipeline/{10000 + number}/{shard}",
            "created_at": D.B._iso(NOW - 60),
        } for shard in range(1, 5)]
    newest = fx.prs[-1]
    newest["created_at"] = D.B._iso(NOW - 600)
    fx.states[newest["head"]["sha"]] = [{
        "context": "ci/woodpecker/pr/ci", "status": "pending",
        "target_url": "https://ci.invalid/repos/48/pipeline/10357/1",
        "created_at": D.B._iso(NOW - 60),
    }]
    fx.m.cycle()
    assert len(fx.slack) == 1
    message = fx.slack[0]
    assert len(message) < 40000, len(message)
    for pr in fx.prs:
        assert f"#{pr['number']} " in message
    assert "RED" in message and "PENDING" in message
    pr_lines = [line for line in message.splitlines() if line.startswith("#")]
    assert len(pr_lines) == 46 and all("checks=" not in line for line in pr_lines)
    assert message.endswith(footer)
    assert len(rca) == 1 and len(rca[0]["observation"]["details"]["prs"]) == 46
    assert len(fx.issues) == 1
    assert newest["title"] in fx.issues[0]["description_html"]
    assert "ci/woodpecker/pr/shard-4" in fx.issues[0]["description_html"]
    assert sum(len(pr["failing"]) for pr in rca[0]["observation"]["details"]["prs"]) == 180


@pytest.mark.parametrize("matching_rows", [0, 1, 2])
def test_legacy_ticket_lookup_matches_exact_project_and_sequence(monkeypatch, matching_rows):
    # intent: URL migration resolves only one exact project/sequence match and refuses absent or ambiguous identity.
    import io

    m = fresh_monitor()
    monkeypatch.setattr(m, "PLANE_API_KEY", "offline-token")
    target = {"id": "147964b1-5671-4942-bff1-e1b0053e8ba1", "sequence_id": 11145,
              "project_id": m.PLANE_PROJECT_ID}
    others = [{"id": "other-project", "sequence_id": 11145, "project_id": "another-project"},
              {"id": "other-sequence", "sequence_id": 11146, "project_id": m.PLANE_PROJECT_ID}]
    calls = []

    def urlopen(request, timeout):
        calls.append((request.full_url, timeout))
        return io.StringIO(json.dumps({"issues": others + [target] * matching_rows}))

    monkeypatch.setattr(m.urllib.request, "urlopen", urlopen)
    ticket_url = f"{m.PLANE_BASE}/gen/projects/{m.PLANE_PROJECT_ID}/issues/11145"
    if matching_rows == 1:
        assert m.plane_ticket_issue_id(ticket_url) == target["id"]
    else:
        with pytest.raises(ValueError, match="consumer_ticket_not_unique:"):
            m.plane_ticket_issue_id(ticket_url)
    assert len(calls) == 1 and "search=GEN-11145" in calls[0][0]
    assert 0 < calls[0][1] <= m.HTTP_TIMEOUT


@pytest.mark.parametrize("classification", ["identity_error", "missing_instances"])
@pytest.mark.parametrize("pr_age", [600, 3 * 3600])
def test_unverified_served_identity_stays_critical_with_open_pr_evidence(fx, classification, pr_age):
    # intent: young or warning-aged PRs cannot hide or downgrade an unverified served consumer identity.
    instances = [] if classification == "missing_instances" else [D.B._instance(contract_hash=None)]
    fx.snapshot["consumers"] = [D.B._consumer("api-docs", instances, deploy=D.B._deploy(NOW))]
    add_pr(fx, 312, pr_age)
    result = fx.m.cycle()["findings"]
    assert len(result) == 1
    item = result[0]
    assert item["classification"] == classification and item["severity"] == "critical"
    assert item["age_s"] == 300
    assert item["details"]["identity_error"] is True
    assert item["details"]["pr_age_s"] == pr_age
    assert item["details"]["pr_classification"] == "RED"
    assert len(item["details"]["prs"]) == 1
    assert len(fx.slack) == len(fx.issues) == 1


def test_normal_served_lag_uses_oldest_pr_clock_and_thresholds(fx):
    # intent: normal verified contract drift uses the consumer's oldest PR age even when Rails deployed much earlier.
    fx.snapshot["rails"]["deployed_at"] = D.B._iso(NOW - 8 * 3600)
    fx.snapshot["consumers"] = [D.B._consumer("api-docs", [D.B._stale()], deploy=D.B._deploy(NOW))]
    add_pr(fx, 312, 3 * 3600)
    result = fx.m.cycle()["findings"]
    assert len(result) == 1
    assert (result[0]["classification"], result[0]["age_s"], result[0]["severity"]) == ("RED", 3 * 3600, "warning")
    assert len(fx.slack) == 1 and fx.issues == []


def test_empty_pr_set_updates_ticket_while_verified_served_lag_stays_active(fx):
    # intent: merged PRs update the ticket's empty set without claiming recovery while the consumer still serves old bytes.
    import html

    fx.snapshot["rails"]["deployed_at"] = D.B._iso(NOW - 8 * 3600)
    fx.snapshot["consumers"] = [D.B._consumer("api-docs", [D.B._stale()], deploy=D.B._deploy(NOW))]
    add_pr(fx, 312, 4 * DAY)
    fx.m.cycle()
    assert len(fx.slack) == len(fx.issues) == 1
    fx.prs.clear()
    item = fx.m.cycle()["findings"][0]
    assert item["classification"] == "behind" and item["details"]["prs"] == []
    assert len(fx.comments) == 1 and fx.comments[0][0] == "consumer-issue"
    comment = html.unescape(fx.comments[0][1]["comment_html"])
    assert '"prs": []' in comment and '"classification": "behind"' in comment
    assert len(fx.slack) == len(fx.issues) == 1
    assert not any("RECOVERED" in message for message in fx.slack)
    fx.m.cycle()
    assert len(fx.comments) == 1


def test_open_pr_collection_survives_server_page_size_clamp(monkeypatch):
    # intent: Gitea's smaller server page size cannot hide the oldest PR beyond a short first page.
    from urllib.parse import parse_qs, urlsplit

    m = fresh_monitor()
    pages = []

    def get(path):
        query = parse_qs(urlsplit(path).query)
        assert query["limit"] == ["50"]
        page = int(query["page"][0])
        pages.append(page)
        return {1: [{"number": i} for i in range(1, 31)], 2: [{"number": 31}]}.get(page, [])

    monkeypatch.setattr(m, "gitea_get", get)
    assert [pr["number"] for pr in m.gitea_list_open_prs("GEN/api-docs")] == list(range(1, 32))
    assert pages == [1, 2, 3]
