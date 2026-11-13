#!/usr/bin/env bash
# Weekly catalogue check (started by muse-catalog-check.timer).
#
# Runs `muse catalog check --pages --json` and keeps the result where the
# dashboard and the home API can show it later:
#
#   $MUSE_VM_STATE/catalog-check.json         the command's JSON output
#   $MUSE_VM_STATE/catalog-check.status.json  when it ran and its exit status
#
# The check is read-only towards the catalogue: it never edits it.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/vm/lib.sh
. "$HERE/lib.sh"
case "${1:-}" in -h|--help) sed -n '2,10p' "$0"; exit 0 ;; esac
load_conf

STATE="${MUSE_VM_STATE:-/var/lib/muse/vm}"
PY="${MUSE_PYTHON:-python3}"
mkdir -p "$STATE"
tmp="$STATE/catalog-check.json.tmp.$$"
trap 'rm -f "$tmp"' EXIT

status=0
"$PY" -m muse catalog check --pages --json > "$tmp" || status=$?
if [ -s "$tmp" ]; then
  mv -f "$tmp" "$STATE/catalog-check.json"
fi
write_json "$STATE/catalog-check.status.json" checked_at "$(utc_now)" exit_status "$status"
# A non-zero status means "something in the catalogue needs a look", which is a
# finding, not a failure of this job.
exit 0
