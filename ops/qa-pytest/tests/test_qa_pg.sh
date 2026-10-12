#!/bin/bash
# GEN-9846 acceptance cases T1-T6 for qa-pytest QA_PG=1.
# Run ON qa-deploy-box: test_qa_pg.sh <candidate qa-pytest> [T1 T2 ...]
# The candidate is copied to the lane runner at the same path and used there via
# QA_PYTEST_REMOTE_BIN. Slots are taken from a private lock dir, so the cases never
# hold the slots real lanes use.
set -uo pipefail
BIN=$(realpath "${1:?usage: test_qa_pg.sh <candidate qa-pytest> [cases...]}"); shift
CASES=${*:-T1 T2 T3 T4 T5 T6}
RUNNER=${QA_LANE_RUNNER:-100.116.155.101}
UNREACHABLE=qa-runner-unavailable.invalid   # Reserved .invalid resolves no trusted runner; no remote tests start.
ROOT=/mnt/data/tmp/gen9846-test
export QA_PYTEST_LOCKDIR=$ROOT/locks QA_PYTEST_REMOTE_BIN=$ROOT/qa-pytest
mkdir -p "$ROOT" "$QA_PYTEST_LOCKDIR"; cp "$BIN" "$QA_PYTEST_REMOTE_BIN"
ssh root@$RUNNER "mkdir -p $ROOT" && scp -q "$BIN" root@$RUNNER:"$QA_PYTEST_REMOTE_BIN"
pass=0; fail=0
ok()  { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1: $2"; fail=$((fail+1)); }
# Containers with this run id: count on this box (local) or on the runner (remote).
n_local()  { docker ps -aq --filter "label=qa-pytest.run=$1" ${2:+--filter "label=qa-pytest.role=$2"} | wc -l; }
n_remote() { ssh root@$RUNNER "docker ps -aq --filter label=qa-pytest.run=$1 ${2:+--filter label=qa-pytest.role=$2} | wc -l"; }

# A lane dir whose one test connects over the socket qa-pytest provides, writes
# its tag into table t, holds for HOLD seconds, then must see only its own row.
mkfix() {  # mkfix <name> <tag> <hold>
  local d=$ROOT/$1; rm -rf "$d"; mkdir -p "$d/tests"
  cat > "$d/tests/test_pg.py" <<'PY'
import asyncio, os
import asyncpg

def test_pg():
    async def go():
        c = await asyncpg.connect(host=os.environ["TEST_DB_HOST"], user=os.environ["TEST_DB_USER"],
                                  password=os.environ["TEST_DB_PASSWORD"], database=os.environ["TEST_DB_NAME"])
        tag = os.environ["QA_TAG"]
        await c.execute("create table if not exists t(v text)")
        await c.execute("insert into t values($1)", tag)
        await asyncio.sleep(float(os.environ["HOLD"]))
        assert [r[0] for r in await c.fetch("select v from t")] == [tag]
    asyncio.run(go())
PY
  printf 'QA_TAG=%s\nHOLD=%s\n' "$2" "$3" > "$d/test.env"
  echo "$d"
}
# start <dir> <run id> <log> [env...]: QA_PG run of tests/test_pg.py in the
# background; $PID is the qa-pytest process itself (env execs it).
start() {
  local d=$1 id=$2 log=$3; shift 3
  env QA_PG=1 QA_PG_RUN_ID=$id PYTEST_ENV_FILE=$d/test.env "$@" "$BIN" "$d" -q tests/test_pg.py >"$log" 2>&1 &
  PID=$!
}

for c in $CASES; do case $c in
T1) # Runner path while local slots 1-3 are held.
  d=$(mkfix t1 t1 8); id=t1-$$
  ( for s in 1 2 3; do exec {fd}>"$QA_PYTEST_LOCKDIR/slot$s"; flock -n $fd; done; sleep 120 ) & holder=$!
  start "$d" $id $ROOT/t1.log; r=$PID
  during=0; for i in $(seq 1 40); do sleep 1; during=$(n_remote $id pg); [ "$during" = 1 ] && break; done
  wait $r; rc=$?; kill $holder 2>/dev/null; sleep 2
  if [ $rc = 0 ] && grep -q "running on lane runner" $ROOT/t1.log && [ "$during" = 1 ] && [ "$(n_remote $id)" = 0 ] && [ "$(n_local $id)" = 0 ]
  then ok T1; else bad T1 "rc=$rc during=$during after_remote=$(n_remote $id) log=$(tail -3 $ROOT/t1.log | tr '\n' ' ')"; fi ;;
T2) # Runner unreachable: local pg, passes.
  d=$(mkfix t2 t2 6); id=t2-$$
  start "$d" $id $ROOT/t2.log QA_LANE_RUNNER=$UNREACHABLE; r=$PID
  during=0; for i in $(seq 1 40); do sleep 1; during=$(n_local $id pg); [ "$during" = 1 ] && break; done
  wait $r; rc=$?; sleep 1
  if [ $rc = 0 ] && [ "$during" = 1 ] && [ "$(n_local $id)" = 0 ]; then ok T2; else bad T2 "rc=$rc during=$during after=$(n_local $id) log=$(tail -3 $ROOT/t2.log | tr '\n' ' ')"; fi ;;
T3) # Postgres never ready: exit 75 within 70 s, no pytest container ever created.
  docker image inspect busybox >/dev/null 2>&1 || docker pull -q busybox >/dev/null
  d=$(mkfix t3 t3 0); id=t3-$$; since=$(date +%s)
  start_t=$(date +%s); start "$d" $id $ROOT/t3.log QA_LANE_RUNNER=$UNREACHABLE QA_PG_IMAGE=busybox; wait $PID; rc=$?; took=$(( $(date +%s) - start_t ))
  created=$(timeout 3 docker events --since $since --until $(date +%s) --filter type=container --filter event=create --filter "label=qa-pytest.run=$id" --filter label=qa-pytest.role=test | wc -l)
  if [ $rc = 75 ] && [ $took -le 70 ] && grep -q "postgres not ready" $ROOT/t3.log && [ "$created" = 0 ] && [ "$(n_local $id)" = 0 ]
  then ok T3; else bad T3 "rc=$rc took=${took}s test_created=$created left=$(n_local $id) log=$(tail -2 $ROOT/t3.log | tr '\n' ' ')"; fi ;;
T4) # SIGTERM 10 s in, on the local path and on the runner path: nothing left after 15 s.
  for where in local remote; do
    d=$(mkfix t4$where t4 120); id=t4$where-$$
    if [ $where = local ]; then start "$d" $id $ROOT/t4$where.log QA_LANE_RUNNER=$UNREACHABLE; else start "$d" $id $ROOT/t4$where.log; fi
    r=$PID; sleep 10; kill -TERM $r
    wait $r; rc=$?; sleep 15
    left=$(( $(n_local $id) + $(n_remote $id) ))
    if [ $rc != 0 ] && [ $left = 0 ]; then ok T4-$where; else bad T4-$where "rc=$rc left=$left"; fi
  done ;;
T5) # Two concurrent runs, each writing table t: no row crosses over.
  a=$(mkfix t5a A 6); b=$(mkfix t5b B 6); ia=t5a-$$; ib=t5b-$$
  start "$a" $ia $ROOT/t5a.log; ra=$PID; start "$b" $ib $ROOT/t5b.log; rb=$PID
  peak=0; for i in $(seq 1 40); do sleep 1; n=$(( $(n_local $ia pg) + $(n_local $ib pg) + $(n_remote $ia pg) + $(n_remote $ib pg) )); [ $n -gt $peak ] && peak=$n; [ $peak = 2 ] && break; done
  wait $ra; rca=$?; wait $rb; rcb=$?; sleep 2
  left=$(( $(n_local $ia) + $(n_local $ib) + $(n_remote $ia) + $(n_remote $ib) ))
  if [ $rca = 0 ] && [ $rcb = 0 ] && [ $peak = 2 ] && [ $left = 0 ]; then ok T5; else bad T5 "rca=$rca rcb=$rcb peak=$peak left=$left"; fi ;;
T6) # No QA_PG, no sockets: unchanged runner path, no pg container.
  d=$ROOT/t6; rm -rf "$d"; mkdir -p "$d/tests"; echo 'def test_ok(): pass' > "$d/tests/test_ok.py"
  "$BIN" "$d" -q tests/test_ok.py > $ROOT/t6.log 2>&1 & r=$!
  pg=0; for i in $(seq 1 8); do sleep 1; n=$(( $(docker ps -q --filter label=qa-pg=1 --filter "label=qa-pytest.lane=$d" | wc -l) + $(ssh root@$RUNNER "docker ps -q --filter label=qa-pg=1 --filter label=qa-pytest.lane=$d | wc -l") )); [ $n -gt $pg ] && pg=$n; done
  wait $r; rc=$?
  if [ $rc = 0 ] && grep -q "running on lane runner" $ROOT/t6.log && [ $pg = 0 ]; then ok T6; else bad T6 "rc=$rc pg=$pg log=$(tail -3 $ROOT/t6.log | tr '\n' ' ')"; fi ;;
*) bad "$c" "unknown case" ;;
esac; done
echo "passed=$pass failed=$fail"; [ $fail = 0 ]
