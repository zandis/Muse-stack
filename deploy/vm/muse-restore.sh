#!/usr/bin/env bash
# Rebuild MUSE_HOME from a backup: fetch, verify, decrypt, check, put in place, start.
#
#   sudo muse-restore.sh --identity KEYFILE --yes              # the latest backup
#   sudo muse-restore.sh --identity KEYFILE --from FILE --yes  # a named local file
#
#   --identity FILE   the age private key that opens the backup. Until pairing
#                     exists this is the key the old VM generated
#                     (MUSE_HOME/backup-key/identity.txt there). A backup made
#                     with a VM-generated key can only be restored if that file
#                     was copied off the VM beforehand.
#   --from FILE       an encrypted backup on this machine (default: the latest
#                     in the bucket, or the latest local one when no bucket is set)
#   --target DIR      where MUSE_HOME goes (default: MUSE_HOME from /etc/muse/vm.conf)
#   --with-keys       also restore provider keys, when the backup holds them
#   --no-services     do not stop or start services (tests, or a machine without them)
#   --yes             really do it; without this the backup is only checked
#
# What happens, in order, and it stops at the first failure with the existing
# MUSE_HOME untouched:
#   1. The SHA-256 written next to the backup is checked.
#   2. The file is decrypted with the identity.
#   3. Every file is checked against the manifest inside the backup, and every
#      database with SQLite's integrity check.
#   4. Services stop; the current MUSE_HOME, if any, is moved aside to
#      <MUSE_HOME>.before-restore-<time> (never deleted); the restored one takes
#      its place; services start.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/vm/lib.sh
. "$HERE/lib.sh"
load_conf

IDENTITY="" FROM="" TARGET="${MUSE_HOME:-/var/lib/muse/home}" WITH_KEYS="no" SERVICES="yes" YES="no"
while [ $# -gt 0 ]; do
  case "$1" in
    --identity) shift; IDENTITY="${1:?--identity needs a file}" ;;
    --from) shift; FROM="${1:?--from needs a file}" ;;
    --target) shift; TARGET="${1:?--target needs a folder}" ;;
    --with-keys) WITH_KEYS="yes" ;;
    --no-services) SERVICES="no" ;;
    --yes) YES="yes" ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

PY="${MUSE_PYTHON:-python3}"
STATE="${MUSE_VM_STATE:-/var/lib/muse/vm}/backup"
BUCKET="${MUSE_BACKUP_BUCKET:-}"
PREFIX="backups/"
[ -n "$IDENTITY" ] || die "--identity FILE is required (the age private key that opens the backup)"
[ -r "$IDENTITY" ] || die "cannot read the identity file: $IDENTITY"
command -v age >/dev/null 2>&1 || die "age is not installed"

umask 077
mkdir -p "$STATE"
WORK="$(mktemp -d "$STATE/.restore.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
oci() { "$PY" "$HERE/oci_object.py" "$@"; }

# 1. Find the backup.
if [ -n "$FROM" ]; then
  [ -f "$FROM" ] || die "no such file: $FROM"
  ARCHIVE="$FROM"
elif [ -n "$BUCKET" ]; then
  latest="$(oci list "$PREFIX" | "$PY" -c '
import json, sys
names = [json.loads(line).get("name", "") for line in sys.stdin if line.strip()]
names = sorted(n for n in names if n.startswith("backups/muse-backup-") and n.endswith(".tar.gz.age"))
print(names[-1] if names else "")')"
  [ -n "$latest" ] || die "the bucket holds no backup"
  ARCHIVE="$WORK/$(basename "$latest")"
  oci get "$latest" "$ARCHIVE"
  oci get "$latest.sha256" "$ARCHIVE.sha256" || die "the backup has no SHA-256 file in the bucket"
  say "fetched $latest"
else
  latest="$(find "$STATE/archives" -maxdepth 1 -name 'muse-backup-*.tar.gz.age' -printf '%f\n' 2>/dev/null | sort | tail -1)"
  [ -n "$latest" ] || die "no bucket is set and there is no local backup in $STATE/archives"
  ARCHIVE="$STATE/archives/$latest"
fi

# 2. Verify the checksum written at backup time.
if [ -f "$ARCHIVE.sha256" ]; then
  expected="$(cut -d' ' -f1 < "$ARCHIVE.sha256")"
  valid_sha256 "$expected" || die "the SHA-256 file next to the backup is not a SHA-256"
  [ "$(sha256_of "$ARCHIVE")" = "$expected" ] || die "the backup does not match its SHA-256; it is damaged or was changed"
  say "checksum matches"
else
  [ -n "$FROM" ] || die "the backup has no SHA-256 file"
  warn "no .sha256 file next to $ARCHIVE; relying on age's own integrity check and the manifest"
fi

# 3. Decrypt and check. age authenticates the file: a wrong key or a changed byte fails here.
age --decrypt --identity "$IDENTITY" --output "$WORK/stage.tar.gz" "$ARCHIVE" \
  || die "could not decrypt the backup with this identity"
NEW="$WORK/home.new"
keys_args=()
[ "$WITH_KEYS" = "yes" ] && keys_args=(--keys-out "$WORK/muse.env")
SUMMARY="$("$PY" "$HERE/backup_tool.py" restore --tar "$WORK/stage.tar.gz" --target "$NEW" "${keys_args[@]}")" \
  || die "the backup's contents failed verification; nothing was changed"
rm -f "$WORK/stage.tar.gz"
say "verified: $SUMMARY"

if [ "$YES" != "yes" ]; then
  say "check only: add --yes to put it in place (the current MUSE_HOME would be moved aside, not deleted)"
  exit 0
fi

# 4. Swap it in.
UNITS="muse-chores.timer muse-backup.timer muse-queue.service muse-home.service muse-proxy.service"
if [ "$SERVICES" = "yes" ]; then
  # shellcheck disable=SC2086
  systemctl stop $UNITS 2>/dev/null || true
fi
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$(dirname "$TARGET")"
if [ -e "$TARGET" ]; then
  # This machine's own backup key stays with this machine.
  if [ -d "$TARGET/backup-key" ]; then cp -a "$TARGET/backup-key" "$NEW/backup-key"; fi
  mv "$TARGET" "$TARGET.before-restore-$STAMP"
  say "the previous MUSE_HOME is kept at $TARGET.before-restore-$STAMP"
fi
mv "$NEW" "$TARGET"
chmod 700 "$TARGET"

if [ "$WITH_KEYS" = "yes" ] && [ -f "$WORK/muse.env" ]; then
  ENV_FILE="${MUSE_ENV_FILE:-/var/lib/muse/muse.env}"
  if [ -e "$ENV_FILE" ]; then mv "$ENV_FILE" "$ENV_FILE.before-restore-$STAMP"; fi
  install -m 600 "$WORK/muse.env" "$ENV_FILE"
  say "provider keys restored to $ENV_FILE"
fi

if [ "$(id -u)" -eq 0 ] && id "${MUSE_USER:-muse}" >/dev/null 2>&1; then
  chown -R "${MUSE_USER:-muse}:" "$TARGET"
  [ -n "${ENV_FILE:-}" ] && chown "${MUSE_USER:-muse}:" "$ENV_FILE"
fi

write_json "$STATE/last-restore.json" finished_at "$(utc_now)" result ok from "$(basename "$ARCHIVE")"
if [ "$SERVICES" = "yes" ]; then
  # shellcheck disable=SC2086
  systemctl start $UNITS
  say "services started"
fi
say "restored $(basename "$ARCHIVE") into $TARGET"
