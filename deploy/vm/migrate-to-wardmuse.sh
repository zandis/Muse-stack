#!/usr/bin/env bash
# Point a VM that still uses Muse paths at the Wardmuse paths.
#
# Nothing here edits an env file or a secret. Old directories stay. When the
# new path is missing, it becomes a symlink to the old one, so both names are
# the same files. See docs/deploy/update.md, "Moving a VM from Muse paths".
#
#   sudo deploy/vm/migrate-to-wardmuse.sh --dry-run
#   sudo deploy/vm/migrate-to-wardmuse.sh --yes
#   sudo deploy/vm/migrate-to-wardmuse.sh --yes --units
#   sudo deploy/vm/migrate-to-wardmuse.sh --yes --containers
#
# --root DIR writes under DIR instead of / so the steps can be checked without
# being root. --home DIR, with --yes, symlinks DIR's sibling .wardmuse name
# only when you pass the old home (for example ~/.muse).
#
# The container name changes only with --containers:
#   muse-stack-litellm-1  ->  wardmuse-litellm-1

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/vm/lib.sh
. "$HERE/lib.sh"

DRY="no" YES="no" DO_UNITS="no" DO_CONTAINERS="no" PREFIX="" SRC_HOME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY="yes" ;;
    --yes) YES="yes" ;;
    --units) DO_UNITS="yes" ;;
    --containers) DO_CONTAINERS="yes" ;;
    --root) shift; PREFIX="${1:?--root needs a directory}"; PREFIX="${PREFIX%/}" ;;
    --home) shift; SRC_HOME="${1:?--home needs a directory}" ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

if [ "$DRY" = "yes" ] && [ "$YES" = "yes" ]; then
  die "pass --dry-run or --yes, not both"
fi
if [ "$DRY" != "yes" ] && [ "$YES" != "yes" ]; then
  die "pass --dry-run or --yes"
fi
if [ "$(id -u)" -ne 0 ] && [ -z "$PREFIX" ] && [ -z "${MUSE_MIGRATE_ALLOW_UNPRIV:-}" ]; then
  die "migration runs as root (it writes /opt/wardmuse, /etc/wardmuse and /var/lib/wardmuse)"
fi

OPT="$PREFIX/opt"
ETC="$PREFIX/etc"
VAR="$PREFIX/var/lib"

link_one() {  # new old-candidates...
  local new="$1" old chosen
  shift
  if [ -e "$new" ] || [ -L "$new" ]; then
    say "keep $new (already present)"
    return 0
  fi
  chosen=""
  for old in "$@"; do
    if [ -e "$old" ] || [ -L "$old" ]; then chosen="$old"; break; fi
  done
  if [ -z "$chosen" ]; then
    say "skip $new (no old directory to point at)"
    return 0
  fi
  if [ "$DRY" = "yes" ]; then
    say "dry run: ln -s $chosen $new"
    return 0
  fi
  mkdir -p "$(dirname "$new")"
  ln -s "$chosen" "$new"
  say "linked $new -> $chosen"
}

say "Wardmuse path migration"
say "container rename, only with --containers: muse-stack-litellm-1 -> wardmuse-litellm-1"
say "env files are copied only as symlinks of their directory; their bytes are not changed"

link_one "$OPT/wardmuse" "$OPT/muse" "$OPT/muse-stack"
link_one "$ETC/wardmuse" "$ETC/muse"
link_one "$VAR/wardmuse" "$VAR/muse"

if [ -n "$SRC_HOME" ]; then
  home_new="$(dirname "$SRC_HOME")/.wardmuse"
  if [ "$(basename "$SRC_HOME")" = ".muse" ]; then
    link_one "$home_new" "$SRC_HOME"
  else
    say "skip home symlink: --home must be a directory named .muse"
  fi
fi

if [ "$DO_UNITS" = "yes" ]; then
  if [ "$DRY" = "yes" ]; then
    say "dry run: install wardmuse-* units and disable muse-* units"
  else
    root="$(readlink -f "$OPT/wardmuse/current" 2>/dev/null || true)"
    [ -n "$root" ] && [ -d "$root" ] || die "no $OPT/wardmuse/current to install units from"
    conf="$(resolve_vm_conf)"
    state="$(wm_dir STATE_ROOT "$VAR/wardmuse" "$VAR/muse")"
    bash "$root/deploy/vm/install-services.sh" --names wardmuse --user "${MUSE_USER:-muse}" \
      --root "$root" --venv "$root/venv" --state "$state" --base "$(dirname "$root")" \
      --conf "$conf" --enable
    if command -v systemctl >/dev/null 2>&1; then
      # shellcheck disable=SC2086
      systemctl disable --now muse-proxy.service muse-home.service muse-queue.service \
        muse-vm-monitor.service muse-chores.timer muse-catalog-check.timer \
        muse-backup.timer muse-update.timer muse-heartbeat.timer >/dev/null 2>&1 || true
    fi
    say "wardmuse-* units installed; muse-* units disabled"
  fi
fi

if [ "$DO_CONTAINERS" = "yes" ]; then
  if [ "$DRY" = "yes" ]; then
    say "dry run: docker compose -p muse-stack stop, then -p wardmuse up -d"
  else
    dir="$(readlink -f "$OPT/wardmuse/current" 2>/dev/null || true)"
    [ -n "$dir" ] && [ -f "$dir/docker-compose.yml" ] || die "no compose file under $OPT/wardmuse/current"
    ( cd "$dir" && docker compose -p muse-stack stop )
    ( cd "$dir" && docker compose -p wardmuse up -d )
    say "compose project is wardmuse; container wardmuse-litellm-1"
  fi
fi

if [ "$DRY" = "yes" ]; then
  say "dry run: nothing was written"
fi
