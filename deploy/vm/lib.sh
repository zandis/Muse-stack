# Shared helpers for the scripts in deploy/vm. Sourced, never run.
#
# Rules every script here follows:
#   - no `set -x`, ever: a trace would copy secrets into the journal or
#     cloud-init-output.log;
#   - a secret is never an argument, never printed, never in a file other
#     than the one it lives in (mode 0600);
#   - LiteLLM is installed only from requirements/proxy.lock with
#     --require-hashes (README "Supply chain").

# shellcheck shell=bash

MUSE_VM_CONF="${MUSE_VM_CONF:-/etc/muse/vm.conf}"

say()  { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Read KEY=VALUE settings (no shell code is evaluated: the file is parsed, not
# sourced). Values already in the environment win, so a test can override.
load_conf() {
  local file="${1:-$MUSE_VM_CONF}" line name value
  [ -r "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    name="${line%%=*}"; value="${line#*=}"
    case "$name" in *[!A-Za-z0-9_]*|'') continue ;; esac
    if [ -z "${!name+x}" ]; then
      printf -v "$name" '%s' "$value"
      # shellcheck disable=SC2163
      export "$name"
    fi
  done < "$file"
}

# A release name must be a fixed version, never a branch or a moving tag.
valid_version() {
  case "$1" in
    main|master|latest|stable|HEAD|head|dev|develop|nightly|edge|'') return 1 ;;
  esac
  printf '%s' "$1" | grep -Eq '^v?[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.]+)?$'
}

valid_sha256() { printf '%s' "$1" | grep -Eq '^[0-9a-f]{64}$'; }

sha256_of() { sha256sum -- "$1" | cut -d' ' -f1; }

# Download over HTTPS only (no redirect to plain HTTP), to a file.
fetch_https() {
  local url="$1" out="$2"
  case "$url" in https://*) ;; *) die "refusing a URL that is not https: $url" ;; esac
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
       --retry 5 --retry-delay 5 --max-time 600 --output "$out" -- "$url"
}

# Write JSON {"key": "value", ...} atomically. Arguments: file key value [key value ...].
# Values are written as strings unless they look like a number, true, false or null.
write_json() {
  local file="$1" tmp; shift
  tmp="$file.tmp.$$"
  python3 - "$tmp" "$@" <<'PY'
import json, re, sys
out, pairs = sys.argv[1], sys.argv[2:]
doc = {}
for key, value in zip(pairs[0::2], pairs[1::2]):
    if re.fullmatch(r"-?\d+(\.\d+)?", value):
        doc[key] = float(value) if "." in value else int(value)
    elif value in ("true", "false", "null"):
        doc[key] = json.loads(value)
    else:
        doc[key] = value
with open(out, "w", encoding="utf-8") as handle:
    json.dump(doc, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
  mv -f "$tmp" "$file"
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Build the Python environment of one release, inside it, from the hash-locked
# list only, then check what was installed. Used by first boot and by updates.
#   build_release_env /opt/muse/releases/<version>
build_release_env() {
  local dir="$1"
  [ -f "$dir/requirements/proxy.lock" ] || die "$dir has no requirements/proxy.lock"
  [ -f "$dir/scripts/verify_litellm.py" ] || die "$dir has no scripts/verify_litellm.py"
  if [ ! -x "$dir/venv/bin/python" ]; then
    python3 -m venv "$dir/venv"
  fi
  # pip refuses any file whose SHA-256 is not in the lock file. pip itself is
  # the copy the distribution's python3-venv ships; it is not upgraded from the
  # network.
  "$dir/venv/bin/python" -m pip install --quiet --disable-pip-version-check --no-input \
      --require-hashes -r "$dir/requirements/proxy.lock"
  "$dir/venv/bin/python" "$dir/scripts/verify_litellm.py" \
      || die "the installed LiteLLM did not pass scripts/verify_litellm.py"
  # The CLI must start from this tree with this environment.
  PYTHONPATH="$dir" "$dir/venv/bin/python" -m muse version >/dev/null \
      || die "the muse CLI in $dir does not start"
}

# Every TCP listener must be on loopback. Arguments: extra allowed "addr:port" patterns.
# Prints the offending sockets and returns 1 when something listens elsewhere.
only_loopback_listeners() {
  local allowed_ssh="${1:-no}" bad=""
  local addr
  while IFS= read -r addr; do
    case "$addr" in
      127.*|'[::1]'*|'[::ffff:127.'*) continue ;;
      *:22) [ "$allowed_ssh" = "yes" ] && continue ;;
    esac
    bad="$bad $addr"
  done < <(ss -tlnH 2>/dev/null | awk '{print $4}' | sort -u)
  if [ -n "$bad" ]; then
    printf 'listening beyond loopback:%s\n' "$bad" >&2
    return 1
  fi
  return 0
}

# Unpack a release tarball that has ALREADY been checked against its SHA-256.
# The archive must hold exactly one top-level folder and no path that climbs
# out of it. Result: <releases>/<version>, not writable by the service user.
#   unpack_release TARBALL VERSION RELEASES_DIR
unpack_release() {
  local tarball="$1" version="$2" releases="$3" listing tops work
  valid_version "$version" || die "'$version' is not a fixed version"
  listing="$(tar -tzf "$tarball")" || die "the release archive cannot be read"
  if printf '%s\n' "$listing" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    die "the release archive holds an unsafe path"
  fi
  tops="$(printf '%s\n' "$listing" | cut -d/ -f1 | sort -u | wc -l)"
  [ "$tops" -eq 1 ] || die "the release archive must hold exactly one top-level folder"
  mkdir -p "$releases"
  work="$(mktemp -d "$releases/.incoming.XXXXXX")"
  tar -xzf "$tarball" -C "$work" --no-same-owner --no-same-permissions
  rm -rf "${releases:?}/${version:?}"
  mv "$work"/* "$releases/$version"
  rmdir "$work"
  chmod -R go-w "$releases/$version"
  chmod 755 "$releases/$version"
}

# Point <base>/current at a release, atomically.
switch_release() {
  local base="$1" version="$2"
  [ -d "$base/releases/$version" ] || die "no such release: $version"
  ln -sfn "releases/$version" "$base/current.new"
  mv -Tf "$base/current.new" "$base/current"
}

current_release() {
  local base="$1"
  if [ -L "$base/current" ]; then basename "$(readlink -f "$base/current")"; else echo ""; fi
}

# Wait for the router to answer on loopback. Argument: seconds (default 180).
wait_for_proxy() {
  local limit="${1:-180}" waited=0
  while [ "$waited" -lt "$limit" ]; do
    if curl --fail --silent --max-time 3 --output /dev/null http://127.0.0.1:4000/health/liveliness; then
      return 0
    fi
    sleep 3; waited=$((waited + 3))
  done
  return 1
}

# Used by the scripts that source this file (bootstrap.sh, muse-update.sh).
# shellcheck disable=SC2034
MUSE_LONG_RUNNING="muse-proxy.service muse-home.service muse-queue.service muse-vm-monitor.service"
# shellcheck disable=SC2034
MUSE_TIMERS="muse-chores.timer muse-catalog-check.timer muse-backup.timer muse-update.timer"
