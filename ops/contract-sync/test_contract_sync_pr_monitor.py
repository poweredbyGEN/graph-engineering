#!/usr/bin/env python3
"""Offline tests for contract-sync-pr-monitor.py (GEN-9537).

intent: a green PR that cannot merge (Gitea ``mergeable=false``) is a conflict,
not a healthy sync. The monitor must classify it CONFLICT and alert, never OK --
otherwise a conflicted sync PR is reported green and sits until a human notices.

Run directly (no network, no Gitea, no Plane):

    python3 ops/contract-sync/test_contract_sync_pr_monitor.py
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
MONITOR = HERE / "contract-sync-pr-monitor.py"
_MODULE = None

# Newest-per-context wins by timestamp: the ci/woodpecker/push/ci context
# resolves to the newer success; the older failure must not make this red.
GREEN = [
    {"id": 1, "context": "ci/woodpecker/push/ci", "status": "failure", "created_at": "2026-10-09T00:00:00Z"},
    {"id": 3, "context": "ci/woodpecker/push/ci", "status": "success", "created_at": "2026-10-10T00:00:00Z"},
]
RED = [
    {"id": 1, "context": "ci/woodpecker/push/ci", "status": "failure", "created_at": "2026-10-10T00:00:00Z"},
]


def _stub_alert_policy() -> None:
    """The monitor imports the box's shared alert_policy module; stub it so the
    classification and message logic run without that deployment artifact."""
    if "alert_policy" in sys.modules:
        return
    stub = types.ModuleType("alert_policy")

    class _Policy:
        def __init__(self, *args, **kwargs):
            pass

        def should_post(self, *args, **kwargs):
            return True, "test"

        def mimo_rca(self, evidence):
            return "test rca"

        def footer(self, *args, **kwargs):
            return "DECISION: test"

    stub.AlertPolicy = _Policy
    stub.fingerprint = lambda *a, **k: "fp"
    stub.DECISION_BUG = "bug"
    stub.post_decision = lambda *a, **k: "DECISION: open a bug"
    stub.is_routine_recovery = lambda body: False
    stub.is_healthy_digest = lambda body: False
    sys.modules["alert_policy"] = stub


def load_monitor():
    global _MODULE
    if _MODULE is not None:
        return _MODULE
    tmp = tempfile.mkdtemp(prefix="contract-sync-monitor-test-")
    os.environ.setdefault("CONTRACT_SYNC_PR_MONITOR_STATE_DIR", tmp + "/state")
    os.environ.setdefault("CONTRACT_SYNC_PR_MONITOR_LOG_DIR", tmp + "/log")
    _stub_alert_policy()
    spec = importlib.util.spec_from_file_location("contract_sync_pr_monitor", MONITOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _MODULE = module
    return module


def _pr(**overrides):
    pr = {
        "number": 7,
        "html_url": "http://git.invalid/GEN/gen-agentic/pulls/7",
        "title": "chore: sync backend contracts",
        "created_at": "2026-10-10T00:00:00Z",
        "head": {"ref": "auto/contract-sync-deadbeef", "sha": "b" * 40},
    }
    pr.update(overrides)
    return pr


def test_green_mergeable_false_is_conflict():
    m = load_monitor()
    classification, failing = m.classify(_pr(mergeable=False), GREEN, 2)
    assert classification == "CONFLICT", classification
    assert failing == [], failing


def test_green_mergeable_true_is_ok():
    m = load_monitor()
    classification, _ = m.classify(_pr(mergeable=True), GREEN, 2)
    assert classification == "OK", classification


def test_green_unknown_mergeable_is_ok():
    m = load_monitor()
    classification, _ = m.classify(_pr(mergeable=None), GREEN, 2)
    assert classification == "OK", classification


def test_red_beats_conflict():
    m = load_monitor()
    classification, failing = m.classify(_pr(mergeable=False), RED, 2)
    assert classification == "RED", classification
    assert failing, "red must report its failing contexts"


def test_conflict_message_is_alerted_like_red():
    m = load_monitor()
    body = m.build_message("GEN/gen-agentic", _pr(mergeable=False), "CONFLICT", [], "", None, "evidence", "rca")
    assert "STATUS: CONTRACT_SYNC_PR_CONFLICT" in body, body
    assert "SEVERITY: warning" in body, body
    assert "DECISION:" in body, body


def test_conflict_cycle_alerts():
    m = load_monitor()
    posted = []
    pr = _pr(mergeable=False)
    m.GITEA_TOKEN = "test-token"
    m.REPOS = ["GEN/gen-agentic"]
    m.gitea_list_open_prs = lambda repo: [pr]
    m.gitea_statuses = lambda repo, sha: GREEN
    m.systemctl_show = lambda: ""
    m.ensure_ticket = lambda *a, **k: None
    m.build_evidence = lambda *a, **k: "evidence"
    m.slack_post = lambda text: posted.append(text)
    result = m.cycle()
    assert any(f["classification"] == "CONFLICT" for f in result["findings"]), result
    assert posted, "no alert posted for a conflict"
    assert "CONTRACT_SYNC_PR_CONFLICT" in posted[0], posted[0]


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  ok    {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL  {test.__name__}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\nFAIL: {failures} monitor test(s) failed")
        return 1
    print(f"\nPASS: contract-sync PR monitor test ({len(tests)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
