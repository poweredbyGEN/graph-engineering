#!/bin/bash
# GEN-9854 T1-T3; infrastructure QA wrapper; epic GEN-9662, dependency GEN-9846.
# Run on QA: bash test_qa_mirror_ownership.sh <candidate> [T1 T2 T3].
# GEN9854_BASELINE=1 requires T1 to fail behaviorally; setup errors never count as XFAIL.
set -euo pipefail
BIN=$(realpath "${1:?candidate required}"); shift
RUNNER=${QA_LANE_RUNNER:-100.116.155.101}
remote() { ssh -o BatchMode=yes -o ConnectTimeout=5 "root@$RUNNER" "$@"; }
resources() {
  local k a; for k in container network volume; do a=(); [ "$k" != container ] || a=(-a); docker "$k" ls "${a[@]}" -q --filter "label=qa-pytest.run=$1" || echo RESOURCE_QUERY_ERROR; done
  remote "set -e; for k in container network volume; do a=; [ \"\$k\" != container ] || a=-a; docker \$k ls \$a -q --filter label=qa-pytest.run=$1; done" || echo RESOURCE_QUERY_ERROR
}
submits() { remote "timeout 3 docker events --since $since --until \$(date +%s) --filter event=create --filter label=qa-pytest.run=$1 --filter label=qa-pytest.role=test | wc -l"; }
if [ "${1:-}" = --case ]; then
  c=$2 ROOT=$3; mkdir -p "$ROOT/locks"
  export QA_PYTEST_LOCKDIR=$ROOT/locks QA_PYTEST_REMOTE_BIN=$BIN
  ids=() pids=()
  cleanup() {
    trap - EXIT; local p id
    for p in "${pids[@]}"; do
      if [[ " $(jobs -pr | tr '\n' ' ') " = *" $p "* ]]; then kill -TERM "$p" 2>/dev/null || true; fi
    done
    for id in "${ids[@]}"; do
      remote "for k in container network volume; do a=; [ \"\$k\" != container ] || a=-a; for x in \$(docker \$k ls \$a -q --filter label=qa-pytest.run=$id); do if [ \"\$k\" = container ]; then docker rm -f \$x; else docker \$k rm \$x; fi; done; done" >/dev/null || true
    done
    for p in "${pids[@]}"; do wait "$p" 2>/dev/null || true; done
  }
  trap cleanup EXIT; trap 'exit 124' TERM INT
  setup() { "$@" || { echo "SETUP_ERROR $c: $*" >&2; exit 2; }; }
  check() { "$@" || { echo "FAIL $c: $*" >&2; exit 1; }; }
  fixture() {
    d=$ROOT/$1; mkdir -p "$d/tests"; printf '%s' "$2" > "$d/payload"
    cat > "$d/tests/test_pg.py" <<'PY'
import asyncio, os, pathlib, asyncpg
def test_pg():
    async def go():
        c = await asyncpg.connect(host=os.environ["TEST_DB_HOST"], user=os.environ["TEST_DB_USER"],
                                  password=os.environ["TEST_DB_PASSWORD"], database=os.environ["TEST_DB_NAME"])
        payload = pathlib.Path("payload")
        tag = payload.read_text()
        await c.execute("create table t(v text)")
        await c.execute("insert into t values($1)", tag)
        pathlib.Path("ready-" + tag).touch()
        for _ in range(700):
            if pathlib.Path("release-" + tag).exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("fixture release deadline")
        assert payload.read_text() == tag
        assert await c.fetchval("select v from t") == tag
        await c.close()
    asyncio.run(go())
PY
  }
  start() {
    ids+=("$2")
    env QA_PG=1 QA_PG_RUN_ID="$2" "$BIN" "$1" -q tests/test_pg.py >"$ROOT/$2.log" 2>&1 &
    PID=$!; pids+=("$PID")
  }
  ready() {
    local n; for n in $(seq 1 40); do
      remote "test -f '$1/ready-$2'" && return 0
      kill -0 "$3" 2>/dev/null || break; sleep 1
    done
    echo "${4:-SETUP_ERROR} $c: fixture $2 not ready" >&2; [ "${4:-}" != FAIL ] || exit 1; exit 2
  }
  id=mirror9854-$(basename "$(dirname "$ROOT")")-$c
  setup remote "mkdir -p '$ROOT/locks'"
  fixture mirror A; a=$d; since=$(date +%s)
  start "$a" "$id-a"; owner=$PID; ready "$a" A "$owner"
  case "$c" in
  T1) # intent: same canonical directory refuses before rsync can overwrite live input (GEN-9854).
    printf B > "$a/payload"; start "$a/" "$id-b"; second=$PID
    for n in $(seq 1 40); do
      remote "test -f '$a/ready-B'" && break
      kill -0 "$second" 2>/dev/null || break; sleep 1
    done
    payload=$(remote "cat '$a/payload'"); setup kill -0 "$owner"
    remote "touch '$a/release-A' '$a/release-B'"; rcb=0; wait "$second" || rcb=$?; rca=0; wait "$owner" || rca=$?
    printf B > "$a/payload"; touch "$a/release-B"
    start "$a" "$id-later"; later=$PID; rcl=0; wait "$later" || rcl=$?
    created=$(submits "$id-b"); first=$(submits "$id-a"); retry=$(submits "$id-later")
    echo "T1 first=$rca second=$rcb later=$rcl remote_payload=$payload submits=$first,$created,$retry"
    setup test "$rcl" = 0
    for run in "${ids[@]}"; do setup test -z "$(resources "$run")"; done
    echo 'T1 resources=0 on QA and runner'
    check test "$first,$retry" = 1,1
    check test "$rcb" = 75; check grep -Fxq 'qa-pytest: lane directory already active' "$ROOT/$id-b.log"
    check test "$payload" = A; check test "$created" = 0; check test "$rca" = 0; check test "$rcl" = 0 ;;
  T2) # intent: independent lane directories retain concurrent admission and isolated input (GEN-9854).
    fixture mirror-b B; b=$d; start "$b" "$id-b"; second=$PID; ready "$b" B "$second" FAIL
    peak=0; for run in "$id-a" "$id-b"; do
      n=$(remote "docker ps -q --filter label=qa-pytest.role=test --filter label=qa-pytest.run=$run | wc -l"); peak=$((peak+n))
    done
    remote "touch '$a/release-A' '$b/release-B'"; rca=0; wait "$owner" || rca=$?; rcb=0; wait "$second" || rcb=$?
    echo "T2 first=$rca second=$rcb peak=$peak"; check test "$peak" = 2; check test "$rca" = 0; check test "$rcb" = 0 ;;
  T3) # intent: SIGTERM of the exact proxy owner releases admission for a new run (GEN-9854).
    kill -TERM "$owner"; rca=0; wait "$owner" || rca=$?
    printf B > "$a/payload"; touch "$a/release-B"; start "$a" "$id-b"; second=$PID; rcb=0; wait "$second" || rcb=$?
    created=$(submits "$id-b"); echo "T3 first=$rca second=$rcb new_submits=$created"
    check test "$rca" != 0; check test "$rcb" = 0; check test "$created" = 1 ;;
  *) exit 2 ;;
  esac
  for run in "${ids[@]}"; do check test -z "$(resources "$run")"; done
  echo "PASS $c resources=0 on QA and runner"; exit 0
fi
ROOT=$(mktemp -d /mnt/data/tmp/gen9854-mirror.XXXXXX); SELF=$(realpath "$0")
cp "$BIN" "$ROOT/candidate"; chmod +x "$ROOT/candidate"
remote "mkdir -p '$ROOT'"; scp -q "$ROOT/candidate" "root@$RUNNER:$ROOT/candidate"
echo "artifacts=$ROOT candidate_sha256=$(sha256sum "$BIN" | cut -d' ' -f1)"
cases=("$@"); [ "${#cases[@]}" != 0 ] || cases=(T1 T2 T3); failed=0
for c in "${cases[@]}"; do
  rc=0; timeout -k 5 115 bash "$SELF" "$ROOT/candidate" --case "$c" "$ROOT/$c" >"$ROOT/$c.log" 2>&1 || rc=$?
  cat "$ROOT/$c.log"
  if [ "${GEN9854_BASELINE:-0}:$c:$rc" = 1:T1:1 ] && grep -q 'remote_payload=B submits=1,1,1' "$ROOT/$c.log"; then echo "XFAIL T1 (strict baseline)"
  elif [ "$rc" != 0 ] || [ "${GEN9854_BASELINE:-0}:$c" = 1:T1 ]; then echo "ERROR $c exit=$rc"; failed=$((failed+1)); fi
done
echo "failed=$failed artifacts=$ROOT"; [ "$failed" = 0 ]
