# Real-project pilot evidence

The first two pilots compared the same bounded change under a single-agent baseline and a
portable graph. Project identities and private configuration stay outside this public repository;
the archetype, topology, timings, checks, and failures are recorded here.

## Pilot A — TypeScript client library

Task: add a client-wide request timeout, cover four transport paths, clean up timers, and normalize
abort-aware fetch failures.

| Run | Wall time | Outcome | Evidence |
|---|---:|---|---|
| Single Codex baseline | 207.9s | Local tests/build passed | Independent abort-aware probe escaped a raw `AbortError` instead of the required typed 408 error. |
| Final Codex + Grok graph | 262.5s | Integration blocked | Implementation passed its lane checks. The test lane needed one localized retry. The combined suite ran 68 passing assertions but reported two unhandled timeout rejections, so no result was accepted. |

This graph was 26.3% slower and did not produce a keepable change. It was nevertheless safer: the
baseline appeared green while violating the external error contract, and the graph refused to
integrate a test implementation whose assertions passed while asynchronous failures leaked. The
runtime correctly retained attempt-specific worktrees and retried only the failed test lane.

The missing product capability found here is now implemented as an explicit contract: a failed
integration check can route typed combined-gate evidence back to named responsible producers.
Only those producers are invalidated and retried before integration is reconstructed; unknown
failures do not trigger guessed repair, and repeated identical failures stop at a bounded
no-progress limit. Retrying the whole graph or accepting a nominally green assertion count would
both be wrong.

## Pilot B — API documentation package

Task: keep a generated OpenAPI mirror byte-identical to its canonical file and add an import-safe,
deterministic drift checker with tests.

| Run | Wall time | Outcome | Evidence |
|---|---:|---|---|
| Single Codex baseline | 94.840s | Accepted | Two tests plus build, byte comparison, diff check, outside-working-directory check, and a deliberate byte-drift sabotage. |
| Final Codex + Grok graph | 82.386s | Accepted | Three nodes passed on attempt one; seven check receipts passed; the graph added a third missing-mirror test and passed the same independent sabotage. |

The steady-state graph was 13.13% faster. Workers started 0.159s apart and overlapped for 35.825s;
their durations were 67.090s and 35.825s, followed by 14.775s of integration gates. Compared with
102.915s of serialized worker time, the overlap avoided 34.81%.

Cold adoption was much slower: two discarded graph designs consumed about 919s before the final
run. One incorrectly assigned an exact large-file copy to a model; another exposed provider output
normalization and provider-specific schema-subset failures. Those failures produced three reusable
rules:

- exact copying, flattening, deduplication, and comparison are deterministic edges, not agent nodes;
- portable JSON Schema is validated at our adapter boundary instead of being sent uncompiled to a
  provider's narrower native schema dialect;
- provider narration may precede the result, so only a complete JSON object or array anchored at
  the end of the authoritative assistant channel can cross the output edge.

## Decision

Continue with measured adoption, not a global always-graph hook. Use graphs when work has real
independent lanes, a meaningful integration gate, or a quality verifier. Keep small sequential
changes linear. The thin CLI exists to make validated plans, runs, status, and resume explicit;
skills teach topology, while deterministic runtime checks decide acceptance.

For subsequent pilots, report steady-state and cold-adoption time separately. A faster successful
run does not erase integration cost, and a blocked defect is a quality result rather than a shipped
change.
