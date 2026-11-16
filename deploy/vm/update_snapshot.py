#!/usr/bin/env python3
"""Snapshot MUSE_HOME before an update, and put that snapshot back on failure.

    update_snapshot.py snapshot --home DIR --out FILE.tar
    update_snapshot.py restore  --tar FILE.tar --home DIR

Standard library only. The archive stays on this machine (mode 0600). It is how
a failed release gives the previous release its data back, including when the
new release changed a SQLite schema.

SQLite databases are copied with the online backup API and checked with
PRAGMA integrity_check, so the copy is consistent while the proxy is still
serving. Sockets, pid files, logs and SQLite sidecars (-wal, -shm, -journal)
are left out; the backup API folds a live database into the main file.

Symlinks are stored as symlinks and never followed. restore refuses a member
whose path escapes the snapshot, and it refuses a database that fails its
integrity check. On success the whole of --home is replaced by the snapshot,
so files the failed release created do not linger.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path

FORMAT = 1
SQLITE_MAGIC = b"SQLite format 3\x00"
EXCLUDE_DIRS = {"__pycache__", ".cache"}
EXCLUDE_NAMES = ("*.sock", "*.pid", "*.log", "*-wal", "*-shm", "*-journal", "*.tmp", "*.part")


class SnapshotError(RuntimeError):
    pass


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
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and row[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def copy_sqlite(source: Path, dest: Path) -> None:
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    if not integrity_ok(dest):
        raise SnapshotError("a database failed its integrity check while it was being copied")


def _excluded(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in EXCLUDE_NAMES)


def _add_file(tar: tarfile.TarFile, path: Path, arcname: str, scratch: Path) -> None:
    if path.is_symlink():
        info = tarfile.TarInfo(arcname)
        info.type = tarfile.SYMTYPE
        info.linkname = os.readlink(path)
        tar.addfile(info)
        return
    if path.is_socket() or path.is_fifo() or not path.is_file():
        return
    if is_sqlite(path):
        copied = scratch / arcname.replace("/", "_")
        copy_sqlite(path, copied)
        tar.add(copied, arcname=arcname, recursive=False)
        return
    tar.add(path, arcname=arcname, recursive=False)


def snapshot(home: Path, dest: Path) -> None:
    home = home.resolve()
    if not home.is_dir():
        raise SnapshotError(f"no such directory: {home}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".snapshot.", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    scratch = Path(tempfile.mkdtemp(prefix=".snapshot-sql.", dir=str(dest.parent)))
    try:
        with tarfile.open(tmp, "w") as tar:
            payload = json.dumps({"format": FORMAT}).encode("utf-8")
            info = tarfile.TarInfo("snapshot.json")
            info.size = len(payload)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(payload))
            for dirpath, dirnames, filenames in os.walk(home, followlinks=False):
                dirnames[:] = sorted(name for name in dirnames if name not in EXCLUDE_DIRS)
                base = Path(dirpath)
                for name in sorted(filenames):
                    if _excluded(name):
                        continue
                    path = base / name
                    rel = path.relative_to(home).as_posix()
                    _add_file(tar, path, rel, scratch)
                for name in list(dirnames):
                    link = base / name
                    if link.is_symlink():
                        _add_file(tar, link, link.relative_to(home).as_posix(), scratch)
                        dirnames.remove(name)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _member_target(dest: Path, name: str) -> Path:
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts or rel.as_posix().startswith("/"):
        raise SnapshotError(f"unsafe path in snapshot: {name}")
    target = (dest / rel).resolve()
    if target != dest and dest not in target.parents:
        raise SnapshotError(f"unsafe path in snapshot: {name}")
    return target


def _extract(tar: tarfile.TarFile, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for member in tar.getmembers():
        target = _member_target(dest, member.name)
        if member.issym():
            link = member.linkname
            if not link or "\n" in link or "\x00" in link:
                raise SnapshotError(f"unsafe symlink in snapshot: {member.name}")
            if not Path(link).is_absolute():
                landing = (target.parent / link).resolve()
                if landing != dest and dest not in landing.parents:
                    raise SnapshotError(f"symlink escapes the snapshot: {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(link)
            continue
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, 0o700)
            continue
        if not member.isfile():
            continue
        source = tar.extractfile(member)
        if source is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with source, open(target, "wb") as handle:
            shutil.copyfileobj(source, handle)
        os.chmod(target, 0o600)


def restore(archive: Path, home: Path) -> None:
    home = home.resolve()
    parent = home.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".snapshot-stage.", dir=str(parent)))
    aside = Path(tempfile.mkdtemp(prefix=".snapshot-aside.", dir=str(parent)))
    moved = False
    swapped = False
    try:
        with tarfile.open(archive, "r:") as tar:
            _extract(tar, stage)
        marker = stage / "snapshot.json"
        try:
            doc = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SnapshotError("the snapshot has no readable format marker") from exc
        if not isinstance(doc, dict) or doc.get("format") != FORMAT:
            raise SnapshotError("the snapshot format is not one this VM can restore")
        marker.unlink()
        for path in stage.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if is_sqlite(path) and not integrity_ok(path):
                raise SnapshotError("a database in the snapshot failed its integrity check")
        os.chmod(stage, 0o700)
        if home.exists():
            os.replace(home, aside / "old")
            moved = True
        os.replace(stage, home)
        swapped = True
        os.chmod(home, 0o700)
    except Exception:
        if moved and not swapped and not home.exists() and (aside / "old").is_dir():
            os.replace(aside / "old", home)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(aside, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Snapshot or restore MUSE_HOME for an update rollback.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--home", type=Path, required=True)
    snap.add_argument("--out", type=Path, required=True)
    rest = sub.add_parser("restore")
    rest.add_argument("--tar", type=Path, required=True)
    rest.add_argument("--home", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "snapshot":
            snapshot(args.home, args.out)
        else:
            restore(args.tar, args.home)
    except (SnapshotError, OSError, sqlite3.Error) as exc:
        print(f"snapshot: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
