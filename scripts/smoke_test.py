#!/usr/bin/env python3
"""Hit each public alias on a running proxy. Skip aliases that were not configured.

This does not invent provider keys. An alias is attempted only when the proxy's
/v1/models list contains it, which happens only for providers whose keys were
set when the proxy rendered its config.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ALIASES = (
    ("fast", "chat"),
    ("cheap", "chat"),
    ("smart", "chat"),
    ("long-context", "chat"),
    ("embed", "embedding"),
    ("rerank", "rerank"),
)


def load_dotenv(path: Path, environ: dict[str, str]) -> None:
    """Fill environ from KEY=VALUE lines. Existing keys win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        environ[key] = value


def request(url: str, *, key: str, payload: dict | None, timeout: float) -> tuple[int, bytes]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def model_ids(body: bytes) -> set[str]:
    parsed = json.loads(body.decode("utf-8"))
    return {item["id"] for item in parsed.get("data", []) if "id" in item}


def call_alias(base: str, name: str, kind: str, key: str, timeout: float) -> tuple[bool, str]:
    if kind == "chat":
        url = f"{base}/v1/chat/completions"
        payload = {
            "model": name,
            "messages": [{"role": "user", "content": "Reply with the single word pong."}],
            "max_tokens": 16,
            "temperature": 0,
        }
    elif kind == "embedding":
        url = f"{base}/v1/embeddings"
        payload = {"model": name, "input": "muse stack smoke test"}
    else:
        url = f"{base}/v1/rerank"
        payload = {
            "model": name,
            "query": "blood pressure",
            "documents": ["systolic pressure", "the weather in Taipei"],
            "top_n": 1,
        }
    status, body = request(url, key=key, payload=payload, timeout=timeout)
    if kind == "rerank" and status == 404:
        status, body = request(f"{base}/rerank", key=key, payload=payload, timeout=timeout)
    if 200 <= status < 300:
        return True, f"HTTP {status}"
    snippet = body.decode("utf-8", errors="replace")[:300].replace("\n", " ")
    return False, f"HTTP {status} {snippet}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test muse-stack aliases.")
    parser.add_argument("--base-url", default=os.environ.get("MUSE_BASE_URL", "http://127.0.0.1:4000"))
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--strict", action="store_true", help="Fail if any public alias is missing.")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Call only these aliases. A missing alias fails. The updater uses --only fast.",
    )
    args = parser.parse_args(argv)

    environ = dict(os.environ)
    load_dotenv(args.env_file, environ)
    key = environ.get("LITELLM_MASTER_KEY", "").strip()
    base = args.base_url.rstrip("/")

    known = dict(ALIASES)
    selected = list(ALIASES)
    if args.only:
        if not key:
            print("FAIL  --only needs LITELLM_MASTER_KEY in the env file")
            return 2
        selected = []
        for name in args.only:
            if name not in known:
                print(f"FAIL  unknown alias {name}")
                return 2
            selected.append((name, known[name]))

    try:
        status, body = request(f"{base}/health/liveliness", key="", payload=None, timeout=10)
    except Exception as exc:  # noqa: BLE001 - report any connection failure the same way
        print(f"FAIL  proxy unreachable at {base}: {exc}")
        return 2
    if status != 200:
        print(f"FAIL  /health/liveliness HTTP {status}")
        return 2
    print("ok    /health/liveliness")

    status, body = request(f"{base}/v1/models", key=key, payload=None, timeout=20)
    if status != 200:
        print(f"FAIL  /v1/models HTTP {status} {body.decode('utf-8', errors='replace')[:200]}")
        return 2
    available = model_ids(body)
    print(f"ok    /v1/models ({len(available)} ids)")

    failed = False
    for name, kind in selected:
        if name not in available:
            if args.only or args.strict:
                print(f"FAIL  {name}  (not in /v1/models)")
                failed = True
            else:
                print(f"skip  {name}  (no deployment; provider key missing or disabled)")
            continue
        ok, detail = call_alias(base, name, kind, key, args.timeout)
        if ok:
            print(f"ok    {name}  {detail}")
        else:
            print(f"FAIL  {name}  {detail}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
