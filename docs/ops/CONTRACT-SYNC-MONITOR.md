# Contract consumer lag alarm (GEN-11056)

The existing contract-sync PR monitor owns consumer drift, grouped PR health and
sync progress. Rails `docs/generated/` is authoritative; MCP vendors it unchanged
and agentic consumes MCP. The monitor reads production; it never repairs contracts,
merges a PR, deploys, restarts a service or changes credentials.

## Evidence and incident boundary

Use gen-deployd `/status` for production deployment identity and quiet windows.
Fetch the Rails contract at both Gitea main and the deployed Rails SHA. Main may
be ahead of production: only production defines the alert target. Read both hosted
MCP units through their active interpreter's immutable deployment directory,
requiring matching DEPLOY_SHA/DEPLOY_COMPLETE. Read agentic from its running PID's
working directory, checking repository identity against deployd and refusing files
modified after that process started. Repository main alone never proves deployment.

Compare action `contract_version`, Rails producer SHA and the unmodified operations
artifact hash. Missing identity is an explicit unverified signal, never inferred
from a version label or replaced by repository main. Agentic currently publishes
neither an action contract_version nor a Rails producer stamp: report that typed
identity gap alongside its actual wire-contract hash. Do not fabricate those fields.

Served-contract lag without open sync PRs has 60 minutes of Rails deployment grace
and escalates at six hours. A deployment of that consumer, or 300 seconds since
deployd's latest attempt, suppresses notification without clearing an open incident.

For PR-backed consumers, every open `auto/contract-sync-` PR targeting `main`
keeps the consumer behind, including green and pending checks. The monitor reads
every page of open PRs; malformed or unreadable responses fail the cycle rather
than pretending the backlog is empty. Age is the time since the oldest open PR's
`created_at`, independent of Rails or consumer deployments. Classification is the
worst state, `RED > CONFLICT > STALE > PENDING`, independent of API ordering.
`WARNING_HOURS` defaults to 2 and `CRITICAL_HOURS` defaults to 24, each overridable
through the monitor environment like `STALE_HOURS`. Rails grace holds only PRs
younger than `CONTRACT_SYNC_LAG_GRACE_S`; older work keeps its clock and severity.
Below the warning threshold an open backlog retains its incident silently.
An unreadable or missing deployed identity remains independently critical; young
PRs cannot conceal it. That finding retains its identity failure and carries the
PR clock and worst state as `pr_age_s` and `pr_classification` evidence.

Critical escalation opens one consumer Plane ticket through the existing incident
path. Its issue ID and last successfully reported PR membership/worst state live
alongside `ticket_url` in `incidents.json`. A later change to membership or worst
state adds one comment, including during Slack cadence suppression or a quiet
window; individual PR head/check changes do not. Legacy URL-only consumer records
resolve the exact existing Plane ticket once before commenting. Existing per-PR
`tickets.json` records remain readable and untouched; no new per-PR tickets are
created. Recovery requires no open sync PRs and no remaining served-contract finding;
only accepted alerts produce a recovery notification, once.

This follows the published [oldest-unprocessed-item age metric](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-available-cloudwatch-metrics.html)
and [stable alert identity](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)
patterns: producer activity does not reset waiting work, and changing observations
update one consumer incident.
The accepted notification severity is stored separately from policy decision time,
so a rejected escalation remains eligible for retry. One stable consumer key controls notification; PR heads, failing check count and
Rails main changes do not multiply alerts. A confirmed recovery resolves only an
incident whose notification was accepted. Suppressed incidents recover silently.

## Dead-man coverage

A successful monitor cycle atomically writes `last-complete.json`. The sync bot
writes `last-success.json` only at its existing all-consumer checkpoint, including
no-change successful runs; dry runs advance neither production heartbeat.

Run this same monitor with `--watchman` as an ExecStopPost of the existing
`gen-mcp-surface-check.service` on gen-prod. It uses bounded read-only SSH to QA:
check the monitor heartbeat (30 minutes), timer enabled and timer active. Its own
state and alert policy live on gen-prod, so disabling QA's timer cannot disable
this check. `contract-sync-watchman.conf` is the proposed drop-in; no second timer
is created. The orchestrator installs it after review. Surface-check failure still
has that service's existing off-box Kuma coverage; ExecStopPost runs after both successful and failed primary probes.

## Budget, rollout and permission boundary

RCA is direct MiMo through shared alert_policy, at most once per due consumer,
20 seconds per call. Dry runs explicitly skip RCA and all Slack/Plane writes.
Statuses are fetched concurrently; each HTTP/SSH operation has a timeout. A
480-second cycle deadline reserves 120 seconds below the 600-second unit timeout.
Measured checks: the isolated real QA dry run completes in 5.45 seconds for
23 MCP PRs, one agentic PR and three grouped incidents. The 15-red-PR budget
replay completes in 20.003 seconds with one full 20-second RCA delay.

Exhaustion fails the run and leaves last-complete stale for the off-box check.
Never mark an incomplete cycle successful. Ticket updates must respect that same
budget. The unit comment records the measured regression timing and real dry run.

Install candidates into an isolated directory on QA, load the existing secret env,
and set DRY_RUN=1 with separate state/log paths. Never overwrite the installed live
script or enable a new sender during lane verification. Preserve production policy,
tickets, timer, logs and cadence. Before activation, the orchestrator installs the
monitor, bot heartbeat change, units and off-box drop-in together, audits root
ownership/write access to each state/log directory, then verifies both hosts.

The off-box SSH account needs read-only access to the QA heartbeat and timer.
The current gen-prod root identity is rejected by QA (publickey). Activation
requires the orchestrator to provision an authorized restricted reader; this
lane does not change credentials. Read failure is itself an unverified critical
incident, with unknown timer values, never a claim that the timer is disabled.
