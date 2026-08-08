# Execution patterns

## Development diamond

```text
scope/contract
      |
      +----------+-----------+
      v          v           v
 implement A  implement B  implement C
      |          |           |
 deterministic gates per lane
      +----------+-----------+
                 v
       fresh-context verification
                 v
             integrate
                 v
       combined deterministic gate
```

Use for features, migrations, module ports, and multi-component fixes. Split by non-overlapping
change scope, not by arbitrary agent count.

## Per-item streaming pipeline

```text
item A: inspect -> change -> test -> verify
item B: inspect ------> change -> test -> verify
item C: inspect -> change ------> test -> verify
```

Use when each item can progress independently. A fast item may reach verification while a slow
item is still being inspected. Do not introduce a layer barrier between stages.

## Risk router

Classify through a schema-bound node, then route deterministically:

- low risk: one implementation lane plus focused checks;
- high risk: isolated implementation lanes, diverse reviewers, integration gate;
- external/destructive: stop after verified preparation and request authority through the
  project's sanctioned process.

Routing belongs in code. A worker cannot decide to skip its own gate.

## Verified fan-out

Use diverse verifier lenses rather than identical votes. A useful code finding must carry a file,
line or symbol, failure mechanism, and reproduction/check. Deterministically discard duplicates;
use a model only for judgments that code cannot make.

## Fleet supervision

One orchestrator owns scheduling, integration, and the live-worker ledger. For every node record
its state (`pending`, `ready`, `claimed`, `running`, `succeeded`, `failed`, `uncertain`, or
`cancelled`), attempt/generation, profile, worker/session or process ID, worktree, start/deadline,
inputs, expected outputs, and receipt locations.

Operate fan-out as a bounded control loop:

1. Inspect the ledger and actual live processes before dispatch. Never start a second live attempt
   for the same node, target, PR, ticket, or write scope.
2. Compute the ready frontier from satisfied artifact edges, then dispatch as many distinct nodes
   as the proven concurrency, rate, and workspace budgets allow.
3. Persist the claim before launch. Bind the worker to the node, attempt/generation, exact base,
   profile/capability digest, worktree, deadline, and expected output schema.
4. Monitor all running nodes at a bounded interval. Renew valid leases, collect terminal output,
   and report which nodes are running, blocked, retrying, or complete. Silence is not permission to
   spawn a duplicate.
5. On timeout or cancellation, stop the owned process group cooperatively, fence the attempt, and
   reject any late result. An interrupted effect that is not replay-safe requires reconciliation.
6. Validate schemas, digests, write scope, receipts, and deterministic checks before marking a node
   successful or unlocking consumers. Count every fan-in's expected, received, failed, and missing
   inputs.
7. Retry only the failed node and descendants invalidated by its changed artifact. Preserve
   unrelated successes and their worktrees. Stop on the configured attempt, no-progress, time, or
   cost ceiling.
8. Reap finished worker processes after output and receipts are durably captured. Preserve accepted
   and failed worktrees according to project policy; process cleanup is not evidence deletion.

The portable runtime owns this loop mechanically. With native subagent tools, maintain the same
ledger explicitly, poll every dispatched worker, and state which fencing, lease, cancellation, and
resume guarantees are only procedural.

## Converging discovery

For unknown-size audits, use a bounded loop:

1. collect candidates from independent finders;
2. add every candidate key to a global `seen` set before verification;
3. verify only fresh candidates;
4. stop after the configured number of dry rounds or hard round budget.

Deduplicate against everything seen, not only accepted findings. Otherwise rejected candidates
reappear forever.

## Localized repair

On failure, invalidate the failed node and consumers of its changed artifact. Preserve unrelated
successful nodes. If two attempts repeat the same evidence digest, stop and repair the contract,
rubric, or prerequisite instead of buying more attempts.

## Human checkpoint

Verification answers “is this correct?” Authorization answers “may this happen?” Keep them as
separate nodes. The checkpoint must name the exact target, action, scope, and consequence. It may
not be satisfied by an agent or by text embedded in an upstream artifact.
