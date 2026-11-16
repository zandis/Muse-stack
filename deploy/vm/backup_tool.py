#!/usr/bin/env python3
"""The plain (not yet encrypted) stage of a backup, and its reverse.

    backup_tool.py stage   --home MUSE_HOME --out FILE.tar.gz [--env-file DOTENV]
    backup_tool.py restore --tar FILE.tar.gz --target NEW_DIR [--keys-out FILE]
    backup_tool.py inspect --tar FILE.tar.gz

Standard library only. muse-backup.sh encrypts what `stage` writes with age
before anything leaves the machine; muse-restore.sh decrypts, then calls
`restore`. The plain archive exists only for a moment, mode 0600, in a private
work folder.

What a backup contains
----------------------
Everything under MUSE_HOME that is a regular file, EXCEPT:

  - the private half of the VM's own backup key (backup-key/), which must
    never travel with the data it protects;
  - things that are rebuilt or meaningless elsewhere: sockets, pid files,
    logs, the rendered LiteLLM config, SQLite side files (-wal, -shm,
    -journal), temporary files, caches.

SQLite databases (ledger, queue, cards, records, chores, ...) are copied with
SQLite's online backup API, so each copy is consistent even while the services
keep writing; nothing is stopped. Each copy is then checked with
`PRAGMA integrity_check`.

That includes whatever secrets live in MUSE_HOME: the device signing key
(device.key), hashed device tokens, the apps file, queued prompts and results.

Provider keys and the proxy master key live OUTSIDE MUSE_HOME, in the dotenv
file named by MUSE_ENV_FILE. They are left out unless --env-file is given, in
which case that file goes into the same archive (as keys/muse.env) and is
therefore encrypted the same way.

manifest.json at the top of the archive lists every file with its size and
SHA-256; `restore` refuses an archive whose contents do not match it.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import stat
import sys
import tarfile
import tempfile
import time
from pathlib import Path

FORMAT = 1
SQLITE_MAGIC = b"SQLite format 3\x00"
EXCLUDE_DIRS = {"backup-key", "__pycache__", ".cache"}
EXCLUDE_NAMES = ("*.sock", "*.pid", "*.log", "*-wal", "*-shm", "*-journal", "*.tmp", "*.part", "litellm-config.yaml")
KEYS_MEMBER = "keys/muse.env"


class BackupError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def integrity_ok(path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        try:
            return conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def copy_sqlite(source: Path, dest: Path) -> None:
    """A consistent copy of a live database (SQLite's online backup API)."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if not integrity_ok(dest):
        raise BackupError(f"the copy of {source.name} failed SQLite's integrity check")


def wanted(home: Path) -> list[Path]:
    """Regular files under home that belong in a backup, sorted."""
    out: list[Path] = []
    for root, dirs, files in os.walk(home, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDE_DIRS and not os.path.islink(os.path.join(root, d)))
        for name in sorted(files):
            path = Path(root) / name
            try:
                mode = path.lstat().st_mode
            except OSError:
                continue
            if not stat.S_ISREG(mode) or any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDE_NAMES):
                continue
            out.append(path)
    return out


def stage(home: Path, out: Path, env_file: Path | None = None, release: str = "") -> dict:
    if not home.is_dir():
        raise BackupError(f"{home} is not a folder")
    out.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".stage-", dir=out.parent))
    os.chmod(work, 0o700)
    try:
        entries = []
        for path in wanted(home):
            rel = path.relative_to(home).as_posix()
            dest = work / "home" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            kind = "sqlite" if is_sqlite(path) else "file"
            if kind == "sqlite":
                copy_sqlite(path, dest)
            else:
                shutil.copyfile(path, dest)
            entries.append({"path": f"home/{rel}", "kind": kind, "size": dest.stat().st_size, "sha256": sha256_file(dest)})
        includes_keys = False
        if env_file is not None and env_file.is_file():
            dest = work / KEYS_MEMBER
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(env_file, dest)
            entries.append({"path": KEYS_MEMBER, "kind": "keys", "size": dest.stat().st_size, "sha256": sha256_file(dest)})
            includes_keys = True
        manifest = {
            "format": FORMAT,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hostname": socket.gethostname(),
            "release": release,
            "includes_provider_keys": includes_keys,
            "files": entries,
        }
        data = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            info = tarfile.TarInfo("manifest.json")
            info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
            tar.addfile(info, io.BytesIO(data))
            for entry in entries:
                info = tarfile.TarInfo(entry["path"])
                info.size, info.mode, info.mtime = entry["size"], 0o600, int(time.time())
                with open(work / entry["path"], "rb") as handle:
                    tar.addfile(info, handle)
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _safe_member(name: str) -> bool:
    parts = Path(name).parts
    return bool(parts) and not name.startswith("/") and ".." not in parts and (name == "manifest.json" or parts[0] in ("home", "keys"))


def read_manifest(tar: tarfile.TarFile) -> dict:
    try:
        member = tar.getmember("manifest.json")
        manifest = json.load(tar.extractfile(member))  # type: ignore[arg-type]
    except (KeyError, ValueError, TypeError):
        raise BackupError("the archive has no readable manifest.json") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), list):
        raise BackupError("the archive's manifest is not one this version understands")
    return manifest


def restore(tar_path: Path, target: Path, keys_out: Path | None = None) -> dict:
    """Unpack into `target` (which must not exist yet), checking every file against the manifest."""
    if target.exists():
        raise BackupError(f"{target} already exists; restore unpacks into a new folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".restore-", dir=target.parent))
    os.chmod(work, 0o700)
    try:
        with tarfile.open(tar_path, mode="r:gz") as tar:
            manifest = read_manifest(tar)
            listed = {entry["path"]: entry for entry in manifest["files"]}
            members = [m for m in tar.getmembers() if m.name != "manifest.json"]
            for member in members:
                if not member.isreg() or not _safe_member(member.name) or member.name not in listed:
                    raise BackupError(f"unexpected entry in the archive: {member.name}")
            if {m.name for m in members} != set(listed):
                raise BackupError("the archive does not hold exactly the files its manifest lists")
            for member in members:
                entry = listed[member.name]
                dest = work / member.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    shutil.copyfileobj(source, handle)  # type: ignore[arg-type]
                if dest.stat().st_size != entry["size"] or sha256_file(dest) != entry["sha256"]:
                    raise BackupError(f"{member.name} does not match the manifest")
                if entry.get("kind") == "sqlite" and not integrity_ok(dest):
                    raise BackupError(f"{member.name} failed SQLite's integrity check")
        home = work / "home"
        home.mkdir(exist_ok=True)
        os.chmod(home, 0o700)
        keys = work / KEYS_MEMBER
        if keys.is_file() and keys_out is not None:
            keys_out.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(keys_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle, open(keys, "rb") as source:
                shutil.copyfileobj(source, handle)
        os.replace(home, target)
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def summary(manifest: dict) -> dict:
    files = manifest.get("files", [])
    return {
        "created_at": manifest.get("created_at"),
        "hostname": manifest.get("hostname"),
        "release": manifest.get("release"),
        "includes_provider_keys": bool(manifest.get("includes_provider_keys")),
        "files": len(files),
        "databases": sum(1 for f in files if f.get("kind") == "sqlite"),
        "bytes": sum(int(f.get("size", 0)) for f in files),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="backup_tool.py", description="Stage or unpack a Muse backup (plain stage; encryption is done by the caller).")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("stage")
    s.add_argument("--home", required=True, type=Path)
    s.add_argument("--out", required=True, type=Path)
    s.add_argument("--env-file", type=Path, default=None)
    s.add_argument("--release", default="")
    r = sub.add_parser("restore")
    r.add_argument("--tar", required=True, type=Path)
    r.add_argument("--target", required=True, type=Path)
    r.add_argument("--keys-out", type=Path, default=None)
    i = sub.add_parser("inspect")
    i.add_argument("--tar", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "stage":
            manifest = stage(args.home, args.out, args.env_file, args.release)
        elif args.command == "restore":
            manifest = restore(args.tar, args.target, args.keys_out)
        else:
            with tarfile.open(args.tar, mode="r:gz") as tar:
                manifest = read_manifest(tar)
    except (BackupError, OSError, tarfile.TarError, sqlite3.Error) as exc:
        print(f"backup_tool: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary(manifest), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
