# Graph — deliberately empty

This directory has no code, and that is the finding rather than an omission.

A graph is explicit control flow between units of work: which node runs when, what each one
consumes, where the approval gates sit. It is the layer everyone reaches for first, and the
one that should be built last.

## Why nothing is here yet

**The traces do not justify it.** `trace-analyze` looks for lanes that always share an
outcome, and across every run recorded so far it has found none. Building a graph anyway
would buy rigidity — a fixed execution order, more moving parts, more to maintain — in
exchange for nothing.

That is a measured answer, not a deferral. Run it yourself:

```bash
cd ../traces && python3 -m trace_analyze.analyze <your-traces>/
```

## What would justify one

**Real dependencies, not correlated outcomes.** Two lanes failing together might share a
dependency — or might both be hitting one flaky check. Correlation is a hint; it is not an
edge.

The decisive question is the **fake edge test**: *does A's output actually flow into B?*
Sequence is the order someone wrote the lanes down. Dependency is B being unable to start
without what A produced. Most workflows carry two or three edges that are the former wearing
the latter's clothes, and each one serializes work that could have run in parallel.

The swarm answers this from data rather than opinion. Declare contracts on your lanes:

```toml
[[lanes]]
name = "research-a"
task = "..."
produces = ["findings-a.json"]

[[lanes]]
name = "synthesis"
task = "..."
consumes = ["findings-a.json", "findings-b.json"]
```

`trace-analyze` then reports real edges, and `swarm` rejects a `consumes` that no lane
produces — a dependency that was assumed rather than real fails at config time.

## The shape to expect

When a graph is justified, it is usually a **diamond**: several independent nodes fanning
out, converging on one node that genuinely needs all of them. Two conditions must hold, and
both are worth checking rather than assuming:

1. The parallel jobs are actually independent.
2. The convergence step actually needs all of them — if it needs one, the rest are waste.

Put a **fresh-context verifier** between the workers and the final step. The agent that did
the work is the worst judge of it: the same reasoning that produced a mistake is the
reasoning being asked to catch it. See [`../swarm`](../swarm), where verifiers run as
separate processes and are handed the worker's diff rather than only the worker's mutated
worktree.

## Build order

**Harness → Loops → Swarm → Graph.** The common failure is doing this backwards: an
elaborate multi-agent graph sitting on a weak harness with uncontrolled retries. Start with
one agent that has excellent tools and real verification. Promote structure into a graph
only when the traces show branching you actually have.
