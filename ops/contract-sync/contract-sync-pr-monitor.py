#!/usr/bin/env python3
"""contract-sync PR monitor (Gitea).

Canonical source-of-truth for the monitor that runs on qa-deploy-box as
``/usr/local/bin/contract_sync_pr_monitor.py`` (systemd timer
``contract-sync-pr-monitor.timer``, every 10 min).

``contract-sync`` (a systemd timer) opens PRs titled
``chore: sync backend contracts to gen-backend-v2 <sha8>`` on Gitea
(https://git.gen.pro) in GEN/gen-mcp-server, GEN/gen-agentic, GEN/limitless-fe
and GEN/api-docs, on branches named ``auto/contract-sync-<sha8>``. It merges
them when CI is green; when CI is red it leaves the PR open and its only signal
is a failed systemd unit that nobody sees.

This monitor closes that gap: every cycle it lists the open PRs whose head
branch starts with ``auto/contract-sync-``, reads the head sha's commit statuses
(newest per ``ci/woodpecker/*`` context, Gitea field ``status`` not ``state``),
classifies each PR RED / CONFLICT / STALE / OK, and for RED, CONFLICT or STALE it
(a) creates exactly one Plane ticket per PR (commenting only when the head sha
changes), and (b) posts a Slack #errors message in the shared alert_policy
format with a MiMo-only advise RCA and exactly one ``DECISION:`` footer.

Read-only: it never mutates Gitea or production. Every network side effect goes
through the module-level functions below so tests can monkeypatch them.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alert_policy as ap  # noqa: E402  -- shared policy module (GEN-3827..3829)


def secret_value(raw: str, *keys: str) -> str:
    """Accept scalar or Infisical JSON-object secrets without logging them."""
    raw = (raw or "").strip()
    if not raw or not raw.startswith("{"):
        return raw
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        if len(data) == 1:
            value = next(iter(data.values()))
            if isinstance(value, str):
                return value
    return raw


def ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(path: Path, line: str) -> None:
    try:
        ensure_dirs()
        with path.open("a") as f:
            f.write(f"ts={ts()} host={HOST} {line}\n")
    except Exception:
        pass


def compact(value: Any, limit: int = 1200) -> str:
    return " ".join(str(value).split())[:limit]


HOST = socket.gethostname()
SCRIPT = Path(__file__).name
STATE_DIR = Path(os.environ.get("CONTRACT_SYNC_PR_MONITOR_STATE_DIR", "/var/lib/contract-sync-pr-monitor"))
LOG_DIR = Path(os.environ.get("CONTRACT_SYNC_PR_MONITOR_LOG_DIR", "/var/log/contract-sync-pr-monitor"))
PROBE_LOG = LOG_DIR / "probe.log"
ALERT_LOG = LOG_DIR / "alerts.log"
TICKETS_FILE = STATE_DIR / "tickets.json"

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

CADENCE_S = int(os.environ.get("CONTRACT_SYNC_PR_MONITOR_CADENCE_S", "86400"))
POLICY = ap.AlertPolicy(state_dir=STATE_DIR, cadence_seconds=CADENCE_S)

# Gitea auth is ``Authorization: token <GITEA_TOKEN>`` (not Bearer).
GITEA_BASE = os.environ.get("GITEA_BASE", "https://git.gen.pro").rstrip("/")
GITEA_TOKEN = secret_value(os.environ.get("GITEA_TOKEN", ""), "token", "value")

PLANE_BASE = os.environ.get("PLANE_BASE", "https://plane.gen.pro").rstrip("/")
PLANE_API_KEY = secret_value(os.environ.get("PLANE_API_KEY", ""), "token", "value", "api_key")
PLANE_WORKSPACE = os.environ.get("PLANE_WORKSPACE", "gen")
PLANE_PROJECT_ID = os.environ.get("PLANE_PROJECT_ID", "a5aea607-62c9-430a-bc93-d46dce835f1e")

SLACK_TOKEN = secret_value(os.environ.get("SLACK_BOT_TOKEN", ""), "value", "slack_bot_token", "bot_token")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL_ID", "C0AKUTQ4T1D").strip()

STALE_HOURS = int(os.environ.get("STALE_HOURS", "2"))
HTTP_TIMEOUT = 15  # every HTTP call: 15s or less

REPOS = [
    r.strip()
    for r in os.environ.get(
        "CONTRACT_SYNC_PR_MONITOR_REPOS",
        "GEN/gen-mcp-server,GEN/gen-agentic,GEN/limitless-fe,GEN/api-docs",
    ).split(",")
    if r.strip()
]

BRANCH_PREFIX = "auto/contract-sync-"
FAILURE_STATES = {"failure", "error"}
PENDING_STATES = {"pending", "running", "in_progress", "queued", "unknown"}
MAX_FILES = 30


# --------------------------------------------------------------------------- #
# Network / side-effect functions -- the only places that touch the outside
# world, and the only functions tests monkeypatch.
# --------------------------------------------------------------------------- #
def gitea_get(path: str) -> Any:
    if not GITEA_TOKEN:
        raise RuntimeError("missing GITEA_TOKEN")
    req = urllib.request.Request(
        GITEA_BASE + path,
        headers={
            "Accept": "application/json",
            "Authorization": f"token {GITEA_TOKEN}",
            "User-Agent": "gen-contract-sync-pr-monitor/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.load(resp)


def plane_post(path: str, payload: dict[str, Any]) -> Any:
    if DRY_RUN:
        log(ALERT_LOG, f"tier=PLANE channel=plane status=dry path={path}")
        return {"id": "dry-run", "sequence_id": 0}
    if not PLANE_API_KEY:
        raise RuntimeError("missing PLANE_API_KEY")
    req = urllib.request.Request(
        PLANE_BASE + path,
        data=json.dumps(payload).encode(),
        headers={
            "X-API-Key": PLANE_API_KEY,
            "User-Agent": "gen-monitor/1.0",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.load(resp)


def gitea_list_open_prs(repo: str) -> list[dict[str, Any]]:
    data = gitea_get(f"/api/v1/repos/{repo}/pulls?state=open")
    return data if isinstance(data, list) else []


def gitea_statuses(repo: str, sha: str) -> list[dict[str, Any]]:
    data = gitea_get(f"/api/v1/repos/{repo}/commits/{sha}/statuses")
    return data if isinstance(data, list) else []


def gitea_pr_files(repo: str, number: int) -> list[dict[str, Any]]:
    data = gitea_get(f"/api/v1/repos/{repo}/pulls/{number}/files")
    return data if isinstance(data, list) else []


def plane_create_issue(payload: dict[str, Any]) -> dict[str, Any]:
    path = f"/api/v1/workspaces/{PLANE_WORKSPACE}/projects/{PLANE_PROJECT_ID}/issues/"
    return plane_post(path, payload)


def plane_create_comment(issue_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    path = f"/api/v1/workspaces/{PLANE_WORKSPACE}/projects/{PLANE_PROJECT_ID}/issues/{issue_id}/comments/"
    return plane_post(path, payload)


def slack_post(text: str) -> None:
    if DRY_RUN or not SLACK_TOKEN:
        print("[slack]", text)
        log(ALERT_LOG, f"tier=SLACK channel=slack status={'dry' if DRY_RUN else 'missing_token'} msg={json.dumps(text)}")
        return
    payload = {"channel": SLACK_CHANNEL, "text": text}
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=HTTP_TIMEOUT))
        status = "ok" if resp.get("ok") else str(resp.get("error"))
        log(ALERT_LOG, f"tier=SLACK channel=slack status={status} msg={json.dumps(text)}")
    except Exception as exc:
        log(ALERT_LOG, f"tier=SLACK channel=slack status=exc:{exc} msg={json.dumps(text)}")


def systemctl_show() -> str:
    """Read the contract-sync.service result/active state (monkeypatchable)."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "contract-sync.service", "-p", "Result", "-p", "ActiveState"],
            capture_output=True, text=True, timeout=10,
        )
        return (out.stdout or out.stderr or "").strip()
    except Exception as exc:
        return f"systemctl_error:{type(exc).__name__}"


# --------------------------------------------------------------------------- #
# Classification (newest entry per context is load-bearing -- see GEN-3834).
# --------------------------------------------------------------------------- #
def parse_time(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def status_time(entry: dict[str, Any]) -> float | None:
    for field in ("updated_at", "created_at", "timestamp"):
        t = parse_time(entry.get(field))
        if t is not None:
            return t
    return None


def newest_per_context(statuses: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Take the NEWEST entry per ``context`` by timestamp; on a tie (or missing
    timestamp) keep the first seen, since the API returns newest-first."""
    by_ctx: dict[str, dict[str, Any]] = {}
    for entry in statuses:
        ctx = entry.get("context")
        if not ctx:
            continue
        if ctx not in by_ctx:
            by_ctx[ctx] = entry
            continue
        t = status_time(entry)
        prev_t = status_time(by_ctx[ctx])
        if t is not None and (prev_t is None or t > prev_t):
            by_ctx[ctx] = entry
    return by_ctx


def classify(pr: dict[str, Any], statuses: list[dict[str, Any]], stale_hours: int) -> tuple[str, list[dict[str, Any]]]:
    # A conflict is a hard stop the same way a red check is: Gitea reports it as
    # mergeable=false on an otherwise green PR, and it never becomes OK on its
    # own (GEN-9537). RED wins when checks are also failing.
    mergeable = pr.get("mergeable")
    woody = {ctx: e for ctx, e in newest_per_context(statuses).items() if ctx.startswith("ci/woodpecker/")}
    failing = [e for e in woody.values() if (e.get("status") or "").lower() in FAILURE_STATES]
    if failing:
        return "RED", failing
    if mergeable is False:
        return "CONFLICT", []

    created_ts = parse_time(pr.get("created_at")) or 0.0
    age_hours = (time.time() - created_ts) / 3600.0

    if not woody:
        # No status at all: stale only past the window.
        return ("STALE", []) if age_hours > stale_hours else ("OK", [])

    pending = [e for e in woody.values() if (e.get("status") or "").lower() in PENDING_STATES]
    if pending:
        return ("STALE", pending) if age_hours > stale_hours else ("OK", [])
    return "OK", []


def fingerprint(repo: str, number: Any, head_sha: str, classification: str) -> str:
    return ap.fingerprint(repo, str(number), classification)


# --------------------------------------------------------------------------- #
# Plane ticket bookkeeping (exactly one ticket per PR, comment on sha change).
# --------------------------------------------------------------------------- #
def load_tickets() -> dict[str, Any]:
    try:
        data = json.loads(TICKETS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_tickets(data: dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = TICKETS_FILE.with_suffix(TICKETS_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(TICKETS_FILE)
    except Exception:
        pass


def issue_url(issue: dict[str, Any]) -> str:
    seq = issue.get("sequence_id")
    ident = str(seq) if seq is not None else str(issue.get("id"))
    return f"{PLANE_BASE}/gen/projects/{PLANE_PROJECT_ID}/issues/{ident}"


def ticket_kind(classification: str) -> str:
    if classification == "RED":
        return "failing CI"
    if classification == "CONFLICT":
        return "not mergeable (conflict with the base branch)"
    return "stale (CI pending or no status)"


def ticket_description(repo: str, pr: dict[str, Any], classification: str, head_sha: str) -> str:
    number = pr.get("number")
    pr_url = str(pr.get("html_url") or "")
    title = str(pr.get("title") or "")
    kind = ticket_kind(classification)
    return (
        f"<p>The contract-sync bot opened PR <a href=\"{pr_url}\">{repo}#{number}</a> "
        f"(\"{title}\") to sync backend contracts, and its CI is {kind}.</p>"
        f"<p>Head sha: <code>{head_sha}</code>. The consumer repository stays out of date "
        f"until this PR is fixed and merged.</p>"
        f"<p>PR: {pr_url}</p>"
    )


def ticket_comment(repo: str, pr: dict[str, Any], classification: str, head_sha: str) -> str:
    number = pr.get("number")
    pr_url = str(pr.get("html_url") or "")
    kind = ticket_kind(classification)
    return (
        f"<p>The head sha changed to <code>{head_sha}</code> and the PR is still {kind}. "
        f"The consumer repository remains out of date. PR: {pr_url}</p>"
    )


def ensure_ticket(repo: str, pr: dict[str, Any], classification: str, head_sha: str) -> str | None:
    number = pr.get("number")
    pr_url = str(pr.get("html_url") or "") or f"{GITEA_BASE}/{repo}/pulls/{number}"
    tickets = load_tickets()
    entry = tickets.get(pr_url)
    if entry is None:
        try:
            title_kind = "conflict" if classification == "CONFLICT" else "red"
            issue = plane_create_issue({
                "name": f"contract-sync PR {title_kind}: {repo}#{number}",
                "description_html": ticket_description(repo, pr, classification, head_sha),
            })
            tickets[pr_url] = {"issue_id": issue.get("id"), "head_sha": head_sha, "url": issue_url(issue)}
            save_tickets(tickets)
            log(PROBE_LOG, f"repo={repo} pr={number} plane_ticket=created issue={issue.get('id')}")
            return tickets[pr_url]["url"]
        except Exception as exc:
            log(ALERT_LOG, f"tier=PLANE channel=plane status=exc:{exc} repo={repo} pr={number} msg='ticket create failed'")
            return None
    if entry.get("head_sha") != head_sha:
        try:
            plane_create_comment(entry.get("issue_id"), {"comment_html": ticket_comment(repo, pr, classification, head_sha)})
            entry["head_sha"] = head_sha
            save_tickets(tickets)
            log(PROBE_LOG, f"repo={repo} pr={number} plane_ticket=commented issue={entry.get('issue_id')}")
        except Exception as exc:
            log(ALERT_LOG, f"tier=PLANE channel=plane status=exc:{exc} repo={repo} pr={number} msg='ticket comment failed'")
        return entry.get("url")
    log(PROBE_LOG, f"repo={repo} pr={number} plane_ticket=existing issue={entry.get('issue_id')}")
    return entry.get("url")


# --------------------------------------------------------------------------- #
# Evidence + message.
# --------------------------------------------------------------------------- #
def build_evidence(repo: str, pr: dict[str, Any], classification: str,
                   failing: list[dict[str, Any]], systemd: str) -> str:
    number = pr.get("number")
    head_sha = str((pr.get("head") or {}).get("sha") or "")
    parts = [
        f"pr={pr.get('html_url')}",
        f"title={compact(pr.get('title'))}",
        f"repo={repo}",
        f"number={number}",
        f"head_sha={head_sha}",
        f"classification={classification}",
        f"mergeable={pr.get('mergeable')}",
    ]
    if failing:
        parts.append("failing_contexts=" + "; ".join(
            f"{e.get('context')}={e.get('status')} target={e.get('target_url')}" for e in failing
        ))
    else:
        parts.append("failing_contexts=none")
    if systemd:
        parts.append(f"systemd={compact(systemd, 200)}")

    files: list[str] = []
    try:
        files = [str(f.get("filename")) for f in gitea_pr_files(repo, number)[:MAX_FILES] if f.get("filename")]
    except Exception as exc:
        log(ALERT_LOG, f"tier=SELF channel=gitea status=files_error repo={repo} pr={number} msg={json.dumps(str(exc))}")
    parts.append(f"changed_files={json.dumps(files[:MAX_FILES])}")
    return " | ".join(parts)


def build_message(repo: str, pr: dict[str, Any], classification: str,
                  failing: list[dict[str, Any]], systemd: str, ticket_url: str | None,
                  evidence: str, rca: str) -> str:
    number = pr.get("number")
    head_sha = str((pr.get("head") or {}).get("sha") or "")[:8]
    pr_url = str(pr.get("html_url") or "")
    if classification == "RED":
        status = "CONTRACT_SYNC_PR_RED"
    elif classification == "CONFLICT":
        status = "CONTRACT_SYNC_PR_CONFLICT"
    else:
        status = "CONTRACT_SYNC_PR_STALE"
    if failing:
        issue = ", ".join(f"{e.get('context')}={e.get('status')}" for e in failing)
    elif classification == "CONFLICT":
        issue = "PR is not mergeable against its base branch (mergeable=false)"
    elif classification == "STALE":
        issue = "ci/woodpecker pending or no status beyond stale window"
    else:
        issue = "unknown"
    details = f"repo={repo} pr={number} head_sha={head_sha} classification={classification}"
    if systemd:
        details += f" systemd={compact(systemd, 120)}"
    if ticket_url:
        decision = ap.post_decision(ap.DECISION_BUG, ticket_url)
    else:
        decision = ap.post_decision(ap.DECISION_BUG, "ticket could not be created (Plane API error)")
    return "\n".join([
        f"STATUS: {status}",
        f"SERVICE: contract-sync:{repo}#{number}",
        "SEVERITY: warning",
        f"ISSUE: {issue}",
        f"DETAILS: {details}",
        "SELF_HEAL: no_safe_auto_fix_mapped",
        f"RCA: {rca}",
        f"EVIDENCE: {compact(evidence, 1200)}",
        "NEXT: " + ("resolve the conflict with the base branch; consumer stays out of date until the PR merges"
                  if classification == "CONFLICT"
                  else "fix the failing CI gate; consumer stays out of date until the PR merges"),
        decision,
        f"URL: {pr_url}",
        f"SOURCE: probe={HOST}:{SCRIPT}",
    ])


# --------------------------------------------------------------------------- #
# Main cycle.
# --------------------------------------------------------------------------- #
def cycle() -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    systemd = systemctl_show()
    log(PROBE_LOG, f"systemd={json.dumps(systemd)}")

    if not GITEA_TOKEN:
        log(ALERT_LOG, "tier=SELF channel=gitea status=missing_token msg='GITEA_TOKEN missing'")
        return {"findings": [], "error": "missing_gitea_token"}

    saw_contract_pr = False
    for repo in REPOS:
        try:
            prs = gitea_list_open_prs(repo)
        except Exception as exc:
            log(ALERT_LOG, f"tier=SELF channel=gitea status=repo_error repo={repo} msg={json.dumps(str(exc))}")
            continue
        for pr in prs:
            branch = str((pr.get("head") or {}).get("ref") or "")
            if not branch.startswith(BRANCH_PREFIX):
                continue
            saw_contract_pr = True
            number = pr.get("number")
            head_sha = str((pr.get("head") or {}).get("sha") or "")
            try:
                statuses = gitea_statuses(repo, head_sha)
            except Exception as exc:
                log(ALERT_LOG, f"tier=SELF channel=gitea status=statuses_error repo={repo} pr={number} msg={json.dumps(str(exc))}")
                continue
            classification, failing = classify(pr, statuses, STALE_HOURS)
            log(PROBE_LOG, f"repo={repo} pr={number} branch={branch} head_sha={head_sha} classification={classification} statuses={len(statuses)}")
            if classification == "OK":
                continue

            fp = fingerprint(repo, number, head_sha, classification)
            slot = f"contract-sync:{repo}:{number}"
            findings.append({"repo": repo, "number": number, "classification": classification, "fp": fp})
            ticket_url = ensure_ticket(repo, pr, classification, head_sha)
            post, why = POLICY.should_post(slot, fp, severity="warning", affected=0)
            if not post:
                log(PROBE_LOG, f"suppressed slot={slot} reason={why} fp={fp[:12]}")
                continue

            evidence = build_evidence(repo, pr, classification, failing, systemd)
            rca = POLICY.mimo_rca(evidence)  # advise-only; MiMo is the ONLY rca provider
            body = build_message(repo, pr, classification, failing, systemd, ticket_url, evidence, rca)
            if ap.is_routine_recovery(body) or ap.is_healthy_digest(body):
                log(PROBE_LOG, f"skip_recovery slot={slot}")
                continue
            slack_post(body + POLICY.footer(HOST, SCRIPT, str(ALERT_LOG), str(STATE_DIR)))

    if not saw_contract_pr:
        log(PROBE_LOG, "result=all_clear")
    return {"findings": findings}


if __name__ == "__main__":
    result = cycle()
    if DRY_RUN:
        print(json.dumps(result, indent=2))
