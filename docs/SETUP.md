# Setup

Deploy this in order. Each step is useful on its own, and each one earns the next — skipping
ahead is the most common way teams end up with an elaborate swarm producing unverifiable work.

**Time:** ~20 minutes for steps 1–3. Steps 4–5 are per-repo and take a few minutes each.

---

## Step 0 — Prerequisites

| Need | Why | Check |
|---|---|---|
| Python 3.11+ | `tomllib` is stdlib from 3.11; configs are TOML | `python3 -V` |
| git 2.20+ | Worktrees are the lane isolation mechanism | `git --version` |
| An agent CLI | Claude Code, Codex, Grok, or Gemini — anything that takes a prompt argument. See [AGENT-CLIS.md](AGENT-CLIS.md) | `claude --version` |
| `uv` (optional) | Faster installs; `pip` works fine | `uv --version` |

---

## Step 1 — MCP 2.0, in a virtualenv

**Do this first**, and understand why before you run it.

> **`pip install mcp` now installs 2.x, and v2 is not source-compatible with 1.x.**
> If anything on your machine already imports `mcp`, a global install silently breaks it.

The 2026-07-28 spec made the protocol **stateless** — no `initialize` handshake, no session ID,
every request self-contained. That's what lets a server run behind a normal load balancer or on
serverless with no sticky routing. It's the right target for new work, but the upgrade is a
one-way door for anything on v1.

```bash
python3 -c "import importlib.metadata as m; print(m.version('mcp'))"   # what you have now
```

- **Nothing installed, or nothing depends on v1** → install globally if you like.
- **Something depends on v1** → pin it (`mcp>=1.28,<2`) and use a virtualenv here.

```bash
cd harness/servers/verify-mcp
uv venv && uv pip install -e ".[dev]"     # or: python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
uv run pytest -q                          # 16 tests, no network
```

Full detail: [`harness/mcp-2-server-guide.md`](../harness/mcp-2-server-guide.md).

---

## Step 2 — Harness: give the agent verification it can't fake

```bash
claude mcp add verify -- "$PWD/.venv/bin/verify-mcp"
```

Set `VERIFY_MCP_ROOT` to the repo you want checked. Then ask your agent to run `list_checks` —
it reports the configured root and which runners are actually installed, so the agent discovers
your stack instead of guessing it.

**Why this before anything else:** agents are normally handed an unrestricted shell, so every
verification is an arbitrary-execution decision a human must approve. In practice that means
people rubber-stamp, or the agent stops checking. `verify-mcp` inverts it: no tool accepts a
command, so it can be pre-approved once and run unattended without widening blast radius.

**What you should NOT add:** MCP servers that wrap CLIs you already have. If your team uses
`gh`, `git`, and `psql` from the shell, an MCP wrapper for them adds tool-listing tokens to
every session and no capability. A server earns its place by adding something you lack or a
*boundary* you want. Tool surface is scarce — a bloated listing degrades routing for every
tool, not just the unused ones.

---

## Step 3 — Loops: stop on evidence, not on "done"

Drop `.evidence.toml` in a repo:

```toml
goal = "All tests and the type checker pass."

[[checks]]
name = "tests"
cmd  = ["pytest", "-q"]        # or ["npx","vitest","run"], ["go","test","./..."], …

[agent]
cmd = ["claude", "-p", "{feedback}"]

[limits]
max_attempts = 4
timeout_sec  = 900
```

```bash
python3 -m evidence_loop.loop --config .evidence.toml --check-only   # gather evidence only
python3 -m evidence_loop.loop --config .evidence.toml                # run the loop
```

**Why:** the default agent loop stops when the model says it's finished. That's a self-report,
and self-reports are the most common source of "it looked done but wasn't". This moves the stop
condition out of the model.

**Start with `--check-only`** for a few days. It runs no agent — it just tells you whether your
checks are meaningful. If they pass on a repo you know is broken, fix the checks before
automating anything on top of them.

Detail: [`loops/README.md`](../loops/README.md).

---

## Step 4 — Swarm: only for genuinely independent work

```bash
python3 -m swarm_run.swarm --config .swarm.toml --repo . --dry-run   # always dry-run first
python3 -m swarm_run.swarm --config .swarm.toml --repo .
```

**Do not start here.** A swarm on a weak harness with no evidence contract is the failure mode
this repo exists to prevent — it produces wrong answers faster and makes them look official.

Use it when the units are independent: 20 repos, 50 tickets, one lane each. Not for one hard
problem — more agents on a single problem multiplies guesses rather than reducing them.

**Set `[verify]` when the cost of being wrong is high.** Verifiers are prompted to *refute*,
and a majority-refuted lane is not confirmed no matter how green its own checks were.

Detail: [`swarm/README.md`](../swarm/README.md).

---

## Step 5 — Graph: use it when the dependencies are real

Install the alpha portable package in its own environment:

```bash
cd graph-engineering
uv venv && uv pip install -e ".[dev]"
uv run graph-engineer --help
uv run graph-engineering-mcp --help
```

The package contains validated contracts, ready-queue scheduling, durable state, artifacts,
worktree transfer, subprocess worker adapters, and the MCP task server. The deliberately thin
`graph-engineer` CLI validates and plans workflows, runs or resumes them, and inspects durable
status. Use the `graph-engineering` skill for host-native topology decisions and the packaged MCP
server for durable cross-client task claiming. See [`CLI.md`](CLI.md) for the JSON contract and
[`PILOTS.md`](PILOTS.md) for the measured boundary between useful graphs and needless overhead.

Promote work to an explicit graph only when traces show real branching, joins that must
synchronize, or human approval gates — and only after you have those traces. A graph drawn
before the evidence exists just adds rigidity to a system that hasn't earned it.

Collect the traces first (`--trace` on both the loop and the swarm), read them, and see whether
the branching is real.

For Claude, Codex, Grok, Kimi K3, GLM 5.2, and other workers, copy the private profile template in
[`subagents.example.toml`](../subagents.example.toml). Kimi/GLM examples use a restricted OpenCode
subprocess route that the runtime implements; direct OpenAI-compatible execution is not yet
implemented. See [`SUBAGENTS.md`](SUBAGENTS.md).

---

## Verifying your deployment

```bash
cd harness/servers/verify-mcp && uv run pytest -q    # 16
cd ../../../loops && python3 -m pytest tests/ -q     # 23
cd ../swarm && python3 -m pytest tests/ -q           # 21
cd ../traces && GRAPH_ENGINEERING_PORTABLE_TESTS=1 python3 -m pytest tests/ -q  # 27
cd ../ops    && python3 -m pytest tests/ -q          # 22
```

101 portable tests, no network, no API keys. Hosts with the installed Graphify reconciler and
nightly sweep run another 26 site integration tests. If the portable tests pass, the machinery
works — what remains is whether
*your* checks are meaningful, which only `--check-only` on a repo you understand will tell you.

These counts are themselves checked: `python3 ops/check-docs-accurate.py` fails if any
markdown file in this repo claims a test count the suites do not have. An audit on
2026-08-07 found **6 of 9 such claims wrong** — every one of them true when written, then
silently rotted as tests were added. Prose degrades; a check does not.

## Troubleshooting

**"command not found" from a check** — intentional. A missing binary fails the lane rather than
being skipped, because a silently absent checker turns the whole system into a rubber stamp.

**A lane fails with `worktree failed`** — usually `swarm/<name>` already exists from a previous
run. `git worktree list`, then `git worktree remove --force <path>` and `git branch -D swarm/<name>`.

**The loop stops early with "no progress"** — two consecutive attempts failed identically, so
the agent isn't converging. Continuing would spend budget reproducing the same error. Read the
feedback: usually the goal is underspecified or the check tests something the agent can't reach.

**Evidence passes but the work is wrong** — your checks are too weak. This is the failure mode
worth taking seriously; add a check that would have caught it, and confirm it fails before the
fix and passes after.
