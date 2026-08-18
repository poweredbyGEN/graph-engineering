---
name: graph-engineering
description: "Choose and execute the simplest evidence-gated software workflow: a linear agent loop by default, native transient fan-out for proven independent lanes, or the durable graph runtime for repeated, long-running, high-value, resumable, or effectful work. Use when facing substantial multi-file work, parallel agents, workflow orchestration, migrations, broad audits, independent verification, mixed workers, durable resume, or when Graphify can reveal real code dependencies. Also use when asked to set up, install, or configure graph engineering on a machine or repository. Do not require graph ceremony for small, sequential, or one-off tasks."
---

# Graph Engineering

Use Graphify as the map, the host agent as the ordinary scheduler, and the portable runtime only
when durability pays. Checks—not agent confidence—decide acceptance.

## 1. Choose the execution tier

Start from the task, not repository maturity. When `graphify-out/graph.json` exists, query it before
raw code searches and validate that returned source paths are tracked. Otherwise inspect the
bounded source and existing project evidence directly.

Classify the work as exactly one tier:

- **LINEAR** — default. One goal/domain/stop condition, no useful parallel frontier, or setup and
  integration would erase the expected benefit. Use one evidence loop plus project checks.
- **TRANSIENT_GRAPH** — at least two useful lanes can overlap, their inputs and write scopes are
  separable, and forecasted wall-time/context/quality benefit exceeds orchestration tax. Use native
  subagents, isolated worktrees, an orchestrator ledger, and one integration owner. No project
  capsule, workflow JSON, state database, or named-human freeze is required for read-only/local work.
- **DURABLE_GRAPH** — the task is repeated, long-running, high-value/high-risk, resumable across
  failures/sessions, or effectful enough to justify leases, fencing, checkpoints, approvals, and
  durable receipts. Use the portable runtime.

Do not use file count, available tests, or multiple possible reviewers as sufficient evidence for
a graph. Apply the fake-edge test: an edge exists only when exact data, authority, quota, or an
unsafe shared resource crosses it.

For the portable runtime, run the task-specific chooser when available:

```bash
graph-engineer choose --task task.json --repo "$PWD" --json
```

Treat repository probes as advisory evidence. A missing type checker, capsule, worker profile, or
workflow does not mean a linear or transient task is invalid.

A LINEAR run that leaves `graph_task_*`, the durable runtime, and fan-out unused is the skill
succeeding, not failing. Report it as "tier choice correct; graph machinery not required" — never
as machinery overhead or as evidence the skill did not help. Machinery invoked on a task that did
not need it is the failure mode; machinery correctly left cold is the success mode.

## 2. Establish the run brief and authority

Every tier needs a compact immutable brief:

```json
{
  "goal": "one outcome",
  "in_scope": ["bounded targets"],
  "out_of_scope": ["explicit exclusions"],
  "acceptance": ["deterministic evidence"],
  "guard_metrics": ["countermetric exposing a gamed objective; required for metric-moving goals"],
  "authority": "read|local_write|external|destructive"
}
```

An objective stated as moving a metric (increase, reduce, maximize a rate/count/score) must name at
least one **guard metric**: an independent signal that would expose the target metric being
satisfied against its intent — the Goodhart failure, e.g. "ticket resolution rate up" achieved by
closing conversations early while churn doubles. Acceptance is not met while a guard metric
regresses unexplained. Pure evidence acceptance (tests pass, exact SHA deployed, artifact read
back) needs no guard metric.

Read project instructions and inspect existing tests, lint, types, builds, probes, git state, and
worktree rules. Never infer permission to merge, deploy, message, mutate credentials, write
production, or perform destructive actions from a graph.

For a recurring product workflow, read [references/planning.md](references/planning.md) and promote
the compact brief into a reviewed project capsule. A capsule is optional for transient work.

## 3. Separate RolePolicy from WorkGraph

Treat authority as a slow, versioned **RolePolicy**: profile capabilities, tool access, write roots,
effect ceilings, deployment targets, approvals, network boundaries, and cost ceilings.

Treat tasks, artifact dependencies, conditions, and iteration as the fast **WorkGraph**. A WorkGraph
may narrow RolePolicy authority; it must never add a capability, scope, effect, target, approval,
or budget absent from RolePolicy. Validate the effective intersection before dispatch and again on
resume or policy drift.

## 4. Build executable nodes and edges

Use models only for judgment and implementation. Use the runtime's closed deterministic operations
for plumbing:

- JSON Schema validation;
- select/map;
- stable union and dedupe;
- deterministic sort;
- typed predicates;
- risk routing;
- typed verdict reduction.

Never use arbitrary expressions, model prose, shell evaluation, or worker self-routing as an edge.
Conditional routes must name typed predicates and declared destinations. Cycles must be explicit,
checkpointed, and bounded by iteration, attempt, time, no-progress, token, and cost ceilings.

For machine-readable durable workflows, read
[references/workflow-contract.md](references/workflow-contract.md). For topology and native fleet
operation, read [references/patterns.md](references/patterns.md).

## 5. Execute the ready frontier

For `TRANSIENT_GRAPH`, fan out distinct ready lanes immediately:

1. one agent per lane and one lane per agent;
2. one isolated nested worktree per writing lane;
3. minimal context: task, required source, typed inputs/outputs, authority, and checks;
4. one orchestrator-owned live-worker ledger with no duplicate target/PR/ticket/protocol;
5. monitor every active lane about every 30–60 seconds;
6. validate returned artifacts and exact checks before unlocking consumers;
7. retry only the failed lane and invalidated descendants;
8. one integration owner runs the combined gate.

For `DURABLE_GRAPH`, read [references/runtime-guide.md](references/runtime-guide.md), run preflight,
and let the runtime own readiness, leases, fencing, receipts, checkpoint/resume, and typed repair.

## 6. Apply risk-based verification

Risk determines verification; model review is not mandatory ceremony:

- **low** — deterministic checks only;
- **medium** — checks plus one independent fresh-context verifier;
- **high** — checks plus distinct multi-lens verifiers, at least one running an adversarial refute
  lens, and a named human/effect approval boundary;
- **external/destructive** — preparation and verification do not grant execution authority.

An independent verifier must not inherit the producer's hidden context or summary. Give it the
requirements, raw source/diff, raw evidence, and its explicit lens. Where profile diversity is
available, do not use the producer identity as its own verifier. Reduce typed verdicts with
deterministic code; agreement alone is not correctness.

An **adversarial refute** verifier is tasked to overturn the conclusion, not to review it: it must
attempt to refute, default to `refuted` when uncertain, and emit a typed verdict. The conclusion
passes only when the refutation demonstrably fails. Survival of refutation — not agreement — is the
pass signal for high-risk work.

## 7. Gate and recover

Run existing deterministic checks before model review. When a check fails:

1. preserve exact structured failure evidence;
2. retry only the responsible node and artifact descendants;
3. fence late results;
4. stop at attempt, no-progress, iteration, time, token, and cost ceilings;
5. repair the task, prerequisite, route, or check when failure evidence repeats.

Sabotage-check every new checker: break the protected behavior, prove the checker fails, restore it,
and prove it passes.

Treat progress as a monotonic evidence transition, never as activity. Progress is exactly one of:

- a newly accepted immutable artifact bound to current input/policy/source digests;
- a deterministic reduction in the unresolved set;
- movement through an evidence-bound lifecycle such as verified → integrated → exact-SHA green →
  merged → deployed-SHA read back → live proof.

Agent completion, worker count, retries, elapsed time, logs, prose summaries, open PRs, and checks
against an older source/input digest are not progress. When an input, policy, source SHA, acceptance
suite, or observation freshness binding changes, invalidate all affected descendants before
reporting forward movement. Every recurring status report must state the accepted delta, invalidated
delta, active unique nodes, exact next frontier, and whether the unresolved set shrank. If none of
those changed, report **no progress** plainly.

## 8. Measure whether the graph paid

Record per objective and per run:

- cold setup/adoption time and steady-state wall time;
- input/output tokens, model, and monetary cost per node and total;
- deterministic gate results, retries, repeated failures, and integration failures;
- verifier overturns and human corrections;
- accepted integrated outcome and time to merged/deployed/live proof when applicable;
- declared objective metrics and their guard metrics — a run that moves the target while a guard
  metric regresses unexplained is a failed outcome, not a win;
- baseline identity and equal acceptance-suite digest.

Do not treat invocation count, artifact count, raw overlap, or nonzero fail-closed exits as product
success/failure. Compare accepted integrated outcomes at equal evidence quality.

Promote a transient graph to a reviewed durable template only after repeated matched runs prove it
beats a direct-loop baseline on the declared latency/quality/cost objective, includes cold setup,
reports cost, does not worsen escaped defects, and does not regress any declared guard metric.
Human review approves promotion; the runtime must never promote itself.

## 9. Report precisely

Report the chosen tier and why, actual parallel frontier, lanes and worktrees, checks and results,
verification appropriate to risk, retries/failures, integration outcome, wall/setup time, tokens
and cost, and remaining assumptions. Distinguish implemented, integrated, merged, deployed, and
verified live.

## Reference routing

- Read [references/setup.md](references/setup.md) when asked to set up, install, verify, or
  configure graph engineering — the runtime, private workers, or MCP registration — on a machine
  or repository.
- Read [references/planning.md](references/planning.md) only for recurring product workflows or
  unresolved product ambiguity—not every transient task.
- Read [references/workflow-contract.md](references/workflow-contract.md) before changing durable
  workflow contracts, deterministic operations, routes, cycles, budgets, effects, or artifacts.
- Read [references/patterns.md](references/patterns.md) for transient fan-out, dependency audits,
  routing, risk verification, integration, and fleet supervision.
- Read [references/runtime-guide.md](references/runtime-guide.md) only for `DURABLE_GRAPH`.
- Read [references/extending.md](references/extending.md) before changing the skill/runtime or adding
  adapters, operations, topologies, policy surfaces, MCP, or A2A.
