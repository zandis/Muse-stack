#!/usr/bin/env bash
# One-time move from a Docker git clone to the signed-release layout.
#
# The production VM keeps the proxy in a clone at /opt/muse-stack and its
# secrets in that clone's .env. This script does not edit the clone and does
# not edit that file. It copies the bytes to /etc/muse/muse.env (mode 0600)
# and leaves the original where it is. The clone stays, so going back is
# starting compose from the clone again.
#
#   sudo deploy/vm/migrate-from-clone.sh --dry-run
#   sudo deploy/vm/migrate-from-clone.sh --yes
#   sudo deploy/vm/migrate-from-clone.sh --rollback --yes
#
# --no-start skips docker and systemd. --root DIR writes under DIR instead
# of / so the same steps can be checked without being root. Data is copied
# from --home (default: the .muse directory compose was using) with a SQLite
# backup. The source directory is left in place.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/vm/lib.sh
. "$HERE/lib.sh"
load_conf

DRY="no" YES="no" NO_START="no" ROLLBACK="no"
CLONE="${MUSE_CLONE:-/opt/muse-stack}"
PREFIX=""
RUN_USER="${MUSE_USER:-}"
SRC_HOME="${MUSE_MIGRATE_HOME:-}"
HOME_SET="no"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY="yes" ;;
    --yes) YES="yes" ;;
    --no-start) NO_START="yes" ;;
    --rollback) ROLLBACK="yes" ;;
    --clone) shift; CLONE="${1:?--clone needs a directory}" ;;
    --root) shift; PREFIX="${1:?--root needs a directory}"; PREFIX="${PREFIX%/}" ;;
    --user) shift; RUN_USER="${1:?--user needs an account}" ;;
    --home) shift; SRC_HOME="${1:?--home needs a directory}"; HOME_SET="yes" ;;
    --no-home) SRC_HOME=""; HOME_SET="yes" ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

if [ "$YES" != "yes" ] && [ "$DRY" != "yes" ]; then
  die "pass --dry-run to print the plan, or --yes to carry it out"
fi
if [ "$DRY" = "yes" ] && [ "$YES" = "yes" ]; then
  die "pass either --dry-run or --yes, not both"
fi

[ -d "$CLONE" ] || die "no such clone: $CLONE"
CLONE="$(cd "$CLONE" && pwd)"
[ -d "$CLONE/.git" ] || die "not a git clone: $CLONE"
[ -f "$CLONE/.env" ] || die "the clone has no .env ($CLONE/.env); refusing to invent keys"
if git -C "$CLONE" ls-files --error-unmatch -- .env >/dev/null 2>&1; then
  die "the clone tracks .env; refusing to put secrets into a release tree"
fi
if [ -n "$(git -C "$CLONE" status --porcelain --untracked-files=no)" ]; then
  die "the clone has uncommitted changes. Commit or stash them first. .env is copied separately."
fi

SHA="$(git -C "$CLONE" rev-parse --verify HEAD)"
SHORT="$(git -C "$CLONE" rev-parse --short=12 HEAD)"
VERSION="0.0.0+$SHORT"
valid_version "$VERSION" || die "could not name a release from commit $SHORT"

BASE="$PREFIX/opt/muse"
ETC="$PREFIX/etc/muse"
STATE_ROOT="$PREFIX/var/lib/muse"
ENV_FILE="$ETC/muse.env"
CONF="$ETC/vm.conf"
HOME_DEST="$STATE_ROOT/home"
VM_STATE="$STATE_ROOT/vm"
PROJECT="$(basename "$CLONE")"
PY="${MUSE_UPDATE_PYTHON:-python3}"

if [ -z "$RUN_USER" ]; then
  if id ubuntu >/dev/null 2>&1; then
    RUN_USER="ubuntu"
  else
    RUN_USER="$(stat -c %U "$CLONE")"
  fi
fi
[ "$RUN_USER" != "root" ] || die "pass --user. The services do not run as root."

if [ "$HOME_SET" = "no" ]; then
  if [ -d /root/.muse ]; then
    SRC_HOME="/root/.muse"
  elif [ -n "${SUDO_USER:-}" ] && [ -d "/home/${SUDO_USER}/.muse" ]; then
    SRC_HOME="/home/${SUDO_USER}/.muse"
  elif [ -d "${HOME}/.muse" ]; then
    SRC_HOME="${HOME}/.muse"
  else
    SRC_HOME=""
  fi
fi

write_vm_conf() {
  [ -e "$CONF" ] && return 0
  cat > "$CONF" <<EOF
# No secrets in this file. Keys stay in MUSE_ENV_FILE.
# Healthchecks URLs, if you use them, go in the files named below (mode 0600).
MUSE_RUNTIME=docker
MUSE_BASE=$BASE
MUSE_STATE_ROOT=$STATE_ROOT
MUSE_VM_STATE=$VM_STATE
MUSE_HOME=$HOME_DEST
MUSE_ENV_FILE=$ENV_FILE
MUSE_COMPOSE_PROJECT=$PROJECT
MUSE_USER=$RUN_USER
MUSE_UPDATE_MANIFEST_URL=https://github.com/zandis/Muse-stack/releases/latest/download/stable.json
MUSE_UPDATE_PUBKEY=
MUSE_HEALTHCHECKS_URL_FILE=$ETC/healthchecks.url
MUSE_HEARTBEAT_URL_FILE=$ETC/heartbeat.url
MUSE_IDLE_GUARD=off
MUSE_ACCOUNT_TYPE=always_free
EOF
  chmod 0644 "$CONF"
}

install_units() {
  local -a enable=()
  if [ "$NO_START" != "yes" ] && [ "$(id -u)" -eq 0 ]; then
    enable=(--enable)
  fi
  bash "$HERE/install-services.sh" \
    --user "$RUN_USER" --root "$BASE/current" --venv /usr \
    --state "$STATE_ROOT" --base "$BASE" --conf "$CONF" \
    --dest "$PREFIX/etc/systemd/system" \
    --units "muse-update muse-heartbeat muse-vm-monitor" \
    "${enable[@]}"
}

start_new() {
  [ "$NO_START" = "yes" ] && return 0
  export MUSE_HOME="$HOME_DEST" MUSE_ENV_FILE="$ENV_FILE" MUSE_COMPOSE_PROJECT="$PROJECT"
  compose_release "$BASE/current" up -d
}

start_clone() {
  # The clone's own .env, not the copy. Nothing in the clone is modified.
  if [ "$DRY" = "yes" ] || [ "$NO_START" = "yes" ]; then
    say "rollback command (not run): project $PROJECT in $CLONE"
    return 0
  fi
  "${MUSE_DOCKER:-docker}" compose -p "$PROJECT" --project-directory "$CLONE" up -d
}

if [ "$ROLLBACK" = "yes" ]; then
  [ -f "$CLONE/.env" ] || die "the clone's .env is missing; refusing to guess where the keys are"
  say "the clone at $CLONE is unchanged and will serve again"
  say "leaving $ENV_FILE and $BASE in place"
  start_clone
  if [ "$DRY" != "yes" ]; then
    mkdir -p "$VM_STATE"
    write_json "$VM_STATE/migration-rollback.json" \
      rolled_back_at "$(utc_now)" clone "$CLONE" project "$PROJECT" result "asked"
  fi
  say "to use the signed layout again, stop the clone's project and start $BASE/current"
  exit 0
fi

if [ -L "$BASE/current" ]; then
  have="$(current_release "$BASE")"
  if [ "$have" = "$VERSION" ] && [ -f "$ENV_FILE" ] && cmp -s "$CLONE/.env" "$ENV_FILE"; then
    say "already migrated ($VERSION)"
    exit 0
  fi
  if [ -n "$have" ] && [ "$have" != "$VERSION" ]; then
    die "$BASE/current already points at $have; refusing to replace a release that is not this clone"
  fi
fi

if [ "$DRY" = "yes" ]; then
  say "dry run: copy the clone's .env to $ENV_FILE (mode 0600) and leave the original untouched"
  say "dry run: install the committed tree of $SHA as $BASE/releases/$VERSION"
  say "dry run: leave $CLONE in place so rollback can start it again"
  if [ -n "$SRC_HOME" ]; then
    say "dry run: copy data from $SRC_HOME to $HOME_DEST and leave the source in place"
  else
    say "dry run: no data directory found; pass --home if the ledger lives somewhere else"
  fi
  say "dry run: write $CONF with MUSE_RUNTIME=docker and MUSE_IDLE_GUARD=off"
  say "dry run: install the update timer, the heartbeat and the sampler. The idle guard stays off."
  exit 0
fi

if [ "$(id -u)" -ne 0 ] && [ -z "${MUSE_MIGRATE_ALLOW_UNPRIV:-}" ]; then
  die "migration runs as root (it writes /opt/muse and /etc/muse)"
fi
if [ -f "$ENV_FILE" ] && ! cmp -s "$CLONE/.env" "$ENV_FILE"; then
  die "$ENV_FILE already exists and differs from the clone; refusing to overwrite it"
fi

if [ "$NO_START" != "yes" ] && [ "$(id -u)" -eq 0 ]; then
  if ! "$PY" -c 'import cryptography' >/dev/null 2>&1; then
    say "installing python3-cryptography so the updater can check signatures"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends python3-cryptography ca-certificates curl
  fi
fi

before="$(sha256_of "$CLONE/.env")"
mode_before="$(stat -c %a "$CLONE/.env")"
install -d -m 0755 "$ETC" "$BASE" "$BASE/releases" "$PREFIX/etc/systemd/system"
install -d -m 0700 "$STATE_ROOT" "$HOME_DEST" "$VM_STATE"
install -m 0600 "$CLONE/.env" "$ENV_FILE"
after="$(sha256_of "$CLONE/.env")"
[ "$before" = "$after" ] || die "the clone's .env changed while it was being copied; the copy was left, the original was not edited"
cmp -s "$CLONE/.env" "$ENV_FILE" || die "the copy of .env does not match the original"
[ "$(stat -c %a "$CLONE/.env")" = "$mode_before" ] || die "the clone's .env mode changed"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git -C "$CLONE" archive --format=tar --prefix="muse-${VERSION}/" HEAD | gzip -n > "$WORK/release.tar.gz"
unpack_release "$WORK/release.tar.gz" "$VERSION" "$BASE/releases"
# The clone's own deploy/vm only knows the venv layout. The timer has to run
# this checkout's updater, which knows Docker. The container does not mount
# deploy/, so the proxy code stays the clone's commit.
TOOLS="$(cd "$HERE" && pwd)"
DEST_VM="$BASE/releases/$VERSION/deploy/vm"
if [ "$TOOLS" != "$DEST_VM" ]; then
  rm -rf "$DEST_VM"
  mkdir -p "$BASE/releases/$VERSION/deploy"
  cp -a "$TOOLS" "$DEST_VM"
  find "$DEST_VM" -depth -type d -name '__pycache__' -exec rm -rf {} +
  chmod -R go-w "$DEST_VM"
fi
export MUSE_ENV_FILE="$ENV_FILE"
link_release_env "$BASE/releases/$VERSION" || die "could not point the release at the copied env file"

if [ -n "$SRC_HOME" ] && [ -d "$SRC_HOME" ]; then
  src_real="$(cd "$SRC_HOME" && pwd)"
  dest_real="$(cd "$HOME_DEST" && pwd)"
  if [ "$src_real" != "$dest_real" ]; then
    "$PY" "$HERE/update_snapshot.py" snapshot --home "$src_real" --out "$WORK/home.tar"
    "$PY" "$HERE/update_snapshot.py" restore --tar "$WORK/home.tar" --home "$HOME_DEST"
    if [ "$(id -u)" -eq 0 ]; then
      chown -R "$RUN_USER:$RUN_USER" "$HOME_DEST" || true
    fi
  fi
fi

write_vm_conf
switch_release "$BASE" "$VERSION"
install_units
start_new

mkdir -p "$VM_STATE"
write_json "$VM_STATE/migration.json" \
  migrated_at "$(utc_now)" clone "$CLONE" version "$VERSION" commit "$SHA" \
  project "$PROJECT" env_file "$ENV_FILE" result "ok"
say "migrated to $BASE/releases/$VERSION"
say "the clone and its .env were not modified: $CLONE"
say "rollback: sudo $HERE/migrate-from-clone.sh --rollback --yes --clone $CLONE"
