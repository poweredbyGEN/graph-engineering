# Traces

**Answers the questions you'd otherwise guess at.**

Both runners emit `--trace` JSON. Until you read it, three important questions stay opinions:

- *Are our checks any good?* — a check that has never failed is not evidence.
- *Is the agent converging, or thrashing?* — repeated identical failures aren't progress.
- *Do we need a graph?* — only if lanes turn out to be coupled.

```bash
python3 -m trace_analyze.analyze run.json                 # one trace
python3 -m trace_analyze.analyze traces/                  # a directory
python3 -m trace_analyze.analyze traces/ --json           # machine-readable
```

Reads both shapes — the loop's `{goal, cwd, attempts[]}` and the swarm's `[lane, …]` — and
tells them apart by structure, not filename.

## Check verdicts

The core output. Every check gets one:

| Verdict | Meaning | What to do |
|---|---|---|
| **EARNING** | Failed, then passed later in the same run — it caught something and drove a fix | Keep it |
| **ACTIVE** | Fails sometimes, no confirmed catch yet | Keep watching |
| **NO SIGNAL** | Never failed across every run | **Suspect.** Try breaking what it covers; if it stays green, delete it |
| **ALWAYS RED** | Failed every run | Usually misconfigured, not a great bug-finder |
| **BROKEN** | Exit 127 — the checker isn't installed | Fix the environment; it's been proving nothing |
| **SLOW** | Timed out | A timeout reads as a failure the check didn't earn |

Real output from this repo's own traces:

```
EARNING    tests            caught 1 real defect(s) that a later attempt fixed
NO SIGNAL  vacuous          never failed in 2 runs — costs 0.0s and catches nothing
NO SIGNAL  syntax           never failed in 2 runs — costs 0.0s and catches nothing
```

**`NO SIGNAL` is the finding worth acting on.** A check that has never failed either tests
nothing or tests something that cannot break — and it costs time on every single run while
catching nothing. The honest test is to break the code it covers on purpose. If it stays green,
it was never evidence.

That's the same discipline as sabotage-checking a test, applied to your whole check suite from
data instead of intuition.

## Convergence

```
✅ trace.json: converged in 2 attempt(s)
❌ t.json: 2 attempt(s), stuck on the same failures
```

**"Stuck"** and **"budget exhausted"** need different responses, so they're reported separately:

- *Stuck* — identical failures on consecutive attempts. The agent isn't converging. Usually the
  goal is underspecified, or the check tests something the agent can't reach. Raising
  `max_attempts` just spends more money reproducing the same error.
- *Budget exhausted* — failures kept changing. It was making progress and ran out of room.
  Raising the limit is reasonable here.

## Do you need a graph?

The analyzer looks for lanes that **always share an outcome** across runs. That coupling is the
first honest argument for explicit control flow — if two lanes always pass together and fail
together, they aren't independent, whatever the config claims.

It deliberately won't cry coupling on:

- fewer than 3 runs (coincidence looks like correlation)
- lanes with a constant outcome (two lanes that always pass share an outcome trivially — that
  would recommend a graph for every healthy swarm)

Absent that evidence it says so plainly: *"a graph would add rigidity without buying anything."*

## Testing

```bash
GRAPH_ENGINEERING_PORTABLE_TESTS=1 python3 -m pytest tests/ -q  # 27 tests
```

Sabotage-checked on all three verdict mechanisms — labelling a dead check healthy, counting a
never-resolved failure as a catch, and calling constant outcomes coupling each fail the suite.
That matters more here than elsewhere: this module's job is to tell you a check is worthless, so
a bug that hides that would protect the exact thing you came to find.
