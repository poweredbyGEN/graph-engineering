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

## Compile and review a model proposal

Keep the assessment, raw candidate, and unaccepted proposal outside the repository:

```bash
graph-engineer compile --repo "$PWD" --assessment /safe/assessment.json \
  --candidate /safe/candidate.workflow.json --proposed-by planning-model \
  --output /safe/candidate.proposal.json --json
graph-engineer accept --repo "$PWD" --proposal /safe/candidate.proposal.json \
  --proposal-digest '<digest from compile>' --reviewed-by '<frozen named approver>' \
  --workflow-output .graph-engineering/workflows/candidate.workflow.json \
  --acceptance-output .graph-engineering/reviews/candidate.acceptance.json --json
```

`compile` validates the candidate against the current workflow schema, project policy, frozen
product contract, repository assessment source, effects, and budgets. It emits an inert proposal
and explicitly reports `dispatch_authorized: false`. `accept` requires the exact digest, a reviewer
distinct from the proposer, and an exact match to the product contract's named approver. It
revalidates source/contract/repository bindings, writes the new acceptance receipt first, and only
then creates the new workflow; neither command overwrites an existing artifact. A raw candidate is
never a reviewed graph.

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

### Typed profile fallback and evaluator-repair

A read-only replay-safe agent may declare up to four ordered `fallback.routes`, each with one
private profile, an exact `on_codes` set, and `max_uses: 1`. Every route uses a fresh isolated
worktree. Unknown failures do not fall through, and successful fallback output still crosses the
same deterministic node checks. Writing, external, destructive, and non-idempotent fallback is
rejected. See [`profile-fallback.workflow.json`](../examples/profile-fallback.workflow.json).

[`evaluator-repair.workflow.json`](../examples/evaluator-repair.workflow.json) demonstrates a
bounded `produce -> evaluate -> repair` graph. The critique is typed advice; the repair's
deterministic project tests—not the evaluator's verdict—remain acceptance authority.

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

### Reconnectable event consumption

Prime, Herdr, CI, and other observers should consume the generic lifecycle stream rather than own
graph state or scheduling:

```bash
graph-engineer events --state /path/to/state.db --run-id auth-audit-001 \
  --limit 100 --wait 20 --json

graph-engineer events --state /path/to/state.db --run-id auth-audit-001 \
  --cursor '<next_cursor>' --limit 100 --wait 20 --json
```

Each call returns at most 256 already-redacted typed lifecycle events, an opaque cursor, `has_more`,
`timed_out`, and `terminal`. A consumer acknowledges a batch by using its `next_cursor`; reconnects
therefore neither skip nor duplicate accepted events. The cursor binds the run, sequence, and event
digest and fails closed across runs or after history corruption. Long polling is capped at 30
seconds. Consumers should drain while `has_more` is true, back off after `timed_out`, and stop after
`terminal`; this pull contract provides backpressure without giving Prime or Herdr write authority.

### Immutable run forks

Create a fresh run from a prior quiescent checkpoint without editing or cloning mutable execution
state:

```bash
graph-engineer fork --state /path/to/state.db \
  --run-id auth-audit-001 --at-sequence 42 --new-run-id auth-audit-experiment-002 --json

graph-engineer run workflow.json --repo "$PWD" --state /path/to/state.db \
  --run-id auth-audit-experiment-002 --resume --json
```

The fork binds the exact parent event and context plus base, workflow, profile, artifact-snapshot,
and receipt-snapshot digests. The child starts with pending nodes and its own budgets, lease,
attempts, artifacts, receipts, and hash chain; its first event is `run.forked`. Fork creation rejects
non-settlement events, in-flight attempts, prior non-replay-safe effects, missing or corrupt
artifacts/receipts, and malformed execution identity. Resume rebuilds the lineage from the parent
checkpoint and rejects drift before dispatch. A fork grants no new approval, merge, deployment, or
external-effect authority.

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

## Measure outcomes and turn feedback into reviewed improvements

Derive graph metrics from the durable state and lifecycle rather than asking a model to estimate
them:

```bash
graph-engineer benchmark --state /path/to/state.db --run-id auth-audit-001 --json
graph-engineer benchmark --state /path/to/state.db --run-id auth-audit-001 \
  --baseline /secure/path/ordinary-session.json --output /secure/path/comparison.json --json
```

The report includes wall time, time to first accepted artifact, retries, rejected attempts,
repeated failures, deterministic-gate rejections, useful overlap, and critical-path utilization.
Metrics that the runtime cannot prove—human corrections, independent-verifier overturns, and time
to merged/deployed/live proof—remain `null` until supplied by separately reviewed evidence. A
baseline is a strict `graph-engineering/baseline/v1` JSON record; comparison never fabricates
missing measurements.

Human, test, verifier, or runtime feedback can be compiled into a non-applying proposal:

```bash
graph-engineer feedback --input /secure/path/feedback.json \
  --output /secure/path/learning-proposal.json --json
```

`graph-engineering/feedback/v1` items target a regression test, project decision, workflow, or the
local graph-engineering skill. Regression targets require a shell-free verification argv and a
sabotage check. Product decisions invalidate the frozen generation; workflows require validation;
skill targets remain local reviewed proposals. The compiler rejects credential-shaped content and
always emits `auto_apply=false`, `auto_share_skills=false`, and named-human review. It never edits a
test, decision, workflow, or skill by itself.

## Usage telemetry: `stats`

Every CLI invocation appends one JSON line to a machine-local log
(`~/.local/share/graph-engineering/usage.jsonl`, override with
`GRAPH_ENGINEERING_USAGE_LOG`, opt out with `GRAPH_ENGINEERING_NO_USAGE_LOG=1`). Because every
agent host — Claude, Codex, Grok, Gemini — shells out to the same binary, this log is the one
place adoption can be counted across all of them. Nothing is uploaded; recording is fail-silent
so telemetry can never break a command. Set `GRAPH_ENGINEERING_CALLER` to attribute invocations
to a host tool.

```bash
graph-engineer stats            # totals by command, repository, day, caller
graph-engineer stats --days 30  # recent window only
graph-engineer stats --json
```

## Deliberate limits

- The CLI does not draw or edit graphs.
- It does not infer a workflow from prose or call a model while planning.
- It does not merge, deploy, message, or grant approval authority.
- Worker profiles remain private; checked-in project configuration can select routing only.
- Workflow checks are argv arrays validated by the runtime. Shells, indirect launchers, and
  direct side-effect tools rejected by runtime preflight remain rejected here.
- Agent nodes use isolated nested worktrees. Exactly one integration node owns accepted change
  transfer into its own integration worktree; the main checkout is not mutated.
