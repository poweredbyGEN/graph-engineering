# `graph-engineer` CLI

`graph-engineer` is a thin operator surface over the portable runtime. It validates and plans
without invoking a model, and `run` delegates readiness, persistence, isolation, checks, artifact
acceptance, and resume to the same `PortableRuntime` used by library callers.

The first workflow format is JSON only. YAML is intentionally not accepted by this command even
though the Python library can parse it. A single input format keeps parse failures and persisted
contracts unambiguous while the runtime is alpha.

## Private worker setup

Copy `subagents.example.toml` to the private user location and keep it private:

```bash
install -d -m 0700 ~/.config/graph-engineering
install -m 0600 subagents.example.toml ~/.config/graph-engineering/config.toml
export TMPDIR=/path/to/disk-backed/scratch
graph-engineer doctor --repo "$PWD"
```

`doctor` checks file permissions, configuration shape, disk-backed scratch readiness, worker
executables on the reduced `PATH`, and required environment-variable presence. It never launches a
worker and never prints environment values, prompts, adapter argv, endpoints, or credentials. Use
`--profile NAME` to inspect one configured profile and `--json` for structured output.

## Validate and inspect topology

```bash
graph-engineer validate workflow.json
graph-engineer plan workflow.json
graph-engineer plan workflow.json --json
```

`validate` reports every schema/graph issue with a JSON path. `plan` reports ready layers, exact
dependencies and unlocks, and a stable unweighted critical path. Both are pure local operations:
they do not load private worker configuration, invoke a model, create a worktree, or execute a
workflow check.

### Typed integration repair routes

An integration node may declare `repair.routes`. Each route binds one or more exact integration
`check_ids` to explicit `{node, input}` producer targets and sets finite `max_rounds` and
`no_progress_limit` values. `validate` rejects unknown or multiply routed checks, targets that do
not directly provide an integration changeset, unsafe target fan-out, input-name collisions,
targets without an explicit replay-safe `effect`, and attempt budgets too small to finish the
declared cycle.

When a routed combined check fails, the runtime persists a schema-validated evidence artifact
containing the check ID, bounded check output, integration attempt, and failure digest. It deletes
only the named producers' accepted artifacts, passes that artifact under each named repair input,
reruns those producers in fresh base-pinned worktrees, and reconstructs integration from repaired
plus still-valid changesets. An unmapped check or non-check failure never guesses a target. An
identical repeated failure stops at the route's no-progress limit; non-idempotent failed effects
still require reconciliation, and a successful non-idempotent or destructive node is ineligible as
an automatic repair target.

[`examples/repair-route.workflow.json`](../examples/repair-route.workflow.json) is validated in the
test suite and uses the runtime's exact canonical changeset contract.

## Run and resume

```bash
graph-engineer run workflow.json \
  --repo "$PWD" \
  --run-id auth-audit-001 \
  --json
```

The command prints the durable state path. By default it is
`$XDG_STATE_HOME/graph-engineering/runs/<run-id>/state.db`, or
`~/.local/state/graph-engineering/runs/<run-id>/state.db` when `XDG_STATE_HOME` is unset. Use
`--state PATH` when an external runner owns placement. Artifacts and cryptographically bound
worker/check receipts are stored in an `artifacts/` sibling directory.

Resume is a flag over the same execution path, so it cannot silently select a different workflow
or profile:

```bash
graph-engineer run workflow.json \
  --repo "$PWD" \
  --run-id auth-audit-001 \
  --state /path/printed/by/the/first/run/state.db \
  --resume
```

Persisted workflow equality, accepted artifact schemas/digests, run ownership, attempt fencing,
receipt bindings, and retained worktrees are revalidated by the runtime. A non-replay-safe
interrupted effect stops in `needs_reconciliation`; the CLI does not guess an operator decision.

## Inspect durable evidence

```bash
graph-engineer status \
  --state /path/to/state.db \
  --run-id auth-audit-001
```

Status includes node state and attempt counts. `--json` additionally makes the full attempt
history, accepted artifact receipts, redacted agent receipts, deterministic check receipts, and
worktree paths easy to consume in CI or Woodpecker. Receipt corruption or deletion fails closed.

## Deliberate limits

- The CLI does not draw or edit graphs.
- It does not infer a workflow from prose or call a model while planning.
- It does not merge, deploy, message, or grant approval authority.
- Worker profiles remain private; checked-in project configuration can select routing only.
- Workflow checks are argv arrays validated by the runtime. Shells, indirect launchers, and
  direct side-effect tools rejected by runtime preflight remain rejected here.
- Agent nodes use isolated nested worktrees. Exactly one integration node owns accepted change
  transfer into its own integration worktree; the main checkout is not mutated.
