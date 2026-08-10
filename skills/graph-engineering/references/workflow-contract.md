# Workflow contract

Use this contract when handing a graph to a scheduler. The CLI accepts JSON only. Omit fields the
runner cannot enforce; never describe retries, isolation, schemas, or approvals as guarantees when
they are only prose.

This minimal read-only diamond is directly valid workflow JSON:

```json
{
  "version": "graph-engineering/v1alpha1",
  "id": "parallel_audit",
  "goal": "Audit two independent surfaces and synthesize a typed result",
  "budgets": {
    "max_nodes": 3,
    "max_concurrency": 2,
    "max_attempts_per_node": 2,
    "max_total_attempts": 5,
    "timeout_seconds": 900
  },
  "nodes": [
    {
      "id": "audit_api",
      "kind": "agent",
      "task": "Audit the API surface and return findings.",
      "needs": [],
      "inputs": {},
      "outputs": {"findings": {"schema": {"type": "object"}}},
      "profile": "verification",
      "workspace": "read-only",
      "permission": "read",
      "required": true
    },
    {
      "id": "audit_config",
      "kind": "agent",
      "task": "Audit configuration boundaries and return findings.",
      "needs": [],
      "inputs": {},
      "outputs": {"findings": {"schema": {"type": "object"}}},
      "profile": "verification",
      "workspace": "read-only",
      "permission": "read",
      "required": true
    },
    {
      "id": "synthesize",
      "kind": "agent",
      "task": "Synthesize the two finding sets without inventing evidence.",
      "needs": ["audit_api", "audit_config"],
      "inputs": {
        "api": "audit_api.findings",
        "config": "audit_config.findings"
      },
      "outputs": {"report": {"schema": {"type": "object"}}},
      "profile": "verification",
      "workspace": "read-only",
      "permission": "read",
      "required": true
    }
  ],
  "outputs": {"report": "synthesize.report"}
}
```

For writing nodes and integration, use the runtime's exact canonical changeset schema. Copy it from
the tagged public `examples/repair-route.workflow.json`; do not shorten or reinterpret it.

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

## Output-aware acceptance

An output schema may intentionally admit both successful results and useful partial failure
evidence. In that case, add a narrower `acceptance_schema` to the output contract. The runtime
content-addresses every schema-valid output first, then evaluates the acceptance schema. A rejected
output fails the attempt and cannot release an edge, but its immutable digest remains in the
structured attempt failure for inspection:

```json
{
  "evidence": {
    "schema": {"type": "object", "required": ["status", "checkpoints"]},
    "acceptance_schema": {
      "type": "object",
      "properties": {"status": {"const": "succeeded"}},
      "required": ["status"]
    }
  }
}
```

Use JSON Schema only: this is a bounded deterministic predicate, not an expression language or a
model verdict. The acceptance schema is also rechecked on resume before a prior success is trusted.

## Typed join

A consumer may declare one deterministic join policy over its `needs`:

```json
{
  "id": "adjudicate",
  "kind": "transform",
  "task": "Consume the exact majority settlement.",
  "needs": ["correctness", "security", "reproduction"],
  "inputs": {},
  "join": {"policy": "majority"},
  "outputs": {"verdict": {"schema": {"type": "object"}}},
  "workspace": "read-only",
  "permission": "read",
  "required": true
}
```

The three producers must be optional. The runtime supplies their immutable settlement snapshot in
`ExecutionContext.join`; a quorum consumer cannot bind artifacts from producers that may still be
running or may fail. `n_of_m` additionally requires `n`. Empty or impossible thresholds and
required producers under a non-`all` policy are validation errors. The snapshot records policy,
threshold, expected, received, passed, failed, cancelled, missing, decision, and each dependency's
state. `majority` means a majority of dependencies completed successfully; it does not count a
boolean or score inside their output. Semantic voting requires typed verdict artifacts and a
deterministic reducer after the required evidence has settled.

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
