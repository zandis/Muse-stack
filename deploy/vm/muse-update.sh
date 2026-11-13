#!/usr/bin/env bash
# Look for a new Muse release and install it side by side (started nightly by
# muse-update.timer, as root).
#
# OFF BY DEFAULT. It does nothing until BOTH of these are set in /etc/muse/vm.conf:
#
#   MUSE_UPDATE_MANIFEST_URL   https address of the release manifest (the owner publishes it)
#   MUSE_UPDATE_PUBKEY         base64 of the raw Ed25519 public key the manifest is signed with
#
# With them set, in order, stopping at the first failure:
#   1. Fetch the manifest and <manifest>.sig over https. Check the signature
#      against MUSE_UPDATE_PUBKEY (deploy/vm/update_verify.py). A manifest that
#      names a branch or a moving tag, a non-https address or a malformed
#      checksum is refused. An older version is refused (no downgrade).
#   2. Fetch the release archive and compare its SHA-256 with the manifest.
#   3. Unpack it next to the running one (<base>/releases/<version>), build a
#      new Python environment there from requirements/proxy.lock with
#      --require-hashes, run scripts/verify_litellm.py and a smoke test
#      (the CLI starts; scripts/validate_config.py prints "config ok").
#   4. Point <base>/current at the new release, reinstall the units, restart,
#      and wait for the router to answer on 127.0.0.1:4000.
#   5. If that fails: point back at the previous release, reinstall its units,
#      restart, and record the failure. The failed release is removed.
#   6. Keep the running release and the one before it; delete older ones.
#
# The result is written to MUSE_VM_STATE/update/last.json for muse-vm-status.
# LiteLLM is never taken from anywhere but the lock file inside the verified
# archive. Operating-system updates are separate (unattended-upgrades).

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/vm/lib.sh
. "$HERE/lib.sh"
load_conf

case "${1:-}" in -h|--help) sed -n '2,29p' "$0"; exit 0 ;; esac

BASE="${MUSE_BASE:-/opt/muse}"
STATE="${MUSE_VM_STATE:-/var/lib/muse/vm}/update"
RUN_USER="${MUSE_USER:-muse}"
STATE_ROOT="${MUSE_STATE_ROOT:-/var/lib/muse}"
MANIFEST_URL="${MUSE_UPDATE_MANIFEST_URL:-}"
PUBKEY="${MUSE_UPDATE_PUBKEY:-}"
CURRENT="$(current_release "$BASE")"

mkdir -p "$STATE"
record() {  # result detail
  write_json "$STATE/last.json" checked_at "$(utc_now)" result "$1" detail "$2" running "${3:-$CURRENT}"
  if [ "$(id -u)" -eq 0 ] && id "$RUN_USER" >/dev/null 2>&1; then chown -R "$RUN_USER:" "$STATE"; fi
}

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
PY="$BASE/current/venv/bin/python"
[ -x "$PY" ] || { record "failed" "The running release has no Python environment."; die "no venv in the current release"; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fetch_https "$MANIFEST_URL" "$WORK/manifest.json" || { record "failed" "Could not fetch the manifest."; die "manifest fetch failed"; }
fetch_https "$MANIFEST_URL.sig" "$WORK/manifest.json.sig" || { record "failed" "Could not fetch the manifest's signature."; die "signature fetch failed"; }

if ! verdict="$("$PY" "$HERE/update_verify.py" "$WORK/manifest.json" "$WORK/manifest.json.sig" "$PUBKEY" "$CURRENT")"; then
  record "refused" "The manifest was refused (bad signature or not a fixed, well-formed release). Nothing was changed."
  die "manifest refused"
fi
# shellcheck disable=SC2086
set -- $verdict
case "$1" in
  same) record "up-to-date" "Up to date."; say "up to date ($CURRENT)"; exit 0 ;;
  older) record "refused" "The manifest names an older release ($2); downgrades are refused."; die "downgrade refused" ;;
  newer) VERSION="$2"; URL="$3"; SHA="$4" ;;
  *) record "failed" "Unexpected answer from the manifest check."; die "unexpected verdict" ;;
esac
valid_version "$VERSION" || { record "refused" "The manifest names a moving version."; die "not a fixed version: $VERSION"; }
VERSION="${VERSION#v}"

say "new release $VERSION (running $CURRENT)"
fetch_https "$URL" "$WORK/release.tar.gz" || { record "failed" "Could not fetch release $VERSION."; die "release fetch failed"; }
if [ "$(sha256_of "$WORK/release.tar.gz")" != "$SHA" ]; then
  record "refused" "Release $VERSION does not match the SHA-256 in the signed manifest. Nothing was changed."
  die "checksum mismatch for $VERSION"
fi

NEW="$BASE/releases/$VERSION"
if ! ( unpack_release "$WORK/release.tar.gz" "$VERSION" "$BASE/releases" \
       && build_release_env "$NEW" \
       && [ "$("$NEW/venv/bin/python" "$NEW/scripts/validate_config.py" | tail -1)" = "config ok" ] ); then
  rm -rf "${NEW:?}"
  record "failed" "Release $VERSION did not pass its checks before the switch. Still running $CURRENT."
  die "release $VERSION failed its checks; nothing was switched"
fi

install_units() {  # release-dir
  bash "$1/deploy/vm/install-services.sh" --user "$RUN_USER" --root "$BASE/current" --venv "$BASE/current/venv" \
       --state "$STATE_ROOT" --base "$BASE" >/dev/null
  systemctl daemon-reload
}
restart_all() {
  # shellcheck disable=SC2086
  systemctl restart $MUSE_LONG_RUNNING
}

switch_release "$BASE" "$VERSION"
if install_units "$NEW" && restart_all && wait_for_proxy 180; then
  record "updated" "Updated from $CURRENT to $VERSION." "$VERSION"
  say "now running $VERSION"
else
  warn "release $VERSION did not come up; going back to $CURRENT"
  switch_release "$BASE" "$CURRENT"
  install_units "$BASE/releases/$CURRENT" || true
  restart_all || true
  rm -rf "${NEW:?}"
  if wait_for_proxy 180; then
    record "rolled-back" "Release $VERSION did not start, so the VM went back to $CURRENT, which is running."
  else
    record "failed" "Release $VERSION did not start and going back to $CURRENT did not bring the router up. See: journalctl -u muse-proxy"
  fi
  exit 1
fi

# Keep the running release and the previous one.
for dir in "$BASE"/releases/*; do
  [ -d "$dir" ] || continue
  name="$(basename "$dir")"
  if [ "$name" != "$VERSION" ] && [ "$name" != "$CURRENT" ]; then rm -rf "${dir:?}"; fi
done
