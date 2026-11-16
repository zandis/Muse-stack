"""The files a skill may touch in this run, checked against its `needs`.

A skill gets a `Workspace` instead of raw filesystem access BY CONVENTION.
Every path that goes through it is resolved (symbolic links followed to
their real target) and must lie inside a folder the person named for this
run; `..` is refused outright. Writes are never performed here: a skill
returns `ProposedWrite`s and the runner applies them only with consent.

This is a check, not a cage. The skill is Python in the runner's process and
could call `open()` itself; a symbolic link swapped between the check and the
read is not caught. Real isolation arrives with the sandbox package.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

MAX_READ_BYTES = 20_000_000
MAX_WRITE_BYTES = 20_000_000
MAX_PROPOSED = 200
MAX_LISTED = 200_000


class NeedsViolation(Exception):
    """A skill asked for something its `needs` do not cover, or a path outside the folders named for this run."""


@dataclass(frozen=True, slots=True)
class FileInfo:
    path: str  # absolute, resolved
    size: int
    mtime_ns: int
    root: str  # the read root it was listed under


@dataclass(slots=True)
class ProposedWrite:
    path: str  # absolute, inside the output folder
    data: bytes = field(repr=False)
    reason: str = ""
    applied: bool = False
    refused: str = ""  # why it was not applied, when it was not

    @property
    def bytes(self) -> bytes:
        return self.data

    def public(self) -> dict[str, object]:
        return {"path": self.path, "size": len(self.data), "reason": self.reason, "applied": self.applied, "refused": self.refused}


def _real(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(os.fspath(path)))


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _no_dotdot(path: str | os.PathLike[str]) -> None:
    text = os.fspath(path)
    if "\x00" in text:
        raise NeedsViolation("a path contains a NUL byte")
    if ".." in Path(text).parts:
        raise NeedsViolation("a path may not contain `..`")


class Workspace:
    """Read roots (need `chosen`), an optional output folder (need `output`) and what the skill proposed to write."""

    def __init__(self, read_roots: Sequence[str | os.PathLike[str]] = (), write_root: str | os.PathLike[str] | None = None,
                 *, may_write: bool = False) -> None:
        self.read_roots: tuple[Path, ...] = tuple(dict.fromkeys(_real(r) for r in read_roots))
        self.write_root: Path | None = _real(write_root) if write_root is not None else None
        self._may_write = may_write and self.write_root is not None
        self.proposed: list[ProposedWrite] = []
        self.skipped_links = 0  # entries skipped by list() because they lead outside the root

    # ------------------------------------------------------------ reading

    def roots(self) -> list[str]:
        return [str(r) for r in self.read_roots]

    def resolve_read(self, path: str | os.PathLike[str]) -> Path:
        """The real path, or NeedsViolation when it is outside every read root."""
        _no_dotdot(path)
        candidate = Path(os.fspath(path))
        if not candidate.is_absolute():
            if len(self.read_roots) != 1:
                raise NeedsViolation("a relative path needs exactly one read folder; use the paths list() returns")
            candidate = self.read_roots[0] / candidate
        real = _real(candidate)
        if not any(_inside(real, root) for root in self.read_roots):
            raise NeedsViolation("that path is outside the folders named for this run")
        return real

    def list(self, root: str | os.PathLike[str] | None = None) -> Iterator[FileInfo]:
        """Every plain file under `root` (default: every read root), sorted by path. Links leading outside are skipped."""
        if root is None:
            chosen: Iterable[Path] = self.read_roots
        else:
            chosen = [self.resolve_read(root)]
        seen: set[str] = set()
        count = 0
        for base in chosen:
            for folder, dirs, names in os.walk(base, followlinks=False):
                kept = []
                for d in sorted(dirs):
                    if (Path(folder) / d).is_symlink():
                        self.skipped_links += 1  # never walk through a linked folder
                    else:
                        kept.append(d)
                dirs[:] = kept
                for name in sorted(names):
                    path = Path(folder) / name
                    real = _real(path)
                    if not _inside(real, base):
                        self.skipped_links += 1
                        continue
                    try:
                        st = real.stat()
                    except OSError:
                        continue
                    if not real.is_file() or str(real) in seen:
                        continue
                    seen.add(str(real))
                    count += 1
                    if count > MAX_LISTED:
                        return
                    yield FileInfo(str(real), st.st_size, st.st_mtime_ns, str(base))

    def read_bytes(self, path: str | os.PathLike[str], limit: int = MAX_READ_BYTES) -> bytes:
        real = self.resolve_read(path)
        if not real.is_file():
            raise FileNotFoundError("not a file")
        with open(real, "rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"the file is larger than {limit} bytes")
        return data

    def read_text(self, path: str | os.PathLike[str], limit: int = MAX_READ_BYTES) -> str:
        return self.read_bytes(path, limit).decode("utf-8", "replace")

    # ------------------------------------------------------------ writing

    def resolve_write(self, path: str | os.PathLike[str]) -> Path:
        if not self._may_write or self.write_root is None:
            raise NeedsViolation("this skill did not declare `writes` with an output folder, so it may not propose files")
        _no_dotdot(path)
        candidate = Path(os.fspath(path))
        if not candidate.is_absolute():
            candidate = self.write_root / candidate
        # Resolve the folder the file would be created in; the file itself must not be a link.
        parent = _real(candidate.parent)
        target = parent / candidate.name
        if not _inside(parent, self.write_root) or target == self.write_root:
            raise NeedsViolation("that path is outside the output folder")
        if target.is_symlink():
            raise NeedsViolation("that path is a symbolic link")
        return target

    def propose_write(self, path: str | os.PathLike[str], data: bytes | str, reason: str = "") -> ProposedWrite:
        """Record a file the skill would like written. Nothing touches the disk here."""
        target = self.resolve_write(path)
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        if len(raw) > MAX_WRITE_BYTES:
            raise ValueError(f"a proposed file may hold at most {MAX_WRITE_BYTES} bytes")
        if len(self.proposed) >= MAX_PROPOSED:
            raise ValueError(f"a run may propose at most {MAX_PROPOSED} files")
        write = ProposedWrite(str(target), raw, str(reason)[:300])
        self.proposed.append(write)
        return write

    def apply(self, write: ProposedWrite) -> bool:
        """Write one proposed file: new files only, whole file or nothing, re-checked at the moment of writing."""
        try:
            target = self.resolve_write(write.path)
        except NeedsViolation as exc:
            write.refused = str(exc)
            return False
        if target.exists():
            write.refused = "a file with that name already exists; Muse does not overwrite"
            return False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not _inside(_real(target.parent), self.write_root):  # type: ignore[arg-type]
                write.refused = "that path is outside the output folder"
                return False
            tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(write.data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.link(tmp, target)  # fails if the name appeared meanwhile: never overwrites
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        except FileExistsError:
            write.refused = "a file with that name already exists; Muse does not overwrite"
            return False
        except OSError as exc:
            write.refused = f"could not write the file ({type(exc).__name__})"
            return False
        write.applied = True
        return True
