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
