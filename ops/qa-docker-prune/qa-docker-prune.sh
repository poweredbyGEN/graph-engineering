#!/usr/bin/env bash
# The scheduled and compatibility entry points share one owner and one lock.
set -euo pipefail
exec /usr/local/sbin/qa-disk-prune "$@"
