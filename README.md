# agent-infra

**Tools and practices for building software with agents.**

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
| [`traces/`](traces) | Reads `--trace` JSON from both runners; grades every check (EARNING / NO SIGNAL / BROKEN…), flags stuck vs budget-exhausted runs, and detects coupled lanes. | ✅ 53 tests, sabotage-checked |

The finding that matters: **a check that has never failed is not evidence.** Run it against your
own traces and see how many of your checks have never caught anything.

### Graph

**[`graph/`](graph)** is deliberately empty, and that is a measured result rather than a
deferral: `trace-analyze` has found no real dependencies in any run recorded so far, so a
graph would buy rigidity in exchange for nothing.

Lanes declare `produces`/`consumes`, so the **fake edge test** — *does A's output actually
flow into B?* — is computed rather than argued. Correlated outcomes are only a hint; two
lanes hitting one flaky check correlate perfectly with no dependency at all.

### Ops

**[`ops/`](ops)** — the parts that keep running whether or not anyone remembers them, each
guarding a failure that is invisible while it happens: graph staleness and scratch
contamination, skill-adoption drift, and documentation that no longer matches the code.

`ops/check-docs-accurate.py` exists because an audit of this repo found **6 of 9 test-count
claims in its own markdown were wrong** — every one true when written, then silently rotted.
The counts in this README are now enforced by a check rather than by memory.

### Skills

*situation* (about to declare something done; about to trust a graph answer) rather than on
a phrasing someone has to remember. 145 of 187 installed skills on the origin machine had
never fired once; that is almost always a description problem, not a quality one.

### This repo gates itself

[`.evidence.toml`](.evidence.toml) runs all four suites plus the docs check. A repo arguing
for evidence-gated work that did not gate itself would be the "built it but never turned it
on" failure it exists to prevent.

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

```bash
git clone <this-repo> && cd agent-infra/harness/servers/verify-mcp
uv venv && uv pip install -e ".[dev]"
uv run pytest -q

claude mcp add verify -- "$PWD/.venv/bin/verify-mcp"
```

Then have your agent call `list_checks` — it reports the configured root and which runners are
installed, so the agent discovers your stack instead of guessing it.

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

MIT
