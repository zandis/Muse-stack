"""The SKILL.md format: open Agent Skills frontmatter plus Muse's additions under `muse:`.

Validation is strict on purpose: a skill states what it touches before the
person approves it, so a misspelt or unknown key must be an error, never
silently ignored. `parse_skill_md` lists every problem at once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

import yaml

TIERS: tuple[str, ...] = ("light", "everyday", "heavy", "frontier")  # chat tiers a skill may default to
PRIVACY: tuple[str, ...] = ("standard", "private", "local")
# Open-format keys (agentskills.io) Muse accepts and keeps; anything else at the top level is an error.
OPEN_KEYS: tuple[str, ...] = ("name", "description", "license", "compatibility", "metadata", "allowed-tools")
MUSE_KEYS: tuple[str, ...] = ("version", "entry", "tier_default", "privacy_default", "budget", "needs", "writes", "check")
BUDGET_KEYS: tuple[str, ...] = ("requests", "tokens", "seconds", "paid_usd")
NEED_KEYS: tuple[str, ...] = ("files.read", "files.write", "net")
# What a need may name. These are roles, bound to real folders by the person at run time; a skill never names a path.
READ_SLOTS: tuple[str, ...] = ("chosen", "data")
WRITE_SLOTS: tuple[str, ...] = ("output", "data")

_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_VERSION = re.compile(r"\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.-]{1,20})?")
_ENTRY = re.compile(r"((?:[A-Za-z_][A-Za-z0-9_]*/)*[A-Za-z_][A-Za-z0-9_]*)\.py:([A-Za-z_][A-Za-z0-9_]*)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HOST = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
MAX_DESCRIPTION = 1024
MAX_SKILL_MD_BYTES = 200_000
LIMITS = {"requests": 100_000, "tokens": 100_000_000, "seconds": 86_400.0, "paid_usd": 1_000.0}


class FormatError(ValueError):
    """SKILL.md is not valid. `problems` lists every problem, each one line, safe to show."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    requests: int
    tokens: int
    seconds: float
    paid_usd: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"requests": self.requests, "tokens": self.tokens, "seconds": self.seconds, "paid_usd": self.paid_usd}


@dataclass(frozen=True, slots=True)
class Needs:
    files_read: tuple[str, ...] = ()
    files_write: tuple[str, ...] = ()
    net: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, list[str]]:
        return {"files.read": list(self.files_read), "files.write": list(self.files_write), "net": list(self.net)}


@dataclass(frozen=True)
class Skill:
    """One parsed SKILL.md. `path`, `source`, `hash`, `state` and `files` are filled by the loader."""

    name: str
    description: str
    version: str
    entry_module: str  # "skill" or "pkg/mod", relative to the folder, without .py
    entry_function: str
    tier_default: str
    privacy_default: str
    budget: BudgetSpec
    needs: Needs
    writes: bool
    check: str
    body: str = ""
    author: str = ""
    license: str = ""
    path: str = ""
    source: str = ""  # shipped | home
    hash: str = ""
    state: str = ""  # ready | not-approved | changed
    files: Mapping[str, bytes] = field(default_factory=dict, repr=False, compare=False)

    @property
    def entry(self) -> str:
        return f"{self.entry_module}.py:{self.entry_function}"

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name, "description": self.description, "version": self.version, "entry": self.entry,
            "tier_default": self.tier_default, "privacy_default": self.privacy_default, "budget": self.budget.as_dict(),
            "needs": self.needs.as_dict(), "writes": self.writes, "check": self.check, "author": self.author,
            "source": self.source, "state": self.state, "hash": self.hash, "path": self.path,
        }


def split_frontmatter(text: str) -> tuple[str, str]:
    """(yaml text, body). Raises FormatError when the file does not open with a `---` block."""
    text = text.lstrip("﻿")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise FormatError(["SKILL.md must start with a `---` line that opens the YAML frontmatter"])
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1:]).strip()
    raise FormatError(["the frontmatter is not closed: add a `---` line after it"])


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf")


def _budget(raw: Any, problems: list[str]) -> BudgetSpec | None:
    if not isinstance(raw, dict):
        problems.append("muse.budget must be a mapping with requests, tokens, seconds and (optional) paid_usd")
        return None
    before = len(problems)
    for key in raw:
        if key not in BUDGET_KEYS:
            problems.append(f"muse.budget.{key}: unknown key (allowed: {', '.join(BUDGET_KEYS)})")
    for key in ("requests", "tokens"):
        value = raw.get(key)
        if not _is_int(value) or not 1 <= value <= LIMITS[key]:
            problems.append(f"muse.budget.{key} must be a whole number from 1 to {LIMITS[key]}")
    seconds = raw.get("seconds")
    if not _is_num(seconds) or not 0 < seconds <= LIMITS["seconds"]:
        problems.append("muse.budget.seconds must be a number above 0 and at most 86400")
    paid = raw.get("paid_usd", 0)
    if not _is_num(paid) or not 0 <= paid <= LIMITS["paid_usd"]:
        problems.append("muse.budget.paid_usd must be a number from 0 to 1000 (default 0: never pay)")
    if len(problems) > before:
        return None
    return BudgetSpec(int(raw["requests"]), int(raw["tokens"]), float(seconds), float(paid))


def _needs(raw: Any, problems: list[str]) -> Needs | None:
    if not isinstance(raw, dict):
        problems.append("muse.needs must be a mapping with files.read, files.write and net (each a list)")
        return None
    before = len(problems)
    for key in raw:
        if key not in NEED_KEYS:
            problems.append(f"muse.needs.{key}: unknown key (allowed: {', '.join(NEED_KEYS)})")
    out: dict[str, tuple[str, ...]] = {}
    for key, allowed in (("files.read", READ_SLOTS), ("files.write", WRITE_SLOTS), ("net", None)):
        if key not in raw:
            problems.append(f"muse.needs.{key} is required (use [] for none)")
            continue
        value = raw[key]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            problems.append(f"muse.needs.{key} must be a list of words")
            continue
        if len(set(value)) != len(value):
            problems.append(f"muse.needs.{key} lists the same entry twice")
        for item in value:
            if allowed is not None and item not in allowed:
                problems.append(f"muse.needs.{key}: {item!r} is not one of {', '.join(allowed)}")
            if allowed is None and not _HOST.fullmatch(item):
                problems.append(f"muse.needs.net: {item!r} is not a plain lower-case host name")
        out[key] = tuple(value)
    if len(problems) > before:
        return None
    return Needs(out["files.read"], out["files.write"], out["net"])


def parse_skill_md(text: str, *, folder_name: str | None = None) -> Skill:
    """Parse and validate SKILL.md text. Raises FormatError listing every problem."""
    if len(text.encode("utf-8", "replace")) > MAX_SKILL_MD_BYTES:
        raise FormatError([f"SKILL.md is larger than {MAX_SKILL_MD_BYTES} bytes"])
    front, body = split_frontmatter(text)
    try:
        data = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 2})" if mark is not None else ""
        raise FormatError([f"the frontmatter is not valid YAML{where}"]) from None
    if not isinstance(data, dict):
        raise FormatError(["the frontmatter must be a YAML mapping"])
    problems: list[str] = []
    for key in data:
        if key not in OPEN_KEYS and key != "muse":
            problems.append(f"{key}: unknown key (allowed: {', '.join(OPEN_KEYS)}, muse)")

    name = data.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        problems.append("name is required: lower-case letters, digits and hyphens, at most 64 characters")
        name = ""
    elif folder_name is not None and name != folder_name:
        problems.append(f"name {name!r} must equal the folder name {folder_name!r}")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append("description is required: one or two sentences saying what the skill does and when to use it")
        description = ""
    elif len(description) > MAX_DESCRIPTION:
        problems.append(f"description is longer than {MAX_DESCRIPTION} characters")
    metadata = data.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        problems.append("metadata must be a mapping")
        metadata = None
    for key in ("license", "compatibility"):
        if data.get(key) is not None and not isinstance(data[key], str):
            problems.append(f"{key} must be text")

    muse = data.get("muse")
    version = entry = tier = privacy = check = None
    budget: BudgetSpec | None = None
    needs: Needs | None = None
    writes: Any = None
    if not isinstance(muse, dict):
        problems.append("muse: is required: a mapping with " + ", ".join(MUSE_KEYS))
    else:
        for key in muse:
            if key not in MUSE_KEYS:
                problems.append(f"muse.{key}: unknown key (allowed: {', '.join(MUSE_KEYS)})")
        for key in MUSE_KEYS:
            if key not in muse:
                problems.append(f"muse.{key} is required")
        if "version" in muse:
            raw = muse["version"]
            version = str(raw) if isinstance(raw, str) or _is_num(raw) else ""
            if not _VERSION.fullmatch(version):
                problems.append('muse.version must look like "1", "0.2" or "0.2.1" (quote it)')
        if "entry" in muse:
            entry = _ENTRY.fullmatch(muse["entry"]) if isinstance(muse["entry"], str) else None
            if entry is None:
                problems.append("muse.entry must be `<file>.py:<function>` inside the skill folder, e.g. skill.py:run")
        if "tier_default" in muse:
            tier = muse["tier_default"]
            if tier not in TIERS:
                problems.append(f"muse.tier_default must be one of {', '.join(TIERS)}")
        if "privacy_default" in muse:
            privacy = muse["privacy_default"]
            if privacy not in PRIVACY:
                problems.append(f"muse.privacy_default must be one of {', '.join(PRIVACY)}")
        if "budget" in muse:
            budget = _budget(muse["budget"], problems)
        if "needs" in muse:
            needs = _needs(muse["needs"], problems)
        if "writes" in muse:
            writes = muse["writes"]
            if not isinstance(writes, bool):
                problems.append("muse.writes must be true or false")
            elif needs is not None and writes != ("output" in needs.files_write):
                problems.append("muse.writes must be true exactly when muse.needs.files.write lists `output`")
        if "check" in muse:
            check = muse["check"]
            if not isinstance(check, str) or not _IDENT.fullmatch(check):
                problems.append("muse.check must be the name of the check function in the entry module")
    if problems:
        raise FormatError(problems)
    assert entry is not None and budget is not None and needs is not None
    author = str((metadata or {}).get("author", "") or "")[:200]
    return Skill(
        name=name, description=description.strip(), version=str(version), entry_module=entry.group(1),
        entry_function=entry.group(2), tier_default=str(tier), privacy_default=str(privacy), budget=budget, needs=needs,
        writes=bool(writes), check=str(check), body=body, author=author, license=str(data.get("license") or ""),
    )


def needs_in_words(skill: Skill, *, data_dir: str = "") -> list[str]:
    """What the skill asks for, one plain sentence per line. Shown before approval and by `muse skills show`."""
    lines: list[str] = []
    needs = skill.needs
    if "chosen" in needs.files_read:
        lines.append("Read files in the folders you name each time you run it. It cannot choose folders itself.")
    if "data" in needs.files_read or "data" in needs.files_write:
        where = f" ({data_dir})" if data_dir else ""
        lines.append(f"Keep its own working data in Muse's folder on this computer{where}. This can include text copied from the files it reads.")
    if "output" in needs.files_write:
        lines.append("Propose new files in an output folder. Nothing is written until you agree.")
    if not needs.files_read and not needs.files_write:
        lines.append("No access to your files.")
    if needs.net:
        lines.append("Reach these hosts on the internet: " + ", ".join(needs.net) + ".")
    else:
        lines.append("No internet access of its own.")
    b = skill.budget
    paid = "never pays" if b.paid_usd <= 0 else f"may spend up to US${b.paid_usd:.2f}"
    lines.append(
        f"Ask models through Muse's router (it never sees your provider keys): normally {skill.tier_default} tasks, "
        f"privacy class {skill.privacy_default}; at most {b.requests} requests, {b.tokens} tokens and "
        f"{b.seconds:g} seconds a run; {paid}."
    )
    return lines
