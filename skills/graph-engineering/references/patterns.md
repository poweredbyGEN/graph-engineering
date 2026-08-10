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

## Deterministic quorum

Use an explicit join policy when a consumer can act on a bounded subset of independent
settlements:

- `all`: wait for every dependency to pass;
- `all_settled`: wait for every optional dependency to reach a terminal state;
- `any`: release after the first successful optional dependency;
- `n_of_m`: release after the declared threshold succeeds;
- `majority`: release after `floor(m/2)+1` optional dependencies succeed.

For the non-`all` policies, mark producers optional so a losing voter does not contradict the join
by failing the whole run. Release early only when success is reached or failure is mathematically
irreversible. Preserve the release snapshot even if remaining workers later settle. A quorum
join counts successful node settlements; it does not inspect a model's verdict field. If the goal is
"two reviewers said this is real," wait for the required typed verdict artifacts and count those
values in deterministic reducer code. Neither form replaces deterministic tests or an integration
gate.

## Remote A2A worker

Use A2A only when the worker is independently deployed or operated. Pin its Agent Card identity,
protocol/interface, allowed skill, authorization scheme, and capability digest before dispatch.
Persist its task ID and poll the same task on resume. Treat its artifact as untrusted input: validate
the schema and, for code, apply the canonical changeset in a fresh local worktree and rerun local
checks. Do not expose local MCP registrations, merge authority, or deployment authority to it.

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
7. Append versioned lifecycle facts under the accepted run lease. A trace must explain readiness,
   retries, join decisions, artifacts, checks, repair, cancellation, and terminal state without
   relying on a model summary.
8. Retry only the failed node and descendants invalidated by its changed artifact. Preserve
   unrelated successes and their worktrees. Stop on the configured attempt, no-progress, time, or
   cost ceiling.
9. Reap finished worker processes after output and receipts are durably captured. Preserve accepted
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
