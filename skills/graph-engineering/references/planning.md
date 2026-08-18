# Planning and promotion

Use a compact per-run brief for linear and transient work: goal, in-scope targets, exclusions,
acceptance evidence, and authority — plus, for any metric-moving objective, at least one guard
metric that would expose the target being satisfied against its intent (the Goodhart failure:
resolution rate up, churn doubled). Resolve missing facts from tracked source and existing
decisions; ask a human only when the answer changes product behavior, authority, or acceptance.

Do not require a repository planning capsule, workflow manifest, private profiles, deployment
metadata, or named-human freeze merely to inspect code, run a bounded audit, or fan out isolated
local changes.

Create or update `.graph-engineering/PROJECT.md` and its versioned product contract only when the
workflow is recurring, durable, product-ambiguous, or governed across operators. The reviewed
capsule may then hold users, outcomes, journeys, surfaces, data, permissions, invariants,
compatibility, recovery, rollout/rollback, live proof, risks, assumptions, decisions, and acceptance.

A slow RolePolicy owns capability and effect ceilings. A fast WorkGraph references that policy and
may only narrow it. Product-answer changes invalidate derived durable templates; ordinary task or
dependency changes do not require rewriting the full product contract.

## Promotion gate

Promote a transient graph into a durable reviewed template only when all are true:

1. multiple matched executions use the same objective class and acceptance-suite digest;
2. a direct-loop baseline exists;
3. cold setup and steady-state time are both reported;
4. token and cost data are present;
5. the graph improves the declared latency/quality/cost objective without worse escaped defects;
6. every declared guard metric is reported and none regresses unexplained — an objective met by
   betraying its guard metric is a failed run, not a win;
7. a named human reviews the evidence and explicitly accepts the durable RolePolicy and template.

Promotion is a product decision, never an automatic runtime side effect.
