#!/usr/bin/env python3
"""Consumer contract lag, grouped sync PR health and off-box monitor heartbeat.

The existing QA timer compares both active MCP units and deployed agentic with
Rails main and production. Alerts deduplicate by consumer, with one bounded
MiMo investigation and retained per-PR Plane mappings. The same entrypoint's
--watchman mode runs from the existing gen-prod surface timer, detecting a dead
QA timer independently. See docs/ops/CONTRACT-SYNC-MONITOR.md (GEN-11056).

No Gitea mutation, deployment, restart or customer write occurs here.
"""
from __future__ import annotations

import base64
import hashlib
import html
import inspect
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.environ.get("ALERT_POLICY_DIR", "/usr/local/bin"))
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
    except OSError as exc:
        print(f"LOG_WRITE_FAILED path={path} error={type(exc).__name__}", file=sys.stderr)


def compact(value: Any, limit: int = 1200) -> str:
    return " ".join(str(value).split())[:limit]


HOST = socket.gethostname()
SCRIPT = Path(__file__).name
STATE_DIR = Path(os.environ.get("CONTRACT_SYNC_PR_MONITOR_STATE_DIR", "/var/lib/contract-sync-pr-monitor"))
LOG_DIR = Path(os.environ.get("CONTRACT_SYNC_PR_MONITOR_LOG_DIR", "/var/log/contract-sync-pr-monitor"))
PROBE_LOG = LOG_DIR / "probe.log"
ALERT_LOG = LOG_DIR / "alerts.log"
TICKETS_FILE = STATE_DIR / "tickets.json"
INCIDENTS_FILE = STATE_DIR / "incidents.json"
COMPLETE_FILE = STATE_DIR / "last-complete.json"
RUN_TIMEOUT_S = 600
DEADLINE = float("inf")
GRACE_S = int(os.environ.get("CONTRACT_SYNC_LAG_GRACE_S", "3600"))
PRODUCTION_HOST = os.environ.get("CONTRACT_SYNC_PRODUCTION_HOST", "root@100.70.158.88")
QA_HOST = os.environ.get("CONTRACT_SYNC_QA_HOST", "root@100.71.21.27")
SYNC_STATE_DIR = Path(os.environ.get("CONTRACT_SYNC_STATE_DIR", "/var/lib/contract-sync"))

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


def remaining_timeout(cap: int = HTTP_TIMEOUT) -> float:
    remaining = DEADLINE - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("contract_sync_cycle_budget_exhausted")
    return min(cap, remaining)

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
    with urllib.request.urlopen(req, timeout=remaining_timeout()) as resp:
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
    with urllib.request.urlopen(req, timeout=remaining_timeout()) as resp:
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
    if DRY_RUN:
        print("[slack]", text)
        log(ALERT_LOG, f"tier=SLACK channel=slack status={'dry' if DRY_RUN else 'missing_token'} msg={json.dumps(text)}")
        return
    if not SLACK_TOKEN:
        raise RuntimeError("missing_SLACK_BOT_TOKEN")
    payload = {"channel": SLACK_CHANNEL, "text": text}
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=remaining_timeout()) as response:
        resp = json.load(response)
    status = "ok" if resp.get("ok") else str(resp.get("error"))
    log(ALERT_LOG, f"tier=SLACK channel=slack status={status} msg={json.dumps(text)}")
    if not resp.get("ok"):
        raise RuntimeError("slack_delivery_rejected:" + status)


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
# GEN-11056: active process identity is the deployment evidence boundary; see
# docs/ops/CONTRACT-SYNC-MONITOR.md. A checkout's main is never served evidence.
REMOTE_SERVICES = r'''
import hashlib, json, os, subprocess, time
from pathlib import Path

def read(unit, mcp):
    row = {"unit": unit, "contract_version": None, "producer_sha": None,
           "contract_hash": None, "deploy_sha": None, "commits_behind": None}
    try:
        pid = int(subprocess.check_output(["systemctl", "show", unit, "-p", "MainPID", "--value"], text=True, timeout=5))
        if pid <= 0:
            raise ValueError("unit_not_running")
        if mcp:
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            executable = Path(os.fsdecode(argv[0]))
            if not executable.is_absolute() or "/opt/gen-mcp-hosted/venvs/" not in str(executable):
                raise ValueError("active_venv_unverified")
            root = executable.parent.parent
            row["deploy_sha"] = (root / "DEPLOY_SHA").read_text().strip()
            if (root / "DEPLOY_COMPLETE").read_text().strip() != row["deploy_sha"]:
                raise ValueError("active_deploy_identity_mismatch")
            candidates = list((root / "lib").glob("python*/site-packages/gen_mcp_server/contracts"))
            if len(candidates) != 1:
                raise ValueError("active_contract_directory_not_unique")
            contracts = candidates[0]
            row["contract_version"] = json.loads((contracts / "vidsheet-action-schema.json").read_text())["contract_version"]
            row["producer_sha"] = (contracts / "vidsheet-operations-schema.source").read_text().strip()
        else:
            root = Path(f"/proc/{pid}/cwd").resolve(strict=True)
            row["deploy_sha"] = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, timeout=5).strip()
            contracts = root / "src/gen/contracts"
            # In-place agentic releases cannot prove loaded bytes after a file
            # changes. Refuse them rather than attributing disk to an older PID.
            mono = int(subprocess.check_output(["systemctl", "show", unit, "-p", "ExecMainStartTimestampMonotonic", "--value"], text=True, timeout=5)) / 1e6
            started = time.time() - float(Path("/proc/uptime").read_text().split()[0]) + mono
            if (contracts / "vidsheet-operations-schema.json").stat().st_mtime > started:
                raise ValueError("contract_modified_after_process_start")
            row["identity_error"] = "missing_agentic_contract_version_and_producer_sha"
        raw = (contracts / "vidsheet-operations-schema.json").read_bytes()
        document = json.loads(raw)
        if not document.get("paths") or not document.get("openapi", "").startswith("3."):
            raise ValueError("invalid_operations_contract")
        row["contract_hash"] = hashlib.sha256(raw).hexdigest()
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}:{exc}"
    return row
print(json.dumps({"gen-mcp-server": [read("gen-mcp-hosted@8090.service", True), read("gen-mcp-hosted@8092.service", True)],
                  "gen-agentic": [read("gen-agentic.service", False)]}))
'''

REMOTE_WATCHMAN = r'''
import json, subprocess
from pathlib import Path
path = Path("/var/lib/contract-sync-pr-monitor/last-complete.json")
result = {"completed_at": None}
if path.exists():
    result = json.loads(path.read_text())
for key, verb in (("timer_enabled", "is-enabled"), ("timer_active", "is-active")):
    result[key] = subprocess.run(["systemctl", verb, "--quiet", "contract-sync-pr-monitor.timer"], timeout=5).returncode == 0
print(json.dumps(result))
'''


def read_remote(host: str, code: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host,
         "python3 -c " + shlex.quote(code)],
        capture_output=True, text=True, timeout=remaining_timeout(35), check=True,
    )
    result = json.loads(proc.stdout)
    if not isinstance(result, dict):
        raise TypeError("invalid_remote_observation")
    return result


def read_watchman() -> dict[str, Any]:
    try:
        observation = read_remote(QA_HOST, REMOTE_WATCHMAN)
        for key in ("completed_at", "timer_enabled", "timer_active"):
            observation[key]
        if observation["completed_at"] is not None:
            epoch(observation["completed_at"])
        return observation
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        return {"completed_at": None, "timer_enabled": None, "timer_active": None,
                "error": "watchman_read_failed:" + type(exc).__name__}


def source_document(sha: str, path: str) -> tuple[dict[str, Any], str]:
    data = gitea_get(f"/api/v1/repos/GEN/gen-backend-v2/contents/{path}?ref={sha}")
    raw = base64.b64decode(data["content"])
    document = json.loads(raw)
    if not isinstance(document, dict) or not document:
        raise ValueError("empty_rails_contract:" + path)
    return document, hashlib.sha256(raw).hexdigest()


def collect_snapshot() -> dict[str, Any]:
    req = urllib.request.Request("http://127.0.0.1:9666/status")
    with urllib.request.urlopen(req, timeout=remaining_timeout()) as response:
        deploys = json.load(response)
    rails_deploy = deploys["gbv2-production"]
    deployed_sha = rails_deploy["last_deployed_sha"]
    main_sha = gitea_get("/api/v1/repos/GEN/gen-backend-v2/branches/main")["commit"]["id"]
    action, _ = source_document(deployed_sha, "docs/generated/vidsheet-action-schema.json")
    _, wire_hash = source_document(deployed_sha, "docs/generated/vidsheet-operations-schema.json")
    main_action, _ = source_document(main_sha, "docs/generated/vidsheet-action-schema.json")
    _, main_hash = source_document(main_sha, "docs/generated/vidsheet-operations-schema.json")
    installed = read_remote(PRODUCTION_HOST, REMOTE_SERVICES)
    consumers = []
    for name, target in (("gen-mcp-server", "gen-mcp-production"), ("gen-agentic", "gen-agentic")):
        deployment = deploys[target]
        rows = installed[name]
        for row in rows:
            if row["deploy_sha"] != deployment["last_deployed_sha"]:
                row["contract_hash"] = None
                row["error"] = "active_identity_disagrees_with_deployd"
            producer = row["producer_sha"]
            if producer is not None:
                comparison = gitea_get(f"/api/v1/repos/GEN/gen-backend-v2/compare/{producer}...{deployed_sha}")
                # A bounded API compare can be truncated: never report its list
                # length as an exact commit count without the server's total.
                row["commits_behind"] = comparison["total_commits"]
        consumers.append({"name": name, "instances": rows, "deploy": deployment})
    heartbeat_path = SYNC_STATE_DIR / "last-success.json"
    if heartbeat_path.exists():
        heartbeat = json.loads(heartbeat_path.read_text())
    else:
        heartbeat = {"last_success": None, "source_sha": None, "error": "missing_last_success"}
    result = dict(line.split("=", 1) for line in systemctl_show().splitlines() if "=" in line)
    heartbeat["result"] = result["Result"]
    return {"rails": {"main_sha": main_sha, "main_contract_version": main_action["contract_version"],
                      "main_contract_hash": main_hash, "deployed_sha": deployed_sha,
                      "contract_version": action["contract_version"], "contract_hash": wire_hash,
                      "deployed_at": rails_deploy["at"]},
            "consumers": consumers, "sync": heartbeat}


def epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError("invalid_required_timestamp")
    return parsed


def deploy_quiet(deploy: dict[str, Any], now: float) -> bool:
    return bool(deploy.get("deploying")) or deploy["last_status"] == "deploying" or now - epoch(deploy["at"]) < 300


def lag_evaluations(snapshot: dict[str, Any], now: float) -> list[dict[str, Any]]:
    rails = snapshot["rails"]
    age = max(0, now - epoch(rails["deployed_at"]))
    severity = "critical" if age >= 21600 else "warning"
    incidents = []
    for consumer in snapshot["consumers"]:
        if deploy_quiet(consumer["deploy"], now):
            continue
        rows = consumer["instances"]
        classification = "missing_instances" if not rows else "behind"
        bad = [row for row in rows if row["contract_hash"] is None
               or row["contract_hash"] != rails["contract_hash"]
               or (row["contract_version"] is not None and row["contract_version"] != rails["contract_version"])]
        if rows and not bad:
            continue
        row = bad[0] if bad else {}
        if rows and row["contract_hash"] is None:
            classification = "identity_error"
        unverified = classification in ("missing_instances", "identity_error")
        if age <= GRACE_S and not unverified:
            continue
        details = {"served_contract_version": row.get("contract_version"),
                   "served_contract_hash": row.get("contract_hash"),
                   "expected_contract_version": rails["contract_version"],
                   "expected_contract_hash": rails["contract_hash"],
                   "producer_sha": row.get("producer_sha"), "deployed_sha": rails["deployed_sha"],
                   "main_sha": rails["main_sha"], "commits_behind": row.get("commits_behind"),
                   "identity_error": unverified or row.get("contract_version") is None or row.get("producer_sha") is None,
                   "instances": rows, "prs": []}
        incidents.append({"consumer": consumer["name"], "classification": classification,
                          "severity": "critical" if unverified else severity, "age_s": age, "details": details})
    sync = snapshot["sync"]
    last_success = sync["last_success"]
    if (age > GRACE_S and sync["source_sha"] != rails["deployed_sha"]
            and (last_success is None or epoch(last_success) < epoch(rails["deployed_at"]))):
        incidents.append({"consumer": "contract-sync", "classification": "sync_stalled",
                          "severity": severity, "age_s": age, "details": {**sync, "deployed_sha": rails["deployed_sha"]}})
    return incidents


def watchman_evaluations(observation: dict[str, Any], now: float) -> list[dict[str, Any]]:
    completed = observation["completed_at"]
    age = None if completed is None else now - epoch(completed)
    if age is not None and 0 <= age <= 1800 and observation["timer_enabled"] and observation["timer_active"]:
        return []
    return [{"consumer": "contract-sync-monitor", "classification": "heartbeat_stale",
             "severity": "critical", "age_s": age, "details": {**observation, "offbox": True}}]


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True))
    temporary.replace(path)


def rca_evidence(item: dict[str, Any]) -> dict[str, Any]:
    try:
        with PROBE_LOG.open("rb") as stream:
            stream.seek(max(0, PROBE_LOG.stat().st_size - 2048))
            probe_tail = stream.read(2048).decode("utf-8", errors="replace")
    except FileNotFoundError:
        probe_tail = "probe_log_missing"
    return {"observation": item, "config": {"lag_grace_s": GRACE_S, "rca_timeout_s": 20,
            "run_timeout_s": RUN_TIMEOUT_S, "deploy_grace_s": 300}, "probe_tail": probe_tail,
            "monitor_source": inspect.getsource(lag_evaluations) + inspect.getsource(watchman_evaluations),
            "boundary": "Read-only evidence; advise only. Never merge, deploy or change customer data."}


def reconcile(evaluations: list[dict[str, Any]], now: float, *, held: set[str] | None = None) -> list[dict[str, Any]]:
    state = json.loads(INCIDENTS_FILE.read_text()) if INCIDENTS_FILE.exists() else {}
    active = {item["consumer"] for item in evaluations}
    try:
        atomic_json(INCIDENTS_FILE, state)
    except OSError:
        log(ALERT_LOG, "STATE_WRITE_FAILED alerts=suppressed")
        raise
    for consumer in list(state):
        if consumer in active or consumer in (held or set()):
            continue
        entry = state[consumer]
        if entry["posted"]:
            slack_post(f"STATUS: CONTRACT_SYNC_RECOVERED\nSERVICE: {consumer}\n"
                       "SELF_HEAL: verified contract probe recovered; no production mutation\n"
                       + ap.post_decision("noise reduced/settings changed", "resolved prior incident; healthy repeats suppressed")
                       + POLICY.footer(HOST, SCRIPT, str(ALERT_LOG), str(STATE_DIR)))
        del state[consumer]
        atomic_json(INCIDENTS_FILE, state)
    for item in evaluations:
        consumer = item["consumer"]
        entry = state.setdefault(consumer, {"posted": False, "posted_severity": None, "ticket_url": None})
        # PR changes and Rails heads are evidence, never incident keys (GEN-11056).
        fp = ap.fingerprint("contract-sync", consumer)
        # Policy records a decision before transport acceptance; a rejected
        # escalation must retain retry authority (GEN-11056).
        delivered = entry["posted"] and entry["posted_severity"] == item["severity"] and not DRY_RUN
        post, reason = POLICY.should_post("contract-sync:" + consumer, fp, severity=item["severity"], affected=0,
                                         cadence_seconds=CADENCE_S if delivered else 0)
        log(PROBE_LOG, f"consumer={consumer} classification={item['classification']} age_s={item['age_s']} policy={reason}")
        if not post:
            atomic_json(INCIDENTS_FILE, state)
            continue
        evidence = json.dumps(item, sort_keys=True)
        if item["severity"] == "critical" and entry["ticket_url"] is None and not DRY_RUN:
            issue = plane_create_issue({"name": f"Contract consumer incident: {consumer}",
                                        "description_html": "<pre>" + html.escape(evidence) + "</pre>"})
            entry["ticket_url"] = issue_url(issue)
            atomic_json(INCIDENTS_FILE, state)
        rca = "mimo_rca=skipped:dry_run" if DRY_RUN else POLICY.mimo_rca(rca_evidence(item), timeout=int(remaining_timeout(20)))
        prs = item["details"].get("prs", [])
        pr_lines = [f"#{p['pr']['number']} {p['pr']['html_url']} {p['classification']} checks="
                    + json.dumps(p["failing"]) for p in prs]
        status = item["classification"].upper()
        if status in ("RED", "CONFLICT", "STALE"):
            status = "PR_" + status
        age_text = "unverified" if item["age_s"] is None else f"{item['age_s']:.0f}s ({item['age_s'] / 3600:.2f}h)"
        body = "\n".join([
            "STATUS: CONTRACT_SYNC_" + status, "SERVICE: " + consumer,
            "SEVERITY: " + item["severity"], "AGE: " + age_text,
            "SURFACE: GEN creation cards and Vidsheets consumers",
            "SELF_HEAL: checked:no_safe_auto_fix_mapped; merging/deploying requires review",
            "EVIDENCE: " + evidence, *pr_lines, "RCA: " + rca,
            "NEXT: repair blocking sync checks and deploy the canonical contract; verify missing identity stamps",
            ap.post_decision(ap.DECISION_BUG if entry["ticket_url"] else "no safe fix, evidence attached, exact next step",
                             entry["ticket_url"] or ("dry run; no ticket created" if DRY_RUN else "one consumer incident; six-hour escalation pending")),
        ])
        slack_post(body + POLICY.footer(HOST, SCRIPT, str(ALERT_LOG), str(STATE_DIR)))
        entry["posted"] = not DRY_RUN
        entry["posted_severity"] = item["severity"] if not DRY_RUN else None
        atomic_json(INCIDENTS_FILE, state)
    return evaluations


def collect_prs() -> dict[str, list[dict[str, Any]]]:
    grouped = {}
    for repo in REPOS:
        prs = [p for p in gitea_list_open_prs(repo)
               if p["head"]["ref"].startswith(BRANCH_PREFIX)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(lambda p, repo=repo: gitea_statuses(repo, p["head"]["sha"]), prs))
        grouped[repo.split("/")[1]] = []
        for pr, checks in zip(prs, statuses):
            classification, failing = classify(pr, checks, STALE_HOURS)
            if classification != "OK":
                grouped[repo.split("/")[1]].append({
                    "pr": {**{key: pr[key] for key in ("number", "html_url", "title", "created_at")},
                           "head": {key: pr["head"][key] for key in ("ref", "sha")}},
                    "classification": classification,
                    "failing": [{key: check[key] for key in ("context", "status", "target_url")} for check in failing],
                })
    return grouped


def cycle() -> dict[str, Any]:
    global DEADLINE
    DEADLINE = time.monotonic() + 480
    try:
        snapshot = collect_snapshot()
        grouped = collect_prs()
        now = time.time()
        evaluations = lag_evaluations(snapshot, now)
        by_consumer = {item["consumer"]: item for item in evaluations}
        held = {c["name"] for c in snapshot["consumers"] if deploy_quiet(c["deploy"], now)}
        # Within Rails grace, PR failures remain evidence, not premature pages.
        grace = now - epoch(snapshot["rails"]["deployed_at"]) <= GRACE_S
        if grace:
            held.update(c["name"] for c in snapshot["consumers"] if any(
                row["contract_hash"] != snapshot["rails"]["contract_hash"] for row in c["instances"]))
        for consumer, prs in grouped.items():
            if consumer in held or not prs or grace:
                continue
            for item in prs:
                ensure_ticket("GEN/" + consumer, item["pr"], item["classification"], item["pr"]["head"]["sha"])
            if consumer not in by_consumer:
                incident = {"consumer": consumer, "classification": prs[0]["classification"],
                            "severity": "warning", "age_s": now - epoch(snapshot["rails"]["deployed_at"]), "details": {"prs": prs}}
                evaluations.append(incident)
                by_consumer[consumer] = incident
            by_consumer[consumer]["details"]["prs"] = prs
        findings = reconcile(evaluations, now, held=held)
        atomic_json(COMPLETE_FILE, {"completed_at": ts()})
        log(PROBE_LOG, f"result=complete consumers={len(findings)} remaining_s={int(remaining_timeout(480))}")
        return {"findings": findings, "snapshot": snapshot}
    finally:
        DEADLINE = float("inf")


def watchman_cycle() -> dict[str, Any]:
    global DEADLINE
    DEADLINE = time.monotonic() + 90
    try:
        return {"findings": reconcile(watchman_evaluations(read_watchman(), time.time()), time.time())}
    finally:
        DEADLINE = float("inf")


if __name__ == "__main__":
    if DRY_RUN and (STATE_DIR == Path("/var/lib/contract-sync-pr-monitor")
                    or LOG_DIR == Path("/var/log/contract-sync-pr-monitor")):
        raise ValueError("dry_run_requires_isolated_state_and_log_dirs")
    result = watchman_cycle() if "--watchman" in sys.argv else cycle()
    if DRY_RUN:
        print(json.dumps(result, indent=2))
