"""Find skills, hash them, and keep the list of approved ones.

Two places: the `skills/` folder shipped in the repo (approved by being in the
repo) and `MUSE_HOME/skills/` (the person's own). A skill from the home is
"not approved" until its content hash is recorded in
`MUSE_HOME/skills-approved.json` by `muse skills approve <name>`, and is
refused again if any byte of the folder changes afterwards.

The hash covers the sorted list of files and their bytes. The same bytes are
kept in memory and are what gets imported (see `import_skill`), so the code
that runs is the code that was hashed: bytecode caches are neither read nor
written, and a file edited between the check and the run has no effect.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Mapping

from muse import paths
from muse.skills.format import FormatError, Skill, parse_skill_md

SKILL_FILE = "SKILL.md"
APPROVED_FILE = "skills-approved.json"
MAX_FILES = 500
MAX_BYTES = 20_000_000
SKIP_DIRS = frozenset({"__pycache__"})
SKIP_SUFFIXES = (".pyc", ".pyo")
MODULE_PREFIX = "muse_skill_"


class LoaderError(ValueError):
    """A skill cannot be loaded or used. The message is one plain sentence, safe to show."""


@dataclass(frozen=True, slots=True)
class Broken:
    """A skill folder that could not be loaded: listed, never runnable."""

    name: str
    path: str
    source: str
    problems: tuple[str, ...]


def shipped_dir(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = str(env.get("MUSE_SKILLS_DIR", "") or "").strip()
    return Path(explicit).expanduser() if explicit else paths.REPO_ROOT / "skills"


def home_dir(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "skills"


def data_dir(name: str, environ: Mapping[str, str] | None = None) -> Path:
    """The skill's own working folder (need `data`). Created 0700 on first use by the runner."""
    return paths.home(environ) / "skills-data" / name


def approved_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / APPROVED_FILE


# ---------------------------------------------------------------- hashing


def read_folder(folder: Path) -> dict[str, bytes]:
    """Every file of a skill folder as {posix relative path: bytes}. Symbolic links are refused."""
    files: dict[str, bytes] = {}
    total = 0
    if folder.is_symlink() or not folder.is_dir():
        raise LoaderError("the skill folder is missing or is a symbolic link")
    for root, dirs, names in os.walk(folder, followlinks=False):
        for d in list(dirs):
            if (Path(root) / d).is_symlink():
                raise LoaderError(f"the skill folder contains a symbolic link ({d}); remove it")
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            path = Path(root) / name
            if name.endswith(SKIP_SUFFIXES):
                continue
            if path.is_symlink():
                raise LoaderError(f"the skill folder contains a symbolic link ({name}); remove it")
            if not path.is_file():
                raise LoaderError(f"the skill folder contains something that is not a plain file ({name})")
            data = path.read_bytes()
            total += len(data)
            if len(files) >= MAX_FILES or total > MAX_BYTES:
                raise LoaderError(f"the skill folder is too large (more than {MAX_FILES} files or {MAX_BYTES} bytes)")
            files[path.relative_to(folder).as_posix()] = data
    return files


def content_hash(files: Mapping[str, bytes]) -> str:
    """sha256 over each file in sorted path order: length-prefixed path, then length-prefixed bytes."""
    digest = hashlib.sha256()
    for rel in sorted(files):
        name = rel.encode("utf-8")
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(files[rel]).to_bytes(8, "big") + files[rel])
    return "sha256:" + digest.hexdigest()


# ---------------------------------------------------------------- approvals


def _load_approved(environ: Mapping[str, str] | None) -> dict[str, Any]:
    try:
        data = json.loads(approved_path(environ).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "skills": {}, "shown": {}}
    if not isinstance(data, dict) or not isinstance(data.get("skills"), dict):
        return {"version": 1, "skills": {}, "shown": {}}
    if not isinstance(data.get("shown"), dict):
        data["shown"] = {}
    return data


def _save_approved(data: Mapping[str, Any], environ: Mapping[str, str] | None) -> None:
    path = approved_path(environ)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def approved_hash(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    entry = _load_approved(environ)["skills"].get(name)
    value = entry.get("hash") if isinstance(entry, dict) else None
    return value if isinstance(value, str) else None


def approve(name: str, environ: Mapping[str, str] | None = None, *, expect_hash: str | None = None,
            now: float | None = None) -> Skill:
    """Record the current hash of a home skill as approved. Returns the skill, now ready.

    `expect_hash` is the hash the person was shown: if the folder changed while
    they were reading, nothing is recorded.
    """
    skill = find(name, environ)
    if skill.source == "shipped":
        return skill
    if expect_hash is not None and skill.hash != expect_hash:
        raise LoaderError(f"skill '{name}' changed while it was being approved; look at it again")
    data = _load_approved(environ)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    data["skills"][name] = {"hash": skill.hash, "version": skill.version, "approved_at": stamp}
    _save_approved(data, environ)
    return replace(skill, state="ready")


def revoke(name: str, environ: Mapping[str, str] | None = None) -> bool:
    data = _load_approved(environ)
    if name not in data["skills"]:
        return False
    del data["skills"][name]
    _save_approved(data, environ)
    return True


def first_use(skill: Skill, environ: Mapping[str, str] | None = None) -> bool:
    """True the first time this exact version of a skill is about to be used (and records that it was shown)."""
    data = _load_approved(environ)
    if data["shown"].get(skill.name) == skill.hash:
        return False
    data["shown"][skill.name] = skill.hash
    _save_approved(data, environ)
    return True


# ---------------------------------------------------------------- discovery


def load_folder(folder: Path, source: str) -> Skill:
    """Parse one skill folder. Raises LoaderError / FormatError."""
    files = read_folder(folder)
    if SKILL_FILE not in files:
        raise LoaderError(f"no {SKILL_FILE} in the folder")
    try:
        text = files[SKILL_FILE].decode("utf-8")
    except UnicodeDecodeError:
        raise LoaderError(f"{SKILL_FILE} is not UTF-8 text") from None
    skill = parse_skill_md(text, folder_name=folder.name)
    if f"{skill.entry_module}.py" not in files:
        raise FormatError([f"muse.entry names {skill.entry_module}.py, which is not in the folder"])
    return replace(skill, path=str(folder), source=source, hash=content_hash(files), files=files)


def discover(environ: Mapping[str, str] | None = None) -> tuple[dict[str, Skill], list[Broken]]:
    """(skills by name, folders that could not be loaded). Shipped skills win a name clash."""
    skills: dict[str, Skill] = {}
    broken: list[Broken] = []
    approved = _load_approved(environ)["skills"]
    for source, base in (("shipped", shipped_dir(environ)), ("home", home_dir(environ))):
        if not base.is_dir():
            continue
        for folder in sorted(p for p in base.iterdir() if not p.name.startswith(".")):
            if not folder.is_dir() and not folder.is_symlink():
                continue
            try:
                skill = load_folder(folder, source)
            except FormatError as exc:
                broken.append(Broken(folder.name, str(folder), source, tuple(exc.problems)))
                continue
            except (LoaderError, OSError) as exc:
                reason = str(exc) if isinstance(exc, LoaderError) else f"cannot read the folder ({type(exc).__name__})"
                broken.append(Broken(folder.name, str(folder), source, (reason,)))
                continue
            if skill.name in skills:
                broken.append(Broken(skill.name, str(folder), source,
                                     (f"a skill named '{skill.name}' ships with Muse; rename this folder and its `name`",)))
                continue
            if source == "shipped":
                state = "ready"
            else:
                entry = approved.get(skill.name)
                recorded = entry.get("hash") if isinstance(entry, dict) else None
                state = "not-approved" if not recorded else ("ready" if recorded == skill.hash else "changed")
            skills[skill.name] = replace(skill, state=state)
    return skills, broken


def find(name: str, environ: Mapping[str, str] | None = None) -> Skill:
    """The skill named `name`, whatever its state. Raises LoaderError when there is none or it is broken."""
    skills, broken = discover(environ)
    if name in skills:
        return skills[name]
    for item in broken:
        if item.name == name:
            raise LoaderError(f"skill '{name}' cannot be loaded: " + "; ".join(item.problems))
    known = ", ".join(sorted(skills)) or "none"
    raise LoaderError(f"no skill named '{name}' (known: {known})")


def require_ready(skill: Skill) -> None:
    """Raise LoaderError unless the skill may run."""
    if skill.state == "not-approved":
        raise LoaderError(f"skill '{skill.name}' is not approved yet. Read what it asks for and approve it: muse skills approve {skill.name}")
    if skill.state == "changed":
        raise LoaderError(f"skill '{skill.name}' changed since you approved it, so it will not run. "
                          f"Look at it again: muse skills approve {skill.name}")
    if skill.state != "ready":
        raise LoaderError(f"skill '{skill.name}' is not ready to run")


# ---------------------------------------------------------------- importing


class _SnapshotLoader(importlib.abc.Loader):
    def __init__(self, source: bytes | None, filename: str) -> None:
        self.source = source
        self.filename = filename

    def create_module(self, spec: Any) -> None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        if self.source is not None:
            exec(compile(self.source, self.filename, "exec", dont_inherit=True), module.__dict__)


class _SnapshotFinder(importlib.abc.MetaPathFinder):
    """Serves one skill's modules from the bytes that were hashed. No disk access, no bytecode cache."""

    def __init__(self, package: str, folder: str, files: Mapping[str, bytes]) -> None:
        self.package = package
        self.folder = folder
        self.files = files

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname != self.package and not fullname.startswith(self.package + "."):
            return None
        rel = fullname[len(self.package):].lstrip(".").replace(".", "/")
        if not rel:
            return self._package(fullname, "", self.files.get("__init__.py"))
        if f"{rel}.py" in self.files:
            origin = os.path.join(self.folder, f"{rel}.py")
            return importlib.machinery.ModuleSpec(fullname, _SnapshotLoader(self.files[f"{rel}.py"], origin), origin=origin)
        if any(name.startswith(rel + "/") for name in self.files):
            return self._package(fullname, rel, self.files.get(f"{rel}/__init__.py"))
        return None

    def _package(self, fullname: str, rel: str, init: bytes | None) -> Any:
        origin = os.path.join(self.folder, rel, "__init__.py")
        spec = importlib.machinery.ModuleSpec(fullname, _SnapshotLoader(init, origin), origin=origin, is_package=True)
        spec.submodule_search_locations = []  # only this finder resolves submodules
        return spec


_FINDERS: dict[str, _SnapshotFinder] = {}


def package_name(skill: Skill) -> str:
    digest = skill.hash.split(":", 1)[-1][:16] or "nohash"
    return f"{MODULE_PREFIX}{skill.name.replace('-', '_')}_{digest}"


def import_skill(skill: Skill, module: str | None = None) -> ModuleType:
    """Import a module of the skill (default: its entry module) from the hashed snapshot.

    The skill folder becomes a private package, so its files can use relative
    imports (`from . import index`). Keyed by content hash: a changed skill is
    a different package.
    """
    if not skill.files:
        raise LoaderError(f"skill '{skill.name}' was not loaded from a folder")
    package = package_name(skill)
    if package not in _FINDERS:
        finder = _SnapshotFinder(package, skill.path, dict(skill.files))
        _FINDERS[package] = finder
        sys.meta_path.insert(0, finder)
    dotted = (module or skill.entry_module).replace("/", ".")
    return importlib.import_module(f"{package}.{dotted}")


def entry_points(skill: Skill) -> tuple[Callable[..., Any], Callable[..., Any]]:
    """(entry function, check function). Raises LoaderError when either is missing."""
    try:
        module = import_skill(skill)
    except LoaderError:
        raise
    except Exception as exc:  # the skill's own import error: say the kind, never the message
        raise LoaderError(f"skill '{skill.name}' could not be imported ({type(exc).__name__})") from exc
    entry = getattr(module, skill.entry_function, None)
    check = getattr(module, skill.check, None)
    if not callable(entry):
        raise LoaderError(f"skill '{skill.name}': {skill.entry} is not a function")
    if not callable(check):
        raise LoaderError(f"skill '{skill.name}': the check `{skill.check}` is not a function in {skill.entry_module}.py")
    return entry, check
