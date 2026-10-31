State: locally verified; no installation or remote release
Branch: agent/contract-sync-lag-clock
Base: origin/main at cb20c8f8cabdd99f8568ae9b7ac69a1e001a9151
Tests: 68 passed; 12 sabotage mutations caught
Next: orchestrator review/release; no push or PR from this worktree

The monitor measures each PR-backed consumer's age from its oldest open sync PR targeting main. Every open sync PR remains evidence, including pending/green checks; the worst state wins (RED > CONFLICT > STALE > PENDING). WARNING_HOURS defaults to 2 and CRITICAL_HOURS to 24. Rails or consumer deployments cannot reset an older PR's clock. Young open PRs retain incident state and ticket evidence silently. Only an empty backlog can recover a PR incident, and only an accepted alert receives a recovery notification.

The existing reconcile critical path creates one consumer ticket. Membership/worst-state changes comment on that ticket independently of Slack cadence, including quiet windows and final recovery; head or individual-check churn is silent. New consumer ticket IDs and successful comment snapshots live in incidents.json. Legacy URL-only consumer records resolve their exact existing Plane issue once, caching the UUID before commenting. Legacy tickets.json rows remain readable and untouched; no new per-PR tickets are created.

Open-PR collection reads every page and rejects malformed lists. Alerts keep all per-PR states and the existing policy/MiMo/SELF_HEAL/footer contract. Their duplicate JSON preview is bounded so the observed backlog fits Slack; Plane and MiMo retain full evidence. No PR/repo mutation path is added. Missing/unreadable deployed identity remains independently critical; a young PR cannot hide it, and its PR age/worst state remain explicit evidence.

Validation:

- Independent tests preceded product edits. On byte-identical base, 16 failed and 1 passed: [base output](evidence/base-tests.txt).
- All five requested cases pass, plus threshold/env boundaries, state priority, legacy records/retries, comments during cadence/holds, pagination, malformed responses, and payload size: `python3 -m pytest ops/contract-sync -q` → **68 passed**. [Output](evidence/scoped-tests.txt).
- Twelve deliberately broken variants failed their targeted regressions; exact candidate bytes were restored. [Summary](evidence/sabotage-summary.txt). Initial mutation selectors were corrected before confirming every failed guard.
- Final live QA read-only DRY_RUN: **api-docs RED, critical, 359509s / 99.86h / 4.16d**, 30 open sync PRs; oldest `2026-10-27T18:23:16Z`. Other observed backlogs: MCP 266.56h/46 PRs; agentic 145.58h/28 PRs. No RECOVERED message. [Full output](evidence/live-dry-run-final.txt), [summary](evidence/live-summary.txt).
- Candidate source was streamed to `python3 -` over SSH; no candidate was installed. Environment came from the existing monitor config. State/log paths were isolated beneath `/mnt/data/tmp/contract-sync-lag-clock-01a193f8-reviewed/`. MiMo was explicitly skipped; Slack and Plane sends were dry-run only. Existing-ticket UUID resolution was separately verified with a read-only Plane lookup. [Lookup](evidence/live-legacy-ticket-resolution.txt).
- Installed script still hashes to `e9d394f61a8f393698a1ca9ff0a8ca184f14af72a6f998bb98a8deafa3638ae7`; 122 legacy ticket rows remain. Candidate SHA256: `e3e389e100948c57e61f22a4ccad7c47ac8e1d12169ae8af21ca17f4d321bebe`.

One cross-model review identified additional edge cases, each converted to independent tests before fixes (8 failed before those fixes): unreadable identity with open PRs, empty PR membership while served lag remains, clamped server page sizes, and concise Slack PR lines. The user's oldest-PR clock takes precedence over the reviewer's suggestion to restore Rails time for ordinary PR-backed lag. [Review](evidence/claude-review.txt), [disposition](evidence/review-disposition.md), [pre-fix cases](evidence/review-cases-before-fix.txt). No second review round.

Published patterns and research: [research summary](evidence/RESEARCH.md); committed design updated in [CONTRACT-SYNC-MONITOR.md](docs/ops/CONTRACT-SYNC-MONITOR.md).

Release limits: no full suite, CI, installation, push, PR or downstream CI repair. Live Slack/Plane/MiMo delivery is intentionally untested. Existing serving-contract/watchman evaluations keep their separate clocks. Plane errors still fail the cycle and leave the heartbeat stale for the off-box watcher; cross-consumer error isolation is outside this clock fix. This changes detection; the consumer backlogs remain blocked until their CI/merge issues are repaired.
