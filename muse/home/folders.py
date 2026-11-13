"""The folders a skill may be given when it is run through the home API.

A run started with `muse skills run` names folders by path, because the person
is typing. A run started through the API (`POST /v1/skills/{name}/run`) may
come from a host bot, which must not be able to point a skill at any path it
likes. So the API takes folder NAMES only, and the names are in a small list
the person keeps on the home:

    muse skills folders add notes ~/Notes            a folder skills may read
    muse skills folders add reports ~/Reports --output    ... and propose new files in
    muse skills folders list
    muse skills folders rm notes

The list is `MUSE_HOME/skill-folders.json` (0600). A folder is stored as its
real, absolute path. It may not be the file system's root, Muse's own folder,
or a folder that holds Muse's own folder (keys and records live there). The
same checks are made again every time a name is used, so a list edited by hand,
or a folder since replaced by a link, is refused rather than followed.

A missing file means no folder is allowed. A file that is not in this form
also means none, and is never overwritten: `add` and `rm` say so and stop.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from muse import paths

FILE_NAME = "skill-folders.json"
FILE_VERSION = 1
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
MAX_FOLDERS = 64
MAX_PATH_CHARS = 1024


class FolderError(ValueError):
    """A change to the list, or a use of a name, that is not allowed. The message is one plain sentence, safe to show."""


def folders_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / FILE_NAME


def _real(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(os.path.expanduser(os.fspath(path))))


def check_path(path: str | os.PathLike[str], environ: Mapping[str, str] | None = None) -> Path:
    """The real path of a folder that may be on the list, or FolderError."""
    text = os.fspath(path)
    if not text or "\x00" in text or len(text) > MAX_PATH_CHARS:
        raise FolderError("that is not a usable folder path")
    real = _real(text)
    if not real.is_dir():
        raise FolderError("that is not a folder on this computer")
    if real == Path(real.anchor):
        raise FolderError("the whole disk cannot be given to skills; choose a folder")
    own = _real(paths.home(environ))
    if real == own or own in real.parents or real in own.parents:
        raise FolderError("that folder is Muse's own folder, or holds it; keys and records live there, so skills may not be given it")
    return real


def _parse(text: str) -> dict[str, dict[str, Any]]:
    data = json.loads(text) if text.strip() else {"folders": []}
    if not isinstance(data, dict) or not isinstance(data.get("folders", []), list):
        raise ValueError("expected an object with a 'folders' list")
    out: dict[str, dict[str, Any]] = {}
    for item in data.get("folders") or []:
        if not isinstance(item, dict):
            continue
        name, path = item.get("name"), item.get("path")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in out:
            continue
        if not isinstance(path, str) or not path or len(path) > MAX_PATH_CHARS or not os.path.isabs(path):
            continue
        out[name] = {"name": name, "path": path, "output": item.get("output") is True, "added_at": str(item.get("added_at") or "")}
    return out


def load(environ: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """name -> {name, path, output, added_at}. Never raises: anything wrong with the file means no folders."""
    try:
        return _parse(folders_path(environ).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


def _load_for_update(environ: Mapping[str, str] | None) -> dict[str, dict[str, Any]]:
    path = folders_path(environ)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise FolderError(f"cannot read {path} ({type(exc).__name__})") from None
    try:
        return _parse(text)
    except ValueError:
        raise FolderError(f"{path} is not in the form Muse writes; fix or remove it before changing the list") from None


def _write(folders: Mapping[str, Mapping[str, Any]], environ: Mapping[str, str] | None) -> None:
    from muse import keys

    body = {"version": FILE_VERSION,
            "note": "Written by `muse skills folders`. The folders a skill may be given when run through the home API.",
            "folders": [dict(folders[name]) for name in sorted(folders)]}
    path = folders_path(environ)
    path.parent.mkdir(parents=True, exist_ok=True)
    keys.atomic_write_text(path, json.dumps(body, indent=2, ensure_ascii=False) + "\n", 0o600)


def add(name: str, path: str | os.PathLike[str], *, output: bool = False, environ: Mapping[str, str] | None = None,
        now: float | None = None) -> dict[str, Any]:
    """Put a folder on the list under a name. Replaces nothing: an existing name is an error."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise FolderError("a folder name is 1-40 lower-case letters, digits and '-', starting and ending with a letter or digit")
    folders = _load_for_update(environ)  # first: a damaged list is the thing to fix, whatever is being added
    real = check_path(path, environ)
    if name in folders:
        raise FolderError(f"a folder named '{name}' is already on the list; remove it first to change it")
    if len(folders) >= MAX_FOLDERS:
        raise FolderError(f"there are already {MAX_FOLDERS} folders on the list; remove one first")
    stamp = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    folders[name] = {"name": name, "path": str(real), "output": bool(output), "added_at": stamp}
    _write(folders, environ)
    return dict(folders[name])


def remove(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """Take a name off the list. False if it was not there. Runs started later can no longer use it."""
    folders = _load_for_update(environ)
    if name not in folders:
        return False
    del folders[name]
    _write(folders, environ)
    return True


def resolve(name: Any, *, for_output: bool = False, environ: Mapping[str, str] | None = None) -> Path:
    """The folder a name stands for, checked again now. FolderError when the name is not on the list, the
    folder is gone or was replaced by a link, or (for_output) it was not allowed for new files."""
    entry = load(environ).get(name) if isinstance(name, str) else None
    if entry is None:
        raise FolderError("that folder name is not on the person's list")
    if for_output and not entry["output"]:
        raise FolderError("that folder is on the list for reading only")
    real = check_path(entry["path"], environ)
    if str(real) != entry["path"]:
        raise FolderError("that folder is no longer where it was when the person allowed it")
    return real


def allowed_output_root(root: str | os.PathLike[str], environ: Mapping[str, str] | None = None) -> bool:
    """Is `root` still a folder on the list that new files may be proposed in?"""
    for entry in load(environ).values():
        if entry["output"] and entry["path"] == os.fspath(root):
            try:
                return str(check_path(entry["path"], environ)) == entry["path"]
            except FolderError:
                return False
    return False


# ---------------------------------------------------------------- `muse skills folders ...`


def _cmd(args: Any, env: Mapping[str, str]) -> int:
    import sys

    def err(text: str) -> None:
        print(text, file=sys.stderr)

    try:
        if args.folders_cmd == "add":
            entry = add(args.name, args.path, output=args.output, environ=env)
            use = "read, and propose new files in," if entry["output"] else "read"
            print(f"Added '{entry['name']}': skills run through the home may {use} {entry['path']}")
            print("A host bot can now name this folder when it runs a skill. It still cannot choose any other path.")
            return 0
        if args.folders_cmd == "rm":
            if remove(args.name, env):
                print(f"Removed '{args.name}'. Runs started from now on cannot use it.")
                return 0
            err(f"Error: no folder named '{args.name}' is on the list")
            return 1
        entries = sorted(_load_for_update(env).values(), key=lambda e: e["name"])
        if getattr(args, "json", False):
            print(json.dumps(entries, indent=2, ensure_ascii=False))
        elif not entries:
            print("No folders. Skills run through the home (by a host bot, or the app) can be given none.")
            print("`muse skills folders add NAME PATH` allows one; add --output to let skills propose new files there.")
        else:
            for entry in entries:
                print(f"{entry['name']:<24} {'read + new files' if entry['output'] else 'read':<17} {entry['path']}")
        return 0
    except FolderError as exc:
        err(f"Error: {exc}")
        return 1


def register_cli(ssub: Any, bind: Any) -> None:
    """Add `folders add|list|rm` under `muse skills` (called from muse/skills/cli.py)."""
    f = ssub.add_parser("folders", help="the folders a skill may be given when a host bot or the app runs it through the home")
    fsub = f.add_subparsers(dest="folders_cmd", required=True)
    fa = fsub.add_parser("add", help="allow a folder, under a name")
    fa.add_argument("name", help="the name a run uses for it, e.g. notes")
    fa.add_argument("path", help="the folder")
    fa.add_argument("--output", action="store_true", help="skills may also PROPOSE new files in it (each still needs your approval)")
    bind(fa, _cmd)
    fl = fsub.add_parser("list", help="the allowed folders")
    fl.add_argument("--json", action="store_true")
    bind(fl, _cmd)
    fr = fsub.add_parser("rm", help="stop allowing a folder")
    fr.add_argument("name")
    bind(fr, _cmd)
