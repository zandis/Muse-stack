#!/usr/bin/env bash
# Build the Resource Manager stack zip, the file behind the "Deploy to Oracle
# Cloud" button.
#
#   deploy/oracle-stack/build-zip.sh --version 0.2.0 \
#       --release-url https://example.org/muse-0.2.0.tar.gz \
#       --release-sha256 <64 hex digits> [--out muse-oracle-stack-0.2.0.zip]
#
# It pins ONE Muse release into the zip (release_pin.tf), so the person
# deploying has nothing to type: first boot downloads exactly that tarball and
# refuses it unless its SHA-256 matches. Make the tarball and its checksum with
# deploy/vm/build-release.sh.
#
# The zip is deterministic: the same inputs give the same bytes (files in
# sorted order, fixed timestamps and permissions, no extra fields), so the
# SHA-256 printed at the end can be published next to it and checked by anyone.
#
# Refused: a version that is a branch or a moving name (main, latest, ...), an
# address that is not https, a checksum that is not a SHA-256.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VERSION="" URL="" SHA="" OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --version) shift; VERSION="${1:?--version needs a value}" ;;
    --release-url) shift; URL="${1:?--release-url needs a value}" ;;
    --release-sha256) shift; SHA="${1:?--release-sha256 needs a value}" ;;
    --out) shift; OUT="${1:?--out needs a file name}" ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
fail() { echo "ERROR: $*" >&2; exit 1; }

VERSION="${VERSION#v}"
case "$VERSION" in
  main|master|latest|stable|HEAD|head|dev|develop|nightly|edge|"") fail "--version must be a fixed release such as 0.2.0, not '${VERSION:-<empty>}'" ;;
esac
printf '%s' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([.+-][0-9A-Za-z.]+)?$' || fail "--version must look like 0.2.0"
printf '%s' "$URL" | grep -Eq "^https://[^[:space:]'\"]+$" || fail "--release-url must start with https://"
printf '%s' "$SHA" | grep -Eq '^[0-9a-f]{64}$' || fail "--release-sha256 must be 64 lower-case hex digits"
[ -n "$OUT" ] || OUT="muse-oracle-stack-$VERSION.zip"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
for file in "$HERE"/*.tf "$HERE/schema.yaml" "$HERE/cloud-init.yaml.tftpl"; do
  cp "$file" "$WORK/"
done
cat > "$WORK/release_pin.tf" <<EOF
# Written by build-zip.sh. The Muse release this stack installs.

locals {
  pinned_release = {
    version = "$VERSION"
    url     = "$URL"
    sha256  = "$SHA"
  }
}
EOF

python3 - "$WORK" "$OUT" <<'PY'
import os, sys, zipfile
src, out = sys.argv[1], sys.argv[2]
names = sorted(os.listdir(src))
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for name in names:
        info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))   # fixed, so the zip is reproducible
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        info.create_system = 3
        with open(os.path.join(src, name), "rb") as handle:
            archive.writestr(info, handle.read(), compresslevel=9)
PY

echo "stack zip: $OUT"
echo "contents:  $(cd "$WORK" && ls | sort | tr '\n' ' ')"
echo "sha256:    $(sha256sum -- "$OUT" | cut -d' ' -f1)"
