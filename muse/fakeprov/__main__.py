"""Run the fake provider in the foreground. `MUSE_FAKE_SCENARIOS` is `model=scenario` pairs, comma-separated."""

from __future__ import annotations

import os

from muse.fakeprov import FakeProvider


def main() -> None:
    provider = FakeProvider().start()
    raw = os.environ.get("MUSE_FAKE_SCENARIOS", "")
    for item in raw.split(","):
        if "=" not in item:
            continue
        model, scenario = item.split("=", 1)
        provider.set(model.strip(), scenario.strip())
    print(f"fake provider on {provider.base()}", flush=True)
    try:
        provider._thread.join()  # type: ignore[union-attr]
    except KeyboardInterrupt:
        provider.stop()


if __name__ == "__main__":
    main()
