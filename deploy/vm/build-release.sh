#!/usr/bin/env bash
# Make the Muse release tarball the always-on helper installs, and print its SHA-256.
#
#   deploy/vm/build-release.sh --version 0.2.0 [--ref v0.2.0] [--out DIR]
#
# The tarball is `git archive` of one commit (default: the tag v<version>),
# with the single top-level folder muse-<version>/, compressed without a
# timestamp, so the same commit always gives the same bytes and the same
# checksum. Publish it at an https address anyone can fetch without signing in
# (the helper has no credentials), then pass that address and the checksum to
# deploy/oracle-stack/build-zip.sh.
#
# Only committed files go in. It never contains .env, keys or local state.

set -euo pipefail
VERSION="" REF="" OUT="."
while [ $# -gt 0 ]; do
  case "$1" in
    --version) shift; VERSION="${1:?--version needs a value}" ;;
    --ref) shift; REF="${1:?--ref needs a commit or tag}" ;;
    --out) shift; OUT="${1:?--out needs a folder}" ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
VERSION="${VERSION#v}"
printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.]+)?$' \
  || { echo "ERROR: --version must be a fixed release such as 0.2.0" >&2; exit 1; }
[ -n "$REF" ] || REF="v$VERSION"

ROOT="$(git rev-parse --show-toplevel)"
COMMIT="$(git -C "$ROOT" rev-parse --verify "$REF^{commit}")" \
  || { echo "ERROR: no such commit or tag: $REF" >&2; exit 1; }
mkdir -p "$OUT"
FILE="$OUT/muse-$VERSION.tar.gz"
git -C "$ROOT" archive --format=tar --prefix="muse-$VERSION/" "$COMMIT" | gzip -n -9 > "$FILE"

echo "release:  $FILE"
echo "commit:   $COMMIT"
echo "sha256:   $(sha256sum -- "$FILE" | cut -d' ' -f1)"
