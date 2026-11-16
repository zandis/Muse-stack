#!/usr/bin/env bash
# First boot of the always-on helper. Run as root from inside a release that has
# ALREADY been checked against its SHA-256 (cloud-init does that before it calls
# this; see deploy/oracle-stack/cloud-init.yaml.tftpl).
#
#   /opt/muse/releases/<version>/deploy/vm/bootstrap.sh
#
# Settings come from /etc/muse/vm.conf, written by cloud-init. None is a secret.
#
# What it does, each step once (safe to run again; finished steps are skipped):
#   packages   python3-venv, age, curl from Ubuntu's own archive
#   account    the service user `muse`: no password, no shell, no sudo
#   folders    /var/lib/muse (state, 0700), /opt/muse (code, root-owned)
#   os         automatic security updates with a reboot window; TCP MTU probing
#   python     a venv inside the release, installed from requirements/proxy.lock
#              with --require-hashes, then scripts/verify_litellm.py
#   secrets    the proxy master key, generated here, written straight into a
#              0600 file and never printed, logged or sent anywhere
#   backupkey  an age key pair for backups (private half stays on this VM)
#   command    /usr/local/bin/muse and /usr/local/bin/muse-vm-status
#   services   the systemd units from deploy/vm/units, started
#   ssh        with no tester key: sshd is stopped, disabled and masked
#   health     the router answers on 127.0.0.1:4000 and NOTHING listens beyond
#              loopback (sshd excepted only when a tester key was given)
#
# If a step fails, it writes /var/lib/muse-setup/FAILED (step and time), stops
# and disables every Muse unit so nothing half-configured keeps listening, and
# exits. On success it writes /var/lib/muse-setup/DONE.
#
# There is no `set -x` here and there must never be: this script handles a secret.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/vm/lib.sh
. "$HERE/lib.sh"
load_conf

case "${1:-}" in -h|--help) sed -n '2,31p' "$0"; exit 0 ;; esac
[ "$(id -u)" -eq 0 ] || die "first boot runs as root"

RELEASE_DIR="$(cd "$HERE/../.." && pwd)"
VERSION="$(basename "$RELEASE_DIR")"
BASE="$(wm_dir BASE /opt/wardmuse /opt/muse /opt/muse-stack)"
RUN_USER="${MUSE_USER:-muse}"
STATE_ROOT="$(wm_dir STATE_ROOT /var/lib/wardmuse /var/lib/muse)"
if val="$(wm_env HOME)"; then HOME_DIR="$val"; else HOME_DIR="$STATE_ROOT/home"; fi
VM_STATE="$(wm_dir VM_STATE "$STATE_ROOT/vm" /var/lib/wardmuse/vm /var/lib/muse/vm)"
if val="$(wm_env ENV_FILE)"; then ENV_FILE="$val"; else ENV_FILE="$STATE_ROOT/wardmuse.env"; fi
# An existing secrets file keeps its name. A new install uses wardmuse.env.
if [ ! -f "$ENV_FILE" ] && [ -f "$STATE_ROOT/muse.env" ]; then ENV_FILE="$STATE_ROOT/muse.env"; fi
SETUP_DIR="${MUSE_SETUP_DIR:-/var/lib/wardmuse-setup}"
TZ_NAME="${MUSE_TZ:-Asia/Taipei}"
REBOOT_TIME="${MUSE_OS_REBOOT_TIME:-03:30}"
TESTER_SSH="${MUSE_TESTER_SSH:-no}"
# Empty on a real machine. The tests point it at a scratch folder so that the
# few files written outside /opt/muse and /var/lib/muse land there instead.
SYSROOT="${MUSE_SYSROOT:-}"

valid_version "$VERSION" || die "this release folder ($VERSION) is not named after a fixed version"
[ "$RELEASE_DIR" = "$BASE/releases/$VERSION" ] || die "run this from $BASE/releases/<version>, not $RELEASE_DIR"

mkdir -p "$SETUP_DIR/steps"
chmod 755 "$SETUP_DIR"
rm -f "$SETUP_DIR/FAILED"
CURRENT_STEP="start"

stop_everything() {
  # shellcheck disable=SC2086
  systemctl disable --now $MUSE_TIMERS $MUSE_LONG_RUNNING >/dev/null 2>&1 || true
}
on_exit() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    printf 'step=%s\ntime=%s\nstatus=%s\nlog=/var/log/cloud-init-output.log\n' "$CURRENT_STEP" "$(utc_now)" "$status" > "$SETUP_DIR/FAILED"
    stop_everything
    printf 'ERROR: first boot stopped at step "%s". Nothing is left running. See %s/FAILED\n' "$CURRENT_STEP" "$SETUP_DIR" >&2
  fi
}
trap on_exit EXIT

step() {  # name function
  local name="$1"; shift
  CURRENT_STEP="$name"
  if [ -f "$SETUP_DIR/steps/$name" ]; then say "$name: already done"; return 0; fi
  say "$name"
  "$@"
  utc_now > "$SETUP_DIR/steps/$name"
}

do_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends python3-venv age curl ca-certificates unattended-upgrades
}

do_account() {
  if ! id "$RUN_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_ROOT" --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
  fi
  passwd --lock "$RUN_USER" >/dev/null
  # The service user gets no sudo, now or later.
  if [ -e "$SYSROOT/etc/sudoers.d/$RUN_USER" ] || id -nG "$RUN_USER" | tr ' ' '\n' | grep -qxE 'sudo|admin|adm|lxd|docker'; then
    die "the service user must not be able to become root"
  fi
}

do_folders() {
  install -d -m 0755 -o root -g root "$BASE" "$BASE/releases" "$SYSROOT/etc/wardmuse" "$SYSROOT/etc/apt/apt.conf.d" "$SYSROOT/etc/sysctl.d" "$SYSROOT/usr/local/bin"
  install -d -m 0700 -o "$RUN_USER" -g "$RUN_USER" "$STATE_ROOT" "$HOME_DIR" "$VM_STATE"
  chown -R root:root "$RELEASE_DIR"
  chmod -R go-w "$RELEASE_DIR"
}

do_os() {
  cat > "$SYSROOT/etc/apt/apt.conf.d/20auto-upgrades" <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
  cat > "$SYSROOT/etc/apt/apt.conf.d/52wardmuse-unattended-upgrades" <<CONF
// Security updates install by themselves; a reboot, when one is needed, happens at this time.
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "$REBOOT_TIME";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
CONF
  # The cloud network lets no ICMP in, so let TCP find the path MTU by itself.
  printf 'net.ipv4.tcp_mtu_probing = 1\n' > "$SYSROOT/etc/sysctl.d/60-wardmuse.conf"
  sysctl -q -p "$SYSROOT/etc/sysctl.d/60-wardmuse.conf" || true
  if command -v timedatectl >/dev/null 2>&1; then timedatectl set-timezone "$TZ_NAME" || true; fi
}

do_python() {
  build_release_env "$RELEASE_DIR"
  chown -R root:root "$RELEASE_DIR/venv"
  switch_release "$BASE" "$VERSION"
}

do_secrets() {
  # Runs as root, writes a file only the service user can read. The key is made
  # by a shell builtin chain that never puts it on a command line or a terminal.
  local key
  umask 077
  if [ ! -f "$ENV_FILE" ]; then : > "$ENV_FILE"; fi
  chmod 600 "$ENV_FILE"
  if grep -qE '^LITELLM_MASTER_KEY=.+' "$ENV_FILE"; then
    say "master key: already set (kept)"
  else
    key="sk-muse-$(openssl rand -hex 32)"
    [ "${#key}" -eq 72 ] || die "could not generate a master key"
    printf 'LITELLM_MASTER_KEY=%s\n' "$key" >> "$ENV_FILE"
    key=""
    say "master key: generated and stored (not shown)"
  fi
  local name value
  for name in MUSE_BIND MUSE_PORT MUSE_HOME MUSE_TZ; do
    case "$name" in
      MUSE_BIND) value="127.0.0.1" ;;
      MUSE_PORT) value="4000" ;;
      MUSE_HOME) value="$HOME_DIR" ;;
      MUSE_TZ) value="$TZ_NAME" ;;
    esac
    grep -qE "^${name}=" "$ENV_FILE" || printf '%s=%s\n' "$name" "$value" >> "$ENV_FILE"
  done
  chown "$RUN_USER:$RUN_USER" "$ENV_FILE"
  local bind
  bind="$(grep -E '^MUSE_BIND=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
  [ "$bind" = "127.0.0.1" ] || die "MUSE_BIND is '$bind'; this VM only ever serves on 127.0.0.1"
  umask 022
}

do_backupkey() {
  local dir="$HOME_DIR/backup-key"
  if [ ! -s "$dir/identity.txt" ]; then
    install -d -m 0700 -o "$RUN_USER" -g "$RUN_USER" "$dir"
    # age-keygen writes the private key to the file and the PUBLIC key to standard error.
    runuser -u "$RUN_USER" -- age-keygen -o "$dir/identity.txt" 2>/dev/null
    chmod 600 "$dir/identity.txt"
  fi
  runuser -u "$RUN_USER" -- age-keygen -y "$dir/identity.txt" > "$dir/recipient.txt"
  chown "$RUN_USER:$RUN_USER" "$dir/recipient.txt"
}

do_command() {
  cat > "$SYSROOT/usr/local/bin/wardmuse" <<WRAP
#!/bin/sh
# The Wardmuse CLI on this VM: runs as the service user with the VM's settings.
#   sudo wardmuse status        sudo wardmuse keys set GEMINI_API_KEY
if [ "\$(id -un)" != "$RUN_USER" ]; then
  [ "\$(id -u)" -eq 0 ] || { echo "run as root (sudo wardmuse ...) or as $RUN_USER" >&2; exit 1; }
  exec runuser -u "$RUN_USER" -- "\$0" "\$@"
fi
export WARDMUSE_HOME="$HOME_DIR" WARDMUSE_ENV_FILE="$ENV_FILE" WARDMUSE_TZ="$TZ_NAME" WARDMUSE_BIND=127.0.0.1
export MUSE_HOME="$HOME_DIR" MUSE_ENV_FILE="$ENV_FILE" MUSE_TZ="$TZ_NAME" MUSE_BIND=127.0.0.1
export PYTHONPATH="$BASE/current" PATH="$BASE/current/venv/bin:/usr/bin:/bin"
cd "$BASE/current" && exec "$BASE/current/venv/bin/python" -m wardmuse "\$@"
WRAP
  chmod 755 "$SYSROOT/usr/local/bin/wardmuse"
  cat > "$SYSROOT/usr/local/bin/muse" <<'WRAP'
#!/bin/sh
# Compatibility name. Prefer wardmuse.
echo "wardmuse: the 'muse' command is now 'wardmuse'" >&2
exec /usr/local/bin/wardmuse "$@"
WRAP
  chmod 755 "$SYSROOT/usr/local/bin/muse"
  ln -sfn "$BASE/current/deploy/vm/muse-vm-status" "$SYSROOT/usr/local/bin/muse-vm-status"
  ln -sfn "$BASE/current/deploy/vm/muse-vm-status" "$SYSROOT/usr/local/bin/wardmuse-vm-status"
}

do_services() {
  bash "$RELEASE_DIR/deploy/vm/install-services.sh" --user "$RUN_USER" --root "$BASE/current" \
       --venv "$BASE/current/venv" --state "$STATE_ROOT" --base "$BASE" --dest "$SYSROOT/etc/systemd/system" --enable
}

do_health() {
  wait_for_proxy 240 || die "the router did not answer on 127.0.0.1:4000 within 4 minutes (journalctl -u wardmuse-proxy or journalctl -u muse-proxy)"
  only_loopback_listeners "$TESTER_SSH" || die "something listens beyond loopback; refusing to leave it running"
  say "router is up on 127.0.0.1:4000; nothing listens beyond loopback"
}

do_ssh() {
  if [ "$TESTER_SSH" = "yes" ]; then
    say "a tester SSH key was given: sshd stays on (key login only)"
    return 0
  fi
  systemctl disable --now ssh.socket ssh.service >/dev/null 2>&1 || true
  systemctl mask ssh.socket ssh.service >/dev/null 2>&1 || true
}

step packages  do_packages
step account   do_account
step folders   do_folders
step os        do_os
step python    do_python
step secrets   do_secrets
step backupkey do_backupkey
step command   do_command
step services  do_services
# Checked on every run, not only the first.
CURRENT_STEP="ssh"; say "ssh"; do_ssh
CURRENT_STEP="health"; say "health"; do_health

CURRENT_STEP="done"
printf 'version=%s\ntime=%s\n' "$VERSION" "$(utc_now)" > "$SETUP_DIR/DONE"
say "first boot finished: Wardmuse $VERSION is running. Nothing was opened to the internet."
