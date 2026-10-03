#!/bin/bash
# Offline test for contract-sync.sh.
#
# intent: a sync that quietly stops running, or that merges without a green check, looks
# exactly like a backend that never changed. These cases pin the behaviour that must hold:
# only a watched backend path triggers a consumer, a converged consumer is a no-op, a diff
# becomes a branch/commit/PR with a fast-forward-only merge, a red check never merges, a hard
# failure leaves the change queued, a multi-step regeneration stops at the first failure, and
# the API token is never printed.
#
# Everything runs against bare repositories created under $TEST_ROOT, with
# CONTRACT_SYNC_GIT_BASE pointed at them, so the change detection, the
# regeneration, the diff and the planned PR/merge all execute for real. DRY_RUN
# stops short of push/PR/merge, which is the part that needs a live Gitea; the
# live path is exercised separately against a local mock forge.
#
#   bash ops/contract-sync/test_contract_sync.sh
#
# Exits non-zero if any case fails.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC="$HERE/contract-sync.sh"
TEST_ROOT="${CONTRACT_SYNC_TEST_ROOT:-/mnt/data/tmp/drift-lanes/autosync-test}"
OWNER="GEN"
GIT_BASE="file://$TEST_ROOT"
STATE_DIR="$TEST_ROOT/state"
BE_URL="$GIT_BASE/$OWNER/gen-backend-v2.git"
BE_BARE="$TEST_ROOT/$OWNER/gen-backend-v2.git"
CONSUMER_BARE="$TEST_ROOT/$OWNER/gen-agentic.git"
WORK="$TEST_ROOT/work"
BE_WORK="$WORK/be"
CONSUMER_WORK="$WORK/consumer"
MCP_BARE="$TEST_ROOT/$OWNER/gen-mcp-server.git"
MCP_WORK="$WORK/mcp"
MCP_RECORD="src/gen_mcp_server/contracts/catalog-record"
export CONTRACT_SYNC_MCP_URL="$GIT_BASE/$OWNER/gen-mcp-server.git"
OUT="$TEST_ROOT/sync-output.log"
TOKEN_SENTINEL="contract-sync-test-token-value-that-must-never-be-printed"
FAILURES=0

pass() { printf '  ok    %s\n' "$1"; }
fail() {
  printf '  FAIL  %s\n' "$1"
  FAILURES=$((FAILURES + 1))
}

assert_contains() { # <label> <haystack> <needle>
  case "$2" in
    *"$3"*) pass "$1" ;;
    *) fail "$1 (missing: $3)" ;;
  esac
}

assert_not_contains() { # <label> <haystack> <needle>
  case "$2" in
    *"$3"*) fail "$1 (unexpected: $3)" ;;
    *) pass "$1" ;;
  esac
}

assert_empty() { # <label> <value>
  if [ -z "$2" ]; then pass "$1"; else fail "$1 (expected empty, got: $2)"; fi
}

assert_equal() { # <label> <expected> <actual>
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 (expected: $2, got: $3)"; fi
}

# Same invocation the systemd unit makes, minus the network: DRY_RUN=1 with the
# consumer set narrowed to the fake repo.
run_sync() {
  CONTRACT_SYNC_STATE_DIR="$STATE_DIR" \
    CONTRACT_SYNC_GIT_BASE="$GIT_BASE" \
    CONTRACT_SYNC_OWNER="$OWNER" \
    CONTRACT_SYNC_BACKEND_URL="$BE_URL" \
    CONTRACT_SYNC_CONSUMERS="gen-agentic" \
    CONTRACT_SYNC_STATUS_POLL_INTERVAL=1 \
    DRY_RUN=1 \
    GEN_GITEA_TOKEN="$TOKEN_SENTINEL" \
    bash "$SYNC" >"$OUT" 2>&1
}

state_sha() {
  cat "$STATE_DIR/last-be-sha" 2>/dev/null || true
}

auto_branches() {
  git -C "$1" branch --list 'auto/contract-sync-*' | tr -d ' *'
}

write_json() { # <file> <version> <kind>
  printf '{"version": %s, "kind": "%s"}\n' "$2" "$3" >"$1"
}

# Bring the fake consumer up to the backend's current contract bytes, the state
# a merged sync PR leaves behind.
catch_consumer_up() {
  local artifact
  for artifact in vidsheet-operations-schema vidsheet-semantic-draft-schema user-job-enums; do
    cp "$BE_WORK/docs/generated/$artifact.json" \
      "$CONSUMER_WORK/src/gen/contracts/$artifact.json"
  done
}

commit_all() { # <work-dir> <message>
  git -C "$1" add -A
  git -C "$1" -c user.name=test -c user.email=test@gen.pro commit -q -m "$2"
}

# A dry run cannot checkpoint real work. These fixture checkpoints represent a
# prior successful real disposition for the next independent backend case.
checkpoint_fixture() {
  git -C "$BE_WORK" rev-parse HEAD >"$STATE_DIR/last-be-sha"
  git -C "$BE_WORK" ls-tree -r HEAD -- docs/generated config/creation_cards.yml \
    config/model_capabilities.yml | awk '{print $3 "\t" $4}' >"$STATE_DIR/last-be-watched.tsv"
  git -C "$MCP_WORK" rev-parse HEAD >"$STATE_DIR/last-mcp-sha"
  git -C "$MCP_WORK" ls-tree -r HEAD -- "$MCP_RECORD/schema-paths.tsv" \
    "$MCP_RECORD/contract-paths.tsv" | awk '{print $3 "\t" $4}' >"$STATE_DIR/last-mcp-watched.tsv"
}

# -----------------------------------------------------------------------------
# Fixtures: a backend and one consumer with the same file layout.
# -----------------------------------------------------------------------------

rm -rf "$TEST_ROOT"
mkdir -p "$TEST_ROOT/$OWNER" "$WORK" "$STATE_DIR"
git init -q --bare --initial-branch=main "$BE_BARE"
git init -q --bare --initial-branch=main "$CONSUMER_BARE"

git init -q --initial-branch=main "$BE_WORK"
mkdir -p "$BE_WORK/docs/generated" "$BE_WORK/config" "$BE_WORK/app"
write_json "$BE_WORK/docs/generated/vidsheet-operations-schema.json" 1 operations
write_json "$BE_WORK/docs/generated/vidsheet-semantic-draft-schema.json" 1 semantic
write_json "$BE_WORK/docs/generated/user-job-enums.json" 1 userjob
printf 'version: 1\n' >"$BE_WORK/config/creation_cards.yml"
printf 'version: 1\n' >"$BE_WORK/config/model_capabilities.yml"
printf '# v1\n' >"$BE_WORK/app/foo.rb"
commit_all "$BE_WORK" "backend v1"
git -C "$BE_WORK" remote add origin "$BE_BARE"
git -C "$BE_WORK" push -q -u origin main

git init -q --initial-branch=main "$CONSUMER_WORK"
mkdir -p "$CONSUMER_WORK/src/gen/contracts"
cp "$BE_WORK/docs/generated/vidsheet-operations-schema.json" \
  "$CONSUMER_WORK/src/gen/contracts/vidsheet-operations-schema.json"
cp "$BE_WORK/docs/generated/vidsheet-semantic-draft-schema.json" \
  "$CONSUMER_WORK/src/gen/contracts/vidsheet-semantic-draft-schema.json"
cp "$BE_WORK/docs/generated/user-job-enums.json" \
  "$CONSUMER_WORK/src/gen/contracts/user-job-enums.json"
printf 'consumer\n' >"$CONSUMER_WORK/README.md"
commit_all "$CONSUMER_WORK" "consumer v1"
git -C "$CONSUMER_WORK" remote add origin "$CONSUMER_BARE"
git -C "$CONSUMER_WORK" push -q -u origin main

git init -q --bare --initial-branch=main "$MCP_BARE"
git init -q --initial-branch=main "$MCP_WORK"
mkdir -p "$MCP_WORK/$MCP_RECORD"
printf 'gen_fixture_action\tv1\n' >"$MCP_WORK/$MCP_RECORD/schema-paths.tsv"
printf 'GET /fixture/v1\n' >"$MCP_WORK/$MCP_RECORD/contract-paths.tsv"
printf 'mcp fixture\n' >"$MCP_WORK/README.md"
commit_all "$MCP_WORK" "mcp fixture v1"
git -C "$MCP_WORK" remote add origin "$MCP_BARE"
git -C "$MCP_WORK" push -q -u origin main

# -----------------------------------------------------------------------------
# T1 — shell syntax and shellcheck.
# -----------------------------------------------------------------------------

printf 'T1 syntax and lint\n'
for script in "$HERE"/*.sh; do
  bash -n "$script" || fail "bash -n $(basename "$script")"
done
pass "bash -n every .sh"
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck "$HERE"/*.sh; then
    pass "shellcheck clean"
  else
    fail "shellcheck reported findings"
  fi
else
  printf '  skip  shellcheck not installed\n'
fi

# -----------------------------------------------------------------------------
# T5 — the consumer already matches the backend, so bootstrap finds no diff.
# -----------------------------------------------------------------------------

printf 'T5 consumer already identical to backend\n'
run_sync || fail "bootstrap run exited non-zero"
out="$(cat "$OUT")"
assert_contains "T5 bootstrap is a first run" "$out" "first run"
assert_contains "T5 reports no diff" "$out" "gen-agentic: no diff"
assert_not_contains "T5 plans no branch" "$out" "DRY_RUN plan"
assert_empty "T5 consumer bare repo has no sync branch" "$(auto_branches "$CONSUMER_BARE")"
assert_empty "T5 dry run does not checkpoint the backend" "$(state_sha)"
checkpoint_fixture

# -----------------------------------------------------------------------------
# T2 — a watched backend path changes: the plan names the branch, commit, PR
# and merge for the consumer.
# -----------------------------------------------------------------------------

printf 'T2 watched backend change produces a plan\n'
write_json "$BE_WORK/docs/generated/user-job-enums.json" 2 userjob
commit_all "$BE_WORK" "backend v2 user-job-enums"
git -C "$BE_WORK" push -q origin main
be2_sha="$(git -C "$BE_WORK" rev-parse HEAD)"
be2_sha8="${be2_sha:0:8}"
rm -rf "$STATE_DIR/gen-agentic"
run_sync || fail "T2 run exited non-zero"
out="$(cat "$OUT")"
assert_contains "T2 detects the backend change" "$out" "backend change"
assert_contains "T2 names the consumer branch" "$out" "branch auto/contract-sync-$be2_sha8"
assert_contains "T2 names the commit" "$out" \
  "commit \"chore: sync backend contracts to gen-backend-v2 $be2_sha8\""
assert_contains "T2 plans the PR" "$out" "open PR"
assert_contains "T2 plans the merge" "$out" "merge (fast-forward-only then rebase)"
assert_contains "T2 names the changed path" "$out" "src/gen/contracts/user-job-enums.json"
assert_empty "T2 DRY_RUN pushes no branch" "$(auto_branches "$CONSUMER_BARE")"
assert_not_contains "T2 dry run does not checkpoint the new backend" "$(state_sha)" "$be2_sha"
assert_not_contains "T6 token is never printed" "$out" "$TOKEN_SENTINEL"
checkpoint_fixture

# -----------------------------------------------------------------------------
# T3 — the same backend sha a second time: no backend change, nothing touched.
# -----------------------------------------------------------------------------

printf 'T3 same backend sha is a no-op\n'
consumer_clone="$STATE_DIR/gen-agentic"
[ -d "$consumer_clone" ] || fail "T3 precondition: consumer clone exists"
before_mtime="$(stat -c %Y "$consumer_clone")"
run_sync || fail "T3 run exited non-zero"
out="$(cat "$OUT")"
assert_contains "T3 reports no backend change" "$out" "no backend change"
assert_not_contains "T3 mentions no consumer" "$out" "gen-agentic:"
assert_equal "T3 leaves the consumer clone untouched" "$before_mtime" \
  "$(stat -c %Y "$consumer_clone")"
assert_equal "T3 state is unchanged" "$be2_sha" "$(state_sha)"

# -----------------------------------------------------------------------------
# T4 — the backend moves, but only outside the watched paths.
# -----------------------------------------------------------------------------

printf 'T4 unwatched backend change touches no consumer\n'
printf '# v2\n' >"$BE_WORK/app/foo.rb"
commit_all "$BE_WORK" "backend v2 app code"
git -C "$BE_WORK" push -q origin main
be3_sha="$(git -C "$BE_WORK" rev-parse HEAD)"
rm -rf "$STATE_DIR/gen-agentic"
run_sync || fail "T4 run exited non-zero"
out="$(cat "$OUT")"
assert_contains "T4 reports no consumer action" "$out" "no consumer action"
assert_not_contains "T4 plans nothing" "$out" "DRY_RUN plan"
if [ ! -d "$STATE_DIR/gen-agentic" ]; then
  pass "T4 clones no consumer"
else
  fail "T4 cloned a consumer for an unwatched change"
fi
assert_equal "T4 dry run keeps the previous backend checkpoint" "$be2_sha" "$(state_sha)"
checkpoint_fixture

# -----------------------------------------------------------------------------
# T5b — a watched backend path changes, but this consumer is already identical.
# -----------------------------------------------------------------------------

printf 'T5b watched change with a converged consumer\n'
write_json "$BE_WORK/docs/generated/vidsheet-semantic-draft-schema.json" 2 semantic
commit_all "$BE_WORK" "backend v2 semantic draft schema"
git -C "$BE_WORK" push -q origin main
# The earlier user-job-enums change is still unmerged (DRY_RUN opened no PR), so
# catch the consumer up first: T5b isolates the converged-artifact case.
catch_consumer_up
commit_all "$CONSUMER_WORK" "consumer catches up"
git -C "$CONSUMER_WORK" push -q origin main
rm -rf "$STATE_DIR/gen-agentic"
run_sync || fail "T5b run exited non-zero"
out="$(cat "$OUT")"
assert_contains "T5b detects the backend change" "$out" "backend change"
assert_contains "T5b reports no diff" "$out" "gen-agentic: no diff"
assert_not_contains "T5b plans no branch" "$out" "DRY_RUN plan"
assert_not_contains "T5b names no branch" "$out" "auto/contract-sync-"
assert_empty "T5b consumer bare repo has no sync branch" "$(auto_branches "$CONSUMER_BARE")"
checkpoint_fixture

# -----------------------------------------------------------------------------
# T7 — the live path: branch, commit, push, PR, check poll and merge, with the
# API served by a local mock forge. Git traffic stays on the local bare repos,
# so this exercises the real code without touching Gitea.
# -----------------------------------------------------------------------------

printf 'T7 live path against a local mock forge\n'
MOCK_LOG="$TEST_ROOT/mock-requests.log"
MOCK_PORT_FILE="$TEST_ROOT/mock-port"
MOCK_MODE="$TEST_ROOT/mock-status-mode"
LIVE_OUT="$TEST_ROOT/live-output.log"
: >"$MOCK_LOG"
rm -f "$MOCK_PORT_FILE"
printf 'success\n' >"$MOCK_MODE"
python3 - "$MOCK_LOG" "$MOCK_PORT_FILE" "$MOCK_MODE" "$TEST_ROOT/$OWNER" <<'PY' &
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log_path, port_file, mode_file, repo_root = sys.argv[1:]


def status_mode():
    try:
        return open(mode_file).read().strip() or "success"
    except OSError:
        return "success"


open_heads = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, entry):
        with open(log_path, "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    def do_GET(self):
        path = self.path.split("?")[0]
        self._record({"path": self.path, "method": "GET"})
        if path.endswith("/pulls"):
            return self._send(200, [{"number": 1, "state": "open", "head": {"ref": h}} for h in open_heads])
        if "/commits/" in path and path.endswith("/statuses"):
            # The second context mirrors a workflow no runner executes: it stays
            # pending forever and must never hold back a merge.
            mode = status_mode()
            state = "success" if mode in ("behind", "busy") else mode
            if mode == "busy" and "/commits/" + "0" * 40 + "/" in path:
                state = "pending"
            if mode == "absent":
                return self._send(200, [])
            return self._send(200, [
                {"id": 1, "context": "ci/woodpecker/push/ci", "status": "failure"},
                {"id": 3, "context": "ci/woodpecker/push/ci", "status": state},
                {"id": 2, "context": "Development Workflow / Unit Tests (pull_request)", "status": "pending"},
            ])
        if "/branches/" in path:
            if path.endswith("/main"):
                return self._send(200, {"name": "main", "commit": {"id": "0" * 40}})
            if path.split("/branches/", 1)[1] in open_heads:
                repo = path.split("/repos/", 1)[1].split("/")[1]
                branch = path.split("/branches/", 1)[1]
                head = subprocess.check_output(["git", "--git-dir", f"{repo_root}/{repo}.git", "rev-parse", f"refs/heads/{branch}"], text=True).strip()
                return self._send(200, {"commit": {"id": head}})
            return self._send(404, {"message": "branch not found"})
        return self._send(404, {"message": "no route"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw or b"{}")
        if self.path.endswith("/merge"):
            self._record({"path": self.path, "do": payload.get("Do")})
            if status_mode() == "behind":
                return self._send(405, {"message": "Not possible to fast-forward"})
            return self._send(200, {"merged": True})
        if "/update" in self.path:
            self._record({"path": self.path, "update": True})
            return self._send(200, {})
        if self.path.endswith("/pulls"):
            self._record({"path": self.path, "head": payload.get("head"), "title": payload.get("title"), "body": payload.get("body")})
            open_heads.append(payload.get("head"))
            return self._send(201, {"number": 1, "html_url": "http://mock.invalid/pr/1", "head": {"ref": payload.get("head")}})
        return self._send(404, {"message": "no route"})


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
with open(port_file, "w") as fh:
    fh.write(str(server.server_address[1]))
server.serve_forever()
PY
MOCK_PID=$!
trap 'kill "$MOCK_PID" 2>/dev/null || true' EXIT
mock_tries=0
while [ ! -s "$MOCK_PORT_FILE" ] && [ "$mock_tries" -lt 50 ]; do
  sleep 0.1
  mock_tries=$((mock_tries + 1))
done
if [ -s "$MOCK_PORT_FILE" ]; then
  pass "T7 mock forge is listening"
else
  fail "T7 mock forge did not start"
fi
MOCK_URL="http://127.0.0.1:$(cat "$MOCK_PORT_FILE")"

write_json "$BE_WORK/docs/generated/user-job-enums.json" 3 userjob
commit_all "$BE_WORK" "backend v3 user-job-enums"
git -C "$BE_WORK" push -q origin main
be4_sha="$(git -C "$BE_WORK" rev-parse HEAD)"
be4_sha8="${be4_sha:0:8}"
rm -rf "$STATE_DIR/gen-agentic"
CONTRACT_SYNC_STATE_DIR="$STATE_DIR" \
  CONTRACT_SYNC_GIT_BASE="$GIT_BASE" \
  CONTRACT_SYNC_OWNER="$OWNER" \
  CONTRACT_SYNC_BACKEND_URL="$BE_URL" \
  CONTRACT_SYNC_CONSUMERS="gen-agentic" \
  CONTRACT_SYNC_STATUS_POLL_INTERVAL=1 \
  CONTRACT_SYNC_STATUS_POLL_TIMEOUT=10 \
  GEN_GITEA_URL="$MOCK_URL" \
  GEN_GITEA_TOKEN="$TOKEN_SENTINEL" \
  bash "$SYNC" >"$LIVE_OUT" 2>&1 || fail "T7 sync exited non-zero"
out="$(cat "$LIVE_OUT")"
assert_contains "T7 opens the PR" "$out" "PR #1 opened"
assert_contains "T7 polls and merges" "$out" "merged PR #1 (fast-forward-only)"
assert_equal "T7 pushed the branch" "auto/contract-sync-$be4_sha8" \
  "$(auto_branches "$CONSUMER_BARE")"
assert_equal "T7 commit subject" \
  "chore: sync backend contracts to gen-backend-v2 $be4_sha8" \
  "$(git -C "$CONSUMER_BARE" log -1 --format=%s "auto/contract-sync-$be4_sha8")"
pr_title="$(python3 -c '
import json, sys
for line in open(sys.argv[1]):
    row = json.loads(line)
    if "title" in row:
        print(row["title"])
        break
' "$MOCK_LOG")"
merge_styles="$(python3 -c '
import json, sys
for line in open(sys.argv[1]):
    row = json.loads(line)
    if "do" in row:
        print(row["do"])
' "$MOCK_LOG")"
assert_equal "T7 PR title" "chore: sync backend contracts to gen-backend-v2 $be4_sha8" "$pr_title"
assert_equal "T7 first merge style is fast-forward-only" "fast-forward-only" "$merge_styles"
assert_equal "T7 state advances only after the merge" "$be4_sha" "$(state_sha)"
assert_not_contains "T7 token never printed on the live path" "$out" "$TOKEN_SENTINEL"

# -----------------------------------------------------------------------------
# T8 — a red check never merges. The PR is pushed and left open, and the run
# reports failure instead of quietly treating the handoff as done.
# -----------------------------------------------------------------------------

printf 'T8 red checks leave the PR open\n'
write_json "$BE_WORK/docs/generated/user-job-enums.json" 4 userjob
commit_all "$BE_WORK" "backend v4 user-job-enums"
git -C "$BE_WORK" push -q origin main
be5_sha="$(git -C "$BE_WORK" rev-parse HEAD)"
be5_sha8="${be5_sha:0:8}"
printf 'failure\n' >"$MOCK_MODE"
: >"$MOCK_LOG"
rm -rf "$STATE_DIR/gen-agentic"
rc=0
CONTRACT_SYNC_STATE_DIR="$STATE_DIR" \
  CONTRACT_SYNC_GIT_BASE="$GIT_BASE" \
  CONTRACT_SYNC_OWNER="$OWNER" \
  CONTRACT_SYNC_BACKEND_URL="$BE_URL" \
  CONTRACT_SYNC_CONSUMERS="gen-agentic" \
  CONTRACT_SYNC_STATUS_POLL_INTERVAL=1 \
  CONTRACT_SYNC_STATUS_POLL_TIMEOUT=10 \
  GEN_GITEA_URL="$MOCK_URL" \
  GEN_GITEA_TOKEN="$TOKEN_SENTINEL" \
  bash "$SYNC" >"$LIVE_OUT" 2>&1 || rc=$?
out="$(cat "$LIVE_OUT")"
assert_equal "T8 a red PR is not a success" "1" "$rc"
assert_contains "T8 reports the red check" "$out" "checks not green"
assert_contains "T8 leaves the PR open" "$out" "leaving PR #1 open"
assert_equal "T8 pushed the branch" "1" \
  "$(git -C "$CONSUMER_BARE" show-ref --verify --quiet "refs/heads/auto/contract-sync-$be5_sha8" && echo 1 || echo 0)"
assert_equal "T8 commit subject" \
  "chore: sync backend contracts to gen-backend-v2 $be5_sha8" \
  "$(git -C "$CONSUMER_BARE" log -1 --format=%s "auto/contract-sync-$be5_sha8")"
assert_empty "T8 records no merge call" "$(python3 -c '
import json, sys
for line in open(sys.argv[1]):
    row = json.loads(line)
    if "do" in row:
        print(row["do"])
' "$MOCK_LOG")"
assert_equal "T8 the handoff still advances the state" "$be5_sha" "$(state_sha)"
assert_not_contains "T8 token never printed" "$out" "$TOKEN_SENTINEL"

# -----------------------------------------------------------------------------
# T9 — a hard failure (an unreachable consumer) must not advance the state, so
# the next tick retries the same change instead of dropping it.
# -----------------------------------------------------------------------------

printf 'T9 hard failure keeps the change queued\n'
write_json "$BE_WORK/docs/generated/user-job-enums.json" 5 userjob
commit_all "$BE_WORK" "backend v5 user-job-enums"
git -C "$BE_WORK" push -q origin main
rm -rf "$STATE_DIR/gen-agentic"
rc=0
CONTRACT_SYNC_STATE_DIR="$STATE_DIR" \
  CONTRACT_SYNC_GIT_BASE="file://$TEST_ROOT/no-such-forge" \
  CONTRACT_SYNC_OWNER="$OWNER" \
  CONTRACT_SYNC_BACKEND_URL="$BE_URL" \
  CONTRACT_SYNC_CONSUMERS="gen-agentic" \
  DRY_RUN=1 \
  bash "$SYNC" >"$LIVE_OUT" 2>&1 || rc=$?
out="$(cat "$LIVE_OUT")"
assert_equal "T9 the run reports failure" "1" "$rc"
assert_contains "T9 names the failure" "$out" "ERROR cannot clone"
assert_contains "T9 says the change is queued" "$out" "state not advanced, the next tick retries"
assert_equal "T9 state did not advance" "$be5_sha" "$(state_sha)"
rm -rf "$STATE_DIR/gen-agentic"
run_sync || fail "T9 retry run exited non-zero"
out="$(cat "$OUT")"
assert_contains "T9 the next tick still sees the change" "$out" "backend change"
assert_contains "T9 the next tick plans the sync" "$out" "DRY_RUN plan"

# -----------------------------------------------------------------------------
# T10 — a multi-step regeneration stops at the first failing step instead of
# silently running the rest and reporting the last step's status.
# -----------------------------------------------------------------------------

printf 'T10 a failing regeneration step stops the loop\n'
state_before="$(state_sha)"
mkdir -p "$MCP_WORK/scripts"
printf 'import sys\nsys.exit(1)\n' >"$MCP_WORK/scripts/vendor_creation_card_branches.py"
printf 'import pathlib\npathlib.Path("LAST_SCRIPT_RAN").write_text("x")\n' \
  >"$MCP_WORK/scripts/sync_model_capabilities.py"
commit_all "$MCP_WORK" "mcp fixture"
git -C "$MCP_WORK" push -q origin main

write_json "$BE_WORK/docs/generated/user-job-enums.json" 6 userjob
commit_all "$BE_WORK" "backend v6 user-job-enums"
git -C "$BE_WORK" push -q origin main
rm -rf "$STATE_DIR/gen-mcp-server"
rc=0
CONTRACT_SYNC_STATE_DIR="$STATE_DIR" \
  CONTRACT_SYNC_GIT_BASE="$GIT_BASE" \
  CONTRACT_SYNC_OWNER="$OWNER" \
  CONTRACT_SYNC_BACKEND_URL="$BE_URL" \
  CONTRACT_SYNC_CONSUMERS="gen-mcp-server" \
  DRY_RUN=1 \
  bash "$SYNC" >"$LIVE_OUT" 2>&1 || rc=$?
out="$(cat "$LIVE_OUT")"
assert_equal "T10 the run reports failure" "1" "$rc"
assert_contains "T10 names the failing consumer" "$out" "gen-mcp-server: ERROR regeneration failed"
assert_equal "T10 the later script never ran" "0" \
  "$([ -f "$STATE_DIR/gen-mcp-server/LAST_SCRIPT_RAN" ] && echo 1 || echo 0)"
assert_equal "T10 state did not advance" "$state_before" "$(state_sha)"

# -----------------------------------------------------------------------------
# T11 — a PR whose checks are still pending is not abandoned: the state stays
# put and the next tick re-checks the same open PR and merges it once green.
# -----------------------------------------------------------------------------

printf 'T11 pending checks are re-checked next tick\n'
write_json "$BE_WORK/docs/generated/user-job-enums.json" 12 userjob
commit_all "$BE_WORK" "backend v12 user-job-enums"
git -C "$BE_WORK" push -q origin main
be7_sha="$(git -C "$BE_WORK" rev-parse HEAD)"
state_before="$(state_sha)"
live_sync() {
  CONTRACT_SYNC_STATE_DIR="$STATE_DIR" \
    CONTRACT_SYNC_GIT_BASE="$GIT_BASE" \
    CONTRACT_SYNC_OWNER="$OWNER" \
    CONTRACT_SYNC_BACKEND_URL="$BE_URL" \
    CONTRACT_SYNC_CONSUMERS="gen-agentic" \
    CONTRACT_SYNC_STATUS_POLL_INTERVAL=1 \
    CONTRACT_SYNC_STATUS_POLL_TIMEOUT=3 \
    GEN_GITEA_URL="$MOCK_URL" \
    GEN_GITEA_TOKEN="$TOKEN_SENTINEL" \
    bash "$SYNC" >"$LIVE_OUT" 2>&1
}
printf 'pending\n' >"$MOCK_MODE"
: >"$MOCK_LOG"
rm -rf "$STATE_DIR/gen-agentic"
rc=0
live_sync || rc=$?
out="$(cat "$LIVE_OUT")"
assert_equal "T11 a pending PR is not a success" "1" "$rc"
assert_contains "T11 says it re-checks next tick" "$out" "re-checked next tick"
assert_equal "T11 state did not advance" "$state_before" "$(state_sha)"
printf 'success\n' >"$MOCK_MODE"
rm -rf "$STATE_DIR/gen-agentic"
rc=0
live_sync || rc=$?
out="$(cat "$LIVE_OUT")"
assert_equal "T11 the next tick succeeds" "0" "$rc"
assert_contains "T11 the next tick re-checks the open PR" "$out" "re-checking it"
assert_contains "T11 merges once green despite a never-run workflow" "$out" "merged PR #1"
assert_equal "T11 state advances after the merge" "$be7_sha" "$(state_sha)"

# -----------------------------------------------------------------------------
# T12 — a fast-forward-only repo whose main moved refuses the merge; the sync
# rebases the PR server-side and re-checks it next tick instead of handing off.
# -----------------------------------------------------------------------------

printf 'T12 a branch behind main is rebased and retried\n'
write_json "$BE_WORK/docs/generated/user-job-enums.json" 13 userjob
commit_all "$BE_WORK" "backend v13 user-job-enums"
git -C "$BE_WORK" push -q origin main
state_before="$(state_sha)"
printf 'behind\n' >"$MOCK_MODE"
: >"$MOCK_LOG"
rm -rf "$STATE_DIR/gen-agentic"
rc=0
live_sync || rc=$?
out="$(cat "$LIVE_OUT")"
assert_equal "T12 a refused merge is not a success" "1" "$rc"
assert_contains "T12 rebases the PR" "$out" "rebased it onto main, re-checked next tick"
assert_contains "T12 calls the rebase endpoint" "$(cat "$MOCK_LOG")" "update?style=rebase"
assert_equal "T12 state did not advance" "$state_before" "$(state_sha)"
printf 'success\n' >"$MOCK_MODE"

# -----------------------------------------------------------------------------
# R07 — both sources flow through the docs subprocesses, with isolated state.
# -----------------------------------------------------------------------------

printf 'R07 MCP source and combined docs flow\n'
STATE_DIR="$TEST_ROOT/docs-state"
DOCS_BARE="$TEST_ROOT/$OWNER/api-docs.git"
DOCS_WORK="$WORK/api-docs"
mkdir -p "$STATE_DIR" "$DOCS_WORK/scripts" "$TEST_ROOT/git-bin"
git init -q --bare --initial-branch=main "$DOCS_BARE"
git init -q --initial-branch=main "$DOCS_WORK"
cp "$HERE/fixtures/api-docs-generator.mjs" "$DOCS_WORK/scripts/sync-from-backend.mjs"
cp "$HERE/fixtures/api-docs-generator.mjs" "$DOCS_WORK/scripts/update-mcp-tools-snapshot.mjs"
cp "$HERE/fixtures/mcp-surface-generator.py" "$DOCS_WORK/scripts/sync_mcp_surface.py"
export FIXTURE_REAL_GIT
FIXTURE_REAL_GIT="$(command -v git)"
cat >"$TEST_ROOT/git-bin/git" <<'SH'
#!/bin/bash
if [ "${FIXTURE_FAIL_MCP_CLONE:-0}" = "1" ] && [ "$1" = "clone" ] \
  && [[ "${!#}" == */mcp-source ]]; then
  exit 1
fi
exec "$FIXTURE_REAL_GIT" "$@"
SH
chmod +x "$TEST_ROOT/git-bin/git"

seed_docs() {
  local sha
  sha="$(git -C "$MCP_WORK" rev-parse HEAD)"
  (
    cd "$DOCS_WORK"
    node scripts/update-mcp-tools-snapshot.mjs --offline --aliases-repo "$MCP_WORK" --ref "$sha"
    node scripts/sync-from-backend.mjs --backend "$BE_WORK" --mcp-repo "$MCP_WORK"
    python3 scripts/sync_mcp_surface.py --derive-servers --mcp-src "$MCP_WORK"
    python3 scripts/sync_mcp_surface.py
  )
}

docs_sync() { # <dry-run> [consumer list]
  PATH="$TEST_ROOT/git-bin:$PATH" \
    CONTRACT_SYNC_STATE_DIR="$STATE_DIR" \
    CONTRACT_SYNC_GIT_BASE="$GIT_BASE" \
    CONTRACT_SYNC_OWNER="$OWNER" \
    CONTRACT_SYNC_BACKEND_URL="$BE_URL" \
    CONTRACT_SYNC_CONSUMERS="${2:-gen-mcp-server gen-agentic limitless-fe api-docs}" \
    CONTRACT_SYNC_STATUS_POLL_INTERVAL=1 \
    CONTRACT_SYNC_STATUS_POLL_TIMEOUT=1 \
    CONTRACT_SYNC_MAIN_PIPELINE_WAIT=1 \
    GEN_GITEA_URL="$MOCK_URL" GEN_GITEA_TOKEN="$TOKEN_SENTINEL" \
    DRY_RUN="$1" bash "$SYNC" >"$LIVE_OUT" 2>&1
}

state_fingerprint() {
  sha256sum "$STATE_DIR"/last-*
}

seed_docs
commit_all "$DOCS_WORK" "docs fixture baseline"
git -C "$DOCS_WORK" remote add origin "$DOCS_BARE"
git -C "$DOCS_WORK" push -q -u origin main
checkpoint_fixture
be_baseline="$(state_sha)"
checkpoint_before="$(state_fingerprint)"

# intent: unchanged Rails cannot hide a changed public tool or route record;
# both exact-source generators run once, and a dry run consumes neither source.
printf 'gen_fixture_action\tv2\ngen_new_action\tv2\n' >"$MCP_WORK/$MCP_RECORD/schema-paths.tsv"
printf 'GET /fixture/v1\nPOST /fixture/v2\n' >"$MCP_WORK/$MCP_RECORD/contract-paths.tsv"
commit_all "$MCP_WORK" "mcp schema and route v2"
git -C "$MCP_WORK" push -q origin main
mcp_v2="$(git -C "$MCP_WORK" rev-parse HEAD)"
docs_sync 1 || fail "R1 MCP-only dry run succeeds"
out="$(cat "$LIVE_OUT")"
assert_contains "R1 unchanged backend still plans docs" "$out" "api-docs:"
assert_equal "R1 exactly one docs plan" "1" "$(grep -c 'api-docs:.*DRY_RUN plan' "$LIVE_OUT" || true)"
assert_contains "R1 plan names exact MCP SHA" "$out" "$mcp_v2"
assert_contains "R1 combined branch identity" "$out" "auto/contract-sync-${be_baseline:0:8}-${mcp_v2:0:8}"
for file in public/openapi.yaml public/.well-known/openapi.yaml public/llms.txt public/llms-full.txt scripts/mcp-tools-snapshot.json; do
  assert_contains "R1 exact MCP reaches $file" "$(cat "$STATE_DIR/api-docs/$file")" "$mcp_v2"
done
assert_contains "R1 new route reaches generated OpenAPI" "$(cat "$STATE_DIR/api-docs/public/openapi.yaml")" "POST /fixture/v2"
assert_not_contains "R1 does not regenerate MCP" "$out" "gen-mcp-server:"
assert_not_contains "R1 does not regenerate agentic" "$out" "gen-agentic:"
assert_not_contains "R1 does not regenerate FE" "$out" "limitless-fe:"
assert_empty "R1 dry run creates no branch" "$(auto_branches "$DOCS_BARE")"
assert_empty "R1 dry run creates no local branch" "$(auto_branches "$STATE_DIR/api-docs")"
assert_equal "R7 dry run keeps both durable checkpoints" "$checkpoint_before" "$(state_fingerprint)"
assert_not_contains "R8 MCP-only plan does not log the token" "$out" "$TOKEN_SENTINEL"

# intent: consumers that already match cause no clock-only snapshot diff, and
# the next tick with the same two SHAs does not run a generator or open a PR.
seed_docs
commit_all "$DOCS_WORK" "docs fixture catches up to MCP v2"
git -C "$DOCS_WORK" push -q origin main
: >"$MOCK_LOG"
docs_sync 0 || fail "R2 converged real disposition succeeds"
assert_contains "R2 converged consumer has no diff" "$(cat "$LIVE_OUT")" "api-docs: no diff"
assert_empty "R2 no forge API mutation" "$(cat "$MOCK_LOG")"
assert_empty "R2 no sync branch or CI-producing push" "$(auto_branches "$DOCS_BARE")"
assert_equal "R2 successful no-diff disposition checkpoints MCP" "$mcp_v2" "$(cat "$STATE_DIR/last-mcp-sha")"
docs_sync 0 || fail "R2 repeated tick succeeds"
assert_not_contains "R2 repeated tick invokes no consumer" "$(cat "$LIVE_OUT")" "api-docs:"

# intent: an unwatched MCP README commit may update bookkeeping, but cannot
# publish new docs merely because a source revision or clock changed.
printf 'mcp readme only\n' >"$MCP_WORK/README.md"
commit_all "$MCP_WORK" "mcp README only"
git -C "$MCP_WORK" push -q origin main
mcp_readme="$(git -C "$MCP_WORK" rev-parse HEAD)"
docs_before="$(git -C "$STATE_DIR/api-docs" diff)"
docs_sync 0 || fail "R3 README-only source disposition succeeds"
assert_contains "R3 watches blobs rather than whole MCP SHA" "$(cat "$LIVE_OUT")" "no consumer action"
assert_equal "R3 leaves generated docs untouched" "$docs_before" "$(git -C "$STATE_DIR/api-docs" diff)"
assert_equal "R3 real bookkeeping advances" "$mcp_readme" "$(cat "$STATE_DIR/last-mcp-sha")"

# intent: the route record is watched independently of the schema record.
printf 'GET /fixture/v1\nPOST /fixture/v2b\n' >"$MCP_WORK/$MCP_RECORD/contract-paths.tsv"
commit_all "$MCP_WORK" "mcp route-only v2b"
git -C "$MCP_WORK" push -q origin main
docs_sync 1 || fail "R1 route-only MCP change succeeds"
assert_contains "R1 route record alone plans docs" "$(cat "$LIVE_OUT")" "api-docs:"
assert_not_contains "R1 route-only MCP change does not touch backend consumers" "$(cat "$LIVE_OUT")" "gen-agentic:"
seed_docs
commit_all "$DOCS_WORK" "docs fixture catches up to route-only MCP"
git -C "$DOCS_WORK" push -q origin main
docs_sync 0 || fail "R1 converged route-only source checkpoints successfully"
mcp_readme="$(git -C "$MCP_WORK" rev-parse HEAD)"

# intent: backend work preserves its original consumers while docs read the
# pinned MCP source too; simultaneous changes join into one docs plan.
write_json "$BE_WORK/docs/generated/user-job-enums.json" 14 userjob
commit_all "$BE_WORK" "backend v14 user-job-enums"
git -C "$BE_WORK" push -q origin main
be_combined="$(git -C "$BE_WORK" rev-parse HEAD)"
docs_sync 1 "gen-agentic api-docs" || fail "R4 backend-only dry run succeeds"
out="$(cat "$LIVE_OUT")"
assert_contains "R4 backend consumer behavior is retained" "$out" "gen-agentic:"
assert_contains "R4 docs read exact current MCP too" "$(cat "$STATE_DIR/api-docs/public/openapi.yaml")" "$mcp_readme"
printf 'POST /fixture/v3\n' >"$MCP_WORK/$MCP_RECORD/contract-paths.tsv"
commit_all "$MCP_WORK" "mcp route-only v3"
git -C "$MCP_WORK" push -q origin main
mcp_combined="$(git -C "$MCP_WORK" rev-parse HEAD)"
docs_sync 1 "gen-agentic api-docs" || fail "R5 combined dry run succeeds"
out="$(cat "$LIVE_OUT")"
assert_equal "R5 one combined docs plan" "1" "$(grep -c 'api-docs:.*DRY_RUN plan' "$LIVE_OUT" || true)"
assert_contains "R5 provenance includes exact backend" "$out" "$be_combined"
assert_contains "R5 provenance includes exact MCP" "$out" "$mcp_combined"
assert_contains "R5 route record alone changes docs" "$(cat "$STATE_DIR/api-docs/public/openapi.yaml")" "POST /fixture/v3"

# intent: source and generator failures, absent/pending checks, and busy main
# never checkpoint unprocessed work or replace an already-open docs PR.
checkpoint_before="$(state_fingerprint)"
rc=0
FIXTURE_FAIL_MCP_CLONE=1 docs_sync 0 "api-docs" || rc=$?
assert_equal "R6 MCP clone failure is reported" "1" "$rc"
assert_contains "R6 MCP clone failure seam executes" "$(cat "$LIVE_OUT")" "ERROR cannot clone"
assert_equal "R6 MCP clone failure keeps checkpoints" "$checkpoint_before" "$(state_fingerprint)"
rc=0
FIXTURE_DOCS_FAIL=1 docs_sync 0 "api-docs" || rc=$?
assert_equal "R6 docs generator failure is reported" "1" "$rc"
assert_equal "R6 generator failure keeps checkpoints" "$checkpoint_before" "$(state_fingerprint)"
: >"$MOCK_LOG"
printf 'pending\n' >"$MOCK_MODE"
rc=0
docs_sync 0 "api-docs" || rc=$?
assert_equal "R6 pending docs PR is not complete" "1" "$rc"
assert_equal "R6 pending checks keep both sources queued" "$checkpoint_before" "$(state_fingerprint)"
docs_branch="auto/contract-sync-${be_combined:0:8}-${mcp_combined:0:8}"
docs_head="$(git -C "$DOCS_BARE" rev-parse "$docs_branch")"
for mode in busy absent; do
  printf '%s\n' "$mode" >"$MOCK_MODE"
  rc=0
  docs_sync 0 "api-docs" || rc=$?
  assert_equal "R6 $mode checks are not complete" "1" "$rc"
  assert_equal "R6 $mode retains both checkpoints" "$checkpoint_before" "$(state_fingerprint)"
  assert_contains "R6 $mode rechecks the same PR" "$(cat "$LIVE_OUT")" "re-checking it"
done
assert_not_contains "R6 pending/busy/absent never merge" "$(cat "$MOCK_LOG")" '"do":'
assert_equal "R6 no replacement push while waiting" "$docs_head" "$(git -C "$DOCS_BARE" rev-parse "$docs_branch")"
printf 'success\n' >"$MOCK_MODE"
docs_sync 0 "api-docs" || fail "R8 green retry succeeds"
assert_contains "R8 only exact PR head is checked" "$(cat "$MOCK_LOG")" "/commits/$docs_head/statuses"
assert_contains "R8 newest success supersedes older red context" "$(cat "$LIVE_OUT")" "merged PR #1"
assert_equal "R8 successful disposition checkpoints backend" "$be_combined" "$(state_sha)"
assert_equal "R8 successful disposition checkpoints MCP" "$mcp_combined" "$(cat "$STATE_DIR/last-mcp-sha")"
commit_body="$(git -C "$DOCS_BARE" log -1 --format=%B "$docs_branch")"
assert_contains "R5 committed provenance names exact backend" "$commit_body" "$be_combined"
assert_contains "R5 committed provenance names exact MCP" "$commit_body" "$mcp_combined"
assert_equal "R5 a single docs PR is opened" "1" "$(grep -c '"title":' "$MOCK_LOG" || true)"
pr_body="$(python3 -c '
import json, sys
for line in open(sys.argv[1]):
    row = json.loads(line)
    if "title" in row:
        print(row["body"])
' "$MOCK_LOG")"
assert_contains "R5 PR provenance names exact backend" "$pr_body" "$be_combined"
assert_contains "R5 PR provenance names exact MCP" "$pr_body" "$mcp_combined"

# intent: a red source change is handed to human monitoring without a merge or
# repeated push; the bot preserves its deliberate terminal-handoff semantics.
printf 'gen_fixture_action\tv4\n' >"$MCP_WORK/$MCP_RECORD/schema-paths.tsv"
commit_all "$MCP_WORK" "mcp schema-only v4"
git -C "$MCP_WORK" push -q origin main
: >"$MOCK_LOG"
printf 'failure\n' >"$MOCK_MODE"
rc=0
docs_sync 0 "api-docs" || rc=$?
assert_equal "R8 red PR remains a reported failure" "1" "$rc"
assert_not_contains "R8 red never merges" "$(cat "$MOCK_LOG")" '"do":'
assert_equal "R8 terminal red handoff checkpoints source" "$(git -C "$MCP_WORK" rev-parse HEAD)" "$(cat "$STATE_DIR/last-mcp-sha")"
docs_sync 0 "api-docs" || fail "R8 monitored red handoff is not re-pushed"
assert_not_contains "R8 terminal red tick causes no generator churn" "$(cat "$LIVE_OUT")" "api-docs:"

# intent: flock admits one worker, even when a second-source tick overlaps it.
(
  exec 8>"$STATE_DIR/lock"
  flock -n 8
  docs_sync 1 "api-docs"
) || fail "R8 overlapping worker exits cleanly"
assert_contains "R8 second worker cannot enter source work" "$(cat "$LIVE_OUT")" "another run holds"
assert_not_contains "R8 token sentinel never reaches source-flow logs" "$(cat "$LIVE_OUT")$(cat "$MOCK_LOG")" "$TOKEN_SENTINEL"

# -----------------------------------------------------------------------------
# T6 — the token value has exactly one home: a curl header argument.
# -----------------------------------------------------------------------------

printf 'T6 token handling\n'
printed="$(grep -nE '(echo|printf|log)[^|]*GEN_GITEA_TOKEN' "$SYNC" || true)"
assert_empty "T6 no printing verb mentions the token" "$printed"
unexpected="$(grep -n 'GEN_GITEA_TOKEN' "$SYNC" \
  | grep -vE 'Authorization: token|\[ -n "\$\{GEN_GITEA_TOKEN:-\}" \]' || true)"
assert_empty "T6 the token appears only in the presence check and curl headers" "$unexpected"
assert_not_contains "T6 the sentinel never reaches output" "$(cat "$OUT")" "$TOKEN_SENTINEL"

# -----------------------------------------------------------------------------

printf '\n'
if [ "$FAILURES" = "0" ]; then
  printf 'PASS: contract-sync offline test (fixtures in %s)\n' "$TEST_ROOT"
  exit 0
fi
printf 'FAIL: %d assertion(s) failed (output in %s)\n' "$FAILURES" "$OUT"
exit 1
