# Worked example: todo-api

A deliberately incomplete Python package with **9 failing tests**. Point an agent at it and
watch the evidence loop decide when the work is actually done.

This is the fastest way to see the whole idea work on something real.

> **Don't fix this in place.** The incompleteness is the fixture. Copy it first:
> `cp -r examples/todo-api /tmp/todo && cd /tmp/todo`

## The three gaps

`src/todo_api/store.py` is missing three things, each with tests that describe the required
behaviour precisely enough that an agent can implement it without guessing:

| Gap | What's missing | The subtle part |
|---|---|---|
| **1. Validation** | `add()` accepts empty and unbounded titles | The 500-char limit is **inclusive** — a `>= 500` check fails `test_accepts_title_at_the_limit` |
| **2. Error type** | `get()` raises a bare `KeyError` | `complete()` must fail the same way, or callers catch two exceptions for one condition |
| **3. Delete** | No `delete()` at all | Ids must **never be reused** — a stale reference would silently point at a different todo |

Each subtlety is there on purpose. They're the kind of thing an agent gets wrong when it's
optimizing to make tests pass rather than to be correct — and the kind of thing a weak check
would let through.

## Run the evidence loop

```bash
cp -r examples/todo-api /tmp/todo
cd loops
python3 -m evidence_loop.loop --config /tmp/todo/.evidence.toml --cwd /tmp/todo --check-only
```

`--check-only` runs no agent — it just shows you the 9 failures. Then drop the flag to let the
agent work:

```bash
python3 -m evidence_loop.loop --config /tmp/todo/.evidence.toml --cwd /tmp/todo --trace run.json
```

### What actually happened when we ran it

Claude Code closed all three gaps in **one attempt** (2 loop iterations: fail → work → pass).
It independently identified both subtleties without being told:

> "caps length at `MAX_TITLE_LENGTH = 500` using `> 500` so the boundary stays *inclusive* —
> `test_accepts_title_at_the_limit` exists specifically to catch the off-by-one that a `>= 500`
> check would introduce"

> "`_next_id` is a monotonic counter that only ever increments in `add()` and is never derived
> from `_items`, so deleting the highest id cannot hand it out again."

Verified independently afterwards: **13/13 tests pass**, and `tests/test_store.py` was
byte-identical to the original — the agent did not weaken the evidence to make it pass. That
check matters more than the pass count: the cheapest way to make a suite green is to delete the
test, and the feedback message explicitly forbids it.

## Run the swarm

Three lanes, one per gap, each in its own git worktree:

```bash
cd swarm
python3 -m swarm_run.swarm --config ../examples/todo-api/.swarm.toml --repo /path/to/agent-infra --dry-run
```

This is the honest demonstration of when *not* to use a swarm: these three lanes all edit the
same file, so they'd conflict on merge. Real swarm work is independent units — 20 repos, 50
tickets. Kept here because seeing the failure mode is more instructive than reading about it.

## Using a different CLI

Every command is an argv list, so swap the agent in `.evidence.toml`:

```toml
[agent]
cmd = ["codex", "exec", "{feedback}"]   # or ["grok","-p",...] / ["gemini","-p",...]
```

Note `codex exec`, not `codex -p` — `-p` selects a config profile. See
[`docs/AGENT-CLIS.md`](../../docs/AGENT-CLIS.md).

## What to look at afterwards

- **`run.json`** — every attempt, exit code, duration, output. This is the raw material for
  deciding whether you need a graph.
- **The diff** — did the agent fix the cause, or special-case the test?
- **`git diff tests/`** — must be empty. If an agent edits the evidence, the evidence is gone.
