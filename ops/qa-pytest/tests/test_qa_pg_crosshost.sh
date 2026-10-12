#!/bin/bash
# GEN-9846 T11-T12 cross-host ownership cases; architecture: universal-deploy lane
# runner, epic GEN-9662. Run ON qa-deploy-box (root@100.71.21.27):
#   bash test_qa_pg_crosshost.sh <scratch candidate qa-pytest> [T11 T12]
#
# T11: a QA_PG run active directly on the lane runner (explicit run id, 4
# resources, pg+test containers running), then a QA-side forward of a different
# dir with the same run id must refuse exit 75 WITHOUT the forwarding caller's
# EXIT cleanup removing the live owner's remote resources. Expected: second exit
# 75, exact "qa-pytest: run id already active", first resource ids stay identical
# (4), both first containers alive; release first -> exit 0; no resources after.
#
# T12: a QA-side forward cancelled with SIGTERM on the exact proxy pid while it is
# blocked in an rsync barrier BEFORE its remote admission must not remove the live
# owner's remote resources. Expected: proxy exits nonzero; the owner's 4 resources
# survive and the owner's fixture succeeds (exit 0); no resources after.
#
# Real Docker/Postgres/shell seam on QA and the lane runner. Unique run ids and a
# private lock dir per invocation; 120-second outer bound per case. Only resources
# and pids this script starts are torn down. The installed /usr/local/bin/qa-pytest
# is never touched: the scratch candidate is copied and addressed by absolute path.
set -uo pipefail
BIN=$(realpath "${1:?usage: test_qa_pg_crosshost.sh <candidate qa-pytest> [T11 T12]}"); shift
RUNNER=${QA_LANE_RUNNER:-100.116.155.101}
RSYNC_REAL=/usr/bin/rsync

# on <host> <cmd...>: run a command on the QA box (local) or the lane runner.
on() { if [ "$1" = local ]; then shift; "$@"; else shift; ssh -o BatchMode=yes -o ConnectTimeout=5 root@"$RUNNER" "$@"; fi; }
# resources <run> <host>: "container/network/volume <id>" lines owned by the run id.
resources() {
  local run=$1 host=$2 kind all=()
  for kind in container network volume; do
    all=(); [ "$kind" != container ] || all=(-a)
    on "$host" docker "$kind" ls -q "${all[@]}" --filter "label=qa-pytest.run=$run" | sort | sed "s/^/$kind /"
  done
}
# remove_run <run> <host>: force-remove every resource owned by the run id.
remove_run() {
  local run=$1 host=$2 kind ids all=()
  for kind in container network volume; do
    all=(); [ "$kind" != container ] || all=(-a)
    ids=$(on "$host" docker "$kind" ls -q "${all[@]}" --filter "label=qa-pytest.run=$run")
    [ -z "$ids" ] || if [ "$kind" = container ]; then on "$host" docker rm -f $ids >/dev/null 2>&1 || true
    else on "$host" docker "$kind" rm $ids >/dev/null 2>&1 || true; fi
  done
}
alive() { on "$1" docker ps -q --filter "label=qa-pytest.run=$2" | wc -l; }

if [ "${1:-}" = --case ]; then
  BIN=$(realpath "$BIN")
  c=$2 case_root=$3 id=$4
  MAIN=$(dirname "$BIN"); lockdir=$MAIN/locks; candidate=$BIN
  firstdir=$case_root/first; seconddir=$case_root/second
  mkdir -p "$lockdir" "$case_root"
  ids=("$id") pids=()
  cleanup() {
    trap - EXIT
    local p id
    for p in "${pids[@]}"; do kill -TERM "$p" 2>/dev/null || true; done
    for id in "${ids[@]}"; do remove_run "$id" runner || true; remove_run "$id" local || true; done
    for p in "${pids[@]}"; do wait "$p" 2>/dev/null || true; done
  }
  trap cleanup EXIT
  trap 'exit 124' TERM INT
  check() { if ! "$@"; then echo "FAIL $c: $*" >&2; exit 1; fi; }
  fixture() {
    local d=$1; mkdir -p "$d/tests"
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
  # start_owner: begin the first QA_PG run directly on the runner and wait for it
  # to be active (4 resources, 2 containers running). Leaves $owner_pid set.
  start_owner() {
    fixture "$firstdir"
    on runner mkdir -p "$firstdir"
    rsync -a "$firstdir/" root@"$RUNNER":"$firstdir/"
    ssh -o BatchMode=yes -o ConnectTimeout=5 root@"$RUNNER" \
      "timeout -k 5 110 env QA_PG=1 QA_PG_RUN_ID='$id' QA_PYTEST_ON_RUNNER=1 QA_PYTEST_LOCKDIR='$lockdir' '$candidate' '$firstdir' -q tests/test_pg.py > '$case_root/first.log' 2>&1" &
    owner_pid=$!; pids+=("$owner_pid")
    local n
    for n in $(seq 1 60); do
      on runner test -f "$firstdir/ready" && return 0
      kill -0 "$owner_pid" 2>/dev/null || break
      sleep 1
    done
    echo "FAIL $c: owner fixture did not become ready" >&2; return 1
  }
  active_owner() {
    check test "$(alive runner "$id")" = 2
    check test "$(resources "$id" runner | wc -l)" = 4
  }
  case "$c" in
  T11)
    # intent: the forwarding caller's EXIT cleanup must not remove a live owner's
    # remote resources after the runner refuses a duplicate run id (exit 75).
    start_owner || exit 1
    active_owner
    resources "$id" runner > "$case_root/before"
    fixture "$seconddir"
    env QA_PG=1 QA_PG_RUN_ID="$id" QA_PYTEST_LOCKDIR="$lockdir" QA_PYTEST_REMOTE_BIN="$candidate" \
      "$candidate" "$seconddir" -q tests/test_pg.py > "$case_root/second.log" 2>&1 & proxy=$!
    pids+=("$proxy")
    rc=0; wait "$proxy" || rc=$?
    resources "$id" runner > "$case_root/after"
    echo "duplicate exit=$rc resources_before=$(wc -l < "$case_root/before") resources_after=$(wc -l < "$case_root/after") alive_after=$(alive runner "$id")"
    check test "$rc" = 75
    check grep -Fxq 'qa-pytest: run id already active' "$case_root/second.log"
    check cmp -s "$case_root/before" "$case_root/after"
    check test "$(alive runner "$id")" = 2
    on runner test ! -f "$seconddir/ready"
    # Release even on a refusal defect so the owner's exit is recorded.
    on runner touch "$firstdir/release"
    owner_rc=0; wait "$owner_pid" || owner_rc=$?
    echo "owner exit=$owner_rc"
    check test "$owner_rc" = 0
    check on runner test -f "$firstdir/succeeded"
    check test -z "$(resources "$id" runner)"; check test -z "$(resources "$id" local)" ;;
  T12)
    # intent: a forward cancelled before its remote admission must not clean the
    # live owner's remote resources; only the owner's own run may release them.
    start_owner || exit 1
    active_owner
    resources "$id" runner > "$case_root/before"
    shim=$case_root/shim; mkdir -p "$shim"
    cat > "$shim/rsync" <<'SH'
#!/bin/bash
touch "$RSYNC_HIT"
while [ ! -e "$RSYNC_RELEASE" ]; do sleep 0.2; done
exec "$RSYNC_REAL" "$@"
SH
    chmod +x "$shim/rsync"
    fixture "$seconddir"
    env PATH="$shim:$PATH" RSYNC_REAL="$RSYNC_REAL" RSYNC_HIT="$shim/hit" RSYNC_RELEASE="$shim/release" \
      QA_PG=1 QA_PG_RUN_ID="$id" QA_PYTEST_LOCKDIR="$lockdir" QA_PYTEST_REMOTE_BIN="$candidate" \
      "$candidate" "$seconddir" -q tests/test_pg.py > "$case_root/proxy.log" 2>&1 & proxy=$!
    pids+=("$proxy")
    n=0
    for n in $(seq 1 60); do [ ! -e "$shim/hit" ] || break; kill -0 "$proxy" 2>/dev/null || break; sleep 0.2; done
    check test -e "$shim/hit"
    kill -TERM "$proxy"
    touch "$shim/release"
    rc=0; wait "$proxy" || rc=$?
    resources "$id" runner > "$case_root/after"
    echo "proxy exit=$rc resources_before=$(wc -l < "$case_root/before") resources_after=$(wc -l < "$case_root/after") alive_after=$(alive runner "$id")"
    check test "$rc" != 0
    check cmp -s "$case_root/before" "$case_root/after"
    check test "$(alive runner "$id")" = 2
    on runner touch "$firstdir/release"
    owner_rc=0; wait "$owner_pid" || owner_rc=$?
    echo "owner exit=$owner_rc"
    check test "$owner_rc" = 0
    check on runner test -f "$firstdir/succeeded"
    check test -z "$(resources "$id" runner)"; check test -z "$(resources "$id" local)" ;;
  *) echo "unknown case $c" >&2; exit 2 ;;
  esac
  echo "PASS $c"
  exit 0
fi

ROOT=$(mktemp -d /mnt/data/tmp/gen9846-crosshost.XXXXXX)
ROOTN=$(basename "$ROOT")
SELF=$(realpath "$0")
cp "$BIN" "$ROOT/candidate"; chmod +x "$ROOT/candidate"
ssh -o BatchMode=yes -o ConnectTimeout=5 root@"$RUNNER" "mkdir -p '$ROOT'"
scp -q "$ROOT/candidate" root@"$RUNNER":"$ROOT/candidate"
echo "artifacts=$ROOT candidate_sha256=$(sha256sum "$BIN" | cut -d' ' -f1)"
failed=0
cases=("$@"); [ "${#cases[@]}" != 0 ] || cases=(T11 T12)
for case_id in "${cases[@]}"; do
    runid="crosshost9846-$case_id-$ROOTN"
    if timeout -k 5 120 bash "$SELF" "$ROOT/candidate" --case "$case_id" "$ROOT/$case_id" "$runid" > "$ROOT/$case_id.log" 2>&1; then
      echo "PASS $case_id"
    else
      rc=$?; echo "FAIL $case_id exit=$rc artifact=$ROOT/$case_id.log"; failed=$((failed+1))
    fi
    # Outer teardown also covers a case killed before its EXIT trap ran.
    for host in local runner; do remove_run "$runid" "$host" || true; done
    cat "$ROOT/$case_id.log"
done
echo "failed=$failed artifacts=$ROOT"
[ "$failed" = 0 ]
