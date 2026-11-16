#!/usr/bin/env bash
# Render the unit templates in deploy/vm/units and install them as system units.
#
#   sudo deploy/vm/install-services.sh \
#        --user muse --root /opt/muse/current --venv /opt/muse/current/venv \
#        --state /var/lib/muse [--units "muse-home muse-queue ..."] [--enable] [--dest DIR]
#
# Used by first boot (deploy/vm/bootstrap.sh), by updates, and by the manual
# script (deploy/oracle-a1-setup.sh --services). It writes unit files and
# nothing else: no port is opened and no firewall rule is changed.
#
#   --user    the account the services run as (never root)
#   --root    the Muse code tree the units run from
#   --venv    the Python environment with the hash-locked install
#   --state   the one folder the services may write to (holds MUSE_HOME)
#   --base    where releases live, writable by the updater only (default: parent of --root)
#   --conf    settings file read by every unit (default /etc/muse/vm.conf)
#   --units   which units to install (default: all); names without suffix
#   --enable  also enable and start them (needs systemd)
#   --dest    where to write the unit files (default /etc/systemd/system)

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/vm/lib.sh
. "$HERE/lib.sh"

RUN_USER="" ROOT="" VENV="" STATE="" BASE="" CONF="/etc/muse/vm.conf"
UNITS="" ENABLE="no" DEST="/etc/systemd/system" SETUP="/var/lib/muse-setup"
while [ $# -gt 0 ]; do
  case "$1" in
    --user) shift; RUN_USER="${1:?}" ;;
    --root) shift; ROOT="${1:?}" ;;
    --venv) shift; VENV="${1:?}" ;;
    --state) shift; STATE="${1:?}" ;;
    --base) shift; BASE="${1:?}" ;;
    --conf) shift; CONF="${1:?}" ;;
    --units) shift; UNITS="${1:?}" ;;
    --dest) shift; DEST="${1:?}" ;;
    --setup) shift; SETUP="${1:?}" ;;
    --enable) ENABLE="yes" ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done
if [ -z "$RUN_USER" ] || [ -z "$ROOT" ] || [ -z "$VENV" ] || [ -z "$STATE" ]; then
  die "--user, --root, --venv and --state are required"
fi
[ "$RUN_USER" != "root" ] || die "the services do not run as root"
[ -n "$BASE" ] || BASE="$(dirname "$ROOT")"
for path in "$ROOT" "$VENV" "$STATE" "$BASE" "$CONF" "$DEST" "$SETUP"; do
  case "$path" in /*) ;; *) die "paths must be absolute: $path" ;; esac
  case "$path" in *[[:space:]@\|\&]*) die "unsupported character in path: $path" ;; esac
done

# A code tree or state folder under /home is hidden by ProtectHome=yes, so the
# manual layout (a clone in the user's home) gets read-only instead.
PROTECT_HOME="yes"
case "$ROOT $VENV $STATE" in /home/*|*" /home/"*|/root/*|*" /root/"*) PROTECT_HOME="read-only" ;; esac

ALL="muse-proxy.service muse-home.service muse-queue.service muse-chores.service muse-chores.timer
muse-catalog-check.service muse-catalog-check.timer muse-backup.service muse-backup.timer
muse-update.service muse-update.timer muse-heartbeat.service muse-heartbeat.timer muse-vm-monitor.service"
if [ -z "$UNITS" ]; then
  FILES="$ALL"
else
  FILES=""
  for name in $UNITS; do
    found=""
    for file in $ALL; do
      case "$file" in "$name".service|"$name".timer) FILES="$FILES $file"; found=1 ;; esac
    done
    [ -n "$found" ] || die "no such unit: $name"
  done
fi

mkdir -p "$DEST"
for file in $FILES; do
  src="$HERE/units/$file"
  [ -f "$src" ] || die "missing template: $src"
  tmp="$DEST/.$file.tmp.$$"
  sed -e "s|@MUSE_USER@|$RUN_USER|g" -e "s|@MUSE_ROOT@|$ROOT|g" -e "s|@MUSE_VENV@|$VENV|g" \
      -e "s|@MUSE_STATE@|$STATE|g" -e "s|@MUSE_BASE@|$BASE|g" -e "s|@MUSE_CONF@|$CONF|g" \
      -e "s|@PROTECT_HOME@|$PROTECT_HOME|g" "$src" > "$tmp"
  if grep -q '@[A-Z_]*@' "$tmp"; then rm -f "$tmp"; die "unfilled placeholder in $file"; fi
  chmod 0644 "$tmp"
  mv -f "$tmp" "$DEST/$file"
  echo "installed $DEST/$file"
done

if [ "$ENABLE" = "yes" ]; then
  systemctl daemon-reload
  for file in $FILES; do
    case "$file" in
      *.timer) systemctl enable --now "$file" ;;
      *.service)
        # A service driven by a timer is started by its timer, not at boot.
        if [ -f "$HERE/units/${file%.service}.timer" ]; then continue; fi
        systemctl enable --now "$file" ;;
    esac
  done
fi
