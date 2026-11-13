#!/usr/bin/env python3
"""Check the LiteLLM this machine runs against what the repo pins. Read-only, no keys.

    python scripts/verify_litellm.py            # the installed package and the incident's indicator files
    python scripts/verify_litellm.py --image    # also: the compose image's digest, asked from the registry

Standard library only. It changes nothing and reads no provider key.

What it checks (background: LiteLLM's incident of 2026-10-21, when
litellm==1.107.0 on PyPI and the image tags v1.107.0, latest, main-latest and
main-stable were malicious for about two hours; see README "Supply chain"):

1. The installed litellm: its version against the pin in requirements/proxy.lock,
   never 1.107.0, and every installed file against the SHA-256 that pip wrote to
   the package's RECORD at install time. That shows a file changed AFTER the
   install. It cannot show that the wheel itself was the right one: a wheel's
   own hash cannot be recomputed from an installed copy. Only
   `pip install --require-hashes -r requirements/proxy.lock` checks that, at
   install time.
2. The indicator files the incident page names, in each site-packages directory:
   cook.pth, cook.py, an unexpected sitecustomize.py, their compiled copies, a
   litellm-1.107.0.dist-info directory, and the lock file
   /tmp/remote_code_loader.lock. Finding one confirms exposure. Finding none
   does not prove the environment is clean (LiteLLM says the same).
3. With --image: the `image:` lines of docker-compose.yml and
   docker-compose.spend.yml. A LiteLLM image must be pinned by digest, the tag
   must not be a floating one, the digest must not be a known-bad one, and the
   registry must still answer the tag with the pinned digest, for both
   linux/amd64 and linux/arm64. This is the one check that uses the network.

If you suspect an environment, do not start its own Python (the malware ran from
a .pth file on every start). Inspect it from another interpreter:

    /usr/bin/python3 scripts/verify_litellm.py --site-packages /path/to/venv/lib/python3.12/site-packages

Exit status: 0 nothing wrong found, 1 a problem, 2 something could not be checked.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import json
import re
import site
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
LOCK_FILE = ROOT / "requirements" / "proxy.lock"
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.spend.yml")

COMPROMISED_VERSIONS = frozenset({"1.107.0"})
# Image index digests LiteLLM listed as compromised. The incident page prints the
# first in full and the other two (litellm-database, litellm-non_root) shortened.
KNOWN_BAD_DIGESTS = frozenset({"sha256:1bbcb1443bc962e45f5aad53fe3d2c30600e0379715fb6c664cdd3ac126c339e"})
KNOWN_BAD_DIGEST_PREFIXES = ("sha256:1bbcb1443bc9", "sha256:c00f9d92c427", "sha256:7b00e91cd2fe")
FLOATING_TAGS = frozenset({"latest", "main-latest", "main-stable", "main", "stable", "nightly", "dev"})
INDICATOR_FILES = ("cook.pth", "cook.py", "sitecustomize.py")
INDICATOR_PYC = ("cook*.pyc", "sitecustomize*.pyc")
RUNTIME_LOCK = Path("/tmp/remote_code_loader.lock")  # held by the malware's detached process
REQUIRED_PLATFORMS = (("linux", "amd64"), ("linux", "arm64"))
ACCEPT = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
TIMEOUT = 20

OK, PROBLEM, UNKNOWN, NOTE = "ok", "PROBLEM", "could not check", "note"


@dataclass(frozen=True)
class Finding:
    level: str
    message: str


# A fetch is (url, headers, method) -> (status, lower-cased headers, body). Tests pass a fake.
Fetch = Callable[[str, Mapping[str, str], str], "tuple[int, dict[str, str], bytes]"]


class CannotCheck(Exception):
    pass


# ---------------------------------------------------------------- image references

@dataclass(frozen=True)
class ImageRef:
    raw: str
    registry: str
    repository: str
    tag: str | None
    digest: str | None

    @property
    def is_litellm(self) -> bool:
        return "litellm" in self.repository.lower()


def parse_image(raw: str) -> ImageRef:
    """registry/repository[:tag][@sha256:...]. Docker Hub is the default registry."""
    rest, _, digest = raw.partition("@")
    tag: str | None = None
    last = rest.rsplit("/", 1)[-1]
    if ":" in last:
        rest, _, tag = rest.rpartition(":")
    first, _, remainder = rest.partition("/")
    if remainder and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, remainder
    else:
        registry, repository = "docker.io", rest if "/" in rest else f"library/{rest}"
    return ImageRef(raw, registry.lower(), repository, tag or None, digest.lower() or None)


_IMAGE_LINE = re.compile(r"""^\s*image:\s*["']?([^\s"'#]+)""")


def compose_images(text: str) -> list[ImageRef]:
    """Every `image:` value in a compose file. Line-based on purpose: no YAML library needed."""
    found = []
    for line in text.splitlines():
        match = _IMAGE_LINE.match(line)
        if match:
            found.append(parse_image(match.group(1)))
    return found


def known_bad(digest: str | None) -> bool:
    digest = (digest or "").lower()
    return bool(digest) and (digest in KNOWN_BAD_DIGESTS or digest.startswith(KNOWN_BAD_DIGEST_PREFIXES))


def image_pin_problems(ref: ImageRef) -> list[str]:
    """Why this LiteLLM image reference is not acceptable in a compose file. No network."""
    problems = []
    if ref.tag is not None and ref.tag.lower() in FLOATING_TAGS:
        problems.append(f"{ref.raw}: '{ref.tag}' is a floating tag; use a version tag with its @sha256: digest")
    if ref.tag is not None and ref.tag.lower().lstrip("v") in COMPROMISED_VERSIONS:
        problems.append(f"{ref.raw}: {ref.tag} is the compromised release")
    if ref.digest is None:
        problems.append(f"{ref.raw}: no @sha256: digest, so the tag decides what runs")
    elif not re.fullmatch(r"sha256:[0-9a-f]{64}", ref.digest):
        problems.append(f"{ref.raw}: the digest is not a full sha256")
    elif known_bad(ref.digest):
        problems.append(f"{ref.raw}: this is a digest LiteLLM listed as compromised on 2026-10-21")
    return problems


# ---------------------------------------------------------------- the registry

def http_fetch(url: str, headers: Mapping[str, str], method: str = "GET") -> tuple[int, dict[str, str], bytes]:
    if not url.startswith("https://"):
        raise CannotCheck(f"refusing a non-HTTPS URL: {url}")
    request = urllib.request.Request(url, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310  # https only, checked above
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()
    except (urllib.error.URLError, OSError, ValueError) as err:
        raise CannotCheck(f"{type(err).__name__} while asking {url.split('/')[2]}") from None


def registry_host(registry: str) -> tuple[str, str]:
    """(API host, token URL prefix) for the registries this repo uses."""
    if registry == "ghcr.io":
        return "ghcr.io", "https://ghcr.io/token?service=ghcr.io&scope=repository:{repo}:pull"
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        return "registry-1.docker.io", "https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
    raise CannotCheck(f"no anonymous lookup implemented for the registry {registry}")


def resolve_index(ref: ImageRef, fetch: Fetch) -> tuple[str, dict]:
    """(digest, manifest JSON) the registry gives for this reference's TAG right now."""
    if ref.tag is None:
        raise CannotCheck(f"{ref.raw} has no tag to look up")
    host, token_url = registry_host(ref.registry)
    status, _, body = fetch(token_url.format(repo=ref.repository), {}, "GET")
    if status != 200:
        raise CannotCheck(f"{host} refused an anonymous token (HTTP {status})")
    try:
        token = json.loads(body).get("token") or json.loads(body).get("access_token")
    except (ValueError, AttributeError):
        token = None
    if not token:
        raise CannotCheck(f"{host} returned no token")
    url = f"https://{host}/v2/{ref.repository}/manifests/{ref.tag}"
    status, headers, body = fetch(url, {"Authorization": f"Bearer {token}", "Accept": ACCEPT}, "GET")
    if status == 404:
        raise CannotCheck(f"{host} does not have the tag {ref.tag} (removed, or never published)")
    if status != 200:
        raise CannotCheck(f"{host} answered HTTP {status} for the tag {ref.tag}")
    computed = "sha256:" + hashlib.sha256(body).hexdigest()
    stated = (headers.get("docker-content-digest") or "").lower()
    if stated and stated != computed:
        raise CannotCheck(f"{host} stated {stated} but the manifest it sent hashes to {computed}")
    try:
        manifest = json.loads(body)
    except ValueError:
        raise CannotCheck(f"{host} sent a manifest that is not JSON") from None
    return computed, manifest if isinstance(manifest, dict) else {}


def platforms(manifest: Mapping) -> dict[tuple[str, str], str]:
    """(os, architecture) -> digest for the runnable images in an index (attestations left out)."""
    out: dict[tuple[str, str], str] = {}
    for entry in manifest.get("manifests") or []:
        platform = entry.get("platform") or {}
        key = (str(platform.get("os", "")), str(platform.get("architecture", "")))
        if "unknown" in key or not all(key):
            continue
        out[key] = str(entry.get("digest", "")).lower()
    return out


def check_image(root: Path = ROOT, fetch: Fetch = http_fetch, files: Iterable[str] = COMPOSE_FILES) -> list[Finding]:
    findings: list[Finding] = []
    refs: list[tuple[str, ImageRef]] = []
    for name in files:
        path = root / name
        if not path.is_file():
            continue
        refs += [(name, ref) for ref in compose_images(path.read_text(encoding="utf-8")) if ref.is_litellm]
    if not refs:
        return [Finding(UNKNOWN, "no LiteLLM image found in " + " or ".join(files))]
    for name, ref in refs:
        problems = image_pin_problems(ref)
        findings += [Finding(PROBLEM, f"{name}: {p}") for p in problems]
        if ref.digest is None or ref.tag is None:
            if ref.tag is None and not problems:
                findings.append(Finding(UNKNOWN, f"{name}: {ref.raw} has no tag, so the registry cannot be asked what the tag points at"))
            continue
        try:
            digest, manifest = resolve_index(ref, fetch)
        except CannotCheck as exc:
            findings.append(Finding(UNKNOWN, f"{name}: {exc}; the pinned digest was not compared with the registry"))
            continue
        if known_bad(digest):
            findings.append(Finding(PROBLEM, f"{name}: the registry answers {ref.tag} with {digest}, a digest LiteLLM listed as compromised"))
        elif digest != ref.digest:
            findings.append(Finding(PROBLEM, f"{name}: the registry answers {ref.tag} with {digest}, but the file pins {ref.digest}. "
                                             "A release tag should never move: do not pull until you know why"))
        else:
            findings.append(Finding(OK, f"{name}: {ref.registry}/{ref.repository}:{ref.tag} still resolves to the pinned {digest}"))
        per_platform = platforms(manifest)
        for key in REQUIRED_PLATFORMS:
            if key not in per_platform:
                findings.append(Finding(PROBLEM, f"{name}: the image index has no {key[0]}/{key[1]} image"))
            elif known_bad(per_platform[key]):
                findings.append(Finding(PROBLEM, f"{name}: the {key[0]}/{key[1]} image {per_platform[key]} is a known-bad digest"))
            else:
                findings.append(Finding(OK, f"{name}: {key[0]}/{key[1]} is {per_platform[key]}"))
    return findings


# ---------------------------------------------------------------- the installed package

def pinned_version(lock_file: Path = LOCK_FILE) -> str | None:
    try:
        text = lock_file.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^litellm==([^\s\\;]+)", text, re.MULTILINE)
    return match.group(1) if match else None


def default_site_dirs() -> list[Path]:
    dirs: list[str] = []
    try:
        dirs += site.getsitepackages()
    except AttributeError:  # some embedded interpreters
        pass
    try:
        user = site.getusersitepackages()
        dirs.append(user)
    except AttributeError:
        pass
    seen: list[Path] = []
    for item in dirs:
        path = Path(item)
        if path.is_dir() and path not in seen:
            seen.append(path)
    return seen


def find_litellm(site_dirs: Iterable[Path]) -> list[importlib.metadata.Distribution]:
    found = []
    for dist in importlib.metadata.distributions(path=[str(d) for d in site_dirs]):
        if (dist.metadata["Name"] or "").lower() == "litellm":
            found.append(dist)
    return found


def record_mismatches(dist: importlib.metadata.Distribution) -> tuple[int, list[str], list[str]] | None:
    """(files checked, changed, missing) against the hashes in RECORD; None when there is no RECORD.

    RECORD is read directly: `Distribution.files` leaves out files that no longer
    exist on newer Pythons, which would hide a deleted file.
    """
    record = dist.read_text("RECORD")
    if record is None:
        return None
    checked, changed, missing = 0, [], []
    for row in csv.reader(record.splitlines()):
        if len(row) < 2 or not row[1]:  # RECORD itself and files written at install time (.pyc, INSTALLER)
            continue
        name, (mode, _, value) = row[0], row[1].partition("=")
        path = Path(str(dist.locate_file(name)))
        try:
            hasher = hashlib.new(mode)
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    hasher.update(chunk)
        except FileNotFoundError:
            missing.append(name)
            continue
        except (OSError, ValueError):
            changed.append(f"{name} (unreadable)")
            continue
        checked += 1
        if base64.urlsafe_b64encode(hasher.digest()).rstrip(b"=").decode() != value:
            changed.append(name)
    return checked, changed, missing


def check_package(site_dirs: Iterable[Path], lock_file: Path = LOCK_FILE, required: bool = True) -> list[Finding]:
    site_dirs = list(site_dirs)
    dists = find_litellm(site_dirs)
    if not dists:
        where = ", ".join(str(d) for d in site_dirs) or "no site-packages directory"
        level = UNKNOWN if required else NOTE
        return [Finding(level, f"litellm is not installed in {where} (a Docker deployment has it in the image: use --image)")]
    pin = pinned_version(lock_file)
    findings: list[Finding] = []
    if len(dists) > 1:
        findings.append(Finding(PROBLEM, "more than one litellm installation: " + ", ".join(sorted(d.version for d in dists))))
    for dist in dists:
        version = dist.version
        if version in COMPROMISED_VERSIONS:
            findings.append(Finding(PROBLEM, f"litellm {version} is the compromised release. Treat this environment as compromised: "
                                             "do not run its Python, rebuild it, and rotate every key it could reach"))
        elif pin is None:
            findings.append(Finding(UNKNOWN, f"litellm {version} is installed, but {lock_file} has no litellm pin to compare with"))
        elif version != pin:
            findings.append(Finding(PROBLEM, f"litellm {version} is installed, but the repo pins {pin} ({lock_file.name})"))
        else:
            findings.append(Finding(OK, f"litellm {version} is installed, the version {lock_file.name} pins"))
        result = record_mismatches(dist)
        if result is None:
            findings.append(Finding(UNKNOWN, f"litellm {version} has no RECORD file, so its installed files were not compared"))
            continue
        checked, changed, missing = result
        if changed:
            shown = ", ".join(changed[:5]) + (f" and {len(changed) - 5} more" if len(changed) > 5 else "")
            findings.append(Finding(PROBLEM, f"{len(changed)} installed litellm file(s) differ from what pip recorded at install: {shown}"))
        if missing:
            shown = ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
            findings.append(Finding(PROBLEM, f"{len(missing)} litellm file(s) listed at install are gone: {shown}"))
        if checked == 0 and not changed and not missing:
            findings.append(Finding(UNKNOWN, f"litellm {version}: RECORD lists no file hashes, nothing was compared"))
        elif not changed and not missing:
            findings.append(Finding(OK, f"{checked} installed litellm files match the hashes pip recorded at install"))
        findings.append(Finding(NOTE, "not checked: that the wheel pip installed was the one in the lock file. A wheel's hash cannot be "
                                      "recomputed from an installed copy; `pip install --require-hashes -r requirements/proxy.lock` checks it at install time"))
    return findings


# ---------------------------------------------------------------- indicator files

def check_indicators(site_dirs: Iterable[Path], runtime_lock: Path = RUNTIME_LOCK) -> list[Finding]:
    site_dirs = list(site_dirs)
    findings: list[Finding] = []
    hits: list[str] = []
    unreadable: list[str] = []
    for directory in site_dirs:
        try:
            for name in INDICATOR_FILES:
                if (directory / name).exists():
                    hits.append(str(directory / name))
            cache = directory / "__pycache__"
            if cache.is_dir():
                for pattern in INDICATOR_PYC:
                    hits += [str(p) for p in sorted(cache.glob(pattern))]
            hits += [str(p) for p in sorted(directory.glob("litellm-1.107.0*.dist-info"))]
        except OSError:
            unreadable.append(str(directory))
    for path in hits:
        what = "an unexpected sitecustomize in site-packages: read it before trusting this environment" \
            if "sitecustomize" in Path(path).name else "an indicator file from the 2026-10-21 incident"
        findings.append(Finding(PROBLEM, f"{path}: {what}"))
    try:
        if runtime_lock.exists():
            findings.append(Finding(PROBLEM, f"{runtime_lock} exists: the incident's remote-code loader holds this file while it runs"))
    except OSError:
        unreadable.append(str(runtime_lock))
    for path in unreadable:
        findings.append(Finding(UNKNOWN, f"{path} could not be read"))
    if not site_dirs:
        findings.append(Finding(UNKNOWN, "no site-packages directory to look in"))
    elif not hits and not unreadable:
        findings.append(Finding(OK, f"no indicator file in {len(site_dirs)} site-packages director{'y' if len(site_dirs) == 1 else 'ies'} "
                                    f"and no {runtime_lock} (this does not prove an environment was never exposed)"))
    return findings


# ---------------------------------------------------------------- command line

def exit_code(findings: Iterable[Finding]) -> int:
    levels = {f.level for f in findings}
    if PROBLEM in levels:
        return 1
    if UNKNOWN in levels:
        return 2
    return 0


def main(argv: list[str] | None = None, *, fetch: Fetch = http_fetch, out: Callable[[str], None] = print,
         root: Path = ROOT, runtime_lock: Path = RUNTIME_LOCK) -> int:
    parser = argparse.ArgumentParser(description="Check the installed LiteLLM and the pinned image. Read-only, no keys.")
    parser.add_argument("--image", action="store_true", help="also ask the registry what the compose image tag points at (network)")
    parser.add_argument("--site-packages", action="append", metavar="DIR", default=None,
                        help="inspect this site-packages directory instead of this interpreter's (repeatable)")
    args = parser.parse_args(argv)

    if args.site_packages:
        site_dirs = [Path(p) for p in args.site_packages]
        absent = [str(p) for p in site_dirs if not p.is_dir()]
        if absent:
            out(f"[{UNKNOWN}] not a directory: {', '.join(absent)}")
            return 2
    else:
        site_dirs = default_site_dirs()

    findings = check_package(site_dirs, root / "requirements" / "proxy.lock", required=not args.image)
    findings += check_indicators(site_dirs, runtime_lock)
    if args.image:
        findings += check_image(root, fetch)
    for finding in findings:
        out(f"[{finding.level}] {finding.message}")
    code = exit_code(findings)
    out({0: "result: nothing wrong found", 1: "result: PROBLEM found, see above",
         2: "result: nothing wrong found, but something could not be checked (see above)"}[code])
    return code


if __name__ == "__main__":
    sys.exit(main())
