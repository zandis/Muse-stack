#!/usr/bin/env bash
# Set up the personal gateway on an Oracle Cloud Always Free ARM VM (Ubuntu 24.04).
#
# Run as the normal user (ubuntu) from the root of a clone of this repository:
#
#   ./deploy/oracle-a1-setup.sh                 # Docker (default)
#   ./deploy/oracle-a1-setup.sh --native        # no Docker; proxy runs in a venv
#   ./deploy/oracle-a1-setup.sh --tz Asia/Taipei
#
# What it does: installs Docker (or a venv), creates .env (mode 600) with a
# generated master key, starts the proxy on 127.0.0.1:4000, installs the muse
# CLI, and starts the dashboard on 127.0.0.1:4180 as a user service.
#
# What it never does: open a port, change iptables or the OCI security list,
# bind anything to a public address, print or log a key, or ask for provider
# keys. Add provider keys afterwards, on the VM, with `muse keys set NAME`.
#
# Safe to run again: it keeps an existing .env and an existing master key.
#
# NOT TESTED on a real Oracle VM. It was written without access to one. Read it
# before running it, and treat the first run as the test.

set -euo pipefail

MODE="docker"
TZ_NAME="Asia/Taipei"
while [ $# -gt 0 ]; do
  case "$1" in
    --native) MODE="native" ;;
    --docker) MODE="docker" ;;
    --tz) shift; TZ_NAME="${1:?--tz needs a zone, e.g. Asia/Taipei}" ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || fail "run this as your normal user, not root; it uses sudo where needed"
[ -f docker-compose.yml ] && [ -f scripts/render_config.py ] || fail "run this from the root of the repository clone"
REPO="$(pwd)"
MUSE_HOME_DIR="${MUSE_HOME:-$HOME/.muse}"
VENV="$HOME/.muse-venv"

say "Host"
uname -m; . /etc/os-release && echo "$PRETTY_NAME"
[ "$(uname -m)" = "aarch64" ] || echo "note: this script was written for aarch64; continuing"

say "Packages"
sudo apt-get update -qq
if [ "$MODE" = "docker" ]; then
  sudo apt-get install -y -qq docker.io docker-compose-v2 python3-venv openssl curl
else
  sudo apt-get install -y -qq python3-venv openssl curl
fi

say "State directory and .env"
mkdir -p "$MUSE_HOME_DIR"; chmod 700 "$MUSE_HOME_DIR"
umask 077
[ -f .env ] || cp .env.example .env
chmod 600 .env

# Set KEY=VALUE in .env only if KEY is missing or empty. Never prints VALUE.
set_if_empty() {
  local key="$1" value="$2"
  if grep -qE "^${key}=.+" .env; then return 0; fi
  if grep -qE "^${key}=" .env; then
    # Replace the empty assignment in place.
    # Write a mode-600 temp file next to .env and move it into place.
    local tmp=".env.tmp.$$"
    awk -v k="$key" -v v="$value" 'BEGIN{FS=OFS="="} $1==k && !done {print k "=" v; done=1; next} {print}' .env > "$tmp"
    chmod 600 "$tmp"; mv "$tmp" .env
  else
    printf '%s=%s\n' "$key" "$value" >> .env
  fi
}

if grep -qE '^LITELLM_MASTER_KEY=.+' .env; then
  echo "master key: already set (kept)"
else
  set_if_empty LITELLM_MASTER_KEY "sk-muse-$(openssl rand -hex 32)"
  echo "master key: generated and stored in .env (not shown)"
fi
set_if_empty MUSE_BIND "127.0.0.1"
set_if_empty MUSE_HOME "$MUSE_HOME_DIR"
set_if_empty MUSE_TZ "$TZ_NAME"

# Refuse to continue if anything would listen on a public address.
bind="$(grep -E '^MUSE_BIND=' .env | tail -1 | cut -d= -f2-)"
[ "$bind" = "127.0.0.1" ] || fail "MUSE_BIND in .env is '$bind'. This script only deploys on 127.0.0.1."

say "muse CLI"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
if [ "$MODE" = "native" ]; then
  # LiteLLM and everything it pulls in, from the hash-locked list only: pip refuses
  # any file whose SHA-256 is not in requirements/proxy.lock (README "Supply chain").
  # The lock already holds PyYAML and cryptography (the repo's own needs, trust layer
  # included); dev.lock adds setuptools, the build backend for the editable install.
  "$VENV/bin/pip" install -q --require-hashes -r requirements/proxy.lock -r requirements/dev.lock
  "$VENV/bin/pip" install -q --no-build-isolation --no-deps -e .
  "$VENV/bin/python" scripts/verify_litellm.py || fail "the installed LiteLLM did not pass scripts/verify_litellm.py"
else
  "$VENV/bin/pip" install -q -e ".[trust]"
fi
mkdir -p "$HOME/.local/bin"
ln -sf "$VENV/bin/muse" "$HOME/.local/bin/muse"

say "Config check (no provider is called)"
"$VENV/bin/python" scripts/validate_config.py | tail -1

USER_UNITS="$HOME/.config/systemd/user"
mkdir -p "$USER_UNITS"

if [ "$MODE" = "docker" ]; then
  say "Proxy (Docker)"
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$USER" || true
  DC="sudo docker compose"   # the docker group only applies after the next login

  # The published port must be loopback. Check what compose will actually do.
  published="$($DC config 2>/dev/null | grep -A4 'published' | grep -E 'host_ip' | awk '{print $2}' | tr -d '"' | sort -u || true)"
  if [ -n "$published" ] && [ "$published" != "127.0.0.1" ]; then
    fail "compose would publish on '$published', not 127.0.0.1"
  fi
  $DC pull -q
  $DC up -d

  # The container writes the usage ledger into MUSE_HOME as root. The muse CLI
  # runs as you and must be able to read and write it.
  sudo chown -R "$(id -u):$(id -g)" "$MUSE_HOME_DIR" || true
else
  say "Proxy (native, as a user service)"
  cat > "$USER_UNITS/muse-proxy.service" <<UNIT
[Unit]
Description=Muse proxy (LiteLLM on 127.0.0.1:4000)
After=network-online.target

[Service]
WorkingDirectory=$REPO
Environment=PATH=$VENV/bin:/usr/bin:/bin
ExecStart=$VENV/bin/muse up --native
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now muse-proxy.service
fi

say "Waiting for the proxy"
ok=""
for _ in $(seq 1 60); do
  if curl -fsS -m 3 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || fail "the proxy did not answer on 127.0.0.1:4000 within 2 minutes. Logs: 'sudo docker compose logs litellm' or 'journalctl --user -u muse-proxy'"
echo "proxy is up on 127.0.0.1:4000"

say "Dashboard (user service on 127.0.0.1:4180)"
cat > "$USER_UNITS/muse-dash.service" <<UNIT
[Unit]
Description=Muse dashboard (127.0.0.1:4180)

[Service]
WorkingDirectory=$REPO
Environment=PATH=$VENV/bin:/usr/bin:/bin
ExecStart=$VENV/bin/muse dash --no-open
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now muse-dash.service
# Keep user services running after logout and across reboots.
sudo loginctl enable-linger "$USER" || true

say "Listening sockets (everything below should be 127.0.0.1, except sshd)"
ss -tlnH 2>/dev/null | awk '{print $4}' | sort -u

say "muse doctor"
"$VENV/bin/muse" doctor || true

cat <<'NEXT'

Done. Nothing was opened to the internet.

Next, on this VM:
  muse keys set GEMINI_API_KEY        # prompts; keys are never taken from the command line
  muse connect                        # what to add next (once that command is on your branch)
  muse restart                        # so the proxy picks up new keys
  muse status

From your own computer, reach it through an SSH tunnel:
  ssh -N -L 4000:127.0.0.1:4000 -L 4180:127.0.0.1:4180 ubuntu@<vm-ip>
  then open http://127.0.0.1:4180 and point apps at http://127.0.0.1:4000/v1

The master key is in .env on this VM. To use the endpoint from your computer:
  muse env --show-master-key          # run on the VM; copy it over your SSH session

If 'muse status' cannot read the ledger after the proxy has handled requests
(Docker mode writes it as root), run:  sudo chown -R "$USER": ~/.muse
NEXT
