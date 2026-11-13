#!/usr/bin/env python3
"""Check a signed release manifest and say what to do with it.

    update_verify.py MANIFEST.json MANIFEST.json.sig PUBLIC_KEY CURRENT_VERSION

Prints one line and exits 0 when the manifest is authentic and well formed:

    newer <version> <https-url> <sha256>
    same <version>
    older <version>

Exits 1 with a reason on standard error otherwise.

The manifest (the owner publishes it; nothing here invents its address):

    {"channel": "stable", "version": "0.2.0",
     "url": "https://.../muse-0.2.0.tar.gz", "sha256": "<64 hex>"}

The signature file holds the base64 of an Ed25519 signature over the manifest's
exact bytes. PUBLIC_KEY is the base64 of the raw 32-byte Ed25519 public key
(MUSE_UPDATE_PUBKEY). Uses `cryptography`, pinned by hash in
requirements/proxy.lock.

Refused: a bad signature; a version that is a branch or a moving name (main,
latest, ...); an address that is not https; a checksum that is not a SHA-256;
a channel other than the one asked for (MUSE_UPDATE_CHANNEL, default stable).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import sys

VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)([.+-][0-9A-Za-z.]+)?$")
FLOATING = {"main", "master", "latest", "stable", "head", "dev", "develop", "nightly", "edge"}


class Refused(RuntimeError):
    pass


def parse_version(text: str) -> tuple[int, int, int]:
    match = VERSION.match(text or "")
    if text.lower() in FLOATING or match is None:
        raise Refused(f"'{text}' is not a fixed version (expected something like 0.2.0)")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def verify_signature(manifest: bytes, signature_b64: bytes, public_key_b64: str) -> None:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise Refused("the 'cryptography' package is needed to check the signature") from exc
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        signature = base64.b64decode(signature_b64.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise Refused("the public key or the signature is not valid base64 of the right length") from None
    try:
        key.verify(signature, manifest)
    except InvalidSignature:
        raise Refused("the manifest's signature does not match the public key") from None


def decide(manifest: bytes, signature_b64: bytes, public_key_b64: str, current: str, channel: str = "stable") -> str:
    if not public_key_b64.strip():
        raise Refused("no public key is set (MUSE_UPDATE_PUBKEY); updates stay off")
    verify_signature(manifest, signature_b64, public_key_b64.strip())
    try:
        doc = json.loads(manifest)
    except ValueError:
        raise Refused("the manifest is not JSON") from None
    if not isinstance(doc, dict):
        raise Refused("the manifest is not a JSON object")
    version, url, sha256 = str(doc.get("version", "")), str(doc.get("url", "")), str(doc.get("sha256", ""))
    if str(doc.get("channel", "")) != channel:
        raise Refused(f"the manifest is for channel '{doc.get('channel')}', not '{channel}'")
    new = parse_version(version)
    if not url.startswith("https://") or any(ch.isspace() for ch in url):
        raise Refused("the release address is not https")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise Refused("the manifest's sha256 is not a SHA-256")
    try:
        old = parse_version(current)
    except Refused:
        old = (0, 0, 0)   # an unknown current version: anything fixed is newer
    if new > old:
        return f"newer {version} {url} {sha256}"
    return f"same {version}" if new == old else f"older {version}"


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        with open(argv[0], "rb") as handle:
            manifest = handle.read()
        with open(argv[1], "rb") as handle:
            signature = handle.read()
        print(decide(manifest, signature, argv[2], argv[3], os.environ.get("MUSE_UPDATE_CHANNEL", "stable") or "stable"))
    except (Refused, OSError) as exc:
        print(f"update refused: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
