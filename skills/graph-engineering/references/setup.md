# Agent setup

Executable from-zero setup for an AI agent installing graph engineering on a machine or repository.
Run every step's verify command before proceeding; fail closed on any mismatch. Do not weaken a
probe, permission mode, or pin to make a step pass.

## 0. Decide whether setup is needed at all

LINEAR and TRANSIENT_GRAPH work needs **no installation** — the host agent and its native subagents
are the scheduler. Install only when the task needs the durable runtime (`DURABLE_GRAPH`), the
`choose`/`execute`/`outcome` CLI evidence path, or cross-client task claiming via MCP. Do not make
installation a prerequisite for ordinary work.

## 1. Install the portable runtime

Install the current reviewed tag (check the repository releases page for a newer reviewed tag and
substitute it):

```bash
uv tool install \
  https://github.com/poweredbyGEN/graph-engineering/releases/download/v0.1.0a1/graph_engineering-0.1.0a1-py3-none-any.whl
```

Verify — both must succeed and the version must match the installed tag:

```bash
graph-engineer --version
graph-engineering-mcp --help >/dev/null && echo mcp-entrypoint-ok
```

For development instead: clone `https://github.com/poweredbyGEN/graph-engineering`, run
`uv sync --frozen --extra dev`, and invoke commands with `uv run`. After any upgrade, re-run
`graph-engineer --version` and confirm the local install actually changed — a release that does not
update the local install is not deployed.

## 2. Configure private workers

Worker commands, models, endpoints, and secret references live in user config, never in project
source:

```bash
install -d -m 0700 ~/.config/graph-engineering
install -m 0600 subagents.example.toml ~/.config/graph-engineering/config.toml
```

Obtain `subagents.example.toml` from the tagged source checkout. Edit **model aliases only**; do not
loosen the least-authority command boundaries. Keep secrets in a secret manager and reference only
environment-variable names. Default writing workers keep `mcp = false` so they cannot inherit the
interactive agent's ambient MCP authority.

Verify modes — expect `0700` and `0600`:

```bash
stat -c '%a %n' ~/.config/graph-engineering ~/.config/graph-engineering/config.toml
```

## 3. Point scratch at real disk

The runtime requires a disk-backed `TMPDIR`. On hosts where `/tmp` is tmpfs (RAM), export a
path on real disk before running workers:

```bash
df -h "$TMPDIR" && echo "TMPDIR=$TMPDIR"
```

If `TMPDIR` is unset or resolves to a tmpfs, fix the environment; do not hardcode `/tmp`.

## 4. Run doctor

```bash
graph-engineer doctor --repo "$PWD" --json
```

Fix every reported item at its cause: config file mode, missing executable, unresolved
environment-variable reference, tmpfs `TMPDIR`. Doctor passing is the gate for dispatching any
worker; never edit the probe to pass.

## 5. Register the MCP task service (optional)

Only when interactive agents need durable cross-client task claiming or polling:

```bash
install -d -m 0700 ~/.local/state/graph-engineering

claude mcp add -s user graph-task -- \
  graph-engineering-mcp --database ~/.local/state/graph-engineering/tasks.db
codex mcp add graph-task -- \
  graph-engineering-mcp --database ~/.local/state/graph-engineering/tasks.db
grok mcp add --scope user graph-task -- \
  graph-engineering-mcp --database ~/.local/state/graph-engineering/tasks.db
```

Verify per client: `claude mcp get graph-task`, `codex mcp get graph-task`,
`grok mcp doctor graph-task`. Keep exactly one graph-task state owner per project or shared task
domain — check for an existing registration before adding a second.

## 6. Smoke-check without spending model tokens

Save the read-only diamond from [workflow-contract.md](workflow-contract.md) as
`/tmp/smoke.workflow.json` (any scratch path), then:

```bash
graph-engineer validate /tmp/smoke.workflow.json
graph-engineer plan /tmp/smoke.workflow.json --json
```

Both must pass; `plan` must report the two audit nodes as the ready layer and `synthesize`
dependent on both. This proves the contract, schema, and planner without launching agents. Delete
the scratch file afterwards.

## 7. Per-repository setup (durable work only)

Only for a repository that will run durable workflows:

- store reviewed workflows under `.graph-engineering/workflows/<name>.json`;
- add `.graph-engineering/role-policy.json` only for policy-bound workflows, reviewed
  independently of any WorkGraph that references it;
- create `.graph-engineering/PROJECT.md` only when [planning.md](planning.md) says a capsule is
  warranted (recurring, durable, product-ambiguous, or multi-operator);
- a checked-in `.graph-engineering.toml` may select routing only — never commands, models,
  endpoints, or environment variables.

`graph-engineer init` scaffolds this boundary; review its output before committing anything.

## 8. Report the result

State exactly: installed version, doctor result, config path and verified modes, `TMPDIR` target,
MCP registration status per client (or "not registered — not needed"), smoke-check result, and
which steps were skipped because the tier does not need them. "Installed" without a passing doctor
and smoke check is not set up.
