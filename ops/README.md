# Ops — the parts that have to keep running

The layers above are things you invoke. These are things that run whether or not anyone
remembers them, because each one guards against a failure that is **invisible while it is
happening**.

| | Guards against |
|---|---|
| [`graphify/`](graphify) | A code graph that silently goes stale or fills with untracked scratch. A contaminated graph looks identical to a good one at the point of use. |
| [`adoption/`](adoption) | Shipping a practice nobody adopts. A skill that fires constantly and one that has never fired look the same from the outside. |
| [`check-docs-accurate.py`](check-docs-accurate.py) | Documentation that describes code it no longer matches. |

## check-docs-accurate.py

```bash
python3 ops/check-docs-accurate.py
```

Fails when any markdown file in this repo claims a test count the suites do not have.

**Why it exists:** an audit on 2026-08-07 found **6 of 9 test-count claims in this repo's
own markdown were wrong**. <!-- historical --> The root README said loops had 18 tests when
it had 23; traces 17 when it had 53; `SETUP.md` claimed 51 total. Every one of those numbers was
*true when it was written*. Tests were added; the prose was never touched.

That is this repo's central argument turned on itself. A README that misreports its own test
count is a stale graph in a different costume: confidently specific, and wrong. So the claim
became checkable rather than remembered.

It runs as a check in [`../.evidence.toml`](../.evidence.toml), alongside the four suites —
the cheapest check in the set and the one most likely to fire, because prose rots faster
than code.

Two details worth knowing:

- It reads **preceding lines**, not just the line with the number. `SETUP.md` writes
  `cd harness/servers/verify-mcp` and `# 16 tests` two lines apart; judging that claim in
  isolation attributes it to nothing and lets it rot silently.
- It matches a bare `# 21` trailing a pytest command, not only the words "N tests". The
  first version missed those and under-reported — a checker that under-reports still gets
  trusted, which is worse than one that does not exist.

Sabotage-checked: rotting a count, rotting a bare count, and rotting the total each fail it.

**The escape hatch is real, so use it deliberately.** A line containing `<!-- historical -->`
is skipped, for prose that deliberately quotes a past wrong number to explain an incident.
Verified by sabotage: a genuinely stale claim carrying that marker is NOT caught. It is an
opt-out, not a nuance the checker can infer — which is why it has to be typed on purpose.

## graphify/ and adoption/

Both ship as systemd timers, capped at `CPUQuota=10%` so background work never
competes with a foreground agent. See [`graphify/README.md`](graphify/README.md) for the
graph freshness story, including the shrink-guard incident that burned 5h31m of CPU
re-deriving one error while `systemctl status` read `active (running)`.
