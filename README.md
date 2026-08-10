# graph-engineering

**Portable, evidence-gated graph development for Claude, Codex, Grok, and Gemini.**

An agent's reliability is mostly a property of its **harness** — the tools, state, permissions,
and observability around the model — not of the model itself. When an agent "can't do the job",
the harness is usually the problem: a missing capability, no way to verify work, or no way to
inspect what happened afterwards.

This repo organizes that work into three layers, in the order you should build them.

## The three layers

| Layer | Answers | Build order |
|---|---|---|
| **[Harness](harness/)** | *What can the agent see, touch, and prove?* Tools, scoped permissions, durable state, traces. | **First — highest leverage** |
| **[Loops](loops/)** | *How does work get verified and retried?* Evidence in, feedback out, a real stop condition. | Second |
| **[Swarm](swarm/)** | *How do we do many of these at once, safely?* Isolated lanes, each gated on its own evidence, plus adversarial verification. | Third |
| **[Traces](traces/)** | *Are the checks real? Is it converging? Do we need a graph?* Reads the run records and answers with data. | Continuous |
| **[Graph](graph/)** | *What is allowed to happen next?* Explicit control flow, handoffs, approval gates. | **Only when traces justify it** |

The common failure is doing this backwards: an elaborate multi-agent graph sitting on a weak
harness with uncontrolled retries. Start with one agent that has excellent tools and real
verification. Promote structure into a graph only when traces show you actually need branching.

### Quick diagnostic

- Agent can't access what it needs, or loses state → **harness**
- Almost works, but inconsistent or fails the same way repeatedly → **loop**
- Real branching, specialist handoffs, approval gates → **graph**

## What's here

### Harness

| Component | What it does | Status |
|---|---|---|
| [`harness/servers/verify-mcp`](harness/servers/verify-mcp) | Deterministic verification over MCP 2.0 — tests, lint, typecheck, git status. Read-only, so it can be pre-approved and run unattended. | ✅ 16 tests, sabotage-checked |
| [`harness/mcp-2-server-guide.md`](harness/mcp-2-server-guide.md) | How to build an MCP 2.0 server: the v1→v2 breaking change, minimum viable server, and the design rules that make a tool usable by a model. | ✅ verified against `mcp==2.0.0` |

### Loops

The layer most teams skip. A loop needs a trigger, a concrete goal, an action policy, **evidence**,
compact feedback, and a stop rule. The principle:

> **Loop on evidence, not on confidence.** "The model says it's done" is never a valid stop
> condition. `exit_code=0` from a suite it didn't write the runner for is.

| Component | What it does | Status |
|---|---|---|
| [`loops/`](loops) | `evidence-loop`: runs an agent until deterministic checks pass, with feedback, prior-attempt history, and a no-progress guard. | ✅ 23 tests, sabotage-checked |

`verify-mcp` is the evidence source for an agent checking its own work mid-session;
`evidence-loop` wraps an agent from outside. Same checks, different consumer.

### Swarm

| Component | What it does | Status |
|---|---|---|
| [`swarm/`](swarm) | Fan out independent lanes, one git worktree each, gated on evidence, with an optional adversarial refutation pass. | ✅ 21 tests, sabotage-checked |

Parallelism alone doesn't improve quality — N agents on one problem produce N confident wrong
answers faster. Lanes must be independent and each must prove it finished.

### Traces

| Component | What it does | Status |
|---|---|---|
| [`traces/`](traces) | Reads `--trace` JSON from both runners; grades every check (EARNING / NO SIGNAL / BROKEN…), flags stuck vs budget-exhausted runs, and detects coupled lanes. | ✅ 27 portable tests + 26 site integration tests |

The finding that matters: **a check that has never failed is not evidence.** Run it against your
own traces and see how many of your checks have never caught anything.

### Graph

The alpha Python package contains validated workflow contracts, a ready-queue scheduler, durable
SQLite run state, immutable artifact receipts, fenced worktree change transfer, worker profile
selection, local subprocess adapters, an optional A2A remote-worker adapter, and an MCP task
coordination server. The deliberately thin
[`graph-engineer` CLI](docs/CLI.md) exposes only the deterministic operations justified by project
pilots: assess/init, validate, doctor, plan, run/resume/handoff, status, and trace. It is not a graph editor or a second
orchestration framework. The `graph-engineering` skill remains the portable playbook for drawing
and executing graphs through host-native orchestration.

Lanes declare `produces`/`consumes`, so the **fake edge test** — *does A's output actually
flow into B?* — is computed rather than argued. Correlated outcomes are only a hint; two
lanes hitting one flaky check correlate perfectly with no dependency at all.

### MCP, A2A, and the graph runtime

| Layer | Responsibility |
|---|---|
| Graph runtime | Owns control flow, budgets, readiness, persistence, evidence, repair, and integration. |
| MCP | Gives a worker bounded tools, data, resources, or a durable task capability. |
| A2A | Delegates one typed node to an independently operated remote agent. |

The layers do not replace each other. A2A is optional perimeter transport, never the scheduler;
MCP access never grants node acceptance; remote output still passes local schemas, worktree scope,
checks, and integration. See [A2A remote workers](docs/A2A.md).

This is narrower than LangGraph or Google ADK: those are general agent-application orchestration
runtimes, while this project bakes in software-development invariants such as isolated git writes,
canonical changesets, exact-base resume, deterministic project gates, evidence receipts, and a
single integration owner. Their useful patterns still transfer: mix deterministic and model nodes,
initialize run context once, fan out independent specialists, use explicit joins and routes, and
persist an observable lifecycle. See the official [LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview),
[Google ADK graph documentation](https://adk.dev/graphs/), and
[Agentic Space Quest codelab](https://codelabs.developers.google.com/way-back-home-level-1/instructions).

### Ops

**[`ops/`](ops)** — the parts that keep running whether or not anyone remembers them, each
guarding a failure that is invisible while it happens: graph staleness and scratch
contamination, skill-adoption drift, and documentation that no longer matches the code.

`ops/check-docs-accurate.py` exists because an audit of this repo found **6 of 9 test-count
claims in its own markdown were wrong** — every one true when written, then silently rotted.
The counts in this README are now enforced by a check rather than by memory.

### Skills

**[`skills/`](skills)** — `graph-engineering` and `evidence-loop`, written so
they trigger on the *situation* rather than on a magic phrase. `graph-engineering` is the
cross-host playbook: it cuts fake edges, isolates parallel writes, gates nodes on deterministic
evidence, retries only invalidated work, and gives one orchestrator ownership of integration.
It can also be invoked explicitly as `$graph-engineering` where the host supports named skills.

## Design provenance

This runtime is a clean-room implementation informed by several independent projects. We adopted
contracts and tests selectively rather than merging skills or treating any repository as a
drop-in engine:

| Upstream | What we learned and changed |
|---|---|
| [reacher-z/GraphEngineering](https://github.com/reacher-z/GraphEngineering) | Keep the graph IR vendor-neutral and back it with cross-runtime conformance; add the durable scheduler and recovery guarantees its early runtime did not yet provide. |
| [sciencemj/graph-engineering](https://github.com/sciencemj/graph-engineering) | Let the server own durable execution state, deterministic commands, and human-only checkpoints; replace its single in-flight cursor with a concurrent ready queue. |
| [gwaghmar/graph](https://github.com/gwaghmar/graph) | Make installation and quality-gate reporting work across agent hosts; keep local cache/report UX separate from execution truth. |
| [Oortonaut/task-graph-mcp](https://github.com/Oortonaut/task-graph-mcp) | Use typed dependencies, cycle tests, claims, locks, and fan-in conformance; strengthen claims with generations/fencing and require real evidence receipts rather than attachment presence. |
| [RecursiveIntell/agent-graph-mcp](https://github.com/RecursiveIntell/agent-graph-mcp) | Separate evidence integrity from factual correctness, bind checkpoints to effects, and impose hard graph ceilings; reproduce useful behavior locally because the audited revision was not clean-clone buildable. |
| [TeamSparkAI/mcpGraph](https://github.com/TeamSparkAI/mcpGraph) and [P0u4a/mcp-workflow](https://github.com/P0u4a/mcp-workflow) | Keep schema preflight, deterministic routing, activity lifecycle, and store interfaces; reject unrestricted transforms, sequential-only execution, and retries without effect safety. |
| [Graph-tl/graph](https://github.com/Graph-tl/graph), [utilitydelta/mcp-graph-engine](https://github.com/utilitydelta/mcp-graph-engine), and [agentralabs/agentic-workflow](https://github.com/agentralabs/agentic-workflow) | Borrow ready-frontier ranking, graph analytics, failure classes, and quorum vocabulary; do not adopt non-atomic claims, broad knowledge-graph attack surfaces, or prepared-but-not-executed runtime facades. |

[NOTICE.md](NOTICE.md) records exact reviewed revisions and licenses. The fuller adoption/rejection
audit is in [ROADMAP.md](docs/ROADMAP.md). Before every minor release or major MCP/runtime redesign,
and at least every 90 days while active, compare those pins with upstream heads and update the
decision when new tests, security work, protocol support, or implementation evidence changes it.
Never copy a new upstream behavior without attribution and a local regression plus sabotage test.

### This repo gates itself

[`.evidence.toml`](.evidence.toml) runs all four component suites plus the docs check.
The [Woodpecker pipelines](.woodpecker/) run the skill contract, documentation accuracy,
component suites, MCP server tests, runtime tests, public tree/history scans, and clean-package
installation on every push and pull request.

```bash
cd loops && python3 -m evidence_loop.loop --config ../.evidence.toml --cwd .. --check-only
```

## See it work first

**[`examples/todo-api`](examples/todo-api)** — a package with 9 failing tests and three real
gaps. Copy it, run the evidence loop, watch an agent close them. Verified: Claude Code closed
all three in one attempt, 13/13 tests passing, and `tests/` byte-identical afterwards — it
fixed the code rather than weakening the evidence.

```bash
cp -r examples/todo-api /tmp/todo
cd loops && python3 -m evidence_loop.loop --config /tmp/todo/.evidence.toml --cwd /tmp/todo --check-only
```

## Deploying this

**[`docs/SETUP.md`](docs/SETUP.md)** — the ordered path, with the reason for each step. Read it
before installing anything: step 1 involves an MCP upgrade that is a one-way door for projects
still on v1.

**[`docs/AGENT-CLIS.md`](docs/AGENT-CLIS.md)** — works with Claude Code, Codex, Grok, or Gemini.
Every command is an argv list, so the agent is just a subprocess; swap the CLI and everything
else is unchanged. Covers the per-CLI invocation differences (`codex -p` means *profile*, not
prompt) and cross-model adversarial verification.

## Quickstart

Install the reviewed alpha artifact from the public release, or clone the repository for
development:

```bash
uv tool install \
  https://github.com/poweredbyGEN/graph-engineering/releases/download/v0.1.0a1/graph_engineering-0.1.0a1-py3-none-any.whl
graph-engineer --version
```

Inspect what the installed runtime actually implements before wiring it into a project:

```bash
graph-engineer capabilities --json
graph-engineer doctor --repo "$PWD" --json
# Explicitly opt in to a bounded real-worker launch only when desired:
graph-engineer doctor --repo "$PWD" --profile codex --smoke --timeout 30 --json
```

`capabilities` is generated from code and packaged schemas. Default `doctor` remains static and
non-spending; `--smoke` uses an empty isolated repository, strict structured output, reduced
environment, no MCP, bounded time/output, and redacted digest-only receipts. On Linux it fails
closed unless `bwrap`, `strace`, and `prlimit` can enforce a read-only host plus audited empty
write root; any attempted local filesystem mutation returns `WRITE_DETECTED`.

```bash
git clone https://github.com/poweredbyGEN/graph-engineering.git
cd graph-engineering
uv venv && uv pip install -e ".[dev]"
uv run graph-engineer --help
uv run graph-engineering-mcp --help

uv run graph-engineer assess --repo "$PWD" --json
uv run graph-engineer init --repo "$PWD" --json
uv run graph-engineer doctor --repo "$PWD"
uv run graph-engineer validate workflow.json
uv run graph-engineer plan workflow.json
uv run graph-engineer run workflow.json --repo "$PWD" --run-id first-run
uv run graph-engineering-mcp --database "$HOME/.local/state/graph-engineering/tasks.db"
```

The final command starts the portable MCP task server over stdio. See [CLI.md](docs/CLI.md) for the
JSON workflow and durable-run contract, [MCP.md](docs/MCP.md) for client integration,
[A2A.md](docs/A2A.md) for independently operated remote workers,
[SUBAGENTS.md](docs/SUBAGENTS.md) for portable worker profiles, and [SETUP.md](docs/SETUP.md) for
the deterministic verification harness. [PILOTS.md](docs/PILOTS.md) reports both the successful
speedup and the slower graph that blocked a defect, including cold-adoption failures.

Contributions are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) describes the deterministic local
gate and public/private boundary; [CHANGELOG.md](CHANGELOG.md) records user-visible releases.

## Conventions

Three rules everything here follows:

1. **A lesson that can fail a test belongs in a test.** Every test carries an `# intent:` comment
   naming the failure it catches. Prose degrades silently and is read at the wrong moment; a test
   fires every run.
2. **Sabotage-check the tests.** Break the fix, confirm the test fails, restore. A test that can't
   fail isn't protection — it's decoration. (`verify-mcp`'s containment tests are verified this
   way: a naive `startswith` path check fails 4 of them, including the symlink escape.)
3. **Narrow beats general.** A tool the model *can't* misuse is worth more than a flexible one it
   can. Enumerate the options; never accept a command string.

## License

[MIT](LICENSE). Third-party provenance and retained notices are recorded in [NOTICE.md](NOTICE.md).
