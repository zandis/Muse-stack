#!/usr/bin/env python3
"""Build a release manifest and sign it with an Ed25519 key.

The private key stays on the owner's machine. The VM only has the public
half, in MUSE_UPDATE_PUBKEY. This script can also generate the keypair.

    sign_manifest.py generate --dir DIR
        Write DIR/release.key (mode 0600) and print the public key (base64 of
        the raw 32 bytes) on stdout. Refuses to overwrite an existing key.
        The private key is never printed.

    sign_manifest.py manifest --version 0.2.0 --url https://.../muse-0.2.0.tar.gz \
        --sha256 <64 hex> --commit <40 hex> --expires 2026-12-15T00:00:00Z \
        --out stable.json
        Write the canonical manifest bytes. The same inputs always produce
        the same bytes. Sign these bytes; do not reformat them afterwards.

    sign_manifest.py sign --manifest stable.json --out stable.json.sig [--key FILE]
        Sign the manifest's exact bytes. The key is FILE (one line, base64 of
        the 32-byte seed, mode 0600) or the environment variable
        MUSE_RELEASE_SIGNING_KEY. The signature on stdout is not used; it is
        written only to --out.

    sign_manifest.py expires --days 30
        Print an expiry timestamp that many days from now, in UTC. The VM
        rejects anything more than 90 days away.

A GitHub Actions fallback can run `sign` when the owner has stored the seed
in an environment secret. The default is to run `sign` on the owner's
machine. See docs/deploy/update.md.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.]+)?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


class SignError(RuntimeError):
    pass


def canonical_manifest(fields: dict[str, str]) -> bytes:
    return (json.dumps(fields, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build_manifest(
    *,
    version: str,
    url: str,
    sha256: str,
    expires: str,
    commit: str = "",
    channel: str = "stable",
) -> bytes:
    version = version[1:] if version.startswith("v") else version
    if VERSION.fullmatch(version) is None:
        raise SignError(f"'{version}' is not a fixed version")
    if not url.startswith("https://") or any(ch.isspace() for ch in url):
        raise SignError("the release address is not https")
    if SHA256.fullmatch(sha256) is None:
        raise SignError("sha256 must be 64 hex characters")
    if channel != "stable":
        raise SignError("only the stable channel is published")
    if commit and COMMIT.fullmatch(commit) is None:
        raise SignError("commit must be a 40-character hex sha")
    fields = {
        "channel": channel,
        "expires": expires,
        "sha256": sha256,
        "url": url,
        "version": version,
    }
    if commit:
        fields["commit"] = commit
    return canonical_manifest(fields)


def expires_in(days: int, now: datetime | None = None) -> str:
    if days < 1 or days > 90:
        raise SignError("--days must be from 1 to 90")
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = (moment.astimezone(timezone.utc) + timedelta(days=days)).replace(microsecond=0)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _private_key(seed: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if len(seed) != 32:
        raise SignError("the private key must be 32 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed)


def _decode_seed(text: str) -> bytes:
    try:
        return base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignError("the private key is not valid base64") from exc


def public_from_seed(seed: bytes) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = _private_key(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.b64encode(raw).decode("ascii")


def sign_bytes(manifest: bytes, seed: bytes) -> bytes:
    return base64.b64encode(_private_key(seed).sign(manifest)) + b"\n"


def _read_seed(path: Path | None) -> bytes:
    if path is None:
        text = os.environ.get("MUSE_RELEASE_SIGNING_KEY", "")
        if not text.strip():
            raise SignError("set --key or MUSE_RELEASE_SIGNING_KEY")
        return _decode_seed(text)
    mode = path.stat().st_mode
    if mode & 0o077:
        raise SignError(f"{path} is readable by someone other than the owner; refusing to sign")
    return _decode_seed(path.read_text(encoding="utf-8"))


def generate(directory: Path) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    key_path = directory / "release.key"
    pub_path = directory / "release.pub"
    if key_path.exists() or pub_path.exists():
        raise SignError(f"{directory} already has a release key; refusing to overwrite it")
    seed = os.urandom(32)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, base64.b64encode(seed) + b"\n")
    finally:
        os.close(fd)
    os.chmod(key_path, 0o600)
    public = public_from_seed(seed)
    pub_path.write_text(public + "\n", encoding="utf-8")
    os.chmod(pub_path, 0o644)
    # The seed must not remain reachable from a caller that prints this function's
    # result. Drop the local name; the file holds the only copy we keep.
    del seed
    print(f"private key written to {key_path}", file=sys.stderr)
    return public


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Sign a Muse release manifest.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    gen = sub.add_parser("generate", help="Write a new keypair. Prints the public key.")
    gen.add_argument("--dir", type=Path, required=True)

    man = sub.add_parser("manifest", help="Write canonical manifest JSON.")
    man.add_argument("--version", required=True)
    man.add_argument("--url", required=True)
    man.add_argument("--sha256", required=True)
    man.add_argument("--expires", required=True)
    man.add_argument("--commit", default="")
    man.add_argument("--out", type=Path, required=True)

    signer = sub.add_parser("sign", help="Sign a manifest file.")
    signer.add_argument("--manifest", type=Path, required=True)
    signer.add_argument("--out", type=Path, required=True)
    signer.add_argument("--key", type=Path)

    exp = sub.add_parser("expires", help="Print an expiry timestamp.")
    exp.add_argument("--days", type=int, default=30)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "generate":
            print(generate(args.dir))
        elif args.cmd == "expires":
            print(expires_in(args.days))
        elif args.cmd == "manifest":
            payload = build_manifest(
                version=args.version,
                url=args.url,
                sha256=args.sha256,
                expires=args.expires,
                commit=args.commit,
            )
            args.out.write_bytes(payload)
        else:
            signature = sign_bytes(args.manifest.read_bytes(), _read_seed(args.key))
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, signature)
            finally:
                os.close(fd)
    except SignError as exc:
        print(f"sign: {exc}", file=sys.stderr)
        return 1
    except ImportError:
        print("sign: the cryptography package is required", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
