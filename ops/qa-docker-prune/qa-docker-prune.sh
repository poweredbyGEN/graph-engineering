#!/usr/bin/env bash
# Reclaim CI leftovers on qa-deploy-box (hourly).
#
# This box hosts Gitea, Woodpecker, staging and lane tests on one root disk.
# A full root disk stops gitea-db from writing and every git fetch/push and API
# call returns 500 (2026-10-11: / at 100%, 0 bytes free, forge down ~10 min).
#
# Only unreferenced objects are touched: a volume attached to any container,
# an image used by any container and an in-flight build are never candidates,
# so a running CI job cannot lose its workspace. Normal runs keep images for 72h;
# above 85% use the window drops to 24h. Journals are capped at 500M.
set -uo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
LOG=/var/log/qa-docker-prune.log
exec >>"$LOG" 2>&1
use=$(df --output=pcent / | tail -1 | tr -dc 0-9)
keep=72h; [ "${use:-0}" -ge 85 ] && keep=24h
echo "===== $(date -u) prune start (root ${use}%, keep ${keep}) ====="
docker volume prune -f 2>&1 | tail -1
# Locally built test images (*-qa*, *cifix*) are rebuilt from nothing and are
# what qa-pytest and every lane runs on, so they are never pruned; only
# dangling images and unused registry images older than the window go.
docker image prune -f 2>&1 | tail -1
cutoff=$(date -d "-${keep%h} hours" +%s)
in_use=$(docker ps -a --format "{{.Image}}" | sort -u)
docker images --format "{{.Repository}}:{{.Tag}} {{.ID}} {{.CreatedAt}}" | while read -r ref id created _; do
  # Pre-baked CI images (/opt/ci-images: limitless-data-ci, gbv2-ci, day-ci,
  # xel-ci-*, ...) are built locally with no registry and Woodpecker runs them with
  # pull: false; they sit idle between runs, so age-based removal breaks every
  # pipeline of that repo until someone rebuilds by hand (GEN-9831).
  case "$ref" in *-qa*|*cifix*|*-ci:*|*-ci-*:*|*"<none>"*) continue ;; esac
  echo "$in_use" | grep -qxF "$ref" && continue
  [ "$(date -d "$created" +%s 2>/dev/null || echo 9999999999)" -lt "$cutoff" ] && docker rmi "$ref" >/dev/null 2>&1 && echo "removed $ref"
done
docker builder prune -f --filter "until=${keep}" 2>&1 | tail -1
journalctl --vacuum-size=500M 2>&1 | tail -1
df -h / | tail -1
after=$(df --output=pcent / | tail -1 | tr -dc 0-9)
[ "${after:-0}" -ge 90 ] && logger -t qa-docker-prune "root disk still ${after}% after prune; needs a person"
echo "===== $(date -u) prune done ====="
