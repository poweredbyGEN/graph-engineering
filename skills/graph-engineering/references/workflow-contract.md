# Workflow contract

Use this contract when handing a graph to a scheduler. Omit fields the runner cannot enforce;
never describe retries, isolation, schemas, or approvals as guarantees when they are only prose.

```yaml
version: graph-engineering/v1alpha1
id: dev-change
goal: One sentence describing the accepted integrated artifact
budgets:
  max_nodes: 20
  max_concurrency: 4
  max_attempts_per_node: 2
  max_total_attempts: 30
  timeout_seconds: 1800
nodes:
  - id: implement_api
    kind: agent
    needs: [scope]
    inputs:
      contract: scope.contract
    outputs:
      change:
        schema: schemas/change.json
    engine: codex
    model_tier: standard
    permission: write
    workspace: worktree
    write_scope: [src/api/**, tests/api/**]
    verify_cmd: [pytest, -q, tests/api]
    timeout_seconds: 600
    max_attempts: 2
    required: true
  - id: integrate
    kind: integration
    needs: [implement_api, implement_ui, verify_changes]
    verify_cmd: [pytest, -q]
outputs:
  result: integrate.result
```

## Required validation

- IDs are unique and references resolve.
- The required-edge graph is acyclic.
- Every non-entry input has exactly one producer unless an explicit reducer owns the collision.
- Producer output and consumer input schemas are compatible.
- Entrypoints have no incoming artifact edges.
- Every node is reachable from an entrypoint and contributes to a declared output or gate.
- Fan-out, attempts, wall time, and concurrency have finite positive bounds.
- Parallel write scopes do not overlap unless isolated and followed by an integration node.
- External, destructive, merge, deploy, or messaging nodes require explicit authority outside the
  workflow; a model-created approval token is invalid.

## Stable failure result

Do not replace failure with `null`. Preserve at least:

```json
{
  "node_id": "implement_api",
  "status": "failed",
  "code": "CHECK_FAILED",
  "attempt": 2,
  "retryable": false,
  "evidence_artifact": "sha256:...",
  "message": "pytest tests/api returned 1"
}
```

Fan-in must see successful, failed, skipped, and cancelled settlements so it can distinguish a
complete result from silent loss.

## Typed repair edge

A combined integration failure is not permission to retry the graph. Declare a route from exact
deterministic check IDs to exact producer nodes and input names:

```yaml
repair:
  routes:
    - id: combined_to_api
      check_ids: [combined]
      targets:
        - node: implement_api
          input: integration_failure
      max_rounds: 1
      no_progress_limit: 1
```

The target must be a required writing agent that directly supplies a changeset to that integration
and has no other downstream consumer that would be silently invalidated. Its retry budget and the
integration retry budget must cover the initial attempt plus declared repair rounds; the workflow
total must cover the complete worst case. Destructive and non-idempotent targets are rejected:
repair routing does not grant authority to repeat an effect that may already have occurred.

The runtime supplies `integration_failure` as a fixed typed artifact with `code`,
`integration_node`, `integration_attempt`, `check_id`, bounded `evidence`, and
`failure_digest`. It invalidates only declared targets, preserves unrelated producer artifacts,
and rebuilds integration. Unknown checks, ambiguous routes, non-check failures, exhausted budgets,
and repeated identical failure digests stop without an inferred target.
