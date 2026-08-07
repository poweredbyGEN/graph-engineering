---
name: evidence-loop
description: Stop work on deterministic evidence rather than on the model reporting it is done. Use whenever about to declare something fixed, done, working, or shipped — a claim of "done" needs a command that proves it, not a self-report. Use when handed a fix/build/repair task that a test, linter, typechecker, or schema can grade; when a task will be repeated; when a previous attempt looked done but wasn't; and before saying a build passes. Terse commands like "fix", "fix it", "go", "hotfix", "what else" all mean drive this to a verified finish, not to a plausible-looking one. Also answers whether a loop is worth building at all, and grades whether existing checks are real evidence (run trace-analyze after ~5 runs for keep rate and dead checks).
user_invocable: true
metadata:
  short-description: "Loop on evidence, not on confidence"
---

# Evidence loop — the model saying "done" is not a stop condition

Repo: wherever you cloned `agent-infra` (this doc assumes `~/projects/agent-infra`).

The default agent loop stops when the model reports it is finished. That is a self-report,
and it is the single most common source of *"it looked done but wasn't."* This runner moves
the stop condition **out of the model**: evidence comes from a subprocess the agent cannot
influence, and the loop continues until that subprocess is happy or the budget runs out.

## Before building a loop — is it even worth it?

All four must hold. Miss one and the loop costs more than doing the work by hand:

1. **You'll run it again** — regularly, not "eventually".
2. **The work can grade itself** — a check that passes or fails without your eyes.
3. **Goal in, result out** — no human stepping in mid-run.
4. **The finish line is a fact, not a feeling.**

Criterion 2 is the one people talk themselves past. *"I'll eyeball the output"* means you
are the stop condition, every run, forever.

## The order that works

Skipping to automation is how you get an expensive loop that never worked.

```bash
cd ~/projects/agent-infra/loops

# 1. LOOK FIRST — gathers evidence, never runs the agent
python3 -m evidence_loop.loop --config .evidence.toml --cwd <repo> --check-only

# 2. Read the plan; heed the warnings it prints
python3 -m evidence_loop.loop --config .evidence.toml --cwd <repo> --dry-run

# 3. Let it work
python3 -m evidence_loop.loop --config .evidence.toml --cwd <repo> --trace run.json
```

`--dry-run` warns when `max_attempts = 1` (that is a command, not a loop), when a check
looks like it cannot fail (not evidence), and when there is no agent (check-only — the right
FIRST step, not an error).

## Minimal config

```toml
goal = "All tests in tests/ pass. Fix the cause, not the symptom."

[limits]
max_attempts = 4
timeout_sec = 600

[agent]
cmd = ["claude", "-p", "{feedback}"]     # or ["codex","exec","{feedback}"] / grok / gemini

[[checks]]
name = "tests"
cmd = ["python3", "-m", "pytest", "tests/", "-q"]
```

Every command is an argv list, so the same runner drives Python, TypeScript, Go, or Rust —
and any CLI. Note `codex exec`, not `codex -p` (`-p` selects a profile).

## After ~5 runs — is it earning its cost?

```bash
cd ~/projects/agent-infra/traces
python3 -m trace_analyze.analyze <dir-of-traces>/
```

Two numbers decide everything:

- **Keep rate** — converged runs / total runs. Below ~50% the loop is losing to doing it by
  hand. Fix the **goal or the checks**; never raise `max_attempts`, which buys more of the
  same failure at a longer context each time.
- **NO SIGNAL checks** — a check that has never failed is not evidence. Break the code it
  covers on purpose; if it stays green, delete it.

Every attempt re-sends the whole context, so ten iterations is not ten prompts — it is ten
prompts that keep getting longer.

## When NOT to use this

- One-off work — a task you do once never earns a loop.
- "Good" requires judgment — no loop can make that call.
- You would be the one deciding when to stop — then you are the stop condition.

## The other layers (only when the loop is not enough)

- **swarm** — 10+ genuinely independent units (repos, tickets), each in its own worktree,
  with adversarial verifiers that try to REFUTE. Declare `produces`/`consumes` on lanes and
  it computes the **fake edge test**: an edge is real only when B consumes what A produces.
  Least-proven layer; lanes touching the same files will conflict on merge.
- **harness** — an MCP server exposing `run_tests`/`run_linter`/`run_typecheck` so the agent
  calls checks itself. Skip if the loop already runs your checks.
- **traces** — see above.

Full docs: `~/projects/agent-infra/docs/SETUP.md`, `docs/AGENT-CLIS.md`. Worked example with
9 deliberately failing tests: `examples/todo-api/`.
