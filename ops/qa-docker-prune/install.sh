#!/usr/bin/env bash
# One scheduled owner prevents old jobs from deleting build state or volumes.
set -euo pipefail
src=$(cd -- "$(dirname -- "$0")" && pwd)
for unit in fleet-disk-guard fs-disk-guard gen-ci-disk-guard qa-release-prune woodpecker-ci-reclaim qa-evidence-reaper; do
  systemctl disable --now "$unit.timer"
done
install -m 755 "$src/qa-disk-prune.py" /usr/local/sbin/qa-disk-prune
install -m 755 "$src/qa-docker-prune.sh" /usr/local/bin/qa-docker-prune.sh
install -m 644 "$src/qa-docker-prune.service" /etc/systemd/system/qa-docker-prune.service
install -m 644 "$src/qa-docker-prune.timer" /etc/systemd/system/qa-docker-prune.timer
systemctl daemon-reload
systemctl enable --now qa-docker-prune.timer
systemctl list-timers --all --no-pager | grep -E 'prune|disk-guard|reaper|reclaim' || true
