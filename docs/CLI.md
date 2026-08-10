# `graph-engineer` CLI

`graph-engineer` is a thin operator surface over the portable runtime. It validates and plans
without invoking a model, and `run` delegates readiness, persistence, isolation, checks, artifact
acceptance, and resume to the same `PortableRuntime` used by library callers.

The first workflow format is JSON only. YAML is intentionally not accepted by this command even
though the Python library can parse it. A single input format keeps parse failures and persisted
contracts unambiguous while the runtime is alpha.

## Discover implemented capabilities

```bash
graph-engineer capabilities --json
```

This manifest is generated from packaged schemas, runtime constants, and the actual CLI parser. It
reports the package and contract versions, commands, join/retry limits, fencing/resume/repair,
artifact and receipt guarantees, worktree/integration behavior, adapter and MCP/A2A availability,
and the frontend evidence pattern. `false` and `not_implemented` are authoritative limitations, not
documentation omissions. The clean-wheel release gate consumes this same command.

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
worker and never prints environment identifiers or values, prompts, adapter argv, filesystem
paths, endpoints, hosts, or credentials. Use
`--profile NAME` to inspect one configured profile and `--json` for structured output.

Launching a worker is separately opt-in:

```bash
graph-engineer doctor --repo "$PWD" --profile codex --smoke --timeout 30 --json
```

Repeat `--profile` to select up to 16 profiles. Smoke runs each statically ready profile in an
empty isolated repository with a strict `{ "ok": true }` result schema, reduced environment,
bounded output and wall time, and MCP disabled. On Linux, smoke requires `bwrap`, `strace`, and
`prlimit`; it mounts the host read-only and audits attempted mutation syscalls, so parent-path,
absolute-path, caught, and successful writes all fail as `WRITE_DETECTED` without modifying the
host. There is no unconfined fallback. A malformed result, timeout, unavailable confinement, or
MCP-capable profile also fails closed. Output contains only stable status/error codes, receipt
digests, byte counts, transport, and timing; it never contains the prompt, argv, stdout/stderr,
endpoint, model, host, path, environment identifier, or secret value. CI uses stub workers only;
choosing `--smoke` is the operator's explicit authorization to invoke the selected real private
profiles.

Codex is the one ptrace-incompatible exception: its smoke still runs inside the same read-only
`bwrap` namespace, disposable HOME/XDG state, timeout/output limits, and post-run empty-repository
check, but omits `strace` and uses a 16 MiB disposable file cap so Codex can emit its authoritative
final JSONL event. The mount namespace prevents host/repository writes; only classification of a
denied-and-caught write attempt as `WRITE_DETECTED` is unavailable for that profile.

Every reviewed project also needs a private, ignored host/checkout binding. Add this section to the
project root's mode-0600 `.graph-engineering.local.toml` using the actual local values; never commit
the file:

```toml
[execution]
allowed_hosts = ["your-hostname"]
allowed_checkout_roots = ["/absolute/private/projects-root"]
```

The live hostname and canonical repository root must match immediately before dispatch. Durable
state, lifecycle context, status, and handoff store only the canonical binding digest, never the
configured hostname or paths.

## Adopt an existing or new project

Start read-only, then initialize only when the task earns graph overhead:

```bash
graph-engineer assess --repo "$PWD" --json
graph-engineer init --repo "$PWD" --json
```

`init` discovers the repository root, its checked-in graph project boundary, reviewed JSON
workflows, private-profile readiness, and matching durable runs. If the boundary is absent, it
scaffolds the minimal public files and returns them for review; complete the frozen product
contract, allowed write roots, required/live deterministic checks, and any sanctioned deployment
metadata before rerunning it. A matching active run blocks a duplicate launch—inspect and resume
that exact run instead. The atomic scope key includes repository identity, base SHA,
product-contract generation/digest, and workflow digest, so the same template in another
repository does not collide.

A saved assessment may seed only its reviewed workflow-template recommendation and private-config
requirement:

```bash
graph-engineer init --repo "$PWD" \
  --from-assessment /secure/path/assessment.json --json
```

The assessment must still match the exact repository identity, HEAD, and bounded tracked/untracked
source digest. Initialization never infers secrets, approvals, transport, deployment targets, or
permission to perform an external effect.

Repository identity is portable and credential-free: it hashes the sanitized public host and
repository path, not remote userinfo, transport, or the local checkout directory. An origin-less
repository uses a bounded git-root fallback for read-only assessment, but remains explicitly
unresolved and cannot pass execution preflight. A malformed, local-path, or ambiguous origin fails
closed with `REMOTE_REVIEW_REQUIRED` rather than being serialized or silently reidentified.

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

Persisted workflow equality, checked-in project-policy digest, private execution-binding digest,
repository/base identity, accepted artifact schemas/digests, run ownership, attempt fencing,
receipt bindings, and retained worktrees are revalidated by the runtime. A non-replay-safe
interrupted effect stops in `needs_reconciliation`; the CLI does not guess an operator decision.

## Inspect durable evidence

```bash
graph-engineer status \
  --state /path/to/state.db \
  --run-id auth-audit-001 \
  --projection

graph-engineer trace \
  --state /path/to/state.db \
  --run-id auth-audit-001 \
  --json
```

Status includes node state and attempt counts. `--json` additionally makes the full attempt
history, accepted artifact receipts, redacted agent receipts, deterministic check receipts, and
worktree paths easy to consume in CI or Woodpecker. `trace` verifies and renders the immutable run
context plus versioned scheduler facts, including attempts, checks, retries, repair, join decisions,
reconciliation, cancellation, and terminal state. Receipt or lifecycle corruption/deletion fails
closed.

`--projection` adds a bounded, redacted `graph-engineering/status-projection/v1` object for a
terminal, CI, or Herdr ingestion adapter. It contains the critical path, each lane's start and
deadline, artifact-count/digest delta, blocker, next deterministic route, and useful overlap. It
does not inspect process command lines, print private profile values, mutate Herdr, or provide a
visual workflow editor. At most 100 lanes and 16 artifact digests per lane are emitted; omissions
are counted.

## Cross-engine handoff

Export a credential-free handoff from trusted durable state, then give the file plus the same
state/artifact directory to another locally authorized engine:

```bash
graph-engineer handoff --state /path/to/state.db --run-id auth-audit-001 \
  --output /secure/path/auth-audit-001.handoff.json --json

graph-engineer run workflow.json --repo "$PWD" --run-id auth-audit-001 \
  --state /path/to/state.db --config /private/config.toml --resume \
  --handoff /secure/path/auth-audit-001.handoff.json --json
```

The strict envelope binds the workflow and project contract, resolved profile manifest, base SHA,
run/node/attempt state, lifecycle head/context, accepted artifact receipts, and worker/check receipt
ledger. It also names completed and remaining nodes, bounded failures, and effects that require
reconciliation rather than retry. Tamper, deletion, stale state, another run, contract drift,
profile drift, or base drift fails closed. A successful import advances the lifecycle, so the
consumed snapshot cannot be replayed as if it were current. Handoff transfers authority to resume
nothing: the receiving engine still needs the original private profile, repository, state,
artifacts, and project authorization.

## Assess adoption without changing the repository

```bash
graph-engineer assess --repo "$PWD" --json
graph-engineer assess --repo "$PWD" --config /private/config.toml \
  --output /secure/path/assessment.json --json
```

`assess` is a read-only `graph-engineering/assessment/v1` audit. It checks for validated workflows,
real test/lint/type/build declarations, private-profile readiness without reading values into the
report, writer isolation plus integration, classified effects and bounded retries, lifecycle and
handoff adoption, evidence runners, and whether repository evidence actually indicates MCP or A2A.
It emits prioritized gaps with exact remediation, never a decorative numeric maturity score. By
default it writes nothing; `--output` creates a new mode-0600 artifact consumable by a compatible
`init --from-assessment` flow. The artifact binds repository identity plus the exact HEAD and
tracked/untracked source digest; init must report it stale and refuse to consume it after
source drift. Every gap includes bounded fix sites, a falsifiable acceptance statement, and a
deterministic verification argv when one can be inferred. Assessment remains advisory and is never
a Stop hook or deployment gate.

Runs created before lifecycle journaling are not silently upgraded during resume. After reviewing
that legacy state, authorize the one-time marker explicitly with
`run ... --resume --bootstrap-legacy-lifecycle`; new runs never need this flag.

## Deliberate limits

- The CLI does not draw or edit graphs.
- It does not infer a workflow from prose or call a model while planning.
- It does not merge, deploy, message, or grant approval authority.
- Worker profiles remain private; checked-in project configuration can select routing only.
- Workflow checks are argv arrays validated by the runtime. Shells, indirect launchers, and
  direct side-effect tools rejected by runtime preflight remain rejected here.
- Agent nodes use isolated nested worktrees. Exactly one integration node owns accepted change
  transfer into its own integration worktree; the main checkout is not mutated.
