# CI resource sizing rollout (GEN-8751)

The shared helper is pinned by commit in each consumer CI configuration. Land and publish the agent-infra commit before publishing consumers: a missing pin must fail the step, never silently use a different helper. Every QA agent mounts the same package-cache directory, so a pipeline's active shard count is immutable across independent workspaces. CPU/load/memory sizing works immediately; the queue-count snapshot requires the separate host rollout below.

## Host changes prepared, not applied

QA currently has eight Docker agents: main and lane-1 at MAX_WORKFLOWS=4, critical/general-1/general-2 at 2, and three DAY agents at 4. Those total 26 configured slots on 16 CPUs. Do **not** raise `WOODPECKER_MAX_WORKFLOWS`; leave each existing value unchanged. The pipeline resource helper limits test workers, while preserving queued workflows and required wildcard status checks.

Keep the existing mount and append this read-only mount to **every** QA agent's `WOODPECKER_BACKEND_DOCKER_VOLUMES`:

```
/var/cache/woodpecker/package-cache:/woodpecker-cache,/var/cache/woodpecker/ci-resources:/ci-resources:ro
```

Create `/var/cache/woodpecker/ci-resources` owned by root with mode 0755. Install `ops/ci-workflow-snapshot.py` from the reviewed infra commit at `/opt/agent-infra/ops/ci-workflow-snapshot.py`. Run it from a host timer every 15 seconds with a 15-second service timeout:

```
python3 /opt/agent-infra/ops/ci-workflow-snapshot.py --agent-ids 8,9,15,16,17,19 --output /var/cache/woodpecker/ci-resources/workflows.tsv
```

Supply `WOODPECKER_SERVER=https://ci-gitea.gen.pro` and the **existing** admin token as `WOODPECKER_TOKEN` in the host service's root-only environment. No new credential or rotation is needed. Do not mount that environment file or pass this token to CI. The publisher fetches the admin queue once with a ten-second HTTP timeout, atomically replaces public counts and exits; a failed publication expires after sixty seconds.

Agent IDs 8,9,15,16,17 are verified from QA's mounted registration files; the DAY containers share ID 8. ID 19 is the current unnamed running gen-agentic workflow observed on QA. Recheck `/api/agents`, registration files and the executing workflow's host before enabling this list: IDs are state, not durable identity. Exclude the retired rw1 agent 18 and mav agent 20. If placement changes, update the host publisher list, not per-repo CI code. The single published row is named `qa-deploy-box`; CI selects it with `CI_WORKER_AGENT_IDS=qa-deploy-box`.

Apply mounts only through the approved infrastructure maintenance path; do not restart live agents while they own workflows. No host settings, services, containers or credentials are modified by the lane.

## Partition and recovery contract

Static workflow maxima are four for gen-agentic/frontend and twelve for Rails. The first test workflow freezes a resource-derived active count at `/woodpecker-cache/ci-shards/<repo>/<pipeline>-<rerun>`; every sibling uses the same count and skips indexes above it. A malformed plan or a lock still held after twenty seconds fails CI. Never recompute a partition independently. Rails cron retains twelve shards because its timing publisher requires all twelve artifacts. Test workers inside an active shard probe current resources again and can grow or shrink without moving test ownership.

Cache retention may remove plan files only after their pipelines have terminated; removing a live plan allows a later shard to recompute ownership. Ordinary dependency-cache cleanup must exclude `/woodpecker-cache/ci-shards` while jobs are active. No cleanup daemon is introduced here.
