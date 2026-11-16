#!/bin/sh
# Container entrypoint. Renders config from the environment, then starts LiteLLM.
# New images use /opt/wardmuse. An older mount at /opt/muse still starts.
set -eu

if command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi

if [ -f /opt/wardmuse/scripts/render_and_start.py ]; then
  ROOT=/opt/wardmuse
elif [ -f /opt/muse/scripts/render_and_start.py ]; then
  ROOT=/opt/muse
else
  ROOT=/opt/wardmuse
fi

exec "$PY" "$ROOT/scripts/render_and_start.py"
