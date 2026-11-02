# QA disk reclamation

The QA host has a root filesystem and a separate Docker data disk. A successful
Docker prune does not establish that root has recovered. The scheduled owner is
`qa-docker-prune.timer`, every 15 minutes, invoking `qa-docker-prune.sh` and
`/usr/local/sbin/qa-disk-prune` from `ops/qa-docker-prune/qa-disk-prune.py`.

At 75% root use (including ext4 reserved-space effects), cleanup selects artifacts
at least 24 hours old and stops below 69%. One flock excludes concurrent runs.
Logs go to `journalctl -u qa-docker-prune.service`; each removal records path,
freed-byte delta and resulting root capacity. No alert transport is added.

Docker cache cleanup uses the documented `until=24h` filter and skips when a local
Docker build runs. Containers require explicit disposable ownership through
`qa.disk-prune=true` and must have no Compose label. Images used by any container,
locally built CI images and Woodpecker images are retained. Volume operations are
prohibited. Root tiers cover Woodpecker dependency caches, inactive QA scratch,
stale worktree dependency copies, non-current workflow releases and rotated logs.

Every filesystem candidate must be on root, old, not a symlink, without open
files, active process references or container mounts. Directories with recent
writes, secret material or database markers remain. Failed activity/size scans
retain data. The live workflow release and five newest releases remain.
Uncommitted source and service/database directories are outside deletion tiers.

Disable the overlapping `fleet-disk-guard`, `fs-disk-guard`, `gen-ci-disk-guard`,
`qa-release-prune`, `woodpecker-ci-reclaim` and `qa-evidence-reaper` timers when installing this owner;
their scripts remain for inspection. Do not interrupt a running maintenance job. `ops/qa-docker-prune/install.sh` performs the timer consolidation and installation.

Existing Rails log rotation uses daily `copytruncate` with maxsize 1G. It can grow
past that size between checks. This cleanup never truncates an open log or
changes that policy. Protected data growth can exhaust capacity; no finite
prune policy can guarantee capacity while preserving all live files.

Published patterns:
- https://docs.docker.com/reference/cli/docker/builder/prune/
- https://docs.docker.com/reference/cli/docker/image/prune/
- https://docs.docker.com/engine/manage-resources/pruning/
- https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html

Validation: `python3 -m unittest discover -s ops/qa-docker-prune -p 'test_qa_disk_prune.py'`.
