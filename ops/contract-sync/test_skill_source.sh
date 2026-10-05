#!/bin/bash
# intent: gen-agentic-only skill changes enter the existing sync queue, without
# triggering frontend or unrelated backend consumers; failures remain queued.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "${TMPDIR:-/mnt/data/tmp}"
CONTRACT_SYNC_STATE_DIR="$(mktemp -d "${TMPDIR:-/mnt/data/tmp}/skills-sync-test.XXXXXX")"
export CONTRACT_SYNC_STATE_DIR
trap 'rm -rf "$CONTRACT_SYNC_STATE_DIR"' EXIT
export DRY_RUN=1
# Run the real source/change-detection flow with deterministic repository seams.
# No push, API write, container or network is used.
python3 - "$HERE/contract-sync.sh" "$CONTRACT_SYNC_STATE_DIR/driver.sh" <<'PY'
from pathlib import Path
import sys
source = Path(sys.argv[1]).read_text()
head, run = source.split('# Run\n', 1)
seams = '''
remote_source_sha() {
  case "$1" in
    *gen-backend-v2*) printf '%040d' 1 ;;
    *gen-agentic*) printf '%040d' 3 ;;
    *) printf '%040d' 2 ;;
  esac
}
clone_repo() {
  if [ "${FAIL_SKILLS_CLONE:-0}" = 1 ] && [[ "$1" == *gen-agentic* ]]; then return 1; fi
  mkdir -p "$3"
}
git() {
  case "$*" in
    *rev-parse*) case "$2" in *skills-source*) printf '%040d' 3 ;; *) printf '%040d' 1 ;; esac ;;
    *) : ;;
  esac
}
watched_manifest() {
  if [[ "$1" == *skills-source* ]]; then
    printf 'skills/vidsheet-mcp/SKILL.md\\tchanged\\nskills/_shared/ref.md\\tshared\\n'
  elif [[ "$1" == *mcp-source* ]]; then
    printf 'mcp1\\tsame\\nmcp2\\tsame\\n'
  else
    printf 'backend\\tsame\\n'
  fi
}
process_consumer() {
  printf 'CONSUMER:%s\\n' "$1"
  if [ "${FAIL_CONSUMER:-0}" = 1 ]; then return 1; fi
}
'''
Path(sys.argv[2]).write_text(head + seams + '\n' + run)
PY
printf '%040d\n' 1 >"$CONTRACT_SYNC_STATE_DIR/last-be-sha"
printf '%040d\n' 2 >"$CONTRACT_SYNC_STATE_DIR/last-mcp-sha"
printf '%040d\n' 4 >"$CONTRACT_SYNC_STATE_DIR/last-skills-sha"
printf 'backend\tsame\n' >"$CONTRACT_SYNC_STATE_DIR/last-be-watched.tsv"
printf 'mcp1\tsame\nmcp2\tsame\n' >"$CONTRACT_SYNC_STATE_DIR/last-mcp-watched.tsv"
printf 'skills/vidsheet-mcp/SKILL.md\told\nskills/_shared/ref.md\tshared\n' >"$CONTRACT_SYNC_STATE_DIR/last-skills-watched.tsv"
bash "$CONTRACT_SYNC_STATE_DIR/driver.sh" >"$CONTRACT_SYNC_STATE_DIR/output"
test "$(grep -c '^CONSUMER:' "$CONTRACT_SYNC_STATE_DIR/output")" = 1
grep -q '^CONSUMER:gen-mcp-server$' "$CONTRACT_SYNC_STATE_DIR/output"
test "$(cat "$CONTRACT_SYNC_STATE_DIR/last-skills-sha")" = "$(printf '%040d' 4)"
if FAIL_SKILLS_CLONE=1 bash "$CONTRACT_SYNC_STATE_DIR/driver.sh" >"$CONTRACT_SYNC_STATE_DIR/output"; then
  echo 'FAIL: missing skills source passed'; exit 1
fi
if FAIL_CONSUMER=1 bash "$CONTRACT_SYNC_STATE_DIR/driver.sh" >"$CONTRACT_SYNC_STATE_DIR/output"; then
  echo 'FAIL: failed consumer passed'; exit 1
fi
test "$(cat "$CONTRACT_SYNC_STATE_DIR/last-skills-sha")" = "$(printf '%040d' 4)"
# The container reads a pinned clone without fetching secrets or upstream.
grep -q -- '--agentic-path /skills-source' "$HERE/contract-sync.sh"
grep -q -- '"$skills":/skills-source:ro' "$HERE/contract-sync.sh"
echo 'PASS: skill-only source detection, scope, failure retention and pinned mount'
