#!/bin/sh
# intent: CI worker sizing stays within available CPU/memory, shares runners fairly,
# falls back without privileged queue credentials, and never emits zero workers.
set -eu

HERE=$(CDPATH= cd "$(dirname "$0")" && pwd)
HELPER="$HERE/../ci-workers"
TEST_ROOT=$(mktemp -d "${TMPDIR:-$PWD}/ci-workers-test.XXXXXX")
trap 'rm -rf "$TEST_ROOT"' 0
trap 'exit 1' HUP INT TERM
. "$HELPER"

failures=0
pass() { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }
assert_equal() {
  label=$1
  expected=$2
  actual=$3
  if [ "$expected" = "$actual" ]; then pass "$label"; else fail "$label (expected $expected, got $actual)"; fi
}

make_fixture() {
  root=$1
  cpus=$2
  load=$3
  host_available_mb=$4
  cpu_max=$5
  memory_max=$6
  memory_current=$7
  mkdir -p "$root/proc/self" "$root/cgroup"
  printf 'Cpus_allowed_list: 0-%s\n' "$((cpus - 1))" >"$root/proc/self/status"
  printf '%s 0.00 0.00 1/1 1\n' "$load" >"$root/proc/loadavg"
  printf 'MemAvailable: %s kB\nMemFree: %s kB\nBuffers: 0 kB\nCached: 0 kB\n' \
    "$((host_available_mb * 1024))" "$((host_available_mb * 1024))" >"$root/proc/meminfo"
  printf '%s\n' "$cpu_max" >"$root/cgroup/cpu.max"
  printf '%s\n' "$memory_max" >"$root/cgroup/memory.max"
  printf '%s\n' "$memory_current" >"$root/cgroup/memory.current"
}

make_snapshot() {
  file=$1
  active=$2
  printf '# ci-workers snapshot v1\nagent_id\thost\tactive_workflows\n1\trunner-a\t%s\n2\trunner-b\t100\n' "$active" >"$file"
}

compute() (
  if [ -x "$1/bin/nproc" ]; then PATH="$1/bin:$PATH"; export PATH; fi
  CI_WORKER_AGENT_IDS=$7
  CI_SYSTEM_HOST=$8
  export CI_WORKER_AGENT_IDS CI_SYSTEM_HOST
  ciw_compute "$1/proc" "$1/cgroup" "$2" "$3" "$4" "$6"
)

printf '1. Idle 16 CPUs, 40 GB available, one active workflow -> cap 16\n'
make_fixture "$TEST_ROOT/case1" 16 0 40960 'max 100000' max 0
make_snapshot "$TEST_ROOT/case1/peers.tsv" 1
actual=$(compute "$TEST_ROOT/case1" 16 4 2048 workers "$TEST_ROOT/case1/peers.tsv" 1 runner-a)
assert_equal 'idle capacity reaches cap' 16 "$actual"

printf '2. 16 CPUs with 1-minute load 12 -> 4\n'
make_fixture "$TEST_ROOT/case2" 16 12 40960 'max 100000' max 0
make_snapshot "$TEST_ROOT/case2/peers.tsv" 1
actual=$(compute "$TEST_ROOT/case2" 16 4 2048 workers "$TEST_ROOT/case2/peers.tsv" 1 runner-a)
assert_equal 'load reduces CPU budget' 4 "$actual"

printf '3. cgroup v2 quota 200000/100000 -> 2\n'
make_fixture "$TEST_ROOT/case3" 16 0 40960 '200000 100000' max 0
make_snapshot "$TEST_ROOT/case3/peers.tsv" 1
actual=$(compute "$TEST_ROOT/case3" 16 4 2048 workers "$TEST_ROOT/case3/peers.tsv" 1 runner-a)
assert_equal 'v2 quota limits workers' 2 "$actual"

printf '4. 3 GB cgroup memory remaining at 2 GB per worker -> 1\n'
make_fixture "$TEST_ROOT/case4" 16 0 40960 'max 100000' 3221225472 0
make_snapshot "$TEST_ROOT/case4/peers.tsv" 1
actual=$(compute "$TEST_ROOT/case4" 16 4 2048 workers "$TEST_ROOT/case4/peers.tsv" 1 runner-a)
assert_equal 'cgroup memory is tighter than host memory' 1 "$actual"

printf '5. Eight active workflows on 16 CPUs -> 2; ignore other agent\n'
make_fixture "$TEST_ROOT/case5" 16 0 40960 'max 100000' max 0
make_snapshot "$TEST_ROOT/case5/peers.tsv" 8
actual=$(compute "$TEST_ROOT/case5" 16 4 2048 workers "$TEST_ROOT/case5/peers.tsv" 1 runner-a)
assert_equal 'peer count divides shared capacity' 2 "$actual"

printf '6. One active workflow can exceed a fixed four-shard baseline\n'
make_fixture "$TEST_ROOT/case6" 16 0 40960 'max 100000' max 0
make_snapshot "$TEST_ROOT/case6/peers.tsv" 1
actual=$(compute "$TEST_ROOT/case6" 16 4 2048 shards "$TEST_ROOT/case6/peers.tsv" 1 runner-a)
if [ "$actual" -gt 4 ]; then pass 'active snapshot allows scale above four'; else fail "active snapshot did not scale above four (got $actual)"; fi

printf '7. Zero/negative remaining capacity still returns one\n'
make_fixture "$TEST_ROOT/case7" 16 0 40960 '0 100000' 2147483648 3221225472
actual=$(compute "$TEST_ROOT/case7" 16 1 2048 workers '' 1 runner-a)
assert_equal 'resource underflow clamps to one' 1 "$actual"

printf '8. Cap three never returns more than three\n'
make_fixture "$TEST_ROOT/case8" 16 0 40960 'max 100000' max 0
make_snapshot "$TEST_ROOT/case8/peers.tsv" 1
actual=$(compute "$TEST_ROOT/case8" 3 4 2048 workers "$TEST_ROOT/case8/peers.tsv" 1 runner-a)
assert_equal 'cap is enforced' 3 "$actual"

printf '9. Missing/malformed snapshots and no token fall back to --shards=4\n'
make_fixture "$TEST_ROOT/case9" 16 0 40960 'max 100000' max 0
printf 'not a snapshot\n' >"$TEST_ROOT/case9/bad.tsv"
unset WOODPECKER_TOKEN
actual=$(compute "$TEST_ROOT/case9" 16 4 2048 workers "$TEST_ROOT/case9/unreachable.tsv" 1 runner-a 2>"$TEST_ROOT/case9/missing.err")
assert_equal 'missing snapshot works without a token' 4 "$actual"
WOODPECKER_TOKEN='must-not-be-printed'
export WOODPECKER_TOKEN
actual=$(compute "$TEST_ROOT/case9" 16 4 2048 workers "$TEST_ROOT/case9/bad.tsv" 1 runner-a 2>"$TEST_ROOT/case9/malformed.err")
assert_equal 'malformed snapshot uses finite shard fallback' 4 "$actual"
if grep -q 'must-not-be-printed' "$TEST_ROOT/case9/missing.err" "$TEST_ROOT/case9/malformed.err"; then fail 'Woodpecker token is never emitted'; else pass 'Woodpecker token is never emitted'; fi
unset WOODPECKER_TOKEN

printf '10. Shard mode is deterministic and stays in [1, cap]\n'
make_fixture "$TEST_ROOT/case10" 16 0 40960 'max 100000' max 0
make_snapshot "$TEST_ROOT/case10/peers.tsv" 1
first=$(compute "$TEST_ROOT/case10" 7 4 2048 shards "$TEST_ROOT/case10/peers.tsv" 1 runner-a)
second=$(compute "$TEST_ROOT/case10" 7 4 2048 shards "$TEST_ROOT/case10/peers.tsv" 1 runner-a)
host_count=$(compute "$TEST_ROOT/case10" 7 4 2048 shards "$TEST_ROOT/case10/peers.tsv" '' runner-a)
assert_equal 'repeated shard count is deterministic' "$first" "$second"
assert_equal 'CI_SYSTEM_HOST selects the matching runner' "$first" "$host_count"
if [ "$first" -ge 1 ] && [ "$first" -le 7 ]; then pass 'shard count is bounded by cap'; else fail "shard count is out of range (got $first)"; fi
cli_output=$("$HELPER" --cap 1 --shards 1 --memory-per-worker-mb 1 --mode shards 2>/dev/null)
assert_equal 'standalone CLI emits one positive integer' 1 "$cli_output"

printf '11. cgroup v1 fractional quota 150000/100000 -> 1\n'
make_fixture "$TEST_ROOT/case11" 16 0 40960 'max 100000' max 0
rm "$TEST_ROOT/case11/cgroup/cpu.max"
mkdir -p "$TEST_ROOT/case11/cgroup/cpu"
printf '150000\n' >"$TEST_ROOT/case11/cgroup/cpu/cpu.cfs_quota_us"
printf '100000\n' >"$TEST_ROOT/case11/cgroup/cpu/cpu.cfs_period_us"
make_snapshot "$TEST_ROOT/case11/peers.tsv" 1
actual=$(compute "$TEST_ROOT/case11" 16 4 2048 workers "$TEST_ROOT/case11/peers.tsv" 1 runner-a)
assert_equal 'v1 fractional quota is floored safely' 1 "$actual"

printf '12. nproc fallback is used when affinity is unavailable\n'
make_fixture "$TEST_ROOT/case12" 16 0 40960 'max 100000' max 0
rm "$TEST_ROOT/case12/proc/self/status"
mkdir -p "$TEST_ROOT/case12/bin"
printf '#!/bin/sh\nprintf "6\\n"\n' >"$TEST_ROOT/case12/bin/nproc"
chmod +x "$TEST_ROOT/case12/bin/nproc"
make_snapshot "$TEST_ROOT/case12/peers.tsv" 1
actual=$(compute "$TEST_ROOT/case12" 16 4 2048 workers "$TEST_ROOT/case12/peers.tsv" 1 runner-a)
assert_equal 'nproc provides the allowed CPU count' 6 "$actual"

printf '13. Malformed load data fails safe to one worker\n'
make_fixture "$TEST_ROOT/case13" 16 . 40960 'max 100000' max 0
make_snapshot "$TEST_ROOT/case13/peers.tsv" 1
actual=$(compute "$TEST_ROOT/case13" 16 4 2048 workers "$TEST_ROOT/case13/peers.tsv" 1 runner-a)
assert_equal 'malformed load cannot increase concurrency' 1 "$actual"

if [ "$failures" -gt 0 ]; then
  printf '%s test assertion(s) failed\n' "$failures" >&2
  exit 1
fi
printf 'All ci-workers acceptance cases passed.\n'
