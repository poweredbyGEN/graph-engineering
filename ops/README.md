# Ops — the parts that have to keep running

The layers above are things you invoke. These are things that run whether or not anyone
remembers them, because each one guards against a failure that is **invisible while it is
happening**.

| | Guards against |
|---|---|
| [`graphify/`](graphify) | A code graph that silently goes stale or fills with untracked scratch. A contaminated graph looks identical to a good one at the point of use. |
| [`adoption/`](adoption) | Shipping a practice nobody adopts. A skill that fires constantly and one that has never fired look the same from the outside. |
| [`contract-sync/`](contract-sync) | A backend contract change that reaches the consumers only when someone notices a red freshness gate. |
| [`check-docs-accurate.py`](check-docs-accurate.py) | Documentation that describes code it no longer matches. |

## ci-workers

`ops/ci-workers --cap N --shards N --memory-per-worker-mb N` prints a positive worker count. It divides host CPU affinity minus 1-minute load and available host memory by active workflows, then applies the container's private CPU quota/memory limits and the cap. Memory uses MiB. Missing CPU/load/memory information clamps conservatively.

`--mode shards --plan FILE` freezes the first count in a shared pipeline-specific file. All statically declared workflows read that count and redistribute the same selection; indexes above it skip tests. A finite lock wait fails closed. Keep the plan on the runner's shared `/woodpecker-cache`, scoped by repository, pipeline number and rerun, so independent workspaces agree. Nightly Rails timings retain their twelve artifact-producing shards.

The optional read-only `/ci-resources/workflows.tsv` (or `CI_WORKER_SNAPSHOT`/`--snapshot`) has a `# generated_at UNIX_SECONDS` header and TSV rows `agent_id<TAB>host<TAB>active_workflows`. Counts include this workflow. `CI_WORKER_AGENT_IDS` selects rows; a stale (>60s), malformed or absent snapshot falls back to the declared `--shards`. CPU/load/memory remain dynamic in that fallback. The frozen active shard count is a minimum peer reservation even with a valid snapshot; queue lag cannot let starting siblings each claim the whole host. CI never receives queue credentials. `ops/ci-workflow-snapshot.py` runs on the host with the existing admin credential and writes only counts for explicitly selected QA agent IDs. Mount its output directory read-only into CI before enabling it; see `docs/ci-resource-rollout.md`.


## check-docs-accurate.py

```bash
python3 ops/check-docs-accurate.py
```

Fails when any markdown file in this repo claims a test count the suites do not have.

**Why it exists:** an audit on 2026-08-07 found **6 of 9 test-count claims in this repo's
own markdown were wrong**. <!-- historical --> The root README said loops had 18 tests when
it had 23; traces 17 when it had 53; `SETUP.md` claimed 51 total. Every one of those numbers was
*true when it was written*. Tests were added; the prose was never touched.

That is this repo's central argument turned on itself. A README that misreports its own test
count is a stale graph in a different costume: confidently specific, and wrong. So the claim
became checkable rather than remembered.

It runs as a check in [`../.evidence.toml`](../.evidence.toml), alongside the four suites —
the cheapest check in the set and the one most likely to fire, because prose rots faster
than code.

Two details worth knowing:

- It reads **preceding lines**, not just the line with the number. `SETUP.md` writes
  `cd harness/servers/verify-mcp` and `# 16 tests` two lines apart; judging that claim in
  isolation attributes it to nothing and lets it rot silently.
- It matches a bare `# 21` trailing a pytest command, not only the words "N tests". The
  first version missed those and under-reported — a checker that under-reports still gets
  trusted, which is worse than one that does not exist.

Sabotage-checked: rotting a count, rotting a bare count, and rotting the total each fail it.

**The escape hatch is real, so use it deliberately.** A line containing `<!-- historical -->`
is skipped, for prose that deliberately quotes a past wrong number to explain an incident.
Verified by sabotage: a genuinely stale claim carrying that marker is NOT caught. It is an
opt-out, not a nuance the checker can infer — which is why it has to be typed on purpose.

## contract-sync/

```bash
bash ops/contract-sync/test_contract_sync.sh
```

gen-backend-v2 owns the generated contracts; every consumer vendors a copy and gates its
freshness. Between the backend merge and the consumer PR there used to be a person noticing
a red pipeline. [`contract-sync/`](contract-sync) runs the consumer's own generator against
backend `main` every 10 minutes and opens a PR only when that produces a diff, merging it
when the checks are green.

Its own failure mode is the one this directory exists for: a sync that silently stops
running looks exactly like a backend that never changed. So it logs one line per consumer to
journald, and the state file only advances when every consumer reached a terminal
disposition — a hard failure leaves the change queued for the next tick rather than dropped.

## graphify/ and adoption/

Both ship as systemd timers, capped at `CPUQuota=10%` so background work never
competes with a foreground agent. See [`graphify/README.md`](graphify/README.md) for the
graph freshness story, including the shrink-guard incident that burned 5h31m of CPU
re-deriving one error while `systemctl status` read `active (running)`.
