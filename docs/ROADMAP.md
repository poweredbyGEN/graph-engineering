# graph-engineering roadmap

This roadmap turns `graph-engineering` from a collection of proven harness, loop, swarm, and
trace components into a public, installable, cross-CLI workflow system that teams can use
across real software projects.

The target is not another coding agent. Claude Code, Codex, Grok, and Gemini remain the
agents. `graph-engineering` supplies the portable control plane around them: typed work contracts,
dependency-aware scheduling, isolated execution, deterministic evidence, adversarial review,
durable artifacts, and comparable traces.

## Product outcome

A new user should be able to start from a clean machine and run:

```bash
pipx install graph-engineering
graph-engineer doctor
graph-engineer init
graph-engineer run dev-change --goal "Add CSV export to this project"
```

The same checked-in workflow must be able to use Claude, Codex, Grok, Gemini, or a mix of
them without changing its dependency graph or evidence contract.

The end-to-end result must include:

- a validated graph and execution plan;
- one isolated workspace for every writing node;
- schema-valid artifacts passed across real producer-to-consumer edges;
- deterministic checks that decide whether a node passed;
- independent verification where the workflow requires it;
- resumable state and a complete trace;
- an explicit integration result rather than a pile of unmerged worktrees.

## Primary success metric: faster development with fewer escaped defects

The first product is not a general graph editor. It is a development loop that makes a
multi-file change faster without weakening its evidence:

```text
scope and contract
       |
       v
ready queue -> isolated implementation workers -> deterministic per-lane gates
       |                                               |
       |<--------- retry only the failed lane ----------|
       v
independent review/refutation -> integrate -> deterministic combined gate -> result
```

Measure the system against a single-agent baseline on the same synthetic and real changes:

- wall-clock time to a gate-passing integrated change;
- percentage of useful work that actually overlaps in time;
- first-pass and final deterministic-check pass rates;
- defects found by independent verification before integration;
- rerun scope after failure (one node and descendants versus the whole job);
- integration-conflict rate and human intervention time;
- token and dollar cost per accepted change.

UI, arbitrary graph editing, distributed execution, and self-modifying workflows are later work.
They do not block the first useful release.

## Non-negotiable boundaries

### Public repository

The public repository owns reusable behavior:

- workflow schemas and validators;
- scheduler, state machine, artifact store, and engine adapters;
- MCP servers and client-registration helpers;
- generic workflow templates and synthetic examples;
- tests, documentation, packaging, CI, release automation, and security policy.

### Local or private configuration

The public repository must never contain:

- private repository names or inventories;
- internal hosts, service topology, ticket-project IDs, or deployment commands;
- credentials, secret names that reveal private systems, or customer data;
- organization-specific approval policy or private workflow prompts.

Those values belong in ignored `config.toml`, per-project `.graph-engineering.local.toml`, environment
variables, or a separately controlled private configuration repository. Examples use fictional
projects and localhost-only endpoints. A history scan is required before every public release,
not only a working-tree scan.

### Portable profiles and private registrations

Workflow files target capabilities, not a vendor account, machine, endpoint, or secret. The public
repository defines a vendor-neutral `AgentProfileCapabilityManifest` contract containing only
portable fields such as adapter kind, supported protocol versions, client capabilities and
extensions, required tool classes, workspace/network policy, output modes, and model/resource
limits. Public fixtures use fictional profile IDs and local fake servers.

Actual profile selection belongs to ignored user or project configuration. Our private rollout
intends to register Claude, Codex, Grok, Kimi K3, and GLM 5.2 profiles there; those names are local
choices, not hard-coded workflow node types or public compatibility promises. The same private
registry owns server IDs, transports, scoped authorization, trust tier, and references to locally
managed credentials. It must never serialize an endpoint, credential reference, private server
inventory, or organization policy into a workflow, trace, artifact, generated skill, or public
example.

Before dispatch, the planner computes an eligibility intersection:

```text
node requirements
  ∩ adapter/client support
  ∩ profile authorization
  ∩ live server-negotiated capabilities
  ∩ request- and tool-level support
```

It selects only an eligible profile, applies a declared deterministic fallback, or fails preflight.
Every node lease and evidence receipt pins the chosen profile ID, adapter version, negotiated MCP
protocol, server IDs, capability-manifest hash, and skill digests. A retry on another profile must
negotiate and validate again; subagents never inherit the orchestrator's entire ambient MCP or
private-server configuration.

## Current baseline

### Foundation status (2026-08-08)

The public foundation is landed on `main`:

- the GitHub repository is now `poweredbyGEN/graph-engineering`, with the former URL redirecting;
- the portable `graph-engineering` skill is validated and shared through the canonical skill tree;
- the superseded `graph-dev` skill is retired after its useful scheduling and evidence rules were
  incorporated;
- `graph-task` and `graph-verify` are registered for Claude Code, Codex, and Grok, with graph state
  stored outside project source trees;
- Woodpecker runs contracts, core-component tests, and `verify-mcp` tests as three independent
  workflows, and the launch merge passed all three on its exact `main` SHA;
- the `universal-deploy` handoff is merged after terminal-green push and PR pipelines on its exact
  SHA.

The portable runtime now includes validated contracts, a concurrent ready queue, durable fenced
state, immutable schema-checked artifacts, attempt-specific worktrees, explicit integration nodes,
provider adapters, tamper-evident receipts, profile/base identity pinning, and a current-protocol MCP
task surface. Integration checks can now use explicit typed repair routes to invalidate only named
producers, pass durable failure evidence back as an input, and reconstruct integration under
round, attempt, no-progress, and total-workflow budgets. Packaging, public tree/history scanning,
and clean-wheel installation are CI gates.

The repository already has tested implementations for:

- a read-only MCP 2.0 verification server;
- deterministic evidence loops with timeouts and no-progress detection;
- parallel agent lanes isolated in git worktrees;
- adversarial refutation of passing lanes;
- Claude, Codex, Grok, and Gemini subprocess invocation guidance;
- trace analysis for check quality, convergence, keep rate, and declared dependencies;
- public/private site configuration separation.

The first MCP control-plane decision is evidence-driven:

- use `verify-mcp` for deterministic project checks;
- adopt the task-claiming, dependency, lock, cost, workflow, and skill-resource contracts proven
  by `Oortonaut/task-graph-mcp` (580 clean tests in the audited revision), initially as a local
  compatibility service and then behind our MCP 2.0 adapter;
- use MCP Tasks for durable long-running handles when the host advertises the extension;
- serve `graph-engineering` through filesystem skills today and a `skill://` resource index when
  Skills-over-MCP stabilizes; capability-detect it because SEP-2640 and the working-group repository
  remain experimental and do not establish a portable host discovery guarantee;
- keep exactly one task-graph owner per project. Multiple graph servers writing independent state
  would create split-brain claims and contradictory readiness.

“MCP 2.0” is not a dependency version or a server label. Support is established per connection by
protocol-version agreement and capability negotiation. Tasks are an extension: task augmentation
is permitted only when the client profile and server negotiate the extension for the exact request
category and the selected tool permits or requires it. Otherwise the portable scheduler uses a
normal tool call and its own durable run state. A Skills-over-MCP resource can describe how to use
already-authorized tools, but it cannot grant authority, expand a profile's server visibility, or
override workflow policy.

Rejected as default dependencies after clean-clone checks: `P0u4a/mcp-workflow` failed its own
pause/resume test and its production dependency audit reported two high and three moderate findings;
`TeamSparkAI/mcpGraph` failed one of 126 tests and its production dependency audit reported seven
high, two moderate, and one low finding; the audited `agent-graph-mcp` revision referenced missing
local path dependencies; and `agentralabs/agentic-workflow` returns prepared placeholders for core
MCP, shell, HTTP, subworkflow, and fan-out execution despite its broad tool surface. Their useful
schema, test, and routing ideas remain design inputs, not installed control planes.

The portable graph runtime is executable through the deliberately thin `graph-engineer` CLI.
`produces` and `consumes` drive readiness and schema-checked artifact transfer; durable SQLite
state, fenced attempts, isolated worktrees, integration gates, receipts, resume, and mixed-engine
subprocess adapters are covered by package and clean-install tests. The first two measured pilots
are recorded in [PILOTS.md](PILOTS.md): one successful graph was 13.13% faster and added a missing
test, while another was 26.3% slower but correctly blocked defects that escaped the nominally green
single-agent baseline. Those results justify the CLI and routed repair work, not an always-on hook.

The public/private split is enforced rather than advisory. Public configuration contains only
routing examples and environment-variable references. User profiles, provider endpoints, secret
references, and private MCP registrations live in ignored user or local configuration. Literal
secret rejection, reduced worker environments, restricted worker templates, and complete public
tree plus reachable-history scans fail closed in CI.

## External prior art and selective reuse

All reviewed repositories are public and permissively licensed (MIT, with the curated list
under CC0), but they are not interchangeable. Do not concatenate their skills or merge their
git histories. Reuse small, attributable components and conformance ideas behind our own stable
workflow contract.

| Source | What is proven/useful | Decision |
|---|---|---|
| `orperelman123/autonomous-graph-engineering` | Concurrent DAG execution, strict graph budgets, output contracts, resume, MCP, installer/doctor, permission gates, interrupted-write reconciliation, extensive tests/evals | Primary runtime and safety reference; adapt tested components and add Grok/Gemini plus a non-barrier ready queue |
| `reacher-z/GraphEngineering` | Best vendor-neutral Graph IR, TypeScript/Python conformance fixtures, deterministic ready-queue scheduler, structured failures, routers/barriers/pattern constructors | Use its contract and conformance corpus as design input; do not claim its not-yet-implemented durable scheduler recovery |
| `sciencemj/graph-engineering` | SQLite state, append-only audit/history, human-only MCP tools, command allowlists, graph integrity hashes, UI, 413 passing tests | Reuse state/audit/checkpoint ideas later; its current executor intentionally permits only one in-flight cursor |
| `gwaghmar/graph` | Lightweight explicit installation across host CLIs, local reports/cache/quality UX | Borrow installer and explicit invocation UX; do not treat its skill-driven host orchestration as a scheduler |
| `VineeTagarwaL-code/graph-engineering` | Clear dependency audit, hidden-edge checklist, bounded layered fan-in, completeness counts, source-based verification | Distill into the portable planning skill and planner tests |
| `codejunkie99/graph-engineering` | Concise task-graph teaching patterns | Keep separate: most of the package concerns knowledge graphs and would make task discovery ambiguous |
| `ChaoYue0307/awesome-graph-engineering` | Useful nine-layer taxonomy and failure-mode catalog | Convert relevant anti-patterns into evaluation fixtures and documentation, not runtime code |

Before copying code, preserve its license notice and record provenance in `NOTICE.md`. Prefer an
upstream dependency or a focused, attributable port over an untraceable rewrite. Every imported
behavior needs a local contract test so later upstream changes cannot silently alter our guarantees.

### MCP and task-graph prior art, pinned

These revisions were fetched and inspected from their primary repositories. A revision appearing
here is provenance for a design input, not a dependency endorsement.

| Source and audited revision | License | Selectively adopt | Reject or constrain |
|---|---|---|---|
| `Oortonaut/task-graph-mcp@75f71d6286b10686fd4c88a38cdef7e9ee1ab1d0` | Apache-2.0 | Typed dependency edges, transaction-safe cycle checks, atomic claim/lock tests, fan-in and cascading-unblock conformance cases | Worker-visible force bypass, attachment-presence gates, advisory file marks, and heartbeat-only stale release; add fencing generations and one graph owner |
| `RecursiveIntell/agent-graph-mcp@aaaa52f09b7aca4b4515af6f3f82712a0145e61e` | MIT | Graph size/iteration budgets, honest resume eligibility, checkpoint/effect rules, intent-result-parent receipts, tamper and crash tests | Not buildable from a clean clone because patched dependencies point outside the repository; signed integrity is not factual correctness |
| `TeamSparkAI/mcpGraph@2fbdb7d6296bcdc3802dbe89bf27d1fa951eb73c` | MIT | Static graph validation, deterministic routing/transform concepts, node/time ceilings | Sequential and non-durable; unrestricted expressions, arbitrary process/server configuration, failed test, and vulnerable dependency tree make it unsuitable as the control plane |
| `P0u4a/mcp-workflow@71811e5dbe2519c14b4e457db38a6b0789ccb893` | MIT | Activity lifecycle and pluggable store interface shapes | Linear in-memory default, failing pause/resume test, uncancelled timed-out effects, and retries without idempotency classification |
| `Graph-tl/graph@9b8d41778cd56ff5c71a4f418afb8589c1c12d73` | MIT | Ready-frontier ranking, inherited decision context, resolved-dependency evidence, atomic planning UX | Non-atomic claim path, TTL duplicate risk, and self-reported/unverified evidence; use as planning UX, not execution truth |
| `utilitydelta/mcp-graph-engine@84ea6765a95955c283259bd9d4d01d858d0a4f62` | MIT | Transitive reduction, critical path, weak components, deterministic JSON/Mermaid reporting | Knowledge graph rather than task scheduler; unbounded path queries, fuzzy task identity, arbitrary file access, and raw query surface |
| `agentralabs/agentic-workflow@1472798bb1dea1486b1a3718e163c27d1e97faa6` | MIT | Failure taxonomy, per-class retry budgets, idempotency schema, dead-letter grouping, WaitAll/Any/N/Timeout joins | Execution facade prepares rather than runs core steps; several resilience fields are not enforced and its protocol implementation pins an older revision |

Clean-room ports should name the source revision in an ADR or code comment and reproduce the
behavior with local conformance tests. Substantial copied MIT code retains copyright and license
text; Apache-2.0 material additionally retains required notices and records modifications.

## Naming and discovery

The repository is `poweredbyGEN/graph-engineering`. Use `graph-engineer` as the public command,
with `graph-engineering` as the skill and workflow product name so it is explicitly
discoverable in Claude, Codex, Grok, and Gemini:

```text
graph-engineer run dev-change --goal "..."
graph-engineer doctor
graph-engineer resume <run>
```

Retain `agent-infra` as a compatibility command and recognize legacy `AGENT_INFRA_*` environment
variables during the transition. New public documentation and package metadata use the new name.

## Throughput-first MVP (build this before the full platform)

The shortest route to internal value is a single `dev-change` workflow:

1. Validate a small workflow file with bounded nodes, real dependency edges, output schemas,
   permissions, worktree policy, time/token budgets, and deterministic `verify_cmd` gates.
2. Run a non-layered ready queue: launch a node as soon as its own dependencies pass and a slot
   is free; never wait for an unrelated slow sibling.
3. Invoke Claude, Codex, or Grok through one adapter interface and require schema-valid results.
4. Put every writing node in a nested worktree; reject overlapping write scopes unless serialized.
5. Run existing project tests/lint/types before any model reviewer. A failed check retries only
   the responsible node and its descendants, with a hard attempt/no-progress ceiling.
6. Give a fresh-context reviewer the diff, requirements, and raw evidence rather than the
   implementer's summary. Review findings must reproduce or point to deterministic evidence.
7. Integrate survivors, then run the combined project gate. Per-lane green is not integration green.
8. Persist node inputs, outputs, evidence, timing, engine/model, cost, and git state so the run can
   resume without replaying completed work.
9. Benchmark the workflow on three representative internal projects against the current working
   style; keep graph mode only where median wall-clock improves without lowering final pass rate.

The first release may use local SQLite and subprocess CLIs. It does not need a browser UI,
distributed workers, automatic workflow generation, or arbitrary cycles.

## Dependency map

```text
M0 Public baseline and product contract
 ├── M1 Root package, CLI, config, and doctor
 └── M2 Workflow schema and static validator
       └── M3 Deterministic scheduler and artifact store
             ├── M4 Engine adapters and structured outputs
             ├── M5 Evidence, MCP, and permission boundaries
             └── M6 Worktree integration and result promotion
                   └── M7 Workflow library and native integrations
                         └── M8 Traces, evaluation, and performance
                               └── M9 Distribution, security, and releases
                                     └── M10 Project adoption and stable release
```

M1 and M2 can proceed in parallel. M4 and M5 can proceed in parallel after M3. Distribution
work begins early, but a stable release waits for the complete end-to-end path.

## M0 — Public baseline and product contract

### Deliverables

- Record supported Python, git, OS, and agent-CLI versions.
- Define the public/private configuration boundary in code and documentation.
- Inventory organization-specific files and move them to a private extension repository; retain
  generic interfaces and synthetic regression fixtures in public.
- Add automated current-tree and git-history scans for secrets and organization identifiers.
- Establish semantic versioning and an explicit compatibility policy for workflow files,
  traces, and MCP tools.
- Capture the current 101-test portable baseline plus 26 site integration tests and the exact
  golden commands that reproduce each set.
- Decide which current components become public Python APIs and which remain internal modules.

### Acceptance evidence

- A clean clone runs the documented baseline command successfully.
- A fixture containing a fake private identifier or secret makes the public-safety test fail.
- A denylist/allowlist audit proves public examples, service units, paths, and skill text are
  vendor-neutral; any retained organization name has a documented public reason.
- The same scan passes on the full reachable git history.
- Compatibility policy names how deprecations warn and when fields may be removed.

## M1 — Root package, CLI, configuration, and doctor

### Deliverables

- Add one root `pyproject.toml`, the stable `graph-engineer` console command, and an
  `agent-infra` compatibility alias.
- Package the existing loop, swarm, trace, graph, and MCP components under one namespace.
- Implement commands:
  - `graph-engineer --version`
  - `graph-engineer doctor`
  - `graph-engineer init`
  - `graph-engineer config validate`
  - `graph-engineer checks`
  - `graph-engineer run`
  - `graph-engineer status`
  - `graph-engineer resume`
- Define config precedence: built-in defaults → user config → project config → environment → CLI.
- Validate public capability manifests separately from ignored private profile/server registrations;
  logs and diagnostics expose opaque IDs and hashes, never connection or credential material.
- Validate configuration with actionable field paths and suggestions.
- Detect installed engines, versions, authentication readiness, MCP registration, git/worktree
  support, and available test runners.
- Add shell completions for bash, zsh, and fish.

### Acceptance evidence

- Install with `pipx`, `uv tool install`, and editable source installation in clean environments.
- `doctor` distinguishes absent, installed, unauthenticated, and unsupported agent CLIs.
- `doctor` reports profile eligibility and negotiated capability drift without printing private
  endpoints, credential references, or server inventories.
- Invalid config fails before any agent or subprocess starts.
- Config precedence and Unicode paths are covered by tests.

## M2 — Portable workflow contract

### Deliverables

- Define a versioned JSON Schema for workflow files.
- Model nodes with:
  - stable ID, task, required capabilities, optional profile selector, model class, inputs,
    outputs, and output schema;
  - `needs`, `foreach`, condition, timeout, retry, and concurrency policy;
  - workspace mode, checks, verifier policy, and approval requirement;
  - required versus optional output and fan-in coverage thresholds.
- Distinguish agent nodes from deterministic transform, check, approval, and integration nodes.
- Reject unknown fields, duplicate node IDs, cycles, ambiguous producers, missing producers,
  incompatible schemas, invalid conditions, and unsafe workspace combinations.
- Render the validated graph as text, JSON, and Mermaid.
- Reject a workflow that embeds a private endpoint, credential reference, or undeclared ambient
  server dependency; resolve authorized profiles only after loading local configuration.
- Version the workflow format independently from the Python package.

### Acceptance evidence

- Every invalid graph class has a focused test and useful diagnostic.
- Breaking the cycle detector, producer validation, or schema compatibility check makes the
  corresponding sabotage test fail.
- The validator performs no model calls and has deterministic output.
- Profile-independent validation and profile-aware preflight produce stable, separately testable
  diagnostics; no worker starts when the capability intersection is empty.

## M3 — Scheduler, state machine, artifacts, and resume

### Deliverables

- Implement topological scheduling: only nodes whose required inputs are satisfied become ready.
- Support fan-out, true fan-in barriers, and streaming pipelines without global barriers.
- Store run state in a crash-safe SQLite database with schema migrations.
- Store immutable, content-addressed artifacts outside agent conversation history.
- Store immutable capability snapshots and evidence receipts alongside artifacts. A receipt binds
  node/run IDs, attempt and fencing generation, profile/adapter version, negotiated protocol and
  capability hashes, workspace/base/result SHAs, check command identity, timing, exit status,
  output digests, artifact digests, and verifier version.
- Validate artifacts against their declared JSON schemas before releasing downstream nodes.
- Implement node states such as pending, ready, running, passed, failed, blocked, skipped,
  awaiting-approval, and cancelled.
- Add bounded per-node retry with failure classification and no-progress detection.
- Classify read-only, idempotent, and non-idempotent effects before retry; a committed
  non-idempotent effect cannot replay without an explicit idempotency proof.
- Resume completed nodes from durable artifacts only when their receipt and capability snapshot
  validate; never trust a partial node after interruption.
- Add global and per-engine concurrency limits plus cancellation and SIGTERM cleanup.

### Acceptance evidence

- A consumer cannot start before its producer artifact validates.
- Independent nodes overlap in time; dependent nodes do not.
- Killing the runner mid-run and resuming does not repeat completed nodes or accept partial output.
- A stale claimant cannot commit after its lease is renewed under a new fencing generation.
- Tampering with a capability snapshot, result digest, parent receipt, or integration receipt fails
  closed, while the diagnostic distinguishes integrity from correctness.
- One failed optional node does not sink unrelated work, while a failed required node blocks only
  its descendants.
- File descriptors, worktrees, child processes, and locks return to baseline after soak tests.

## M4 — Agent adapters and profile capabilities

### Deliverables

- Create one adapter interface for launch, structured output, event streaming, cancellation,
  model selection, permissions, and token/cost accounting.
- Claude adapter: headless execution plus optional native dynamic-workflow compilation.
- Codex adapter: `codex exec`, JSONL events, `--output-schema`, sandbox and working-directory policy.
- Grok adapter: headless execution, JSON schema, worktree/cwd, subagent and permission options.
- Gemini adapter: headless JSON output, worktree, approval mode, and policy configuration.
- Keep adapter implementations vendor-specific but profile manifests vendor-neutral. A private
  profile may bind Claude, Codex, Grok, Kimi K3, GLM 5.2, Gemini, or another compatible runner
  without changing the workflow graph.
- Probe each adapter's supported protocol versions, MCP client capabilities/extensions, structured
  output modes, sandbox controls, and cancellation semantics; hash the result for dispatch.
- Isolate agent home/config when requested so ambient global instructions cannot corrupt a node.
- Normalize engine-specific errors into stable categories without discarding original diagnostics.
- Allow different engines/models per node in the same run.
- Do not pass ambient global MCP registrations to subagents. Materialize the minimum authorized,
  workspace-rooted server set for each node from the private registry.

### Acceptance evidence

- The same synthetic workflow fixture completes on every installed engine.
- A mixed run can use one engine to implement and a different engine to refute.
- A workflow requiring a capability routes to every eligible configured profile and to no
  ineligible profile; deterministic fallback and no-match failure are contract-tested.
- Changing a profile, server negotiation, or adapter version changes the capability hash and
  invalidates unsafe cached eligibility without invalidating unrelated pure artifacts.
- Malformed structured output is rejected or retried; it never reaches a consumer as free text.
- Missing CLI, authentication failure, rate limit, timeout, refusal, and cancellation each produce
  distinct normalized failures.

## M5 — Evidence plane, MCP 2.0, and permissions

### Deliverables

- Package `verify-mcp` with the root distribution.
- Add idempotent registration and health-check commands for Claude, Codex, Grok, and Gemini.
- Maintain a private server registry with opaque IDs, project/profile scopes, trust tier, transport
  policy, and external credential references; public graph files contain none of those values.
- Negotiate protocol and capabilities for every client/server connection. Gate extension use by
  profile, server, request category, and tool-level support rather than by SDK version.
- Generate per-node MCP configuration rooted at the node's isolated workspace.
- Keep verification tools enumerated and read-only; never accept arbitrary command strings.
- Add workflow-status and artifact-reading MCP tools where they improve agent context.
- Keep state transitions owned by the scheduler rather than allowing agents to mark themselves done.
- Issue atomic scheduler-owned claims with lease generations/fencing tokens; workers cannot bypass
  dependencies, extend authority, or commit with an expired generation.
- Validate evidence receipts rather than the existence or label of an attachment. Receipt integrity,
  source authority, and deterministic correctness are distinct statuses.
- Continue filesystem skill distribution as the portable baseline. Treat Skills-over-MCP as an
  experimental, capability-detected resource path whose versioned/digested skills declare required
  servers, tools, capabilities, provenance, and trust scope.
- Define unattended, approval-required, and forbidden actions in portable policy.
- Implement approval nodes for destructive or externally visible actions.
- Allow project-specific check providers without weakening the fixed-command boundary.

### Acceptance evidence

- Each supported CLI can list and call the same verification tool in a clean test project.
- A capability matrix fixture proves that unsupported Tasks/Skills extensions degrade to the
  documented portable path and that supported extensions are used only for authorized requests.
- A private skill cannot reveal a hidden server to an unauthorized profile or grant a tool/action
  outside the profile's capability intersection.
- Path traversal, symlink escape, arbitrary-command injection, and wrong-worktree access fail.
- An agent exit code cannot override failed evidence.
- An approval-required node cannot execute from another agent's message or self-approval.

## M6 — Worktree integration and result promotion

### Deliverables

- Create nested worktrees safely and serialize git worktree metadata mutation.
- Deduplicate live jobs by workflow run, node, and target resource.
- Track base SHA, branch, diff, commits, checks, and worktree ownership for every writing node.
- Validate the claim fencing generation and capability hash again before accepting or integrating
  a writing node's result.
- Define explicit integration nodes that compare, merge, or cherry-pick passing results.
- Detect conflicting diffs before attempting integration.
- Re-run integration-level evidence after combining independently green nodes.
- Route a failed combined check only through declared typed repair edges. Preserve unrelated
  producer artifacts, pass durable check evidence to named producers, and stop on round,
  no-progress, per-node attempt, or total-attempt limits.
- Preserve passing work for review; clean failed or cancelled scratch only under explicit policy.
- Never push, merge, deploy, or write externally without the configured authority.

### Acceptance evidence

- Parallel writers never share a working tree.
- Two independently green but conflicting changes fail at integration with a useful report.
- A combined result cannot pass solely because each lane passed separately.
- A routed combined failure reruns only its named producer and integration; an unmapped or
  repeatedly identical failure stops without guessing or replaying unrelated work.
- Interrupted cleanup never deletes a worktree containing uncommitted or unpushed work.

## M7 — Workflow library and native integrations

### Deliverables

- Ship small, inspectable reference workflows:
  - `dev-change`: scope → implementation fan-out → checks → refutation → integration;
  - `diff-audit`: risk route → diverse reviewers → reproduction → ranked findings;
  - `bug-sweep`: discover → deduplicate against all seen → verify → bounded dry rounds;
  - `migration`: inventory → per-unit pipeline → compatibility checks → integration;
  - `research`: scope → source fan-out → deterministic dedupe → verification → synthesis.
- Provide explicit plugin commands for supported CLIs; do not depend on semantic skill discovery.
- Compile eligible portable workflows to Claude native JavaScript workflows as an optimization.
- Ensure native compilation preserves required-node coverage, verification, permissions, and traces.
- Provide a programmatic Python API and stable subprocess JSON protocol.

### Acceptance evidence

- Every bundled workflow runs against a synthetic fixture offline except for the agent call.
- Explicit commands resolve deterministically on every supported CLI.
- Native and portable execution produce equivalent required artifacts and gate outcomes.
- Bundled workflows declare when they are inappropriate rather than forcing graph overhead on
  trivial work.

## M8 — Tracing, evaluation, cost, and performance

### Deliverables

- Define a versioned event schema covering run, node, edge, artifact, check, retry, approval,
  integration, and cleanup events.
- Record wall time, queue time, model, tokens, estimated cost, retries, and artifact lineage.
- Redact secrets and configurable sensitive patterns before persistence.
- Extend analysis with workflow keep rate, critical path, idle barrier time, failed-edge coverage,
  verifier overturn rate, collision rate, capability-negotiation/preflight failures, stale-claim
  rejections, receipt-integrity failures, and cost per accepted result.
- Add an evaluation corpus of representative small, independent, diamond, routed, and cyclic jobs.
- Compare linear, swarm, portable graph, and Claude-native workflow executions.
- Establish performance budgets and run scale tests at 1, 10, 50, and 100 nodes.

### Acceptance evidence

- A trace alone explains why every node ran, skipped, blocked, retried, or failed.
- A trace identifies the immutable capability/skill/receipt hashes used for each node without
  exposing private registry contents.
- Redaction tests prove secrets never land in traces or artifacts.
- Benchmarks demonstrate that pipelines do not wait behind unrelated slow items.
- Adoption decisions use measured quality, cost, and latency rather than invocation counts alone.

## M9 — Public product completeness

### Deliverables

- Add continuous integration for supported Python and OS versions.
- Enforce tests, lint, formatting, types, docs accuracy, workflow-schema compatibility, secret
  scanning, dependency audit, and public-history scan.
- Add `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, support policy, changelog, and
  architecture/decision records.
- Publish an installation matrix, full configuration reference, testing guide, and troubleshooting
  guide with expected output.
- Add a containerized clean-machine smoke test; a container image may remain optional.
- Generate SBOM, checksums, provenance, and signed release artifacts.
- Automate tagged releases to the chosen package registry and GitHub Releases.
- Add dependency update policy, lock strategy, deprecation policy, and migration guides.
- Test Unicode, hostile inputs, signals, resource cleanup, and schema fuzz/property cases.

### Acceptance evidence

- An external contributor can clone, bootstrap, test, change, and open a compliant contribution
  using only public documentation.
- A tagged release installs from the registry and completes the first-run example.
- Release artifacts have verifiable checksums, provenance, and SBOM.
- Vulnerability reports have a private, documented intake path.

## M10 — Adoption across real projects

Adoption is a measured rollout, not a global hook installed everywhere at once.

### Wave 0 — Baseline

- Record current task completion rate, wall time, rework, CI failures, review overturns, and
  context usage for representative projects.
- Inventory each project's tests, type checks, linters, setup command, protected resources, and
  permitted agent engines in local/private configuration.
- Do not claim improvement without this baseline.

### Wave 1 — Read-only verification

- Install the CLI and register `verify-mcp` for each supported agent CLI.
- Run `doctor`, `checks`, and workflow dry-runs without allowing agents to write.
- Fix missing or vacuous evidence before automation.

### Wave 2 — Low-risk pilots

- Select three structurally different pilot projects: a small library, a service, and a frontend.
- Start with `diff-audit` and a small `dev-change` workflow.
- Require human integration; no automated merge or deployment.
- Compare results with the baseline after at least ten representative runs.

### Wave 3 — Mixed-engine execution

- Use one engine for implementation and another for verification.
- Enable worktree writing and deterministic integration checks.
- Tune concurrency and model tiers from measured critical paths and cost.

### Wave 4 — Repeated workflows

- Promote only high-keep-rate patterns into checked-in project workflows.
- Add explicit project commands; skills may explain them but are not the discovery mechanism.
- Keep completeness advice and semantic review advisory unless a deterministic check can adjudicate
  the condition.

### Wave 5 — Controlled automation

- Allow unattended execution only for workflows with strong evidence and safe permissions.
- Keep external writes, destructive actions, merges, and deployment behind existing authority.
- Re-evaluate every project after engine, workflow-schema, or evidence-contract upgrades.

### Adoption success criteria

- At least 80% of pilot runs produce a kept result.
- On tasks with at least three real independent lanes, median time to a gate-passing integrated
  change improves by at least 25% versus the recorded linear baseline, with final deterministic
  pass rate no lower and escaped deterministic defects no higher.
- At least 60% of theoretically parallel worker time overlaps during eligible pilot runs; report
  critical-path time and idle barrier time so apparent speedups cannot come from skipped evidence.
- A failed lane reruns only itself and invalidated descendants in at least 90% of retrying runs;
  whole-workflow retry rate remains below 10%.
- Capability negotiation and profile preflight complete before worker launch in 100% of runs, and
  every dispatched node records matching immutable capability and skill hashes.
- Evidence-receipt integrity, stale-claim fencing, and duplicate-target conformance fixtures have a
  100% rejection rate; pilot runs have zero accepted stale or duplicate writer commits.
- Graph workflows beat the relevant linear baseline on quality or wall time without unacceptable
  cost growth; report median/p95 wall time, token/cost per accepted result, verifier overturn rate,
  integration-conflict rate, and human intervention time by workflow shape and profile.
- No duplicate writer targets or shared-worktree collisions occur.
- Every accepted result has deterministic evidence and artifact lineage.
- A verifier overturn is visible and prevents integration.
- Teams can disable or remove `graph-engineering` without corrupting their project or agent configs.

## Test strategy

Tests are organized around invariants rather than implementation details:

- unit tests for schemas, graph validation, state transitions, conditions, retries, and adapters;
- contract tests run against every engine adapter;
- integration tests use fake CLIs to control timing, malformed output, timeouts, and signals;
- end-to-end tests run synthetic repositories through fan-out, fan-in, routing, verification,
  integration, interruption, and resume;
- property tests generate malformed graphs and hostile artifact shapes;
- soak tests exercise process, file-descriptor, lock, and worktree cleanup;
- sabotage tests break every safety-critical enforcement point and confirm the test fails.

No test may call a paid model by default. Live-engine smoke tests are opt-in and separately
reported so ordinary contributors can run the full deterministic suite offline.

## Release sequence

- `0.1`: root package, CLI, doctor, config, existing components preserved.
- `0.2`: workflow schema, validator, graph rendering.
- `0.3`: scheduler, artifacts, durable state, resume, fake adapter.
- `0.4`: Claude, Codex, Grok, and Gemini adapters plus structured outputs.
- `0.5`: MCP registration, evidence policies, worktrees, integration nodes.
- `0.6`: bundled workflows, explicit plugins, Claude native compiler.
- `0.7`: trace analytics, evaluation corpus, cost/performance reporting.
- `0.8`: pilot feedback, compatibility hardening, migration tooling.
- `1.0`: stable workflow/event schemas, reproducible releases, public security and support policy,
  and successful multi-project adoption evidence.

Versions are capability milestones, not calendar promises. A milestone ships only when its
acceptance evidence exists on the exact released tree.

## Definition of fully built

`graph-engineering` is fully built for version 1.0 when all of the following are true:

1. A public user can install, diagnose, initialize, and complete the golden workflow from a
   clean environment without source reading.
2. One portable workflow runs with Claude, Codex, Grok, Gemini, and a mixed-engine configuration.
3. Real producer outputs gate consumers and cross worktree boundaries through validated artifacts.
4. Runs survive interruption, resume safely, clean resources, and explain every state transition.
5. Agents cannot self-certify, bypass required evidence, access another node's workspace, or
   approve protected actions.
6. Integration proves the combined result rather than merely collecting green lanes.
7. Public releases are tested, versioned, reproducible, scanned, documented, and independently
   installable.
8. Representative project pilots show measurable value over the prior development process.
9. The reachable public history contains no private configuration, identifiers, or credentials.
10. The project can be adopted or removed without hidden global state or irreversible changes.
11. Arbitrary configured profiles are selected by a validated capability intersection, and every
    accepted node result is bound to immutable negotiation, claim, artifact, and evidence receipts.

That is the finish line. A working graph demo is M4, not completion.
