# Loops

**Loop on evidence, not on confidence.**

An agent that says "done" is guessing. A loop that stops when a test suite it didn't write the
runner for returns `exit_code=0` is not. This layer turns that distinction into something
mechanical.

Works on any repo, any language — the evidence commands come from a per-repo config, not from
hardcoded assumptions.

## First: is a loop even worth it?

A loop pays only when **all four** of these hold. Miss one and it costs more than doing the
work yourself:

1. **You'll run it again** — regularly, not "eventually". A one-off task never earns a loop.
2. **The work can grade itself** — a check that passes or fails without your eyes on it.
3. **Goal in, result out** — if a human has to step in mid-run, it isn't a loop.
4. **The finish line is a fact, not a feeling** — if "good enough" needs judgment, no loop
   can make that call.

Criterion 2 is the one people talk themselves past. "I'll eyeball the output" means you are
the stop condition, and you'll be the stop condition every run forever.

## Build it in this order

Skipping straight to automation is the most common way to end up with an expensive loop that
never worked:

1. **Run it manually once**, in a single conversation. If it isn't reliable there, it will
   not be reliable on a schedule — it will just fail faster and cost more.
2. **Lock what worked into a config.** A loop that lives in one chat dies with that chat.
3. **Then add the gate and the stop condition** — the check that can fail, and the ceiling
   on attempts. Without both you don't have a loop, you have an automated way to spend money.

`--check-only` is step 1 in this runner: it gathers evidence and never invokes the agent, so
you can see exactly what the loop will react to before you let it act.

```bash
python3 -m evidence_loop.loop --config .evidence.toml --check-only   # look first
python3 -m evidence_loop.loop --config .evidence.toml --dry-run      # then read the plan
```

## What it costs

Every attempt re-sends the whole context, so ten iterations is not ten prompts — it is ten
prompts that keep getting longer. The number to watch is not tokens spent but **how many runs
you kept**. Below a ~50% keep rate the loop is losing to doing it by hand, and the fix is the
goal or the checks, never a higher `max_attempts` — that buys more of the same failure at a
longer context each time.

`trace-analyze` reports this from your traces; see [`../traces`](../traces).

## The anatomy

Every real loop has seven parts. Skip one and it either never stops or stops too early.

| Part | What it is | Failure if you skip it |
|---|---|---|
| **Trigger** | What starts it — a ticket, a failing test, a schedule | Runs when nobody wanted it |
| **Goal** | A concrete success condition, written before work starts | "Improve X" — never satisfiable |
| **State** | Current attempt + previous attempts + their errors | Repeats the same failed fix |
| **Action policy** | What the agent may do this cycle | Scope creep, unrelated edits |
| **Evidence** | Deterministic checks: tests, lint, types, schema | **Stops on the model's opinion** |
| **Feedback** | Compact and actionable — "this citation 404s" | Agent can't act on "it failed" |
| **Stop rule** | Success, max retries, budget, or escalate | Loops forever, burns budget |

The one that gets skipped is **evidence**, and it's the one that matters. "The model says it's
done" is never a valid stop condition.

## `evidence-loop`

A minimal runner implementing the above. It does not call an LLM — it wraps whatever agent
command you already use, and enforces the loop around it.

```bash
evidence-loop --config .evidence.toml            # run until evidence passes or budget runs out
evidence-loop --check-only                       # just gather evidence, no agent
evidence-loop --config .evidence.toml --dry-run  # print the plan, run nothing
```

### Per-repo config

Drop `.evidence.toml` in any repo:

```toml
goal = "All tests and the type checker pass; no new lint errors."

# Deterministic checks. Ordered: cheapest first, so a lint error doesn't wait on a test run.
[[checks]]
name = "lint"
cmd  = ["ruff", "check", "."]

[[checks]]
name = "tests"
cmd  = ["pytest", "-q"]

[[checks]]
name = "types"
cmd  = ["mypy", "src"]

[agent]
# Whatever drives the work. {feedback} is replaced with the failure output.
cmd = ["claude", "-p", "{feedback}"]

[limits]
max_attempts = 4
timeout_sec  = 900
```

Language doesn't matter — `cmd` is just an argv list. A TypeScript repo swaps in
`["npx","vitest","run"]` and `["npx","tsc","--noEmit"]`; a Go repo uses `["go","test","./..."]`.

### What it guarantees

- **Evidence is gathered by a subprocess the agent doesn't control.** The agent cannot report
  its own success.
- **Feedback is the actual failure output**, truncated from both ends so the summary survives.
- **Every attempt is recorded** — command, exit code, duration, output digest — so a run can be
  audited afterwards instead of trusted.
- **It always terminates**: max attempts, per-check timeout, and a no-progress rule that stops
  early when two consecutive attempts produce identical failure output.

That last one matters. An agent stuck on the same error will happily burn every retry; identical
consecutive failures mean it isn't converging, and continuing just spends budget.

## Where this fits

```
harness/   verify-mcp gives the agent read-only access to evidence
loops/     evidence-loop enforces work → evidence → feedback → retry
graph/     only once traces show real branching
```

`verify-mcp` and `evidence-loop` overlap deliberately: the server is for an agent checking its
own work mid-session, the runner is for wrapping an agent from outside. Same checks, different
consumer.
