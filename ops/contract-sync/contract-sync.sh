#!/bin/bash
# contract-sync — propagate backend contracts and the public MCP projection.
#
# gen-backend-v2 owns the generated contracts (docs/generated/**, the creation
# card and model capability configs). Each consumer vendors a byte-identical
# copy or derives its own artifact from them, and each has a freshness gate that
# goes red while the copy is stale. When a watched backend path changes, this
# script runs the consumer's OWN regeneration command in a fresh shallow clone
# and opens a PR only when that regeneration produced a diff, so the change
# arrives without anyone filing a ticket for it.
#
# Guardrails that the unattended path must keep:
#   - generated artifacts are never hand-edited here; the consumer's own
#     generator produces the bytes.
#   - nothing is force-pushed. A branch for a backend sha that already has an
#     open PR is that PR, not a new one.
#   - a PR merges only when every newest-per-context status on its head is
#     success. Red, pending, or absent checks leave the PR open and log it.
#   - main's own push pipeline must be terminal before merging. Merging while
#     the pipeline for the previous main sha still runs cancels it and leaves
#     main without a verdict, which blocks every deploy gated on it (GEN-6025).
set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration. Every value is overridable from the environment so the offline
# test can run this exact code against local bare repositories.
# -----------------------------------------------------------------------------

STATE_DIR="${CONTRACT_SYNC_STATE_DIR:-/var/lib/contract-sync}"
GITEA_URL="${GEN_GITEA_URL:-https://git.gen.pro}"
GITEA_OWNER="${CONTRACT_SYNC_OWNER:-GEN}"
GIT_BASE="${CONTRACT_SYNC_GIT_BASE:-$GITEA_URL}"
BACKEND_REPO="${CONTRACT_SYNC_BACKEND_REPO:-gen-backend-v2}"
BACKEND_BRANCH="${CONTRACT_SYNC_BACKEND_BRANCH:-main}"
CONSUMER_BRANCH="${CONTRACT_SYNC_CONSUMER_BRANCH:-main}"
BACKEND_URL="${CONTRACT_SYNC_BACKEND_URL:-$GIT_BASE/$GITEA_OWNER/$BACKEND_REPO.git}"
MCP_SOURCE_REPO="${CONTRACT_SYNC_MCP_REPO:-gen-mcp-server}"
MCP_SOURCE_BRANCH="${CONTRACT_SYNC_MCP_BRANCH:-main}"
MCP_SOURCE_URL="${CONTRACT_SYNC_MCP_URL:-$GIT_BASE/$GITEA_OWNER/$MCP_SOURCE_REPO.git}"
DRY_RUN="${DRY_RUN:-0}"

BRANCH_PREFIX="auto/contract-sync-"
LAST_BE_SHA_FILE="$STATE_DIR/last-be-sha"
LAST_BE_MANIFEST_FILE="$STATE_DIR/last-be-watched.tsv"
LAST_MCP_SHA_FILE="$STATE_DIR/last-mcp-sha"
LAST_MCP_MANIFEST_FILE="$STATE_DIR/last-mcp-watched.tsv"
LOCK_FILE="$STATE_DIR/lock"
PRS_FILE="$STATE_DIR/.pulls.json"
STATUS_TMP_FILE="$STATE_DIR/.pull-statuses.json"
MANIFEST_TMP_FILE="$STATE_DIR/.watched-manifest.new"
MCP_MANIFEST_TMP_FILE="$STATE_DIR/.mcp-watched-manifest.new"

CURL_TIMEOUT="${CONTRACT_SYNC_CURL_TIMEOUT:-30}"
GIT_TIMEOUT="${CONTRACT_SYNC_GIT_TIMEOUT:-300}"
REGEN_TIMEOUT="${CONTRACT_SYNC_REGEN_TIMEOUT:-600}"
AGENTIC_IMAGE="${CONTRACT_SYNC_AGENTIC_IMAGE:-git.gen.pro/gen/gen-agentic-ci:py311}"
MCP_IMAGE="${CONTRACT_SYNC_MCP_IMAGE:-python:3.12-bookworm}"
# Bounded history fetch on the merged-branch retry path. The evidence that a
# merge is still in main needs real commits, but only a few: main moves by one
# commit per sync, and the cap keeps a retry from becoming a history scan.
MERGE_ANCESTRY_DEPTH="${CONTRACT_SYNC_MERGE_ANCESTRY_DEPTH:-50}"
# The one generated artifact whose declared stamp cells the retry may disregard
# (GEN-10539). Other consumers have no such mirror and keep the plain refusal.
CATALOG_SHEET_PATH="src/gen_mcp_server/contracts/catalog-sheet.tsv"
STATUS_POLL_INTERVAL="${CONTRACT_SYNC_STATUS_POLL_INTERVAL:-20}"
STATUS_POLL_TIMEOUT="${CONTRACT_SYNC_STATUS_POLL_TIMEOUT:-900}"
MAIN_PIPELINE_WAIT="${CONTRACT_SYNC_MAIN_PIPELINE_WAIT:-180}"
COMMIT_NAME="${CONTRACT_SYNC_COMMIT_NAME:-contract-sync}"
COMMIT_EMAIL="${CONTRACT_SYNC_COMMIT_EMAIL:-contract-sync@gen.pro}"

# gen-backend-v2 paths whose change triggers a consumer sync. A change anywhere
# else (app code, tests) is not a contract change and must not open a PR.
WATCHED_PATHS=(
  "docs/generated"
  "config/creation_cards.yml"
  "config/model_capabilities.yml"
)

# Rails owns backend contracts; the MCP owns its public tool/route projection.
# Both catalog records trigger docs regeneration, never backend consumer work
# on an MCP-only change (GEN-8246).
MCP_WATCHED_PATHS=(
  "src/gen_mcp_server/contracts/catalog-record/schema-paths.tsv"
  "src/gen_mcp_server/contracts/catalog-record/contract-paths.tsv"
)

# gen-mcp-server regeneration, in this order. These are the vendor_*.py scripts
# whose upstream is gen-backend-v2: vendor_publish_platforms.py reads
# gen-backend-python and vendor_layer_prompts.py reads limitless-data, so a
# backend-v2 clone cannot run them and they are deliberately absent.
MCP_REGENERATE=(
  "scripts/vendor_creation_card_branches.py"
  "scripts/vendor_creation_card_matrix.py"
  "scripts/vendor_user_job_enums.py"
  "scripts/vendor_vidsheet_action_schema.py"
  "scripts/sync_model_capabilities.py"
)

# gen-agentic consumes these three artifacts byte-identically at
# src/gen/contracts/. Its freshness gate byte-compares the remote artifact, so
# copying the generated bytes is the whole regeneration step.
GEN_AGENTIC_ARTIFACTS=(
  "docs/generated/vidsheet-operations-schema.json"
  "docs/generated/vidsheet-semantic-draft-schema.json"
  "docs/generated/user-job-enums.json"
)

# Consumers, in the order they are processed. A repo is cloned from
# $GIT_BASE/$GITEA_OWNER/<name>.git and regenerated by the matching case arm in
# process_consumer(). CONTRACT_SYNC_CONSUMERS narrows the list for the offline
# test; it is space-separated.
CONSUMERS=(
  "gen-mcp-server"
  "gen-agentic"
  "limitless-fe"
  "api-docs"
)
if [ -n "${CONTRACT_SYNC_CONSUMERS:-}" ]; then
  read -r -a CONSUMERS <<<"$CONTRACT_SYNC_CONSUMERS"
fi

# git must fail fast without credentials instead of blocking a timer run on a
# prompt. git.gen.pro credentials come from the box's git credential helper, not
# from the API token, which is only for the API calls below.
export GIT_TERMINAL_PROMPT=0

# Everything the script runs lives next to it, including the catalog-sheet
# retry comparator.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# -----------------------------------------------------------------------------
# Logging. One line per event on stdout, which systemd forwards to journald.
# -----------------------------------------------------------------------------

log() {
  printf '%s contract-sync: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

# -----------------------------------------------------------------------------
# Gitea API. The API token appears only as a curl header argument; its value is
# never printed, and the presence check below is the only other mention.
# -----------------------------------------------------------------------------

require_token() {
  [ -n "${GEN_GITEA_TOKEN:-}" ] || {
    log "ERROR: the Gitea API token is not set; refusing to push or open a PR"
    exit 2
  }
}

gitea_request() { # <method> <path> [payload-file] — response body on stdout
  local method="$1" path="$2" payload="${3:-}"
  if [ -n "$payload" ]; then
    curl -sS --max-time "$CURL_TIMEOUT" -X "$method" \
      -H "Authorization: token ${GEN_GITEA_TOKEN:-}" \
      -H "User-Agent: gen-contract-sync" \
      -H "Content-Type: application/json" \
      --data-binary "@$payload" \
      "$GITEA_URL/api/v1$path"
  else
    curl -sS --max-time "$CURL_TIMEOUT" -X "$method" \
      -H "Authorization: token ${GEN_GITEA_TOKEN:-}" \
      -H "User-Agent: gen-contract-sync" \
      "$GITEA_URL/api/v1$path"
  fi
}

gitea_status_code() { # <method> <path> [payload-file] — HTTP status on stdout
  local method="$1" path="$2" payload="${3:-}"
  if [ -n "$payload" ]; then
    curl -sS --max-time "$CURL_TIMEOUT" -o /dev/null -w '%{http_code}' -X "$method" \
      -H "Authorization: token ${GEN_GITEA_TOKEN:-}" \
      -H "User-Agent: gen-contract-sync" \
      -H "Content-Type: application/json" \
      --data-binary "@$payload" \
      "$GITEA_URL/api/v1$path" || true
  else
    curl -sS --max-time "$CURL_TIMEOUT" -o /dev/null -w '%{http_code}' -X "$method" \
      -H "Authorization: token ${GEN_GITEA_TOKEN:-}" \
      -H "User-Agent: gen-contract-sync" \
      "$GITEA_URL/api/v1$path" || true
  fi
}

# Print a scalar from a JSON file at a dotted path, or an empty line when absent.
json_scalar() { # <file> <dotted.path>
  python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    print("")
    raise SystemExit(0)
for part in sys.argv[2].split("."):
    if isinstance(data, list):
        try:
            data = data[int(part)]
        except (IndexError, ValueError):
            data = None
    elif isinstance(data, dict):
        data = data.get(part)
    else:
        data = None
    if data is None:
        break
print("" if data is None or isinstance(data, (dict, list)) else data)
' "$1" "$2"
}

# Verdict for a Gitea statuses payload: the NEWEST row per context (by row id,
# never API order), restricted to a context prefix. Prints NONE, GREEN, RED:<cs>
# or PENDING:<cs>. Only a success counts as green; anything unrecognised is
# pending, so an unknown state can never be mistaken for a pass.
status_verdict() { # <statuses-file> <context-prefix>
  python3 -c '
import json, sys
try:
    rows = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    print("PENDING:unreadable-statuses")
    raise SystemExit(0)
prefix = sys.argv[2]
newest = {}
for row in rows if isinstance(rows, list) else []:
    context = str(row.get("context") or "")
    if prefix and not context.startswith(prefix):
        continue
    if context not in newest or int(row.get("id") or 0) > int(newest[context].get("id") or 0):
        newest[context] = row
if not newest:
    print("NONE")
    raise SystemExit(0)
red = sorted(
    "%s:%s" % (c, r.get("status"))
    for c, r in newest.items()
    if r.get("status") in ("failure", "error", "warning")
)
pending = sorted(
    "%s:%s" % (c, r.get("status"))
    for c, r in newest.items()
    if r.get("status") not in ("success", "failure", "error", "warning")
)
if red:
    print("RED:" + ",".join(red))
elif pending:
    print("PENDING:" + ",".join(pending))
else:
    print("GREEN")
' "$1" "$2"
}

# -----------------------------------------------------------------------------
# git helpers
# -----------------------------------------------------------------------------

clone_repo() { # <url> <branch> <dir>
  local url="$1" branch="$2" dir="$3"
  case "$dir" in
    "$STATE_DIR"/*) ;;
    *)
      log "ERROR: refusing to refresh $dir outside $STATE_DIR"
      return 1
      ;;
  esac
  rm -rf -- "$dir"
  timeout "$GIT_TIMEOUT" git clone --quiet --depth 1 --branch "$branch" \
    --single-branch "$url" "$dir" || return 1
  # A shallow clone does not always publish the remote-tracking ref for the
  # branch it checked out. The consumer generators read the backend through
  # `git show origin/<branch>:<path>`, so make the ref exist explicitly.
  if ! git -C "$dir" show-ref --verify --quiet "refs/remotes/origin/$branch"; then
    git -C "$dir" update-ref "refs/remotes/origin/$branch" HEAD
  fi
  return 0
}

remote_source_sha() { # <url> <branch>
  timeout "$GIT_TIMEOUT" git ls-remote "$1" "refs/heads/$2" \
    | awk 'NR==1 {print $1}'
}

# "<blob-sha>\t<path>" for every watched file, sorted by git. Comparing this
# against the stored manifest answers "did a watched path change" without
# needing the previous commit's tree, which a depth-1 clone does not have.
watched_manifest() { # <source-clone> <path...>
  local clone="$1"
  shift
  git -C "$clone" ls-tree -r HEAD -- "$@" | awk '{print $3 "\t" $4}'
}

commit_message_file() { # <file> <subject> <backend-sha> <mcp-sha-or-empty> <path...>
  local file="$1" subject="$2" sha="$3" mcp_sha="$4"
  shift 4
  {
    printf '%s\n\n' "$subject"
    printf 'Synced from gen-backend-v2 %s.\n\n' "$sha"
    if [ -n "$mcp_sha" ]; then
      printf 'Public MCP projection from gen-mcp-server %s.\n\n' "$mcp_sha"
    fi
    printf '%s\n' "$@"
  } >"$file"
}

# Record the synced sha and the watched-file manifest together, each through a
# rename so a kill mid-write cannot leave a torn state file behind.
record_state() { # <backend-sha> <manifest-file>
  cp -f "$2" "$LAST_BE_MANIFEST_FILE.new"
  mv -f "$LAST_BE_MANIFEST_FILE.new" "$LAST_BE_MANIFEST_FILE"
  printf '%s\n' "$1" >"$LAST_BE_SHA_FILE.new"
  mv -f "$LAST_BE_SHA_FILE.new" "$LAST_BE_SHA_FILE"
}

record_sources() { # <backend-sha> <mcp-sha>
  # A plan cannot consume a change that a later real tick still owes (GEN-8246).
  if [ "$DRY_RUN" = "1" ]; then
    log "DRY_RUN: source state not advanced"
    return 0
  fi
  record_state "$1" "$MANIFEST_TMP_FILE"
  cp -f "$MCP_MANIFEST_TMP_FILE" "$LAST_MCP_MANIFEST_FILE.new"
  mv -f "$LAST_MCP_MANIFEST_FILE.new" "$LAST_MCP_MANIFEST_FILE"
  printf '%s\n' "$2" >"$LAST_MCP_SHA_FILE.new"
  mv -f "$LAST_MCP_SHA_FILE.new" "$LAST_MCP_SHA_FILE"
}

# -----------------------------------------------------------------------------
# Per-consumer regeneration. Each function writes into the consumer clone with
# the repo's own generator; none of them edits an artifact by hand.
# -----------------------------------------------------------------------------

# These functions run inside a `|| rc=$?` list, where bash suppresses errexit
# for the whole function body. Every step that must stop the consumer has to
# check its own status explicitly.

regen_gen_mcp_server() { # <consumer-clone> <backend-clone>
  local clone="$1" backend="$2" script
  # scripts/refresh_contracts.py re-vendors AND moves the pins that must change
  # with the artifacts (action-schema hash, catalog record, rc09 tool cards).
  # Vendoring alone leaves the sync PR red (GEN-8145). The pins need the
  # package installed, so it runs in a Python image that also carries git.
  if [ -f "$clone/scripts/refresh_contracts.py" ]; then
    # shellcheck disable=SC2016  # $0 belongs to the inner sh, not this shell
    timeout "$REGEN_TIMEOUT" docker run --rm --cpus 2 \
      -v "$clone":/w -v "$backend":/backend -w /w "$MCP_IMAGE" sh -c \
      'git config --global --add safe.directory "*" &&
        pip install -q -e . pytest >/dev/null 2>&1 &&
        python scripts/refresh_contracts.py --backend-path /backend --ref "origin/$0"' \
      "$BACKEND_BRANCH" || return $?
    return 0
  fi
  for script in "${MCP_REGENERATE[@]}"; do
    ( cd "$clone" && timeout "$REGEN_TIMEOUT" python3 "$script" \
        --backend-path "$backend" --ref "origin/$BACKEND_BRANCH" ) || return $?
  done
}

regen_gen_agentic() { # <consumer-clone> <backend-clone>
  local clone="$1" backend="$2" artifact dest
  for artifact in "${GEN_AGENTIC_ARTIFACTS[@]}"; do
    dest="$clone/src/gen/contracts/$(basename "$artifact")"
    mkdir -p "$(dirname "$dest")"
    [ -f "$backend/$artifact" ] || {
      log "gen-agentic: ERROR backend is missing $artifact"
      return 1
    }
    cp -- "$backend/$artifact" "$dest" || return 1
  done
  # The embedded MCP package's creation-cards.json is derived from gen.* modules
  # that read the contracts copied above, and a parity test byte-compares it, so
  # copying without rebuilding leaves the sync PR red. gen.* needs the project's
  # dependencies, which the CI image provides.
  [ -f "$clone/scripts/build_creation_card_artifact.py" ] || return 0
  timeout "$REGEN_TIMEOUT" docker run --rm --cpus 2 -v "$clone":/w -w /w \
    "$AGENTIC_IMAGE" sh -c 'pip install -q -e . >/dev/null 2>&1 &&
      PYTHONPATH=src python scripts/build_creation_card_artifact.py' || return $?
}

regen_limitless_fe() { # <consumer-clone> <backend-clone>
  local clone="$1" backend="$2"
  local artifact="src/schema/vidsheets/contracts/vidsheet-operations-schema.json"
  local sidecar="src/schema/vidsheets/contracts/vidsheet-operations-schema.source.json"
  local generated="src/schema/vidsheets/railsContract.generated.ts"
  if [ -f "$clone/scripts/sync-vidsheet-contract.mjs" ]; then
    # The repo's own generator so the artifact, its provenance sidecar and the
    # derived railsContract.generated.ts stay consistent. It stamps a fresh
    # vendoredAt on every run, so when the artifact bytes did not change there
    # is nothing to ship and those timestamp-only edits are reverted.
    ( cd "$clone" && GEN_BACKEND_PATH="$backend" timeout "$REGEN_TIMEOUT" \
        node scripts/sync-vidsheet-contract.mjs ) || return $?
    if git -C "$clone" diff --quiet -- "$artifact"; then
      git -C "$clone" checkout -- "$sidecar" "$generated" 2>/dev/null || true
    fi
  else
    mkdir -p "$(dirname "$clone/$artifact")"
    cp -- "$backend/docs/generated/vidsheet-operations-schema.json" "$clone/$artifact" || return 1
  fi
}

# 20 means "skip": the repo does not carry a regeneration command yet.
regen_api_docs() { # <consumer-clone> <backend-clone> <mcp-clone> <mcp-sha>
  local clone="$1" backend="$2" mcp="$3" mcp_sha="$4"
  if [ ! -f "$clone/scripts/sync-from-backend.mjs" ]; then
    SKIP_REASON="scripts/sync-from-backend.mjs is not on $CONSUMER_BRANCH yet"
    return 20
  fi
  # The docs generator checks tool parity, so its offline snapshot precedes
  # the registry render. Both read the same immutable clone (GEN-8246).
  ( cd "$clone" && timeout "$REGEN_TIMEOUT" node scripts/update-mcp-tools-snapshot.mjs \
      --offline --aliases-repo "$mcp" --ref "$mcp_sha" ) || return $?
  # The snapshot generator stamps generated_at even when its content matches.
  # Revert only that timestamp-only diff rather than publishing a clock tick.
  if python3 - "$clone" <<'PY'
import json
import pathlib
import subprocess
import sys
clone = pathlib.Path(sys.argv[1])
relative = "scripts/mcp-tools-snapshot.json"
try:
    prior = json.loads(subprocess.check_output(["git", "-C", str(clone), "show", f"HEAD:{relative}"], stderr=subprocess.DEVNULL))
    current = json.loads((clone / relative).read_text())
except (OSError, ValueError, subprocess.CalledProcessError):
    raise SystemExit(1)
for item in (prior, current):
    item.pop("generated_at", None)
raise SystemExit(0 if prior == current else 1)
PY
  then
    git -C "$clone" checkout -- scripts/mcp-tools-snapshot.json || return $?
  fi
  ( cd "$clone" && timeout "$REGEN_TIMEOUT" node scripts/sync-from-backend.mjs \
      --backend "$backend" --mcp-repo "$mcp" ) || return $?
  # openapi.yaml's operation regions render from the contracts the step above
  # vendors; api-docs CI checks both outputs, so both regenerate together.
  [ -f "$clone/scripts/sync_mcp_surface.py" ] || return 0
  ( cd "$clone" && timeout "$REGEN_TIMEOUT" python3 scripts/sync_mcp_surface.py \
      --derive-servers --mcp-src "$mcp" ) || return $?
  ( cd "$clone" && timeout "$REGEN_TIMEOUT" python3 scripts/sync_mcp_surface.py )
}

# -----------------------------------------------------------------------------
# PR lifecycle
# -----------------------------------------------------------------------------

remote_branch_exists() { # <repo> <branch> — 0 yes, 1 no, 2 unknown
  local code
  code="$(gitea_status_code GET "/repos/$GITEA_OWNER/$1/branches/$2")"
  case "$code" in
    200) return 0 ;;
    404) return 1 ;;
    *) return 2 ;;
  esac
}

find_open_pr() { # <repo> <branch> — PR number or empty
  local repo="$1" branch="$2"
  gitea_request GET "/repos/$GITEA_OWNER/$repo/pulls?state=open&limit=50" >"$PRS_FILE" \
    || return 0
  python3 -c '
import json, sys
try:
    pulls = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    print("")
    raise SystemExit(0)
for pull in pulls if isinstance(pulls, list) else []:
    if (pull.get("head") or {}).get("ref") == sys.argv[2]:
        print(pull.get("number"))
        break
else:
    print("")
' "$PRS_FILE" "$branch"
}

create_pr() { # <repo> <branch> <title> <body> — PR number or empty
  local repo="$1" branch="$2" title="$3" body="$4"
  local payload="$STATE_DIR/.pr-payload.json"
  python3 -c '
import json, sys
print(json.dumps({
    "head": sys.argv[1],
    "base": sys.argv[2],
    "title": sys.argv[3],
    "body": sys.argv[4],
}))
' "$branch" "$CONSUMER_BRANCH" "$title" "$body" >"$payload"
  gitea_request POST "/repos/$GITEA_OWNER/$repo/pulls" "$payload" >"$PRS_FILE" || return 0
  json_scalar "$PRS_FILE" "number"
}

# Poll a commit's statuses until every newest-per-context row is a success, a
# terminal failure, or the wait budget expires. With none_terminal=yes an empty
# status list is also an answer (a repo with no pipeline on that branch).
await_checks() { # <repo> <sha> <context-prefix> <max-wait-sec> <none-terminal>
  local repo="$1" sha="$2" prefix="$3" max_wait="$4" none_terminal="$5"
  local deadline verdict now
  deadline=$(( $(date +%s) + max_wait ))
  while :; do
    gitea_request GET "/repos/$GITEA_OWNER/$repo/commits/$sha/statuses?limit=50" \
      >"$STATUS_TMP_FILE" || true
    verdict="$(status_verdict "$STATUS_TMP_FILE" "$prefix")"
    case "$verdict" in
      GREEN | RED:* | PENDING:unreadable-statuses)
        printf '%s' "$verdict"
        return 0
        ;;
      NONE)
        if [ "$none_terminal" = "yes" ]; then
          printf '%s' "$verdict"
          return 0
        fi
        ;;
    esac
    now=$(date +%s)
    if [ "$now" -ge "$deadline" ]; then
      printf '%s' "$verdict"
      return 0
    fi
    sleep "$STATUS_POLL_INTERVAL"
  done
}

merge_pr() { # <repo> <number> <style> — HTTP status on stdout
  local repo="$1" number="$2" style="$3"
  local payload="$STATE_DIR/.merge-payload.json"
  printf '{"Do":"%s"}\n' "$style" >"$payload"
  gitea_status_code POST "/repos/$GITEA_OWNER/$repo/pulls/$number/merge" "$payload"
}

# Post a comment, then close a PR. The comment goes first so the reason
# survives a failed close; neither call may abort the tick.
comment_and_close_pr() { # <repo> <number> <comment>
  local repo="$1" number="$2" comment="$3"
  local payload="$STATE_DIR/.pr-comment.json"
  python3 -c 'import json, sys; print(json.dumps({"body": sys.argv[1]}))' "$comment" >"$payload"
  gitea_status_code POST "/repos/$GITEA_OWNER/$repo/issues/$number/comments" "$payload" >/dev/null || true
  python3 -c 'import json, sys; print(json.dumps({"state": sys.argv[1]}))' closed >"$payload"
  gitea_status_code PATCH "/repos/$GITEA_OWNER/$repo/pulls/$number" "$payload" >/dev/null || true
}

# Close every open auto/contract-sync PR in <repo> whose branch names a backend
# sha other than <keep-sha>. A merged or already-in-sync consumer otherwise
# leaves older PRs open forever, which is how eight of them sat for a week
# (GEN-9537). The caller supplies the comment naming the newer sha or
# "main already in sync".
close_superseded_prs() { # <repo> <keep-sha> <comment>
  local repo="$1" keep_sha="$2" comment="$3"
  [ "$DRY_RUN" = "1" ] && return 0
  local keep8="${keep_sha:0:8}"
  local number ref suffix be8
  gitea_request GET "/repos/$GITEA_OWNER/$repo/pulls?state=open&limit=50" >"$PRS_FILE" 2>/dev/null || return 0
  while IFS=$'\t' read -r number ref; do
    [ -n "$number" ] || continue
    suffix="${ref#"$BRANCH_PREFIX"}"
    be8="${suffix%%-*}"
    [ "$be8" = "$keep8" ] && continue
    log "$repo: closing superseded PR #$number ($ref)"
    comment_and_close_pr "$repo" "$number" "$comment"
  done < <(
    python3 - "$PRS_FILE" "$BRANCH_PREFIX" <<'PY'
import json
import sys
try:
    pulls = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    pulls = []
prefix = sys.argv[2]
for pull in pulls if isinstance(pulls, list) else []:
    ref = str((pull.get("head") or {}).get("ref") or "")
    if ref.startswith(prefix):
        print("%s\t%s" % (pull.get("number"), ref))
PY
  )
}

# Merge the PR for this backend sha and, when it lands, close the older
# superseded PRs in the same consumer. A merge is proof the older sha is
# obsolete; while the PR is red or pending nothing is closed.
merge_and_cleanup() { # <repo> <number> <head-sha> <branch> <backend-sha>
  local repo="$1" number="$2" head_sha="$3" branch="$4" backend_sha="$5" rc=0
  merge_when_green "$repo" "$number" "$head_sha" "$branch" || rc=$?
  if [ "$rc" = "0" ]; then
    close_superseded_prs "$repo" "$backend_sha" \
      "Superseded by backend ${backend_sha:0:8}, which this PR carried and merged."
  fi
  return "$rc"
}

merge_when_green() { # <repo> <number> <head-sha> <branch> — 0 merged, 11 retry next tick
  local repo="$1" number="$2" head_sha="$3" branch="$4"
  local verdict main_sha main_verdict tried style code

  # Only ci/woodpecker/* gates a merge, as in branch protection. Other contexts
  # (GitHub-Actions workflows no runner executes) stay pending forever.
  verdict="$(await_checks "$repo" "$head_sha" "ci/woodpecker/" "$STATUS_POLL_TIMEOUT" "no")"
  case "$verdict" in
    GREEN) ;;
    NONE)
      log "$repo: no check status for $head_sha yet; PR #$number is re-checked next tick"
      return 11
      ;;
    PENDING:*)
      log "$repo: checks still pending (${verdict#PENDING:}) after ${STATUS_POLL_TIMEOUT}s; PR #$number is re-checked next tick"
      return 11
      ;;
    RED:*)
      log "$repo: checks not green (${verdict#RED:}); leaving PR #$number open, re-checked next tick"
      return 11
      ;;
  esac

  # Merging cancels the pipeline running for main's current head when that repo
  # runs its pipeline with concurrency: 1, which leaves main with no verdict at
  # all. Wait for main's push contexts to settle first; a repo with none has
  # nothing to cancel.
  main_sha="$(json_scalar <(gitea_request GET "/repos/$GITEA_OWNER/$repo/branches/$CONSUMER_BRANCH" || true) "commit.id")"
  if [ -z "$main_sha" ]; then
    log "$repo: cannot read $CONSUMER_BRANCH head; leaving PR #$number open, re-checked next tick"
    return 11
  fi
  main_verdict="$(await_checks "$repo" "$main_sha" "ci/woodpecker/push/" "$MAIN_PIPELINE_WAIT" "yes")"
  case "$main_verdict" in
    GREEN | NONE) ;;
    *)
      log "$repo: $CONSUMER_BRANCH $main_sha push pipeline is ${main_verdict#*:}; PR #$number is re-checked next tick"
      return 11
      ;;
  esac

  tried=""
  for style in fast-forward-only rebase; do
    tried="${tried:+$tried,}$style"
    code="$(merge_pr "$repo" "$number" "$style")"
    if [ "$code" = "200" ]; then
      log "$repo: merged PR #$number ($style)"
      return 0
    fi
  done
  # A fast-forward-only repo refuses the merge once main has moved past the
  # sync branch. Rebase the PR onto main server-side; its pipeline re-runs and
  # the next tick re-checks and merges it.
  code="$(gitea_status_code POST "/repos/$GITEA_OWNER/$repo/pulls/$number/update?style=rebase")"
  if [ "$code" = "200" ]; then
    log "$repo: merge refused for PR #$number ($tried); rebased it onto $CONSUMER_BRANCH, re-checked next tick"
    return 11
  fi
  if [ "$code" = "409" ] || [ "$code" = "422" ]; then
    # A rebase conflict cannot be resolved by the bot. Close the PR and drop its
    # branch so the next tick regenerates a fresh branch from current main
    # instead of re-visiting a PR that can never merge (GEN-9537).
    comment_and_close_pr "$repo" "$number" \
      "Closed: this sync branch conflicts with $CONSUMER_BRANCH and cannot be rebased automatically. A fresh branch from current main is generated on the next run."
    gitea_status_code DELETE "/repos/$GITEA_OWNER/$repo/branches/$branch" >/dev/null || true
    log "$repo: rebase of PR #$number onto $CONSUMER_BRANCH conflicted (HTTP $code); closed it and dropped the branch, next tick regenerates"
    return 11
  fi
  log "$repo: merge refused for PR #$number ($tried) and rebase onto $CONSUMER_BRANCH failed (HTTP $code); re-checked next tick"
  return 11
}

# -----------------------------------------------------------------------------
# A retained branch whose PR already merged this exact input (GEN-10539).
#
# A retry after a sibling failure re-processes every consumer, including one
# whose own PR merged on the earlier tick. That branch has no open PR, so the
# plain rule would refuse it forever and the checkpoint would never advance.
# The evidence that closes the gap is the merge itself: the PR for this exact
# branch merged into the consumer branch, from this consumer's own repo, with
# this run's backend and MCP provenance in its body, and the commit it merged
# is still in main's history. Nothing else is accepted, and the only tolerated
# regeneration difference is the catalog mirror's declared stamp cell.
# -----------------------------------------------------------------------------

merged_pr_evidence() { # <repo> <branch> <backend-sha> <mcp-sha> — "number<TAB>head"
  local repo="$1" branch="$2" backend_sha="$3" mcp_sha="$4"
  gitea_request GET "/repos/$GITEA_OWNER/$repo/pulls?state=all&limit=50" >"$PRS_FILE" || return 1
  python3 - "$PRS_FILE" "$GITEA_OWNER/$repo" "$CONSUMER_BRANCH" "$branch" \
    "$backend_sha" "$mcp_sha" <<'PY'
import json, sys
path, repo_full, base_ref, branch, backend_sha, mcp_sha = sys.argv[1:7]
try:
    pulls = json.load(open(path))
except (OSError, ValueError):
    raise SystemExit(1)
provenance = {"Synced from gen-backend-v2 %s" % backend_sha}
if mcp_sha:
    provenance.add("Public MCP projection from gen-mcp-server %s" % mcp_sha)
for pull in pulls if isinstance(pulls, list) else []:
    head = pull.get("head") or {}
    base = pull.get("base") or {}
    if (head.get("ref") or "") != branch:
        continue
    if pull.get("state") != "closed" or pull.get("merged") is not True:
        continue
    if (base.get("ref") or "") != base_ref:
        continue
    if ((base.get("repo") or {}).get("full_name") or "") != repo_full:
        continue
    if ((head.get("repo") or {}).get("full_name") or "") != repo_full:
        continue
    lines = {line.strip().rstrip(".") for line in (pull.get("body") or "").splitlines()}
    if not provenance <= lines:
        continue
    print("%s\t%s" % (pull.get("number"), head.get("sha") or ""))
    break
PY
}

# main's history must contain the merged commit. A shallow clone carries one
# commit, so deepen by a capped number of commits instead of cloning history;
# a fetch that cannot prove ancestry refuses.
merged_head_is_in_main() { # <clone> <branch> <merged-sha>
  local clone="$1" branch="$2" sha="$3"
  if [ "$(git -C "$clone" rev-parse HEAD)" != "$sha" ]; then
    timeout "$GIT_TIMEOUT" git -C "$clone" fetch --quiet --deepen="$MERGE_ANCESTRY_DEPTH" \
      origin "+refs/heads/$CONSUMER_BRANCH:refs/remotes/origin/$CONSUMER_BRANCH" \
      "+refs/heads/$branch:refs/remotes/origin/$branch" || return 1
  fi
  git -C "$clone" cat-file -e "$sha^{commit}" 2>/dev/null || return 1
  git -C "$clone" merge-base --is-ancestor "$sha" HEAD
}

# The comparison runs the consumer's own catalog_sheet module. In a consumer
# checkout that installs its package, `refresh_contracts.py` is also what makes
# the module importable, so the comparison uses that same regeneration image and
# invocation; a checkout whose catalogue is a standalone stdlib module needs no
# runtime of its own. Neither run writes into the clone.
catalog_sheet_retry_check() { # <consumer-clone> — 0 only declared stamps differ
  local clone="$1"
  if [ -f "$clone/scripts/refresh_contracts.py" ]; then
    # shellcheck disable=SC2016  # $0 belongs to the inner sh, not this shell
    timeout "$REGEN_TIMEOUT" docker run --rm --cpus 2 \
      -v "$clone":/w -v "$SCRIPT_DIR/catalog_sheet_retry_check.py":/retry_check.py:ro \
      -w /w "$MCP_IMAGE" sh -c \
      'git config --global --add safe.directory "*" &&
        pip install -q -e . >/dev/null 2>&1 &&
        PYTHONDONTWRITEBYTECODE=1 python /retry_check.py /w "$0"' \
      "$CATALOG_SHEET_PATH"
    return $?
  fi
  PYTHONDONTWRITEBYTECODE=1 python3 "$SCRIPT_DIR/catalog_sheet_retry_check.py" \
    "$clone" "$CATALOG_SHEET_PATH"
}

# True only for the catalog mirror retried after its own merge, where the whole
# regenerated difference is the served_sha stamp the mirror declares. The
# comparison is the consumer's own catalog_sheet parse/render, so a changed
# cell, header, key, row count or extra file refuses here and the caller keeps
# the plain refusal (zero remote writes either way).
merged_retry_is_converged() { # <name> <clone> <branch> <backend-sha> <mcp-sha> <paths...>
  local name="$1" clone="$2" branch="$3" backend_sha="$4" mcp_sha="$5"
  shift 5
  [ "$name" = "gen-mcp-server" ] || return 1
  [ "$#" = "1" ] && [ "$1" = "$CATALOG_SHEET_PATH" ] || return 1
  local evidence number merged_head current_head
  # gen-mcp-server's regeneration and its PR body carry the backend provenance
  # only; the MCP source never reaches this consumer.
  evidence="$(merged_pr_evidence "$name" "$branch" "$backend_sha" "")" || return 1
  number="${evidence%%$'\t'*}"
  merged_head="${evidence#*$'\t'}"
  [ -n "$number" ] && [ -n "$merged_head" ] || return 1
  current_head="$(json_scalar <(gitea_request GET "/repos/$GITEA_OWNER/$name/branches/$branch" || true) "commit.id")"
  [ -n "$current_head" ] && [ "$current_head" = "$merged_head" ] || return 1
  merged_head_is_in_main "$clone" "$branch" "$merged_head" || return 1
  catalog_sheet_retry_check "$clone"
}

# -----------------------------------------------------------------------------
# One consumer: regenerate, and hand a diff off as a green-and-merged PR.
# Returns 0 (nothing to do / merged / skipped), 11 (a PR is still open and is
# re-checked next tick) or 1 (error, so the next tick retries instead of losing
# the change). A red check or a rebase conflict never advances the state, so the
# next tick always revisits the open PR instead of abandoning it (GEN-9537).
# -----------------------------------------------------------------------------

process_consumer() { # <name> <backend-sha> <backend-clone> <mcp-sha> <mcp-clone>
  local name="$1" backend_sha="$2" backend="$3" mcp_sha="$4" mcp="$5"
  local clone="$STATE_DIR/$name"
  local url="$GIT_BASE/$GITEA_OWNER/$name.git"
  local branch="$BRANCH_PREFIX${backend_sha:0:8}"
  local title="chore: sync backend contracts to gen-backend-v2 ${backend_sha:0:8}"
  local provenance_mcp=""
  if [ "$name" = "api-docs" ]; then
    branch="$branch-${mcp_sha:0:8}"
    title="chore: sync API contracts from backend ${backend_sha:0:8} and MCP ${mcp_sha:0:8}"
    provenance_mcp="$mcp_sha"
  fi
  local regen_rc=0 pr_number="" head_sha="" body="" status_file
  local -a changed_paths=()
  local entry

  SKIP_REASON=""

  clone_repo "$url" "$CONSUMER_BRANCH" "$clone" || {
    log "$name: ERROR cannot clone $url"
    return 1
  }

  case "$name" in
    gen-mcp-server) regen_gen_mcp_server "$clone" "$backend" || regen_rc=$? ;;
    gen-agentic) regen_gen_agentic "$clone" "$backend" || regen_rc=$? ;;
    limitless-fe) regen_limitless_fe "$clone" "$backend" || regen_rc=$? ;;
    api-docs) regen_api_docs "$clone" "$backend" "$mcp" "$mcp_sha" || regen_rc=$? ;;
    *)
      log "$name: ERROR no regeneration command is defined"
      return 1
      ;;
  esac

  if [ "$regen_rc" = "20" ]; then
    log "$name: skipped — $SKIP_REASON"
    return 0
  fi
  if [ "$regen_rc" != "0" ]; then
    log "$name: ERROR regeneration failed (exit $regen_rc)"
    return 1
  fi

  status_file="$STATE_DIR/.status.$name"
  if ! git -C "$clone" status --porcelain -z >"$status_file"; then
    log "$name: ERROR cannot read the working tree status"
    return 1
  fi
  while IFS= read -r -d '' entry; do
    changed_paths+=("${entry:3}")
  done <"$status_file"

  if [ "${#changed_paths[@]}" = "0" ]; then
    log "$name: no diff — vendored copies already current with ${backend_sha:0:8}"
    close_superseded_prs "$name" "$backend_sha" \
      "main already in sync with backend ${backend_sha:0:8}; this auto/contract-sync PR is superseded."
    return 0
  fi

  if [ "$DRY_RUN" = "1" ]; then
    log "$name: ${#changed_paths[@]} file(s) changed — DRY_RUN plan: branch $branch, commit \"$title\", push, open PR, poll checks, merge (fast-forward-only then rebase); backend $backend_sha${provenance_mcp:+; MCP $provenance_mcp}; paths: ${changed_paths[*]}"
    return 0
  fi

  # A branch for this backend sha that already has an open PR is the previous
  # run's handoff. Never reuse a branch whose PR a human closed.
  local branch_rc=0
  remote_branch_exists "$name" "$branch" || branch_rc=$?
  if [ "$branch_rc" = "0" ]; then
    pr_number="$(find_open_pr "$name" "$branch")"
    if [ -n "$pr_number" ]; then
      head_sha="$(json_scalar <(gitea_request GET "/repos/$GITEA_OWNER/$name/branches/$branch" || true) "commit.id")"
      log "$name: PR #$pr_number is already open for ${backend_sha:0:8}; re-checking it"
      merge_and_cleanup "$name" "$pr_number" "$head_sha" "$branch" "$backend_sha"
      return $?
    fi
    if merged_retry_is_converged "$name" "$clone" "$branch" "$backend_sha" "$mcp_sha" \
      "${changed_paths[@]}"; then
      log "$name: branch $branch merged this input already; the only regenerated difference is the declared catalog mirror stamp — nothing to sync"
      return 0
    fi
    log "$name: ERROR branch $branch exists with no open PR; refusing to reuse it"
    return 1
  elif [ "$branch_rc" = "2" ]; then
    log "$name: ERROR cannot read branch $branch from Gitea"
    return 1
  fi

  git -C "$clone" checkout -q -b "$branch" || {
    log "$name: ERROR cannot create branch $branch"
    return 1
  }
  git -C "$clone" add -- "${changed_paths[@]}" || {
    log "$name: ERROR cannot stage the regenerated files"
    return 1
  }
  commit_message_file "$STATE_DIR/.commit-msg.$name" "$title" "$backend_sha" "$provenance_mcp" "${changed_paths[@]}"
  git -C "$clone" -c user.name="$COMMIT_NAME" -c user.email="$COMMIT_EMAIL" \
    commit -q -F "$STATE_DIR/.commit-msg.$name" || {
    log "$name: ERROR cannot commit the regenerated files"
    return 1
  }
  head_sha="$(git -C "$clone" rev-parse HEAD)" || {
    log "$name: ERROR cannot read the new commit"
    return 1
  }

  if ! git -C "$clone" push --quiet origin "HEAD:refs/heads/$branch"; then
    log "$name: ERROR push of $branch failed"
    return 1
  fi

  pr_number="$(find_open_pr "$name" "$branch")"
  if [ -z "$pr_number" ]; then
    body="$(printf 'Synced from gen-backend-v2 %s.\n\nChanged paths:\n\n%s\n' \
      "$backend_sha" "${changed_paths[*]}")"
    if [ -n "$provenance_mcp" ]; then
      body="$(printf '%s\n\nPublic MCP projection from gen-mcp-server %s.\n' "$body" "$provenance_mcp")"
    fi
    pr_number="$(create_pr "$name" "$branch" "$title" "$body")"
  fi
  if [ -z "$pr_number" ]; then
    log "$name: ERROR $branch pushed but no PR could be opened"
    return 1
  fi

  log "$name: PR #$pr_number opened for $head_sha; waiting for checks"
  # A merge is the whole hand-off: gen-deployd deploys gen-mcp-server main to
  # staging and production (mcp.gen.pro) and every other consumer on its own.
  merge_and_cleanup "$name" "$pr_number" "$head_sha" "$branch" "$backend_sha"
}

# -----------------------------------------------------------------------------
# Run
# -----------------------------------------------------------------------------

for tool in git python3 curl flock; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "contract-sync: ERROR required tool not found: $tool" >&2
    exit 1
  }
done

mkdir -p "$STATE_DIR"

# One run at a time. A second timer tick that overlaps the first exits instead
# of cloning a second copy of every consumer.
exec 9>"$LOCK_FILE"
flock -n 9 || {
  log "another run holds $LOCK_FILE — skipping this tick"
  exit 0
}

if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN=1: regeneration runs locally; no push, PR or merge"
else
  require_token
fi

backend_sha="$(remote_source_sha "$BACKEND_URL" "$BACKEND_BRANCH" || true)"
if [ -z "$backend_sha" ]; then
  log "ERROR cannot resolve $BACKEND_URL refs/heads/$BACKEND_BRANCH"
  exit 1
fi
mcp_sha="$(remote_source_sha "$MCP_SOURCE_URL" "$MCP_SOURCE_BRANCH" || true)"
if [ -z "$mcp_sha" ]; then
  log "ERROR cannot resolve $MCP_SOURCE_URL refs/heads/$MCP_SOURCE_BRANCH"
  exit 1
fi

last_sha=""
if [ -r "$LAST_BE_SHA_FILE" ]; then
  last_sha="$(cat "$LAST_BE_SHA_FILE")"
fi

last_mcp_sha=""
if [ -r "$LAST_MCP_SHA_FILE" ]; then
  last_mcp_sha="$(cat "$LAST_MCP_SHA_FILE")"
fi

if [ "$backend_sha" = "$last_sha" ] && [ "$mcp_sha" = "$last_mcp_sha" ] \
  && [ -r "$LAST_BE_MANIFEST_FILE" ] && [ -r "$LAST_MCP_MANIFEST_FILE" ]; then
  log "backend main ${backend_sha:0:8}: no backend change; MCP main ${mcp_sha:0:8}: no MCP change since the last sync"
  exit 0
fi

backend_clone="$STATE_DIR/$BACKEND_REPO"
if ! clone_repo "$BACKEND_URL" "$BACKEND_BRANCH" "$backend_clone"; then
  log "ERROR cannot clone $BACKEND_URL"
  exit 1
fi

# main can move between the ls-remote above and this clone. The clone's HEAD is
# what the artifacts actually come from, so the branch name, the commit message
# and the state file all name this sha, not the one resolved before the clone.
backend_sha="$(git -C "$backend_clone" rev-parse HEAD)"
mcp_clone="$STATE_DIR/mcp-source"
if ! clone_repo "$MCP_SOURCE_URL" "$MCP_SOURCE_BRANCH" "$mcp_clone"; then
  log "ERROR cannot clone $MCP_SOURCE_URL"
  exit 1
fi
mcp_sha="$(git -C "$mcp_clone" rev-parse HEAD)"
# The docs registry reader uses origin/main internally; that ref must name the
# exact selected MCP clone even when its configured source branch differs.
git -C "$mcp_clone" update-ref refs/remotes/origin/main "$mcp_sha"

watched_manifest "$backend_clone" "${WATCHED_PATHS[@]}" >"$MANIFEST_TMP_FILE"
watched_manifest "$mcp_clone" "${MCP_WATCHED_PATHS[@]}" >"$MCP_MANIFEST_TMP_FILE"
watched_count="$(awk 'END {print NR+0}' "$MANIFEST_TMP_FILE")"
mcp_watched_count="$(awk 'END {print NR+0}' "$MCP_MANIFEST_TMP_FILE")"
if [ "$mcp_watched_count" != "${#MCP_WATCHED_PATHS[@]}" ]; then
  log "ERROR MCP source is missing a watched catalog record"
  exit 1
fi

backend_changed=1
mcp_changed=1
if [ -n "$last_sha" ] && [ -r "$LAST_BE_MANIFEST_FILE" ] \
  && cmp -s "$MANIFEST_TMP_FILE" "$LAST_BE_MANIFEST_FILE"; then
  backend_changed=0
fi
if [ -n "$last_mcp_sha" ] && [ -r "$LAST_MCP_MANIFEST_FILE" ] \
  && cmp -s "$MCP_MANIFEST_TMP_FILE" "$LAST_MCP_MANIFEST_FILE"; then
  mcp_changed=0
fi

if [ "$backend_changed" = "0" ] && [ "$mcp_changed" = "0" ]; then
  log "backend main ${backend_sha:0:8}, MCP main ${mcp_sha:0:8}: watched files unchanged — no consumer action"
  record_sources "$backend_sha" "$mcp_sha"
  exit 0
fi

if [ -z "$last_sha" ]; then
  log "backend main ${backend_sha:0:8}: first run ($watched_count watched file(s)) — syncing every consumer"
elif [ "$backend_changed" = "1" ]; then
  log "backend change ${last_sha:0:8}..${backend_sha:0:8}: $watched_count watched file(s)"
fi
if [ "$mcp_changed" = "1" ]; then
  log "MCP change ${last_mcp_sha:0:8}..${mcp_sha:0:8}: $mcp_watched_count watched catalog file(s)"
fi

failed=0
handed_off=0
retry=0
for name in "${CONSUMERS[@]}"; do
  # An MCP projection change does not alter the backend artifacts other
  # consumers vendor. Public docs join both exact sources once (GEN-8246).
  if [ "$backend_changed" = "0" ] && [ "$name" != "api-docs" ]; then
    continue
  fi
  rc=0
  process_consumer "$name" "$backend_sha" "$backend_clone" "$mcp_sha" "$mcp_clone" || rc=$?
  case "$rc" in
    0) ;;
    10) handed_off=1 ;;
    11) retry=1 ;;
    *) failed=1 ;;
  esac
done

# Advance only after every consumer reached a terminal disposition. A hard
# error leaves the state untouched so the next tick retries the same change
# instead of silently dropping it.
if [ "$failed" = "0" ] && [ "$retry" = "0" ]; then
  record_sources "$backend_sha" "$mcp_sha"
  if [ "$DRY_RUN" != "1" ]; then
    log "backend main ${backend_sha:0:8}, MCP main ${mcp_sha:0:8}: state advanced"
  fi
elif [ "$failed" = "0" ]; then
  log "backend main ${backend_sha:0:8}: a sync PR is still waiting on checks; state not advanced, the next tick re-checks it"
else
  log "backend main ${backend_sha:0:8}: a consumer errored; state not advanced, the next tick retries"
fi

if [ "$failed" != "0" ]; then
  exit 1
fi
if [ "$handed_off" != "0" ] || [ "$retry" != "0" ]; then
  exit 1
fi
exit 0
