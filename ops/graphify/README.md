# Graphify freshness: keeping the code graph true

A code graph that silently goes stale is worse than no graph. Agents told "graph before
grep" will follow it confidently into a picture of the repo as it was months ago — and a
stale graph and a fresh one look identical at the point of use.

Two processes keep it true, deliberately split:

| | `gen-graphify-postdeploy` | `gen-graphify-nightly` |
|---|---|---|
| **Trigger** | a repo deploys | 22:00 local, every night |
| **Covers** | repos listed in `graph.externally_managed` | every other active repo |
| **Owns** | the `paths.mirrors` tree | the `paths.projects` checkouts |
| **Latency** | minutes after deploy | up to 24h |

Both run under `graphify-cap.slice` (`CPUQuota=10%`). Indexing must never compete with a
foreground agent.

## Why not a Woodpecker step?

It was considered and rejected. A graph rebuild takes 2–20 minutes; as a pipeline step it
becomes a deploy gate, standing between a hotfix and production for work that has nothing
to do with whether the code is correct. The reconciler's own docstring is explicit:

> *This process is deliberately independent of gen-deployd/Woodpecker. A slow/failed graph
> build can never hold a deploy worker or make a deploy fail.*

Async reconciliation gives the same freshness without putting indexing on the critical path.

## Why a nightly sweep on top of deploy-triggered?

Deploy-triggered refresh only helps repos that deploy. An audit on 2026-08-07 found **26 of
34 repos with commits in the last 30 days had no graph at all** — infra, tooling, SDK, and
docs repos ship by other paths, so nothing ever triggered a build for them.

The sweep discovers repos instead of reading a hardcoded list. That list is precisely why
those 26 were invisible: the reconciler's `REPOS` dict has 8 entries and a new repo has to
be added by hand, which never happens.

## Discovery rules

A repo enrolls itself when it has ≥5 commits in the last 30 days and is not already owned
by the reconciler. Three details that are easy to get wrong:

- **Dedup by remote, not by path.** `aura-executor` and `aura-automation-service` are two
  checkouts of one GitHub repo. Keying on directory builds the same graph twice and races
  two writers onto one output.
- **Skip worktrees.** A worktree's `.git` is a *file*, not a directory. Graphing one
  duplicates its parent against a different path.
- **Never touch reconciler-owned repos.** Two processes writing one `graphify-out` can
  corrupt a graph that agents then trust.

## Safety rails

| Rail | Default | Why |
|---|---|---|
| `min_free_gb` | 25 | The origin machine's disk was at 84% when this was written. A build that fills the disk takes the box with it. Re-checked *between* repos, not just at start. |
| `SWEEP_BUDGET` | 6h | Starts at 22:00, so it is always done before the workday. |
| `BUILD_TIMEOUT` | 30m | One pathological repo must not starve every other repo. |
| `MAX_GRAPH_AGE_HOURS` | 20 | Don't rebuild what is already fresh. |
| `MAX_AUTO_FORCE_SHRINK` | 50 | See below. |

A failed repo is logged but does **not** fail the unit — one bad repo must not mark the
sweep red and hide that the other twenty succeeded.

## The shrink-guard, and why `--force` is conditional

`graphify` refuses to overwrite a graph that has fewer nodes than the existing one,
protecting against truncated chunk files. But **node IDs embed line numbers**
(`billing_client_rationale_253`), so any commit that *moves* code renames nodes rather than
deleting them.

On 2026-08-07 a revert on one indexed repo produced **22 removed / 20 added = net −2**, with
zero files deleted. The guard read that identity churn as data loss and refused. Combined
with `Restart=always` and a cooldown that had no failure counter, the same build retried
every ~7 minutes for 5.5 hours — 5h31m of CPU and a 9.7G memory peak, while
`systemctl status` read `active (running)` the entire time.

Both paths now retry with `--force` **only** for a small net drop (≤50 nodes). A large drop
is the truncation the guard exists to catch and must still fail loudly. A blanket `--force`
would have disabled a real safety check to fix a misfire.

## Deploying this elsewhere

```bash
install -m 755 gen-graphify-nightly /usr/local/sbin/
install -m 644 gen-graphify-nightly.{service,timer} /etc/systemd/system/
# site drop-in: point the installed script at this checkout's ops/ for site_config
mkdir -p /etc/systemd/system/gen-graphify-nightly.service.d
printf '[Service]\nEnvironment=AGENT_INFRA_OPS=%s\n' "$(pwd)/.." \
  > /etc/systemd/system/gen-graphify-nightly.service.d/site.conf
systemctl daemon-reload
systemctl enable --now gen-graphify-nightly.timer
```

Requires `graphify-cap.slice` to exist (both units reference it). Verify without waiting for
the night:

```bash
systemctl start gen-graphify-nightly.service    # runs now, still CPU-capped
tail -f /var/log/gen-graphify-nightly.log
```

Tests: `traces/tests/test_graphify_nightly.py` (16) and
`traces/tests/test_graphify_reconciler.py` (10). Both suites are sabotage-checked — the
safety rails, the ownership boundary, and the retry bound each fail their test when broken.
