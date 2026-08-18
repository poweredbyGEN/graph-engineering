# Runtime guide

Use this guide for the enforced portable path. The skill decides whether a graph pays; the runtime
enforces contracts, readiness, isolation, evidence, repair, and resume.

## 1. Install and verify

Install the current reviewed alpha, or a newer reviewed tag when available:

```bash
uv tool install \
  https://github.com/poweredbyGEN/graph-engineering/releases/download/v0.1.0a1/graph_engineering-0.1.0a1-py3-none-any.whl
graph-engineer --version
graph-engineering-mcp --help
```

For development, clone `https://github.com/poweredbyGEN/graph-engineering`, run
`uv sync --frozen --extra dev`, and invoke commands with `uv run`.

## 2. Configure private workers

Worker commands, models, endpoints, and secret references belong outside project source:

```bash
install -d -m 0700 ~/.config/graph-engineering
install -m 0600 subagents.example.toml \
  ~/.config/graph-engineering/config.toml
export TMPDIR=/path/to/disk-backed/scratch
graph-engineer doctor --repo "$PWD" --json
```

Obtain `subagents.example.toml` from the tagged source checkout. Replace model aliases, not the
least-authority command boundaries. The template supports Claude, Codex, Grok, Kimi K3, and GLM
5.2; arbitrary profiles use the same contract. Put profiles into stable-hash pools for portable
routing. A checked-in `.graph-engineering.toml` may select routing only; it may not define commands,
models, endpoints, or environment variables.

Keep secrets in a secret manager and reference only environment-variable names. Default writing
workers set `mcp = false` so they cannot inherit the interactive agent's ambient MCP authority.
Create a separate private MCP-capable profile only for a node with an explicit server/tool policy.

For Kimi or GLM through OpenCode, install the tagged
`examples/opencode-agents/graph-worker.md` under `~/.config/opencode/agents/` with mode `0600`, then
use the matching subprocess profile. The permission file is not an OS sandbox; the runtime's
worktree, reduced environment, write scope, and changeset validation are the security boundary.

## 3. Choose before creating a workflow

```bash
graph-engineer choose --brief task-brief.json --repo "$PWD" --focus-path src --json
```

The default is `LINEAR`. Use `TRANSIENT_GRAPH` only with at least two independent lanes, explicit
linear/graph cost estimates, and at least 10% forecast latency gain. Use `DURABLE_GRAPH` only when
the earned graph is long-running, resumable, effectful, or has passed the exact-digest promotion
gate. Graphify is bounded dependency evidence for explicit tracked fresh focus paths; it is not a
scheduler, and node count is never an eligibility signal.

For LINEAR and TRANSIENT_GRAPH, `graph-engineer execute --brief ...` returns a host dispatch
contract without requiring a project capsule. The host owns transient agents. Continue below only
for durable work.

## 4. Create a durable workflow

Store reviewed workflows under `.graph-engineering/workflows/<name>.json`. The CLI accepts JSON,
not YAML. Start from the read-only example in
[workflow-contract.md](workflow-contract.md). For a writing graph, copy the exact canonical
changeset and repair shape from the tagged `examples/repair-route.workflow.json` rather than
retyping its schema.

For each node declare:

- a stable `id`, one bounded `task`, `kind`, dependencies, and typed inputs/outputs;
- a concrete profile or private pool/tier selector;
- `workspace`, `permission`, and non-overlapping `write_scope` for writers;
- deterministic check IDs and argv arrays, never shell command strings;
- finite retry, no-progress, node, concurrency, total-attempt, and wall-time budgets;
- an explicit effect classification before any automatic retry or repair;
- `required` behavior and the downstream artifact consumers.

Prefer the closed deterministic operation registry for data movement and decisions:
`schema_validate`, `select`, `map`, `stable_union`, `dedupe`, `sort`, `typed_predicate`,
`risk_router`, and `verdict_reducer`. Typed routes persist their decision. A bounded loop may target
only a static ancestor region; every looped node must be replay-safe/effect-free and every
iteration is checkpointed under the shared attempt budget.

When the workflow binds `.graph-engineering/role-policy.json`, every agent declares exact narrowed
authority and risk verification. Low risk uses deterministic checks only. Medium uses one distinct
profile/lineage/context verifier consuming an explicitly classified producer raw-evidence output.
High uses two distinct lenses and a required named-human approval. A required verifier's typed
verdict schema must accept only `verdict: pass`. Provider-reported tokens/cost are stored in
receipts and checked against the node's reviewed ceiling; missing usage fails closed.

Use an integration node for changesets. It alone combines accepted writer artifacts and runs the
complete project gate. An integration repair route maps exact failing check IDs to exact producer
and input names; it is not permission to retry the graph.

## 5. Start the workflow

Run the deterministic preflight before spending model tokens:

```bash
workflow=.graph-engineering/workflows/auth-audit.json
run_id=auth-audit-001

graph-engineer doctor --repo "$PWD"
graph-engineer validate "$workflow"
graph-engineer plan "$workflow" --json
graph-engineer run "$workflow" --repo "$PWD" --run-id "$run_id" --json
```

`validate` checks the schema and graph. `plan` reports ready layers, dependencies, unlocks, and the
critical path without loading private profiles or launching agents. `run` resolves profiles,
preflights capabilities, creates durable state, and launches only ready nodes.

Do not start a second live run with the same objective/worktree scope. Inspect the existing run
first and resume it when appropriate.

## 6. Inspect, resume, and integrate

The run output prints its state database. The default is
`~/.local/state/graph-engineering/runs/<run-id>/state.db`; artifacts and receipts live beside it.

```bash
state=~/.local/state/graph-engineering/runs/auth-audit-001/state.db

graph-engineer status --state "$state" --run-id auth-audit-001 --json
graph-engineer status --state "$state" --run-id auth-audit-001 --projection --json
graph-engineer trace --state "$state" --run-id auth-audit-001 --json
graph-engineer run "$workflow" --repo "$PWD" --run-id auth-audit-001 \
  --state "$state" --resume --json
```

Resume with the exact workflow, run ID, repository, base SHA, profile identity, and state path.
Workflow/profile/base drift, missing or corrupt receipts, and invalid artifacts fail closed. A
non-replay-safe interrupted effect enters reconciliation; never edit state to force it forward.
For a run created by a pre-lifecycle release, inspect it first and explicitly add
`--bootstrap-legacy-lifecycle` to the resume command. That one-time authorization records a legacy
bootstrap fact; it is never inferred and is not needed for new runs.

Read status for per-node attempts, errors, receipts, accepted artifacts, and worktree paths. The
bounded `trace` output verifies and renders immutable run context plus versioned lifecycle facts;
ledger corruption or deletion fails closed. The accepted result remains in the integration
worktree; the runtime does not merge or deploy it.
Review the combined diff and evidence, then hand that exact accepted change to the project's normal
branch/PR process or `$universal-deploy`.

To move orchestration from Claude to Codex, Grok, or another configured host without replacing the
run with a prose summary, export and consume an exact handoff:

```bash
graph-engineer handoff --state "$state" --run-id auth-audit-001 \
  --output auth-audit-001.handoff.json --json
graph-engineer run "$workflow" --repo "$PWD" --run-id auth-audit-001 \
  --state "$state" --resume --handoff auth-audit-001.handoff.json --json
```

The receiver must have the same workflow, repository base, private resolved profiles, durable
state/artifacts, and authorization. Handoff verifies those facts; it does not copy secrets or grant
authority. Use `status --projection` for bounded JSON that a Herdr adapter or terminal can render;
the runtime does not mutate Herdr or require a visual builder.

`graph-engineer assess --repo "$PWD" --json` is advisory repository evidence, not task selection
and not a readiness score. Add `--output assessment.json` only when a reviewed durable init flow
will consume the stable artifact. The consumer must reject it after its bound HEAD or source digest
changes.

Terse host requests all mean the same enforced path:

- Claude: `Use $graph-engineering and the portable runtime for this change.`
- Codex: `Use $graph-engineering; validate, plan, run, and report exact evidence.`
- Grok: `Use $graph-engineering with isolated lanes and the portable runtime.`

If the portable CLI is unavailable, the host may use native subagents only after disclosing that
durable leases, fencing, schema-bound handoff, localized repair, and verified resume are procedural
rather than mechanically enforced. Native fallback is not silently equivalent.

## 7. Record economics and promote only proven templates

Use `graph-engineer outcome` to record measured tokens, cost, model, verifier overturns, cold
adoption time, integration failures, escaped defects, failure class, and live-proof time. Use
`graph-engineer promote` only on matched LINEAR/TRANSIENT_GRAPH runs with the same acceptance
suite. Promotion needs at least three accepted wins on one declared objective, no escaped-defect
regression, reported cost, and a named review bound to the exact evidence digest.

## 8. Register the graph task MCP service

The CLI runtime does not require MCP. Register the MCP service when interactive agents need durable
cross-client task claiming or polling:

```bash
install -d -m 0700 ~/.local/state/graph-engineering

claude mcp add -s user graph-task -- \
  graph-engineering-mcp --database ~/.local/state/graph-engineering/tasks.db
codex mcp add graph-task -- \
  graph-engineering-mcp --database ~/.local/state/graph-engineering/tasks.db
grok mcp add --scope user graph-task -- \
  graph-engineering-mcp --database ~/.local/state/graph-engineering/tasks.db
```

Verify with `claude mcp get graph-task`, `codex mcp get graph-task`, and
`grok mcp doctor graph-task`. Keep exactly one graph-task state owner per project or shared task
domain. Legacy clients negotiate their supported handshake version; capable clients can use the
stateless `2026-07-28` lifecycle and optional Tasks extension.

## 9. Add an A2A remote worker only when needed

Use `adapter = "a2a"` in the private user configuration when a node must be delegated to an
independently operated agent. Configure its private Agent Card URL, Bearer token environment
reference, expected identity, and exact allowed skills. Set `mcp = false`; the remote agent may use
its own tools but cannot inherit local MCP authority. Run `doctor` before dispatch.

The current adapter supports the A2A 1.x HTTP+JSON polling path. It pins the card and task identity,
and remote code changes still pass through a local worktree and deterministic checks. Read the
repository's `docs/A2A.md` for the exact subset and residual idempotency risk.

## 10. Troubleshoot without bypassing gates

- `doctor` fails: fix config mode, missing executable/environment reference, or disk-backed
  `TMPDIR`; do not weaken the probe.
- Validation fails: fix the reported JSON path, missing artifact edge, effect, scope, or budget.
- `SCHEMA_MISMATCH`: fix the worker's authoritative final structured result or adapter; do not parse
  progress/tool chatter as the result.
- `RESUME_MISMATCH`: use the original workflow/profile/base or start an explicitly new run.
- `needs_reconciliation` or `uncertain`: determine whether the external effect occurred before
  deciding any next action.
- `NO_PROGRESS`: repair the task, prerequisite, route, or check; do not increase attempts blindly.
- Integration check fails without a route: preserve evidence and add a reviewed explicit route only
  when one producer is genuinely responsible.
- MCP works on one host but not another: inspect the client's negotiated version and capabilities;
  registration does not imply Tasks or other extensions are supported.
- A2A preflight fails: repair the private URL/identity/skill/auth profile or the remote Agent Card;
  never weaken identity, origin, version, or capability pinning to make a card pass.
