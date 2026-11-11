#!/usr/bin/env python3
"""Render the LiteLLM config, then replace this process with the proxy."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from render_config import DEFAULT_CATALOG, dumps, load_catalog, render, write_config  # noqa: E402

OUT = Path(os.environ.get("MUSE_CONFIG_PATH", "/tmp/muse-config.yaml"))


def main() -> None:
    catalog = load_catalog(Path(os.environ.get("MUSE_CATALOG", str(DEFAULT_CATALOG))))
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
        paid = os.environ.get("MUSE_ALLOW_PAID_FALLBACK", "").strip().lower() in {"1", "true", "yes", "on"}
        print(
            f"muse-stack: enabled providers: {', '.join(enabled)}. "
            f"paid fallback {'on' if paid else 'off'}.",
            file=sys.stderr,
        )

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
