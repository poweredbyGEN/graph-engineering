#!/bin/bash
# GEN-9971 T1-T3; shared QA runner protocol, architecture gen-workflow/docs/TECH-DESIGN.md, epic GEN-9662.
# Run on qa-deploy-box: bash test_qa_completion_framing.sh <candidate qa-pytest> [T1 T2 T3].
# GEN9971_BASELINE=1 records the reproduced T1 timeout and any T3 lock retention.
set -euo pipefail
if ! printenv GEN9971_BASELINE >/dev/null 2>&1; then export GEN9971_BASELINE=0; fi
BIN=$(realpath "${1:?usage: test_qa_completion_framing.sh <candidate qa-pytest> [T1 T2 T3]}"); shift
RUNNER=${QA_LANE_RUNNER:-100.116.155.101}
remote() { timeout -k 2 5 ssh -o BatchMode=yes -o ConnectTimeout=3 "root@$RUNNER" "$@"; }

count_lines() {
  local file=$1 prefix=$2
  if [ -f "$file" ]; then grep -c "^$prefix:" "$file" || true; else echo 0; fi
}

if [ "${1:-}" = --case ]; then
  c=$2 ROOT=$3 LANE=$3/lane
  mkdir -p "$LANE/src" "$ROOT/locks"
  lock_key=$(printf '%s' "$LANE" | sha256sum | cut -d' ' -f1)
  lock_file=$ROOT/locks/lane-$lock_key
  cat > "$LANE/src/qa_completion_fixture.py" <<'PY'
from pathlib import Path
import sys

outputs = {"bare": b"[]", "newline": b"[]\n", "ok": b"ok\n"}
mode = sys.argv[1]
Path(".qa-completion-calls").open("a", encoding="utf-8").write(f"{mode}\n")
sys.stdout.buffer.write(outputs[mode])
PY
  export QA_PYTEST_LOCKDIR="$ROOT/locks" QA_PYTEST_REMOTE_BIN="$BIN" PYTEST_MODULE=qa_completion_fixture
  LOCAL_RUNS="$ROOT/local-docker-runs"
  mkdir -p "$ROOT/bin"
  cat > "$ROOT/bin/docker" <<'SH'
#!/bin/bash
if [ "\${1:-}" = run ]; then printf 'local-fallback:run\n' >> "$QA_PYTEST_LOCAL_RUNS"; fi
exec "$QA_PYTEST_REAL_DOCKER" "$@"
SH
  chmod +x "$ROOT/bin/docker"
  REAL_DOCKER=$(command -v docker)
  export QA_PYTEST_LOCAL_RUNS="$LOCAL_RUNS" QA_PYTEST_REAL_DOCKER="$REAL_DOCKER"

  run_call() {
    local mode=$1 out=$2 err=$3
    CALL_RC=0
    PATH="$ROOT/bin:$PATH" timeout -k 5 20 "$BIN" "$LANE" "$mode" >"$out" 2>"$err" || CALL_RC=$?
  }
  get_runner_calls() {
    : > "$ROOT/runner.calls"
    remote "cat '$LANE/.qa-completion-calls'" > "$ROOT/runner.calls" 2>/dev/null || : > "$ROOT/runner.calls"
  }
  fallback_count() {
    count_lines "$LOCAL_RUNS" local-fallback
  }
  runner_count() { [ -f "$ROOT/runner.calls" ] && awk 'END {print NR}' "$ROOT/runner.calls" || echo 0; }
  output_is() {
    local file=$1 bytes=$2
    printf '%s' "$bytes" > "$ROOT/expected"
    cmp -s "$ROOT/expected" "$file"
  }
  marker_free() { ! grep -q 'completed-' "$1"; }
  runner_sessions() {
    remote "self=\$\$; ps -eo pid=,comm=,args= | awk -v self=\"\$self\" -v candidate='$BIN' -v lane='$LANE' '\$1 != self && \$2 == \"bash\" && index(\$0,candidate) && index(\$0,lane) {n++} END {print n+0}'"
  }
  runner_lock_free() { remote "flock -n '$lock_file' -c true" >/dev/null 2>&1; }
  wait_runner_clean() {
    local n sessions
    for n in $(seq 1 10); do
      sessions=$(runner_sessions) || return 1
      if [ "$sessions" = 0 ] && runner_lock_free; then return 0; fi
      sleep 1
    done
    echo "runner_sessions=${sessions:-unknown}" >&2
    return 1
  }
  cleanup_case() {
    local pids
    pids=$(remote "self=\$\$; ps -eo pid=,comm=,args= | awk -v self=\"\$self\" -v candidate='$BIN' -v lane='$LANE' '\$1 != self && \$2 == \"bash\" && index(\$0,candidate) && index(\$0,lane) {print \$1}'") || pids=
    if [ -n "$pids" ]; then remote "kill -TERM $pids" >/dev/null 2>&1 || true; fi
    remote "rm -rf '$LANE'; rm -f '$lock_file'" >/dev/null 2>&1 || true
  }
  trap cleanup_case EXIT
  preserve_runner_calls() {
    get_runner_calls
    [ "$(fallback_count)" = 0 ] || return 1
    cp "$ROOT/runner.calls" "$LANE/.qa-completion-calls"
  }
  run_first_bare() {
    run_call bare "$ROOT/first.stdout" "$ROOT/first.stderr"
    get_runner_calls
    FIRST_REMOTE=$(runner_count)
    FIRST_LOCAL=$(fallback_count)
    if [ "$CALL_RC" = 0 ] && output_is "$ROOT/first.stdout" $'[]\n' && marker_free "$ROOT/first.stdout"; then
      FIRST_RESULT=pass
    elif [ "${GEN9971_BASELINE:-0}" = 1 ] && [ "$CALL_RC" = 124 ] && grep -qF '[]completed-' "$ROOT/first.stdout"; then
      FIRST_RESULT=baseline-red
    else
      FIRST_RESULT=fail
    fi
  }

  case "$c" in
  T1) # intent: output without a final newline must not coalesce with or expose the completion marker.
    run_call bare "$ROOT/stdout" "$ROOT/stderr"
    get_runner_calls
    sessions=$(runner_sessions) || sessions=unknown
    if [ "$CALL_RC" = 0 ] && output_is "$ROOT/stdout" $'[]\n' && marker_free "$ROOT/stdout" && \
       [ "$(runner_count)" = 1 ] && [ "$(fallback_count)" = 0 ] && wait_runner_clean; then
      echo "PASS T1 exit=0 stdout_hex=$(od -An -tx1 -v "$ROOT/stdout" | tr -d ' \n') remote_calls=1 local_fallbacks=0 runner_sessions=0"
      exit 0
    fi
    if [ "$CALL_RC" = 124 ] && grep -qF '[]completed-' "$ROOT/stdout" && \
       [ "$(runner_count)" = 1 ] && [ "$(fallback_count)" = 0 ]; then
      echo "RED T1 baseline_timeout=124 marker_coalesced=yes remote_calls=1 local_fallbacks=0 runner_sessions=$sessions"
      exit 1
    fi
    echo "FAIL T1 exit=$CALL_RC remote_calls=$(runner_count) local_fallbacks=$(fallback_count) runner_sessions=$sessions"
    exit 1 ;;
  T2) # intent: newline-terminated output keeps its exact bytes and hides the completion marker.
    run_call newline "$ROOT/stdout" "$ROOT/stderr"
    get_runner_calls
    if [ "$CALL_RC" != 0 ] || ! output_is "$ROOT/stdout" $'[]\n' || ! marker_free "$ROOT/stdout" || \
       [ "$(runner_count)" != 1 ] || [ "$(fallback_count)" != 0 ] || ! wait_runner_clean; then
      sessions=$(runner_sessions) || sessions=unknown
      echo "FAIL T2 exit=$CALL_RC remote_calls=$(runner_count) local_fallbacks=$(fallback_count) runner_sessions=$sessions"
      exit 1
    fi
    echo "PASS T2 exit=0 stdout_hex=$(od -An -tx1 -v "$ROOT/stdout" | tr -d ' \n') remote_calls=1 local_fallbacks=0 runner_sessions=0"
    exit 0 ;;
  T3) # intent: a timed-out no-newline call releases its directory owner before the next call.
    run_first_bare
    if [ "$FIRST_RESULT" = fail ] || [ "$FIRST_REMOTE" != 1 ] || [ "$FIRST_LOCAL" != 0 ]; then
      echo "FAIL T3 first_result=$FIRST_RESULT first_exit=$CALL_RC remote_calls=$FIRST_REMOTE local_fallbacks=$FIRST_LOCAL"
      exit 1
    fi
    FIRST_SESSIONS=$(runner_sessions) || FIRST_SESSIONS=unknown
    if [ "$FIRST_SESSIONS" != 0 ] && [ "$FIRST_RESULT" != baseline-red ]; then
      echo "FAIL T3 first_call_sessions=$FIRST_SESSIONS local_fallbacks=$FIRST_LOCAL"
      exit 1
    fi
    if ! preserve_runner_calls; then
      echo "FAIL T3 first_call_accounting remote_calls=$FIRST_REMOTE local_fallbacks=$FIRST_LOCAL"
      exit 1
    fi
    run_call ok "$ROOT/stdout" "$ROOT/stderr"
    get_runner_calls
    if [ "$CALL_RC" != 0 ] || ! output_is "$ROOT/stdout" $'ok\n' || ! marker_free "$ROOT/stdout" || \
       grep -qF 'qa-pytest: lane directory already active' "$ROOT/stderr" || \
       [ "$(runner_count)" != 2 ] || [ "$(fallback_count)" != 0 ] || ! wait_runner_clean; then
      sessions=$(runner_sessions) || sessions=unknown
      lock_error=no; grep -qF 'qa-pytest: lane directory already active' "$ROOT/stderr" && lock_error=yes || true
      echo "FAIL T3 first_call=$FIRST_RESULT second_exit=$CALL_RC lane_lock_error=$lock_error remote_calls=$(runner_count) local_fallbacks=$(fallback_count) runner_sessions=$sessions"
      exit 1
    fi
    echo "PASS T3 first_call=$FIRST_RESULT first_runner_sessions=$FIRST_SESSIONS second_exit=0 stdout_hex=$(od -An -tx1 -v "$ROOT/stdout" | tr -d ' \n') remote_calls=2 local_fallbacks=0 runner_sessions=0"
    exit 0 ;;
  *) echo "unknown case $c" >&2; exit 2 ;;
  esac
fi

ROOT=$(mktemp -d /mnt/data/tmp/gen9971-framing.XXXXXX)
SELF=$(realpath "$0")
cp "$BIN" "$ROOT/qa-pytest-candidate"; chmod +x "$ROOT/qa-pytest-candidate"
ARTIFACTS=/mnt/data/tmp/tool-output-hygiene/GEN-9971
ARTIFACT="$ARTIFACTS/$(basename "$ROOT")"
cleanup_all() {
  rc=$?
  trap - EXIT
  mkdir -p "$ARTIFACT"
  for item in "$ROOT"/*.log "$ROOT"/T1 "$ROOT"/T2 "$ROOT"/T3 "$ROOT"/runner.calls; do
    [ -e "$item" ] && cp -a "$item" "$ARTIFACT/"
  done
  printf 'candidate_sha256=%s\nbaseline=%s\n' \
    "$(sha256sum "$BIN" | cut -d' ' -f1)" "$GEN9971_BASELINE" > "$ARTIFACT/run-summary.txt"
  timeout -k 2 8 ssh -o BatchMode=yes -o ConnectTimeout=3 "root@$RUNNER" \
    "self=\$\$; pids=\$(ps -eo pid=,comm=,args= | awk -v self=\"\$self\" -v candidate='$ROOT/qa-pytest-candidate' -v root='$ROOT/' '\$1 != self && \$2 == \"bash\" && index(\$0,candidate) && index(\$0,root) {print \$1}'); [ -z \"\$pids\" ] || kill -TERM \$pids; sleep 1; rm -rf '$ROOT'" \
    >/dev/null 2>&1 || true
  rm -rf "$ROOT"
  echo "evidence=$ARTIFACT"
  return "$rc"
}
trap cleanup_all EXIT
remote "mkdir -p '$ROOT'"
scp -q "$ROOT/qa-pytest-candidate" "root@$RUNNER:$ROOT/qa-pytest-candidate"
echo "artifacts=$ROOT candidate_sha256=$(sha256sum "$BIN" | cut -d' ' -f1) baseline=${GEN9971_BASELINE:-0}"
cases=("$@"); [ "${#cases[@]}" != 0 ] || cases=(T1 T2 T3)
failed=0
for case_id in "${cases[@]}"; do
  rc=0
  timeout -k 5 70 bash "$SELF" "$ROOT/qa-pytest-candidate" --case "$case_id" "$ROOT/$case_id" >"$ROOT/$case_id.log" 2>&1 || rc=$?
  if [ "${GEN9971_BASELINE:-0}:$case_id:$rc" = '1:T1:1' ] && \
     grep -Eq '^RED T1 baseline_timeout=124 marker_coalesced=yes remote_calls=1 local_fallbacks=0 runner_sessions=([0-9]+|unknown)$' "$ROOT/$case_id.log"; then
    grep -E '^RED T1 ' "$ROOT/$case_id.log"
    echo 'XFAIL T1 baseline (coalesced marker leaves the proxy waiting)'
  elif [ "$GEN9971_BASELINE:$case_id:$rc" = '1:T3:1' ] && \
       grep -Eq '^FAIL T3 first_call=baseline-red second_exit=[0-9]+ lane_lock_error=yes remote_calls=1 local_fallbacks=[0-9]+ runner_sessions=([0-9]+|unknown)$' "$ROOT/$case_id.log"; then
    grep -E '^FAIL T3 ' "$ROOT/$case_id.log"
    echo 'XFAIL T3 baseline (first timeout retains the directory owner)'
  elif [ "${GEN9971_BASELINE:-0}:$case_id" = '1:T1' ] && [ "$rc" = 0 ]; then
    echo 'XPASS T1 baseline (strict baseline case unexpectedly passed)'; failed=$((failed+1))
  elif [ "$rc" != 0 ]; then
    echo "FAIL $case_id exit=$rc artifact=$ROOT/$case_id.log"; tail -n 20 "$ROOT/$case_id.log"; failed=$((failed+1))
  else
    grep -E '^(PASS|RED|FAIL) ' "$ROOT/$case_id.log" | tail -n 5
  fi
done
echo "failed=$failed artifacts=$ROOT"
[ "$failed" = 0 ]
