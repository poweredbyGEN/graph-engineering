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
- the `universal-deploy` handoff is implemented in a focused shared-skills PR, but remains unmerged
  until that repository provides a terminal-green exact-SHA gate.

This is the control-plane foundation, not the finished runtime. The next executable product work is
M1 and M2 in parallel: the installable CLI/doctor and the portable workflow contract/validator.

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
  Skills-over-MCP stabilizes; capability-detect it because SEP-2640 remains experimental;
- keep exactly one task-graph owner per project. Multiple graph servers writing independent state
  would create split-brain claims and contradictory readiness.

Rejected as default dependencies after clean-clone checks: `P0u4a/mcp-workflow` failed its own
pause/resume test and reported critical dependency vulnerabilities; `TeamSparkAI/mcpGraph` failed
one of 126 tests and reported eight high-severity dependency vulnerabilities; the audited
`agent-graph-mcp` revision referenced a missing local path dependency; and
`agentralabs/agentic-workflow` documents MCP/shell step runners as planned despite its broad tool
surface. Their useful schema and routing ideas remain design inputs, not installed control planes.

The portable graph runtime is not yet executable. `produces` and `consumes` are currently trace metadata:
they do not schedule consumers after producers or transfer artifacts between worktrees. There is
also no root package, stable CLI, durable run store, mixed-engine run, release process, or
clean-machine installation path. The repository now has a parallel Woodpecker CI baseline; later
milestones must extend it with package, conformance, integration, safety, and release gates.

The public/private split is incomplete. The public tree still carries organization-specific
service/unit names, filesystem locations, private-project examples, incident statistics, and
deployed reconciler assumptions under `ops/` and `traces/`.
They are not credentials, but they are private operational policy and make the public package
look environment-specific. M0 must extract them into a private extension or replace them with
synthetic fixtures before the project is presented as a reusable public product.

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
- Validate configuration with actionable field paths and suggestions.
- Detect installed engines, versions, authentication readiness, MCP registration, git/worktree
  support, and available test runners.
- Add shell completions for bash, zsh, and fish.

### Acceptance evidence

- Install with `pipx`, `uv tool install`, and editable source installation in clean environments.
- `doctor` distinguishes absent, installed, unauthenticated, and unsupported agent CLIs.
- Invalid config fails before any agent or subprocess starts.
- Config precedence and Unicode paths are covered by tests.

## M2 — Portable workflow contract

### Deliverables

- Define a versioned JSON Schema for workflow files.
- Model nodes with:
  - stable ID, task, engine, model, inputs, outputs, and output schema;
  - `needs`, `foreach`, condition, timeout, retry, and concurrency policy;
  - workspace mode, checks, verifier policy, and approval requirement;
  - required versus optional output and fan-in coverage thresholds.
- Distinguish agent nodes from deterministic transform, check, approval, and integration nodes.
- Reject unknown fields, duplicate node IDs, cycles, ambiguous producers, missing producers,
  incompatible schemas, invalid conditions, and unsafe workspace combinations.
- Render the validated graph as text, JSON, and Mermaid.
- Version the workflow format independently from the Python package.

### Acceptance evidence

- Every invalid graph class has a focused test and useful diagnostic.
- Breaking the cycle detector, producer validation, or schema compatibility check makes the
  corresponding sabotage test fail.
- The validator performs no model calls and has deterministic output.

## M3 — Scheduler, state machine, artifacts, and resume

### Deliverables

- Implement topological scheduling: only nodes whose required inputs are satisfied become ready.
- Support fan-out, true fan-in barriers, and streaming pipelines without global barriers.
- Store run state in a crash-safe SQLite database with schema migrations.
- Store immutable, content-addressed artifacts outside agent conversation history.
- Validate artifacts against their declared JSON schemas before releasing downstream nodes.
- Implement node states such as pending, ready, running, passed, failed, blocked, skipped,
  awaiting-approval, and cancelled.
- Add bounded per-node retry with failure classification and no-progress detection.
- Resume completed nodes from durable artifacts; never trust a partial node after interruption.
- Add global and per-engine concurrency limits plus cancellation and SIGTERM cleanup.

### Acceptance evidence

- A consumer cannot start before its producer artifact validates.
- Independent nodes overlap in time; dependent nodes do not.
- Killing the runner mid-run and resuming does not repeat completed nodes or accept partial output.
- One failed optional node does not sink unrelated work, while a failed required node blocks only
  its descendants.
- File descriptors, worktrees, child processes, and locks return to baseline after soak tests.

## M4 — Claude, Codex, Grok, and Gemini adapters

### Deliverables

- Create one adapter interface for launch, structured output, event streaming, cancellation,
  model selection, permissions, and token/cost accounting.
- Claude adapter: headless execution plus optional native dynamic-workflow compilation.
- Codex adapter: `codex exec`, JSONL events, `--output-schema`, sandbox and working-directory policy.
- Grok adapter: headless execution, JSON schema, worktree/cwd, subagent and permission options.
- Gemini adapter: headless JSON output, worktree, approval mode, and policy configuration.
- Isolate agent home/config when requested so ambient global instructions cannot corrupt a node.
- Normalize engine-specific errors into stable categories without discarding original diagnostics.
- Allow different engines/models per node in the same run.

### Acceptance evidence

- The same synthetic workflow fixture completes on every installed engine.
- A mixed run can use one engine to implement and a different engine to refute.
- Malformed structured output is rejected or retried; it never reaches a consumer as free text.
- Missing CLI, authentication failure, rate limit, timeout, refusal, and cancellation each produce
  distinct normalized failures.

## M5 — Evidence plane, MCP 2.0, and permissions

### Deliverables

- Package `verify-mcp` with the root distribution.
- Add idempotent registration and health-check commands for Claude, Codex, Grok, and Gemini.
- Generate per-node MCP configuration rooted at the node's isolated workspace.
- Keep verification tools enumerated and read-only; never accept arbitrary command strings.
- Add workflow-status and artifact-reading MCP tools where they improve agent context.
- Keep state transitions owned by the scheduler rather than allowing agents to mark themselves done.
- Define unattended, approval-required, and forbidden actions in portable policy.
- Implement approval nodes for destructive or externally visible actions.
- Allow project-specific check providers without weakening the fixed-command boundary.

### Acceptance evidence

- Each supported CLI can list and call the same verification tool in a clean test project.
- Path traversal, symlink escape, arbitrary-command injection, and wrong-worktree access fail.
- An agent exit code cannot override failed evidence.
- An approval-required node cannot execute from another agent's message or self-approval.

## M6 — Worktree integration and result promotion

### Deliverables

- Create nested worktrees safely and serialize git worktree metadata mutation.
- Deduplicate live jobs by workflow run, node, and target resource.
- Track base SHA, branch, diff, commits, checks, and worktree ownership for every writing node.
- Define explicit integration nodes that compare, merge, or cherry-pick passing results.
- Detect conflicting diffs before attempting integration.
- Re-run integration-level evidence after combining independently green nodes.
- Preserve passing work for review; clean failed or cancelled scratch only under explicit policy.
- Never push, merge, deploy, or write externally without the configured authority.

### Acceptance evidence

- Parallel writers never share a working tree.
- Two independently green but conflicting changes fail at integration with a useful report.
- A combined result cannot pass solely because each lane passed separately.
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
  verifier overturn rate, collision rate, and cost per accepted result.
- Add an evaluation corpus of representative small, independent, diamond, routed, and cyclic jobs.
- Compare linear, swarm, portable graph, and Claude-native workflow executions.
- Establish performance budgets and run scale tests at 1, 10, 50, and 100 nodes.

### Acceptance evidence

- A trace alone explains why every node ran, skipped, blocked, retried, or failed.
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
- Graph workflows beat the relevant linear baseline on quality or wall time without unacceptable
  cost growth.
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

That is the finish line. A working graph demo is M4, not completion.
