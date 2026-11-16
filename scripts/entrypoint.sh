#!/bin/sh
# Container entrypoint. Renders config from the environment, then starts LiteLLM.
set -eu

if command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

exec "$PY" /opt/muse/scripts/render_and_start.py
