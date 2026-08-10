---
name: graph-engineering
description: Plan and execute substantial software work as an evidence-gated dependency graph with parallel subagents, isolated writes, deterministic tests, quorum joins, localized retries, independent review, remote A2A workers, and an explicit integration node. Use when the user asks for graph-style development, a workflow, parallel agents, mixed local or remote workers, verifier consensus, a migration or audit across many files, or a feature with at least three independent work lanes or real ordering constraints. Skip graph overhead for small tasks whose steps are genuinely sequential.
---

# Graph Engineering

Turn complex development work into a bounded graph whose scheduler—not an agent's narrative—
decides what is ready and whose checks—not an agent's confidence—decide what is done.

## 1. Decide whether a graph pays

Use graph execution when at least two of these are true:

- three or more useful work items can run independently;
- one prerequisite unlocks several downstream items;
- the change spans about five or more files, components, routes, or repositories;
- different engines or reviewers can handle bounded portions;
- prior attempts lost context, collided, or repeated whole-task work after one failure;
- success can be graded by tests, lint, types, schemas, builds, or probes.

For a small edit or a true chain, state that graph overhead will not help and execute normally.
Never manufacture parallelism by giving multiple agents the same target.

For an existing or new repository, begin with the read-only adoption audit:

```bash
graph-engineer assess --repo "$PWD" --json
```

If graph execution pays, run `graph-engineer init --repo "$PWD" --json`. This discovers reviewed
workflows and matching durable runs, or scaffolds the minimal public project boundary when none
exists. Inspect and complete any scaffolded product contract, checks, write roots, and deployment
metadata; configure private profiles outside the repository; then rerun `init`. To consume a saved
assessment, pass `--from-assessment <path>`—the CLI rejects it if HEAD or tracked/untracked source
changed. Resume a matching active run instead of creating a duplicate. Assessment and init never
infer secrets, approvals, MCP/A2A transport, deployment targets, or permission to perform effects.

Before any dependency audit or worker fan-out, read
[references/planning.md](references/planning.md). Discover the repository capsule, ask only its
missing product questions, and require a named human to approve the frozen generation. `init` must
report no planning questions and no brief/decision/contract drift before graph construction begins.

## 2. Establish the authority and scope

Before drawing nodes:

1. Read the project instructions and inspect its existing tests, linters, type checks, build,
   git state, and worktree rules.
2. Define the final artifact and the deterministic evidence required to accept it.
3. Identify protected or externally visible actions. Graph execution never grants deployment,
   merge, messaging, credential, production-write, or destructive authority.
4. Choose the execution mode:
   - **Portable runtime:** read [references/runtime-guide.md](references/runtime-guide.md) in full,
     run `graph-engineer doctor`, then follow its start, status, resume, and handoff procedure.
   - **Native fallback:** use the host's subagent primitives and state that fencing, durable resume,
     schema-bound artifact transfer, and typed repair edges are not mechanically enforced.

## 3. Perform the dependency audit

For every proposed edge ask: **what exact data or constrained resource crosses this edge?**

Create an edge only when the consumer needs a producer artifact, both nodes share an unsafe write
target, a schema/interface must land first, or a real quota/permission constraint serializes them.
Words such as “then” and “after that” are not dependencies.

Every node must have:

- one bounded job and stable ID;
- explicit inputs and expected outputs;
- an output schema or artifact contract;
- engine/model tier and permission level;
- workspace mode and non-overlapping write scope;
- timeout, bounded retry, and deterministic acceptance command;
- failure policy: required, optional, or quorum;
- downstream consumers.

Read [references/workflow-contract.md](references/workflow-contract.md) before writing or changing a
machine-readable workflow. The CLI accepts JSON only. Reject cycles, missing producers, ambiguous
input bindings, unbounded fan-out/retries, and parallel writes without isolation before launching.

## 4. Schedule the frontier

Prefer a ready queue or streaming pipeline: launch a node as soon as its own dependencies pass and
a concurrency slot is free. Do not wait for unrelated slow siblings.

Use a barrier only for a real cross-set operation such as complete-set comparison, deduplication,
ranking, quorum, or integration. Count `received` versus `expected` at every fan-in and name missing
items. For large fan-ins, consolidate in bounded layers without discarding file paths, errors, or
evidence.

Declare join semantics in the consumer contract, never in its prompt. Use `any`, `n_of_m`, or
`majority` only over optional independent producers; their failures are settlements, not global run
failures. These policies count successful node settlements, not `true` values inside model output.
For semantic voting, collect typed verdicts and reduce them with deterministic code. Use
`all_settled` when the consumer needs every optional terminal outcome, and `all` when every
dependency must pass. Persist the exact decision snapshot that released or blocked the consumer.
Quorum is useful for availability and bounded evidence collection; it does not turn model agreement
into deterministic code correctness.

Prioritize the neck: ready nodes that unlock the most downstream work. Cap concurrency by actual
independent targets, agent capacity, rate limits, and workspace safety—not an aspirational number.
Follow the fleet-supervision procedure in
[references/patterns.md](references/patterns.md): keep one orchestrator-owned ledger, prevent
duplicate live attempts, monitor every claimed node, fence late workers, validate returned
artifacts, and reap completed processes. Fan-out is not fire-and-forget.

## 5. Execute nodes with isolation

- Give each writing node its own nested git worktree or equivalent sandbox.
- Give every worker only its task, required source context, output contract, allowed tools, and
  acceptance command. Do not dump the orchestrator's full conversation into every node.
- Keep deterministic plumbing—filtering, flattening, stable dedupe, schema validation, routing—
  in code. Use models for judgment and implementation.
- Run mechanical extraction/classification on a fast tier, implementation on the standard tier,
  and fresh-context adjudication on the strongest justified tier.
- Record failures as structured results. One optional branch failure must not discard independent
  successes.

Prefer local subprocess profiles for locally controlled CLIs. Use MCP for bounded tools, data, or
durable task capabilities. Use A2A only to delegate one typed node to an independently operated
remote agent; the graph runtime remains the scheduler and local checks remain the acceptance
authority. Read [references/extending.md](references/extending.md) before adding either boundary.

Exactly one orchestrator owns integration, merge, deploy, or other shared single-writer surfaces.
Workers never self-integrate or self-deploy.

## 6. Gate edges on deterministic evidence

Run the node's existing project checks before model review. A zero agent exit code is not proof;
a nonzero agent exit code does not invalidate a correct artifact if the authoritative checks pass.

When a check fails:

1. return the exact failure and prior-attempt summary to the responsible node;
2. retry only that node and invalidated descendants;
3. stop after the configured attempt/no-progress ceiling;
4. re-plan when the same failure class repeats rather than increasing the budget.

When adding a new checker, sabotage-check it: break the protected behavior, prove the checker
fails, restore the behavior, and prove it passes.

## 7. Review independently, then integrate

After deterministic checks pass, use a fresh-context verifier for material changes. Give it the
requirements, source/diff, and raw evidence—not the implementer's summary. Ask it to reproduce or
refute specific claims through distinct lenses such as correctness, security, composition, and
regression risk.

The final node integrates accepted changes and runs the combined project gate again. Independently
green lanes are not proof that their combination is green. Detect conflicts and interface drift
before promotion; preserve failed work for diagnosis according to project policy.

When an integration check has a known responsible producer, use an explicit typed repair route.
Never infer a repair target from prose or replay a writer without an explicit replay-safe effect.

See [references/patterns.md](references/patterns.md) for diamonds, routers, verified fan-out,
converging discovery, and the canonical `dev-change` graph.

## 8. Report the graph result

Report:

- graph shape and critical path;
- expected, received, passed, failed, skipped, and retried nodes;
- engine/model and worktree for each writing node;
- exact deterministic commands and their results;
- verifier findings and integration outcome;
- wall time, useful overlap, token/cost data when available;
- the versioned lifecycle trace and exact join settlement that released each quorum consumer;
- remaining blockers or unverified assumptions.

Say whether the result is implemented, integrated, merged, deployed, and verified live. These are
different states. Never call a plan, agent message, green lane, or open pull request “done.”

## Reference routing

- Read [references/planning.md](references/planning.md) before planning a feature, changing a frozen
  outcome, drawing dependencies, or dispatching workers.
- Read [references/runtime-guide.md](references/runtime-guide.md) for installation, private worker
  profiles, starting a workflow, status/resume, MCP registration, and troubleshooting.
- Read [references/workflow-contract.md](references/workflow-contract.md) before authoring workflow
  JSON or changing node, edge, budget, effect, artifact, or repair contracts.
- Read [references/patterns.md](references/patterns.md) when selecting topology or deciding between
  a ready queue, streaming pipeline, barrier, router, verifier, cycle, or checkpoint.
- Read [references/extending.md](references/extending.md) before changing this skill/runtime, adding
  an agent adapter, creating an MCP service, or expanding a tool/capability surface.
