# graph-engineering

Portable, evidence-gated graph development for Claude, Codex, Grok, and Gemini. Read
[README.md](README.md) for the harness / loops / swarm layering and
[CONTRIBUTING.md](CONTRIBUTING.md) for the release gate and change discipline.

## Comment Durability

Comments are read as a specification of the code as it is, not as a record of how it
changed. Four rules:

- **Present tense, code as it is now.** Never "we used to X then switched to Y",
  "changed to", "now uses", "previously". If a reader cannot tell the comment was
  ever edited, it is right.
- **No process residue.** No wave names, agent-run labels, "pass 2", "round 3",
  "per review", "as discussed", sprint names, investigation dates, or measurement
  counts from the work that produced the change.
- **"Why this constraint exists" stays.** Ordering guarantees, a cap and what
  overruns it, why a guard is load-bearing, why an obvious refactor breaks a
  caller. That is the class of comment whose deletion causes outages; its absence
  on non-obvious code is itself a finding.
- **"How we got here" goes** to git history, the PR body, or a test name.

An architectural decision (a boundary, an ownership rule, a cross-service contract,
an alternative rejected for a reason) must appear as a comment at the code it
constrains — never left only in a PR, a session, or a chat thread, where the next
reader will not find it and will re-derive it wrong.

A `see <path>` citation must resolve to a doc committed in this repo or a Plane
ticket ID. Never cite a scratch or machine-local path (`/tmp`, `/mnt/data/tmp`, a
scratchpad, a session directory, `~/projects/...`, a worktree), a session
transcript, or an artifact URL as the only home. A dead pointer is worse than no
citation.

### Regression test convention: `# intent:`

A regression test carries an `# intent:` comment naming the failure it catches, so a
later reader can tell whether the test still guards anything. Write it as a standing
statement of what must hold, not as a note about the investigation. Sabotage-check
the guard (break the fix, confirm the test fails, restore) — a test that cannot fail
is not protection.
