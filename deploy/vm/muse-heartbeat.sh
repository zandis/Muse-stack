#!/usr/bin/env bash
# Ping the optional 5-minute healthchecks URL. Off when that file is absent.
# The URL is a secret: it is never printed. See docs/deploy/update.md.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/vm/lib.sh
. "$HERE/lib.sh"
load_conf

case "${1:-}" in
  -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
  "") ;;
  *) die "unknown option: $1" ;;
esac

ping_healthchecks success "${MUSE_HEARTBEAT_URL_FILE:-/etc/muse/heartbeat.url}"
