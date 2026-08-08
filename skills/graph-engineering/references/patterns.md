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
