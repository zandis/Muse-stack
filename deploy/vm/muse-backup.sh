#!/usr/bin/env bash
# Daily backup of MUSE_HOME (started by muse-backup.timer; runs as the service user).
#
#   muse-backup.sh                      # stage, encrypt, keep locally, upload if a bucket is set
#   muse-backup.sh --stage-only FILE    # write only the plain stage to FILE (tests; never uploaded)
#
# Steps:
#   1. A consistent copy of MUSE_HOME is staged without stopping anything
#      (SQLite's online backup API). deploy/vm/backup_tool.py lists exactly
#      what goes in and what is left out.
#   2. The stage is encrypted with age to ONE public key, before it is written
#      anywhere lasting:
#        - MUSE_BACKUP_RECIPIENT, the key a paired device supplies, when set;
#        - otherwise a key generated on this VM, whose private half stays in
#          MUSE_HOME/backup-key/ and is never part of a backup. That protects
#          the backup against exposure of the bucket only. It does NOT help if
#          this VM is lost, because the only key that opens the backup is lost
#          with it.
#   3. The encrypted file and its SHA-256 stay in MUSE_VM_STATE/backup/archives
#      (the newest MUSE_BACKUP_KEEP_LOCAL, default 3).
#   4. When MUSE_BACKUP_BUCKET is set, both are uploaded to that bucket under
#      backups/ with the instance's own identity (deploy/vm/oci_object.py; no
#      stored API key), and all but the newest MUSE_BACKUP_KEEP (default 14)
#      are deleted there. With no bucket the backup is local only, which the
#      result file says in plain words.
#
# Provider keys (the dotenv file MUSE_ENV_FILE) are left out unless
# MUSE_BACKUP_INCLUDE_KEYS=1; then they travel inside the same encrypted file.
#
# The result is written to MUSE_VM_STATE/backup/last.json for muse-vm-status.
# Nothing unencrypted is ever uploaded: without age, or without a recipient,
# this stops.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/vm/lib.sh
. "$HERE/lib.sh"
load_conf

STAGE_ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --stage-only) shift; STAGE_ONLY="${1:?--stage-only needs a file name}" ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

HOME_DIR="${MUSE_HOME:-/var/lib/muse/home}"
STATE="${MUSE_VM_STATE:-/var/lib/muse/vm}/backup"
ARCHIVES="$STATE/archives"
PY="${MUSE_PYTHON:-python3}"
KEEP="${MUSE_BACKUP_KEEP:-14}"
KEEP_LOCAL="${MUSE_BACKUP_KEEP_LOCAL:-3}"
MAX_MB="${MUSE_BACKUP_MAX_MB:-512}"
BUCKET="${MUSE_BACKUP_BUCKET:-}"
PREFIX="backups/"
RELEASE="$(basename "$(readlink -f "${MUSE_ROOT:-/opt/muse/current}" 2>/dev/null || echo unknown)")"

case "$KEEP$KEEP_LOCAL$MAX_MB" in *[!0-9]*) die "MUSE_BACKUP_KEEP, MUSE_BACKUP_KEEP_LOCAL and MUSE_BACKUP_MAX_MB must be whole numbers" ;; esac
[ "$KEEP" -ge 1 ] && [ "$KEEP_LOCAL" -ge 1 ] || die "at least one backup must be kept"
[ -d "$HOME_DIR" ] || die "MUSE_HOME ($HOME_DIR) does not exist"

stage_args=(stage --home "$HOME_DIR" --release "$RELEASE")
INCLUDES_KEYS="false"
if [ "${MUSE_BACKUP_INCLUDE_KEYS:-0}" = "1" ] && [ -n "${MUSE_ENV_FILE:-}" ] && [ -f "$MUSE_ENV_FILE" ]; then
  stage_args+=(--env-file "$MUSE_ENV_FILE")
  INCLUDES_KEYS="true"
fi

if [ -n "$STAGE_ONLY" ]; then
  umask 077
  "$PY" "$HERE/backup_tool.py" "${stage_args[@]}" --out "$STAGE_ONLY"
  exit 0
fi

umask 077
mkdir -p "$ARCHIVES"
record() {  # result detail [more key value pairs]
  local result="$1" detail="$2"; shift 2
  write_json "$STATE/last.json" finished_at "$(utc_now)" result "$result" detail "$detail" "$@"
}

case "${MUSE_BACKUP:-on}" in
  off|0|false|no) record "disabled" "Backups are switched off (MUSE_BACKUP=off)."; say "backups are off"; exit 0 ;;
esac

command -v age >/dev/null 2>&1 || { record "failed" "age is not installed; nothing was written."; die "age is not installed"; }

# Who can read the backup.
RECIPIENT="${MUSE_BACKUP_RECIPIENT:-}"
READER="the key supplied by a paired device"
if [ -z "$RECIPIENT" ]; then
  KEYDIR="$HOME_DIR/backup-key"
  if [ ! -s "$KEYDIR/identity.txt" ]; then
    mkdir -p "$KEYDIR"; chmod 700 "$KEYDIR"
    # age-keygen prints the public key on standard error; that is public, but keep the log quiet anyway.
    age-keygen -o "$KEYDIR/identity.txt" 2>/dev/null
    chmod 600 "$KEYDIR/identity.txt"
  fi
  age-keygen -y "$KEYDIR/identity.txt" > "$KEYDIR/recipient.txt"
  RECIPIENT="$(cat "$KEYDIR/recipient.txt")"
  READER="a key generated on this VM (protects against bucket exposure only, not against losing the VM)"
fi
case "$RECIPIENT" in age1*) ;; *) record "failed" "The backup recipient is not an age public key."; die "MUSE_BACKUP_RECIPIENT must be an age public key (age1...)" ;; esac

WORK="$(mktemp -d "$STATE/.work.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="muse-backup-$STAMP.tar.gz.age"
SUMMARY="$("$PY" "$HERE/backup_tool.py" "${stage_args[@]}" --out "$WORK/stage.tar.gz")" \
  || { record "failed" "Could not make a consistent copy of MUSE_HOME."; die "staging failed"; }
age --encrypt --recipient "$RECIPIENT" --output "$WORK/$NAME" "$WORK/stage.tar.gz" \
  || { record "failed" "Encryption failed; nothing was kept."; die "age failed"; }
rm -f "$WORK/stage.tar.gz"
SHA="$(sha256_of "$WORK/$NAME")"
SIZE="$(stat -c %s "$WORK/$NAME")"
printf '%s  %s\n' "$SHA" "$NAME" > "$WORK/$NAME.sha256"
mv -f "$WORK/$NAME" "$WORK/$NAME.sha256" "$ARCHIVES/"
say "backup $NAME ($SIZE bytes): $SUMMARY"

# Keep the newest few locally. Names sort by time.
mapfile -t local_all < <(find "$ARCHIVES" -maxdepth 1 -name 'muse-backup-*.tar.gz.age' -printf '%f\n' | sort)
if [ "${#local_all[@]}" -gt "$KEEP_LOCAL" ]; then
  for old in "${local_all[@]:0:${#local_all[@]}-KEEP_LOCAL}"; do
    rm -f "$ARCHIVES/$old" "$ARCHIVES/$old.sha256"
  done
fi

common=(name "$NAME" size_bytes "$SIZE" sha256 "$SHA" readable_by "$READER" includes_provider_keys "$INCLUDES_KEYS" kept_local "$KEEP_LOCAL")

if [ -z "$BUCKET" ]; then
  record "local-only" "No backup bucket is set, so this backup exists only on this VM. If the VM is lost, so is the backup." \
    "${common[@]}" uploaded false
  warn "no bucket: the backup is on this VM only"
  exit 0
fi

if [ "$SIZE" -gt $((MAX_MB * 1024 * 1024)) ]; then
  record "local-only" "The backup is larger than MUSE_BACKUP_MAX_MB ($MAX_MB MB) and was not uploaded, to stay inside the free storage allowance." \
    "${common[@]}" uploaded false
  die "backup too large to upload ($SIZE bytes)"
fi

oci() { "$PY" "$HERE/oci_object.py" "$@"; }
if ! oci put "$ARCHIVES/$NAME" "$PREFIX$NAME" || ! oci put "$ARCHIVES/$NAME.sha256" "$PREFIX$NAME.sha256"; then
  record "upload-failed" "The backup was made and kept on this VM, but the upload to the bucket failed. See: journalctl -u muse-backup" \
    "${common[@]}" uploaded false bucket "$BUCKET"
  die "upload failed"
fi

# Keep the newest KEEP in the bucket.
pruned=0
if listing="$(oci list "$PREFIX")"; then
  mapfile -t remote_all < <(printf '%s\n' "$listing" | "$PY" -c '
import json, sys
names = []
for line in sys.stdin:
    line = line.strip()
    if line:
        name = json.loads(line).get("name", "")
        if name.startswith("backups/muse-backup-") and name.endswith(".tar.gz.age"):
            names.append(name)
print("\n".join(sorted(names)))' | sed '/^$/d')
  if [ "${#remote_all[@]}" -gt "$KEEP" ]; then
    for old in "${remote_all[@]:0:${#remote_all[@]}-KEEP}"; do
      if oci delete "$old"; then pruned=$((pruned + 1)); fi
      oci delete "$old.sha256" || true
    done
  fi
else
  warn "could not list the bucket; old backups were not pruned"
fi

record "ok" "Encrypted backup uploaded." "${common[@]}" uploaded true bucket "$BUCKET" kept_remote "$KEEP" pruned_remote "$pruned"
say "uploaded to bucket $BUCKET"
