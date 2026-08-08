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

## 3. Create a workflow

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

Use an integration node for changesets. It alone combines accepted writer artifacts and runs the
complete project gate. An integration repair route maps exact failing check IDs to exact producer
and input names; it is not permission to retry the graph.

## 4. Start the workflow

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

## 5. Inspect, resume, and integrate

The run output prints its state database. The default is
`~/.local/state/graph-engineering/runs/<run-id>/state.db`; artifacts and receipts live beside it.

```bash
state=~/.local/state/graph-engineering/runs/auth-audit-001/state.db

graph-engineer status --state "$state" --run-id auth-audit-001 --json
graph-engineer run "$workflow" --repo "$PWD" --run-id auth-audit-001 \
  --state "$state" --resume --json
```

Resume with the exact workflow, run ID, repository, base SHA, profile identity, and state path.
Workflow/profile/base drift, missing or corrupt receipts, and invalid artifacts fail closed. A
non-replay-safe interrupted effect enters reconciliation; never edit state to force it forward.

Read status for per-node attempts, errors, receipts, accepted artifacts, and worktree paths. The
accepted result remains in the integration worktree; the runtime does not merge or deploy it.
Review the combined diff and evidence, then hand that exact accepted change to the project's normal
branch/PR process or `$universal-deploy`.

## 6. Register the graph task MCP service

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

## 7. Troubleshoot without bypassing gates

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
