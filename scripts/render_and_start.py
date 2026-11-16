#!/usr/bin/env python3
"""Render the LiteLLM config, then replace this process with the proxy."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from render_config import (  # noqa: E402
    DEFAULT_CATALOG,
    dumps,
    envget,
    load_catalog,
    paid_cap_positive,
    paid_fallback_enabled,
    render,
    write_config,
)

OUT = Path(envget(os.environ, "MUSE_CONFIG_PATH", "/tmp/wardmuse-config.yaml"))


def main() -> None:
    catalog = load_catalog(Path(envget(os.environ, "MUSE_CATALOG", str(DEFAULT_CATALOG))))
    config, enabled = render(catalog, os.environ)
    text = dumps(config, enabled)
    write_config(text, OUT)

    if not os.environ.get("LITELLM_MASTER_KEY", "").strip():
        print(
            "muse-stack: LITELLM_MASTER_KEY is unset. The proxy will accept unauthenticated calls. "
            "Set a master key before exposing the port.",
            file=sys.stderr,
        )
    if not enabled:
        print(
            "muse-stack: no provider keys are set. The proxy will start with an empty model list. "
            "Add keys to .env and restart to enable aliases.",
            file=sys.stderr,
        )
    else:
        paid = paid_fallback_enabled(os.environ)
        cap = envget(os.environ, "MUSE_DAILY_PAID_CAP_USD", "").strip() or "0.9375 (default, NT$30 at 32)"
        cap_text = f"daily paid cap ${cap}" if paid_cap_positive(os.environ) else "daily paid cap 0 (paid never used)"
        print(
            f"muse-stack: enabled providers: {', '.join(enabled)}. "
            f"paid fallback {'on' if paid else 'off'}; {cap_text}.",
            file=sys.stderr,
        )

    # The config names the callback muse.hook.handler; LiteLLM imports it, so
    # the repo root (/opt/muse in the container) must be importable.
    python_path = [part for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part]
    if str(ROOT) not in python_path:
        os.environ["PYTHONPATH"] = os.pathsep.join([str(ROOT), *python_path])

    os.execvp(
        "litellm",
        [
            "litellm",
            "--config",
            str(OUT),
            "--host",
            "0.0.0.0",
            "--port",
            os.environ.get("LITELLM_PORT", "4000"),
            "--num_workers",
            os.environ.get("LITELLM_NUM_WORKERS", "1"),
        ],
    )


if __name__ == "__main__":
    main()
