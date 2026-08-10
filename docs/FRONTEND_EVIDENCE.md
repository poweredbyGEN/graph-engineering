# Frontend evidence nodes

Use the ordinary workflow and artifact contracts for browser verification. The public template is
[`examples/frontend-evidence.workflow.json`](../examples/frontend-evidence.workflow.json). It adds
no browser runtime, queue, dashboard, endpoint, secret, or production authority.

Select a focused, checked-in suite from the changed UI area. Keep the runner profile and its exact
allowed command in private configuration; never pass model-produced shell text. Free local checks
should run before a live browser node. Live, costly, or manual suites require a trusted host approval
node that binds the named suite and target. The browser node is non-replay-safe: a timeout after the
trigger may mean the action succeeded, so preserve partial evidence and adjudicate instead of
automatically clicking again.

The result is a discriminated contract. A `succeeded` UI artifact must contain five independently
hashed, repository-relative files:

1. `prepared` — complete input/configuration before the action;
2. `triggered` — visible confirmation immediately after the single action;
3. `waiting` — genuine in-progress state;
4. `result` — final user-visible result;
5. `reload` — the result after a real reload, proving persistence.

Each descriptor binds path, SHA-256, byte count, and media type. `evidence_digest` binds the complete
structured artifact. A `failed`, `crashed`, or `blocked` artifact instead accepts any non-empty,
schema-valid subset of those checkpoints plus the optional `partial` trace/events file. Runners
should flush `partial` while work is live, so a crash can leave inspectable evidence even when no
later visual checkpoint exists. Non-success artifacts are retained for diagnosis and adjudication,
but their status can never satisfy the success gate—even if they happen to contain all five success
checkpoints. Screenshots alone do not override the suite's assertions.

Playwright fits this node when it already owns deterministic assertions, traces, and screenshots.
Herdr Browser can expose the same authorized browser session for observation or human takeover, but
it does not replace the runner receipt or acceptance checks. Private CDP URLs, credentials, target
accounts, environment names, slot allocation, and runner endpoints remain outside the public repo.
