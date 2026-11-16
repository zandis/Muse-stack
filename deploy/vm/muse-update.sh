#!/usr/bin/env bash
# Look for a new Muse release and install it (nightly, or by hand).
#
# OFF BY DEFAULT. Nothing happens until BOTH are set in /etc/muse/vm.conf:
#
#   MUSE_UPDATE_MANIFEST_URL   https address of the signed manifest
#   MUSE_UPDATE_PUBKEY         base64 of the raw Ed25519 public key
#
# MUSE_RUNTIME=docker is the production VM: a compose project, fed from a
# signed tarball. MUSE_RUNTIME=native (the default) is the venv layout.
#
# Docker, stopping at the first failure:
#   1. Fetch the manifest and its signature over https. Check the signature,
#      the expiry, and that the version is fixed and not a downgrade.
#   2. Fetch the tarball and compare its SHA-256 with the signed manifest.
#   3. Take a lock, snapshot MUSE_HOME, pull, validate, and compose up.
#   4. Smoke: liveliness, an authenticated /v1/models, one cheap fast call.
#   5. On failure, compose the previous release and restore the snapshot.
#
#   sudo systemctl start muse-update.service
#   deploy/vm/muse-update.sh --rollback
#
# The result is MUSE_VM_STATE/update/last.json. Logs go to the journal.
# If the file named by MUSE_HEALTHCHECKS_URL_FILE holds an https URL, that
# URL is pinged. The URL is never printed.
#
# MUSE_UPDATE_PIN, when set to a version, refuses every other version.
# Set it after a hand rollback so the next night does not move forward again.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/vm/lib.sh
. "$HERE/lib.sh"
load_conf

case "${1:-}" in
  -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
  --rollback) DO_ROLLBACK="yes" ;;
  "") DO_ROLLBACK="no" ;;
  *) die "unknown option: $1 (try --help)" ;;
esac

BASE="$(wm_dir BASE /opt/wardmuse /opt/muse /opt/muse-stack)"
VM_STATE="$(wm_dir VM_STATE /var/lib/wardmuse/vm /var/lib/muse/vm)"
STATE="$VM_STATE/update"
RUN_USER="${MUSE_USER:-muse}"
STATE_ROOT="$(wm_dir STATE_ROOT /var/lib/wardmuse /var/lib/muse)"
if val="$(wm_env HOME)"; then HOME_DIR="$val"; else HOME_DIR="$STATE_ROOT/home"; fi
MANIFEST_URL="${MUSE_UPDATE_MANIFEST_URL:-}"
PUBKEY="${MUSE_UPDATE_PUBKEY:-}"
RUNTIME="${MUSE_RUNTIME:-native}"
export MUSE_HOME="$HOME_DIR"
CURRENT="$(current_release "$BASE")"

mkdir -p "$STATE"
record() {  # result detail [running]
  write_json "$STATE/last.json" checked_at "$(utc_now)" result "$1" detail "$2" running "${3:-$CURRENT}"
  if [ "$(id -u)" -eq 0 ] && id "$RUN_USER" >/dev/null 2>&1; then chown -R "$RUN_USER:" "$STATE" || true; fi
}
refuse() {
  record "refused" "$1"
  ping_healthchecks fail
  die "manifest refused"
}
fail() {
  record "failed" "$1"
  ping_healthchecks fail
  exit 1
}

case "$RUNTIME" in
  docker) PY="${MUSE_UPDATE_PYTHON:-python3}" ;;
  native) PY="$BASE/current/venv/bin/python" ;;
  *) record "refused" "MUSE_RUNTIME must be native or docker."; die "bad MUSE_RUNTIME" ;;
esac

acquire_lock() {
  mkdir -p "$STATE"
  touch "$STATE/muse-update.lock"
  exec 9>>"$STATE/muse-update.lock"
  if ! flock -n 9; then
    say "another update is already running"
    exit 0
  fi
}

# --- compose mode ---------------------------------------------------------
# Defined before they are called. They do not name the native switch lines,
# so the native order in this file stays checkable.

write_pending() {  # previous target snapshot
  "$PY" - "$STATE/pending.json" "$1" "$2" "$3" <<'PY'
import json, os, sys
path, previous, target, snapshot = sys.argv[1:]
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    json.dump({"previous": previous, "target": target, "snapshot": snapshot}, handle)
    handle.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, path)
PY
}

remove_release_dir() {
  local dir="$1" name
  [ -n "$dir" ] && [ -d "$dir" ] || return 0
  name="$(basename "$dir")"
  valid_version "$name" || return 0
  [ "$dir" = "$BASE/releases/$name" ] || return 0
  rm -rf "${dir:?}"
}

smoke_release() {
  "$PY" "$1/scripts/smoke_test.py" --only fast \
    --env-file "$MUSE_ENV_FILE" \
    --base-url "http://127.0.0.1:${MUSE_PORT:-4000}"
}

validate_release() {
  local dir="$1" out last inner="/opt/muse/scripts/validate_config.py"
  if [ -f "$dir/docker-compose.yml" ] && grep -q '/opt/wardmuse/scripts/' "$dir/docker-compose.yml"; then
    inner="/opt/wardmuse/scripts/validate_config.py"
  fi
  if ! out="$(compose_release "$dir" run --rm --no-deps --entrypoint python litellm "$inner" 2>&1)"; then
    return 1
  fi
  last="$(printf '%s\n' "$out" | awk 'NF { line = $0 } END { print line }' | tr -d '\r')"
  [ "$last" = "config ok" ]
}

rollback_docker() {  # previous snapshot failed_dir remove_failed(yes|no)
  local prev="$1" snap="$2" failed_dir="${3:-}" remove_failed="${4:-yes}"
  local prev_dir="$BASE/releases/$prev" up_ok=0 detail
  warn "going back to $prev"
  if [ -n "$failed_dir" ] && [ -d "$failed_dir" ]; then
    compose_release "$failed_dir" stop || true
  fi
  if [ -n "$snap" ] && [ -f "$snap" ]; then
    "$PY" "$HERE/update_snapshot.py" restore --tar "$snap" --home "$HOME_DIR" \
      || warn "could not restore the data snapshot"
  fi
  if [ -d "$prev_dir" ]; then
    link_release_env "$prev_dir" || true
    if compose_release "$prev_dir" up -d && wait_for_proxy "${MUSE_UPDATE_WAIT:-180}"; then
      up_ok=1
    fi
  fi
  switch_release "$BASE" "$prev" || true
  if [ "$up_ok" -eq 1 ]; then
    if [ "$remove_failed" = "yes" ]; then
      remove_release_dir "$failed_dir"
    fi
    rm -f "$STATE/pending.json"
    if [ -n "${ROLLBACK_REASON:-}" ]; then
      detail="$ROLLBACK_REASON"
    else
      detail="Release ${VERSION:-} did not stay up, so the VM went back to $prev and restored the data snapshot."
    fi
    record "rolled-back" "$detail" "$prev"
  else
    record "failed" "Release ${VERSION:-} did not stay up and going back to $prev did not bring the router up. See: journalctl -u muse-update" "$prev"
  fi
  ping_healthchecks fail
  exit 1
}

resume_pending() {
  [ -f "$STATE/pending.json" ] || return 0
  [ "$RUNTIME" = "docker" ] || { warn "a pending update was left behind; docker rollback was not attempted"; return 0; }
  local previous snapshot target
  previous="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["previous"])' "$STATE/pending.json")"
  snapshot="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["snapshot"])' "$STATE/pending.json")"
  target="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["target"])' "$STATE/pending.json")"
  VERSION="$target"
  warn "the previous update to $target did not finish; rolling back to $previous"
  rollback_docker "$previous" "$snapshot" "$BASE/releases/$target" yes
}

apply_docker() {
  if ! link_release_env "$NEW"; then
    remove_release_dir "$NEW"
    fail "Could not point release $VERSION at MUSE_ENV_FILE. Still running $CURRENT."
  fi
  if ! compose_release "$NEW" pull; then
    remove_release_dir "$NEW"
    fail "Could not pull the images for $VERSION. Still running $CURRENT."
  fi
  if ! validate_release "$NEW"; then
    remove_release_dir "$NEW"
    fail "Release $VERSION did not pass validation. Still running $CURRENT."
  fi
  mkdir -p "$STATE/snapshots" "$HOME_DIR"
  local snap="$STATE/snapshots/pending.tar"
  if ! "$PY" "$HERE/update_snapshot.py" snapshot --home "$HOME_DIR" --out "$snap"; then
    remove_release_dir "$NEW"
    fail "Could not snapshot data before switching. Still running $CURRENT."
  fi
  write_pending "$CURRENT" "$VERSION" "$snap"
  if ! compose_release "$NEW" up -d; then
    rollback_docker "$CURRENT" "$snap" "$NEW" yes
  fi
  if ! wait_for_proxy "${MUSE_UPDATE_WAIT:-180}" || ! smoke_release "$NEW"; then
    rollback_docker "$CURRENT" "$snap" "$NEW" yes
  fi
  target_version="$VERSION"
  switch_release "$BASE" "$target_version"
  rm -f "$STATE/pending.json"
  mkdir -p "$STATE/rollback"
  cp -f "$snap" "$STATE/rollback/home.tar"
  chmod 600 "$STATE/rollback/home.tar"
  printf '%s\n' "$CURRENT" > "$STATE/rollback/previous"
  chmod 600 "$STATE/rollback/previous"
  local dir name
  for dir in "$BASE"/releases/*; do
    [ -d "$dir" ] || continue
    name="$(basename "$dir")"
    if [ "$name" != "$VERSION" ] && [ "$name" != "$CURRENT" ]; then
      remove_release_dir "$dir"
    fi
  done
  record "updated" "Updated from $CURRENT to $VERSION." "$VERSION"
  ping_healthchecks success
  say "now running $VERSION"
  exit 0
}

manual_rollback() {
  [ "$RUNTIME" = "docker" ] || die "--rollback is for MUSE_RUNTIME=docker"
  if [ -f "$STATE/pending.json" ]; then
    resume_pending
  fi
  local prev snap
  prev="$(tr -d '[:space:]' < "$STATE/rollback/previous" 2>/dev/null || true)"
  snap="$STATE/rollback/home.tar"
  if [ -z "$prev" ] || [ ! -d "$BASE/releases/$prev" ] || [ ! -f "$snap" ]; then
    die "no previous release is recorded; nothing was changed"
  fi
  ROLLBACK_REASON="Rolled back by hand from $CURRENT to $prev. The next nightly run will move forward again unless MUSE_UPDATE_PIN=$prev is set in vm.conf."
  VERSION="$CURRENT"
  write_pending "$prev" "$CURRENT" "$snap"
  rollback_docker "$prev" "$snap" "$BASE/releases/$CURRENT" no
}

if [ "$DO_ROLLBACK" = "yes" ]; then
  acquire_lock
  manual_rollback
fi

if [ -z "$MANIFEST_URL" ] || [ -z "$PUBKEY" ]; then
  record "disabled" "Automatic Muse updates are off: no manifest address and signing key are set. Operating-system security updates are separate and on."
  say "updates are off (MUSE_UPDATE_MANIFEST_URL and MUSE_UPDATE_PUBKEY are not both set)"
  exit 0
fi
case "$MANIFEST_URL" in
  https://*) ;;
  *) record "refused" "The manifest address is not https."; die "MUSE_UPDATE_MANIFEST_URL must be https" ;;
esac
[ -n "$CURRENT" ] || { record "failed" "No running release found under $BASE."; die "no current release under $BASE"; }
if [ "$RUNTIME" = "native" ]; then
  [ -x "$PY" ] || { record "failed" "The running release has no Python environment."; die "no venv in the current release"; }
fi

acquire_lock
resume_pending

PUBKEYS="$PUBKEY"
if [ -n "${MUSE_UPDATE_PUBKEY_NEXT:-}" ]; then
  PUBKEYS="${PUBKEY},${MUSE_UPDATE_PUBKEY_NEXT}"
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fetch_https "$MANIFEST_URL" "$WORK/manifest.json" || { record "failed" "Could not fetch the manifest."; ping_healthchecks fail; die "manifest fetch failed"; }
fetch_https "$MANIFEST_URL.sig" "$WORK/manifest.json.sig" || { record "failed" "Could not fetch the manifest's signature."; ping_healthchecks fail; die "signature fetch failed"; }

if ! verdict="$("$PY" "$HERE/update_verify.py" "$WORK/manifest.json" "$WORK/manifest.json.sig" "$PUBKEYS" "$CURRENT" 2>"$WORK/verify.err")"; then
  detail="$(tail -n 1 "$WORK/verify.err" || true)"
  refuse "The manifest was refused. ${detail}"
fi
# shellcheck disable=SC2086
set -- $verdict
case "$1" in
  same)
    record "up-to-date" "Up to date."
    ping_healthchecks success
    say "up to date ($CURRENT)"
    exit 0
    ;;
  older) refuse "The manifest names an older release ($2); downgrades are refused." ;;
  newer) VERSION="$2"; URL="$3"; SHA="$4" ;;
  *) record "failed" "Unexpected answer from the manifest check."; ping_healthchecks fail; die "unexpected verdict" ;;
esac
valid_version "$VERSION" || refuse "The manifest names a moving version."
VERSION="${VERSION#v}"

PIN="${MUSE_UPDATE_PIN:-}"
PIN="${PIN#v}"
if [ -n "$PIN" ] && [ "$PIN" != "$VERSION" ]; then
  record "pinned" "MUSE_UPDATE_PIN=$PIN, so $VERSION was not installed. Still running $CURRENT."
  ping_healthchecks success
  say "pinned at $PIN; not installing $VERSION"
  exit 0
fi

say "new release $VERSION (running $CURRENT)"
ping_healthchecks start
fetch_https "$URL" "$WORK/release.tar.gz" || fail "Could not fetch release $VERSION."
if [ "$(sha256_of "$WORK/release.tar.gz")" != "$SHA" ]; then
  record "refused" "Release $VERSION does not match the SHA-256 in the signed manifest. Nothing was changed."
  ping_healthchecks fail
  die "checksum mismatch for $VERSION"
fi

NEW="$BASE/releases/$VERSION"
if ! unpack_release "$WORK/release.tar.gz" "$VERSION" "$BASE/releases"; then
  fail "Release $VERSION could not be unpacked. Still running $CURRENT."
fi

if [ "$RUNTIME" = "docker" ]; then
  apply_docker
fi

if ! ( build_release_env "$NEW" \
       && [ "$("$NEW/venv/bin/python" "$NEW/scripts/validate_config.py" | tail -1)" = "config ok" ] ); then
  rm -rf "${NEW:?}"
  record "failed" "Release $VERSION did not pass its checks before the switch. Still running $CURRENT."
  ping_healthchecks fail
  die "release $VERSION failed its checks; nothing was switched"
fi

install_units() {  # release-dir
  bash "$1/deploy/vm/install-services.sh" --names "$(unit_prefix)" --user "$RUN_USER" --root "$BASE/current" --venv "$BASE/current/venv" \
       --state "$STATE_ROOT" --base "$BASE" >/dev/null
  systemctl daemon-reload
}
restart_all() {
  # shellcheck disable=SC2086
  systemctl restart $MUSE_LONG_RUNNING
}

switch_release "$BASE" "$VERSION"
if install_units "$NEW" && restart_all && wait_for_proxy "${MUSE_UPDATE_WAIT:-180}"; then
  record "updated" "Updated from $CURRENT to $VERSION." "$VERSION"
  ping_healthchecks success
  say "now running $VERSION"
else
  warn "release $VERSION did not come up; going back to $CURRENT"
  switch_release "$BASE" "$CURRENT"
  install_units "$BASE/releases/$CURRENT" || true
  restart_all || true
  rm -rf "${NEW:?}"
  if wait_for_proxy "${MUSE_UPDATE_WAIT:-180}"; then
    record "rolled-back" "Release $VERSION did not start, so the VM went back to $CURRENT, which is running."
  else
    record "failed" "Release $VERSION did not start and going back to $CURRENT did not bring the router up. See: journalctl -u muse-proxy"
  fi
  ping_healthchecks fail
  exit 1
fi

# Keep the running release and the previous one.
for dir in "$BASE"/releases/*; do
  [ -d "$dir" ] || continue
  name="$(basename "$dir")"
  if [ "$name" != "$VERSION" ] && [ "$name" != "$CURRENT" ]; then rm -rf "${dir:?}"; fi
done

