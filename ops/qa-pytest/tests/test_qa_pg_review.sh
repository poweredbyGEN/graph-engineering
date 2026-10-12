#!/bin/bash
# GEN-9846 T7-T10; architecture: universal-deploy lane runner, epic GEN-9662.
# Run on QA: bash test_qa_pg_review.sh <scratch candidate> [T7 T8 T9 T10]
# Real Docker/Postgres; private locks; each case has a 120-second outer bound.
set -euo pipefail
BIN=$(realpath "${1:?candidate required}"); shift
RUNNER=${QA_LANE_RUNNER:-100.116.155.101}

resources() {
  local kind all
  for kind in container network volume; do
    all=(); [ "$kind" != container ] || all=(-a)
    docker "$kind" ls -q "${all[@]}" --filter "label=qa-pytest.run=$1" | sort | sed "s/^/$kind /"
  done
}
remove_run() {
  local kind ids all
  for kind in container network volume; do
    all=(); [ "$kind" != container ] || all=(-a)
    ids=$(docker "$kind" ls -q "${all[@]}" --filter "label=qa-pytest.run=$1")
    [ -z "$ids" ] || if [ "$kind" = container ]; then docker rm -f $ids >/dev/null 2>&1 || true
    else docker "$kind" rm $ids >/dev/null 2>&1 || true; fi
  done
}

if [ "${1:-}" = --case ]; then
  c=$2 ROOT=$3
  mkdir -p "$ROOT/locks"
  export QA_PYTEST_LOCKDIR=$ROOT/locks QA_PYTEST_ON_RUNNER=1
  ids=() pids=()
  cleanup() {
    trap - EXIT
    local p id
    for p in "${pids[@]}"; do kill -TERM "$p" 2>/dev/null || true; done
    for id in "${ids[@]}"; do remove_run "$id"; done
    for p in "${pids[@]}"; do wait "$p" 2>/dev/null || true; done
  }
  trap cleanup EXIT
  trap 'exit 124' TERM INT
  check() { if ! "$@"; then echo "FAIL $c: $*" >&2; exit 1; fi; }
  fixture() {
    d=$ROOT/$1; mkdir -p "$d/tests"
    cat > "$d/tests/test_pg.py" <<'PY'
import asyncio, os, pathlib
import asyncpg

def test_pg():
    async def go():
        c = await asyncpg.connect(host=os.environ["TEST_DB_HOST"],
                                  user=os.environ["TEST_DB_USER"],
                                  password=os.environ["TEST_DB_PASSWORD"],
                                  database=os.environ["TEST_DB_NAME"])
        await c.execute("create table t(v int)")
        await c.execute("insert into t values(9846)")
        pathlib.Path("ready").touch()
        for _ in range(700):
            if pathlib.Path("release").exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("fixture release deadline")
        assert await c.fetchval("select v from t") == 9846
        await c.close()
        pathlib.Path("succeeded").touch()
    asyncio.run(go())
PY
  }
  start() {
    local candidate=$1 lane=$2 id=$3 log=$4
    ids+=("$id")
    env QA_PG=1 QA_PG_RUN_ID="$id" "$candidate" "$lane" -q tests/test_pg.py >"$log" 2>&1 &
    PID=$!; pids+=("$PID")
  }
  ready() {
    local n
    for n in $(seq 1 35); do
      [ ! -f "$1/ready" ] || return 0
      kill -0 "$2" 2>/dev/null || break
      sleep 1
    done
    echo "FAIL $c: fixture did not become ready" >&2; return 1
  }
  finished() {
    local rc=0
    wait "$1" || rc=$?
    echo "$2 exit=$rc"
    check test "$rc" = "$3"
  }
  clean() { check test -z "$(resources "$1")"; }
  active() {
    check test "$(docker ps -q --filter "label=qa-pytest.run=$1" | wc -l)" = 2
    check test "$(resources "$1" | wc -l)" = 4
  }
  id=review9846-$(basename "$(dirname "$ROOT")")-$(basename "$ROOT")
  case "$c" in
  T7)
    # intent: direct runner invocation detects its host and never forwards to itself.
    unset QA_PYTEST_ON_RUNNER
    mkdir "$ROOT/audit"
    for cmd in ssh rsync; do
      cat > "$ROOT/audit/$cmd" <<'SH'
#!/bin/bash
echo "$0 $*" >> "$QA_FORWARD_AUDIT"
exit 99
SH
      chmod +x "$ROOT/audit/$cmd"
    done
    export PATH=$ROOT/audit:$PATH QA_FORWARD_AUDIT=$ROOT/forwarded
    fixture first; start "$BIN" "$d" "$id" "$ROOT/run.log"; r=$PID
    ready "$d" "$r"; active "$id"
    touch "$d/release"; finished "$r" direct 0
    check test -f "$d/succeeded"
    check test ! -e "$ROOT/forwarded"
    check test "$(grep -c 'running on lane runner' "$ROOT/run.log" || true)" = 0
    clean "$id" ;;
  T8)
    # intent: seven private busy runner slots leave eligible slot eight usable.
    load=$(cut -d. -f1 /proc/loadavg)
    mem=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
    echo "eligibility load=$load available_gib=$mem"
    check test "$load" -lt 12; check test "$mem" -ge 16
    for slot in $(seq 1 7); do
      exec {fd}>"$ROOT/locks/slot$slot"; flock -n "$fd"
    done
    fixture first; start "$BIN" "$d" "$id" "$ROOT/run.log"; r=$PID
    ready "$d" "$r"; active "$id"
    check test -e "$ROOT/locks/slot8"
    check test "$(flock -n "$ROOT/locks/slot8" true; echo $?)" = 1
    touch "$d/release"; finished "$r" slot8 0
    check flock -n "$ROOT/locks/slot8" true
    check test -f "$d/succeeded"; clean "$id" ;;
  T9)
    # intent: a duplicate explicit run id refuses before touching the live owner's resources.
    fixture first; first=$d
    start "$BIN" "$first" "$id" "$ROOT/first.log"; r=$PID
    ready "$first" "$r"; active "$id"; resources "$id" > "$ROOT/before"
    fixture second; second=$d
    start "$BIN" "$second" "$id" "$ROOT/second.log"; duplicate=$PID
    rc=0; wait "$duplicate" || rc=$?
    resources "$id" > "$ROOT/after"
    alive=$(docker ps -q --filter "label=qa-pytest.run=$id" | wc -l)
    echo "duplicate exit=$rc resources_before=$(wc -l < "$ROOT/before") resources_after=$(wc -l < "$ROOT/after")"
    # Release even on refusal defects so the first run's exit is recorded and cleaned.
    touch "$first/release"
    first_rc=0; wait "$r" || first_rc=$?
    echo "first exit=$first_rc"; tail -n 4 "$ROOT/second.log"
    check test "$rc" = 75
    check grep -Fxq 'qa-pytest: run id already active' "$ROOT/second.log"
    check cmp -s "$ROOT/before" "$ROOT/after"
    check test "$alive" = 2
    check test ! -e "$second/ready"
    check test "$first_rc" = 0; check test -f "$first/succeeded"
    clean "$id" ;;
  T10)
    # intent: the hard deadline terminates an active test and removes every owned resource.
    # Only the duration changes in this scratch copy; watchdog/cleanup logic stays intact.
    sed 's/5400/15/g' "$BIN" > "$ROOT/deadline-bin"; chmod +x "$ROOT/deadline-bin"
    check test "$(grep -c 5400 "$BIN")" -ge 1
    fixture deadline; since=$SECONDS
    start "$ROOT/deadline-bin" "$d" "$id-deadline" "$ROOT/deadline.log"; r=$PID
    ready "$d" "$r"; active "$id-deadline"
    rc=0; wait "$r" || rc=$?
    echo "deadline exit=$rc elapsed=$((SECONDS-since))"
    check test "$rc" = 143
    check test "$((SECONDS-since))" -ge 13; check test "$((SECONDS-since))" -le 30
    check test ! -e "$d/succeeded"; clean "$id-deadline"

    # intent: SIGKILL leaves owned orphans which the next start sweeps before succeeding.
    fixture killed; killed=$d
    start "$BIN" "$killed" "$id-killed" "$ROOT/killed.log"; r=$PID
    ready "$killed" "$r"; active "$id-killed"
    # Track this candidate's children, including its watchdog timer, for exact teardown.
    children=$(cat "/proc/$r/task/$r/children")
    for child in $children; do
      pids+=("$child")
      if [ -r "/proc/$child/task/$child/children" ]; then
        read -r -a grandchildren < "/proc/$child/task/$child/children" || true
        pids+=("${grandchildren[@]}")
      fi
    done
    kill -KILL "$r"; rc=0; wait "$r" || rc=$?
    check test "$rc" = 137; active "$id-killed"
    fixture restart; touch "$d/release"
    start "$BIN" "$d" "$id-restart" "$ROOT/restart.log"; r=$PID
    finished "$r" restart 0
    check test -f "$d/succeeded"; check test ! -e "$killed/succeeded"
    clean "$id-killed"; clean "$id-restart" ;;
  *) echo "unknown case $c" >&2; exit 2 ;;
  esac
  echo "PASS $c"
  exit 0
fi

ROOT=$(mktemp -d /mnt/data/tmp/gen9846-review.XXXXXX)
SELF=$(realpath "$0")
cp "$BIN" "$ROOT/candidate"; chmod +x "$ROOT/candidate"
ssh -o BatchMode=yes -o ConnectTimeout=5 "root@$RUNNER" "mkdir -p '$ROOT'"
scp -q "$ROOT/candidate" "$SELF" "root@$RUNNER:$ROOT/"
echo "artifacts=$ROOT candidate_sha256=$(sha256sum "$BIN" | cut -d' ' -f1)"
failed=0
cases=("$@"); [ "${#cases[@]}" != 0 ] || cases=(T7 T8 T9 T10)
for case_id in "${cases[@]}"; do
    case "$case_id" in T7|T8) remote=1 ;; T9|T10) remote=0 ;; *) exit 2 ;; esac
    if [ "$remote" = 1 ]; then
      if timeout -k 5 120 ssh -o BatchMode=yes -o ConnectTimeout=5 "root@$RUNNER" "timeout -k 5 115 bash '$ROOT/$(basename "$SELF")' '$ROOT/candidate' --case '$case_id' '$ROOT/$case_id'" > "$ROOT/$case_id.log" 2>&1; then rc=0; else rc=$?; fi
      scp -qr "root@$RUNNER:$ROOT/$case_id" "$ROOT/" || true
    else
      if timeout -k 5 120 bash "$SELF" "$ROOT/candidate" --case "$case_id" "$ROOT/$case_id" > "$ROOT/$case_id.log" 2>&1; then rc=0; else rc=$?; fi
    fi
    # Outer teardown also covers a case process killed before its EXIT trap runs.
    for suffix in '' -deadline -killed -restart; do
      run=review9846-$(basename "$ROOT")-$case_id$suffix
      if [ "$remote" = 1 ]; then
        timeout 20 ssh -o ConnectTimeout=5 "root@$RUNNER" "$(declare -f resources remove_run); remove_run '$run'; test -z \"\$(resources '$run')\""
      else remove_run "$run"; test -z "$(resources "$run")"; fi
    done
    cat "$ROOT/$case_id.log"
    if [ "$rc" != 0 ]; then echo "FAIL $case_id exit=$rc artifact=$ROOT/$case_id.log"; failed=$((failed+1)); fi
done
echo "failed=$failed artifacts=$ROOT"
[ "$failed" = 0 ]
