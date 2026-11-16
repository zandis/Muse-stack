#!/usr/bin/env python3
"""Publish the catalogue as dated JSON (work package A15).

Why: no maintained, machine-readable list of free allowances across AI,
storage, compute, chain and messaging was found (docs/ecosystem-review-2026-11.md
3.1). The catalogue in config/ already holds dated, cited facts, so it is
published as one JSON document that other projects can read.

What it writes, from config/providers.yaml and config/resources/*.yaml:

    catalogue/catalogue.v1.json       the document, indented
    catalogue/catalogue.v1.min.json   the same document on one line
    catalogue/CHANGELOG.md            a short entry whenever an entry changed
    app/src/lib/link-hosts.json       the hosts the app may open, derived from the
                                      document (scripts/export_link_hosts.py); only
                                      when writing to (or checking) catalogue/

Usage:
    python scripts/publish_catalogue.py              # rewrite catalogue/
    python scripts/publish_catalogue.py --check      # exit 1 if catalogue/ is out of date with config/
    python scripts/publish_catalogue.py --out DIR    # write somewhere else
    python scripts/publish_catalogue.py --date D     # set generated_at (default: the newest checked_on)
    python scripts/publish_catalogue.py --release    # mark the document released and keep a dated copy;
                                                     # refused until LICENCE_SPDX below is set by the owner

The output is deterministic: entries are in a fixed order, and the only date
that is not a catalogue fact is `generated_at`, which is `--date` or the newest
`checked_on`. No clock is read. Two runs give the same bytes.

`changes[]` is worked out against the document already in the output folder:
what was added or removed, which limits, policy facts and dates changed. It
grows until a release; the first change after a release starts it again. When
nothing in the entries changed, `changes[]` is kept as it is, which is what
makes `--check` possible.

Only public facts leave this script (docs/catalogue.md lists them). Before
anything is written the document is checked: it must match
catalogue/schema/catalogue.v1.schema.json, nothing in it may look like a key or
token (muse.catalog.find_secretlike), and it may not contain a local path or a
field that is internal by name. A failed check writes nothing.

Standard library plus PyYAML (through muse.catalog) only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wardmuse import catalog  # noqa: E402

# --------------------------------------------------------------------------
# THE ONE LICENCE SETTING. Chosen by the owner on 2026-11-15: the published
# catalogue is CC BY 4.0 (text in catalogue/LICENSE); the code is Apache-2.0
# (LICENSE at the repository root). Setting this back to None makes
# `--release` refuse to run.
LICENCE_SPDX: str | None = "CC-BY-4.0"
# --------------------------------------------------------------------------

LICENCE_UNDECIDED = "licence to be chosen by the owner before publication"
LICENCE_OPTIONS = (
    {"spdx": "CC0-1.0", "summary": "Public-domain dedication: anyone may copy and reuse the facts with no conditions. The usual choice for plain facts."},
    {"spdx": "CC-BY-4.0", "summary": "Free to copy and adapt, provided the source is credited and changes are indicated."},
)

OUT_DIR = ROOT / "catalogue"
SCHEMA_PATH = OUT_DIR / "schema" / "catalogue.v1.schema.json"
SCHEMA_REF = "catalogue/schema/catalogue.v1.schema.json"
PRETTY = "catalogue.v1.json"
MINIFIED = "catalogue.v1.min.json"
CHANGELOG = "CHANGELOG.md"
RELEASES = "releases"
SECTIONS = ("ai_services", "other_allowances", "subscriptions")
RULES = (
    "Every fact was read on the provider's own page on the entry's checked_on date, and source_urls says which page. "
    "A fact that could not be confirmed is unknown, not guessed. Free allowances change often: re-read the source page before relying on a number."
)
CHANGELOG_HEAD = (
    "# Catalogue changelog\n\n"
    "Written by `scripts/publish_catalogue.py` whenever an entry changes. Newest first.\n"
    "The same changes are in `changes[]` of `catalogue.v1.json`.\n"
)

# Field names that are internal to this repository and must never be published.
INTERNAL_KEYS = frozenset({"test", "api_base", "raw", "rpm", "order", "aliases", "join", "extra_headers", "env", "base_env", "also_env", "managed"})
LOCAL_PATH = re.compile(r"(?:^|[\s\"'(=])(?:/home/|/Users/|/root/|/tmp/|/var/|/etc/|~/|[A-Za-z]:\\)")


class PublishError(Exception):
    """Why nothing was written."""


# ------------------------------------------------------------------ licence


def licence_block(spdx: str | None = None) -> dict[str, Any]:
    spdx = LICENCE_SPDX if spdx is None else spdx
    options = [dict(o) for o in LICENCE_OPTIONS]
    if spdx is None:
        return {"status": "undecided", "spdx": None, "statement": LICENCE_UNDECIDED, "options": options}
    if spdx not in {o["spdx"] for o in LICENCE_OPTIONS}:
        raise PublishError(f"LICENCE_SPDX {spdx!r} is not one of {', '.join(o['spdx'] for o in LICENCE_OPTIONS)}")
    return {"status": "chosen", "spdx": spdx, "statement": f"Licensed under {spdx}.", "options": options}


# --------------------------------------------------------------------- diff


def _limits_key(entry: Mapping[str, Any]) -> list[str]:
    allowance = entry.get("allowance") or {}
    out = []
    for item in allowance.get("limits") or []:
        text = f"{item.get('limit')} {item.get('unit')} per {item.get('window')}"
        for extra in ("scope", "condition"):
            if item.get(extra):
                text += f" ({item[extra]})"
        out.append(f"{text} [{item.get('status')}]")
    reset = allowance.get("reset") or {}
    out.append(f"reset: {reset.get('rule')}")
    return out


def _policy_key(entry: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for group in ("policy", "terms"):
        for key, value in (entry.get(group) or {}).items():
            if key != "note":
                out[f"{group}.{key}"] = value
    conn = entry.get("connection") or {}
    for key in ("card_required", "phone_required"):
        if key in conn:
            out[f"connection.{key}"] = conn[key]
    if "handoff_only" in entry:
        out["handoff_only"] = entry["handoff_only"]
    return out


def _rest(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The entry without the facts that _limits_key, _policy_key and the date and confidence records already cover."""
    out = json.loads(json.dumps({k: v for k, v in entry.items() if k not in ("checked_on", "confidence", "handoff_only")}))
    allowance = out.get("allowance")
    if isinstance(allowance, dict):
        allowance.pop("limits", None)
        allowance.pop("status", None)
        if isinstance(allowance.get("reset"), dict):
            allowance["reset"].pop("rule", None)
    for group in ("policy", "terms"):
        if isinstance(out.get(group), dict):
            out[group] = {"note": out[group].get("note")}
    if isinstance(out.get("connection"), dict):
        for key in ("card_required", "phone_required"):
            out["connection"].pop(key, None)
    return out


def _record(date: str, change: str, section: str, entry_id: str, field: str | None = None, old: Any = None, new: Any = None) -> dict[str, Any]:
    return {"date": date, "change": change, "section": section, "id": entry_id, "field": field, "from": old, "to": new}


def diff_entries(old: Mapping[str, Any], new: Mapping[str, Any], date: str) -> list[dict[str, Any]]:
    """What changed between two documents' entries, in a fixed order."""
    records: list[dict[str, Any]] = []
    for section in SECTIONS:
        before = {e["id"]: e for e in old.get(section) or [] if isinstance(e, Mapping) and "id" in e}
        after = {e["id"]: e for e in new.get(section) or []}
        for entry_id in sorted(set(before) | set(after)):
            a, b = before.get(entry_id), after.get(entry_id)
            if a is None:
                records.append(_record(date, "added", section, entry_id))
                continue
            if b is None:
                records.append(_record(date, "removed", section, entry_id))
                continue
            if a == b:
                continue
            if _limits_key(a) != _limits_key(b):
                records.append(_record(date, "limits_changed", section, entry_id, "allowance", _limits_key(a), _limits_key(b)))
            pa, pb = _policy_key(a), _policy_key(b)
            for key in sorted(set(pa) | set(pb)):
                if pa.get(key) != pb.get(key):
                    records.append(_record(date, "policy_changed", section, entry_id, key, pa.get(key), pb.get(key)))
            if a.get("checked_on") != b.get("checked_on"):
                records.append(_record(date, "date_changed", section, entry_id, "checked_on", a.get("checked_on"), b.get("checked_on")))
            if a.get("confidence") != b.get("confidence"):
                records.append(_record(date, "confidence_changed", section, entry_id, "confidence", a.get("confidence"), b.get("confidence")))
            # Whatever else differs (wording, steps, notes, models) is named, without its text.
            ra, rb = _rest(a), _rest(b)
            other = sorted(k for k in set(ra) | set(rb) if ra.get(k) != rb.get(k))
            if other:
                records.append(_record(date, "details_changed", section, entry_id, ", ".join(other)))
    return records


# -------------------------------------------------------------------- build


def build(cat: catalog.Catalog, previous: Mapping[str, Any] | None = None, *, date: str | None = None,
          release: bool = False, licence_spdx: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The document to publish and the change records that are new in it."""
    entries = catalog.public_entries(cat)
    newest = catalog.newest_checked_on(cat)
    if newest is None:
        raise PublishError("no entry has a checked_on date")
    generated = date or newest
    if not re.fullmatch(r"\d{4}-\d\d-\d\d", generated):
        raise PublishError("--date must be written YYYY-MM-DD")
    licence = licence_block(licence_spdx)
    if release and licence["status"] != "chosen":
        raise PublishError(
            "refusing to release: " + LICENCE_UNDECIDED + ". Set LICENCE_SPDX at the top of scripts/publish_catalogue.py "
            "(" + " or ".join(o["spdx"] for o in LICENCE_OPTIONS) + ") once the owner has decided."
        )
    changes: list[dict[str, Any]] = []
    state = {"status": "draft", "released_on": None}
    fresh: list[dict[str, Any]] = []
    if previous is not None:
        fresh = diff_entries(previous, entries, generated)
        earlier = [c for c in previous.get("changes") or [] if isinstance(c, Mapping)]
        old_state = previous.get("release") if isinstance(previous.get("release"), Mapping) else {}
        was_released = old_state.get("status") == "released"
        if not fresh:
            changes = [dict(c) for c in earlier]
            if was_released:
                state = {"status": "released", "released_on": old_state.get("released_on")}
        else:
            changes = ([] if was_released else [dict(c) for c in earlier]) + fresh
    if release:
        state = {"status": "released", "released_on": generated}
    doc: dict[str, Any] = {
        "schema_version": catalog.PUBLIC_SCHEMA_VERSION,
        "schema": SCHEMA_REF,
        "generated_at": generated,
        "catalogue_date": newest,
        "release": state,
        "licence": licence,
        "source": {
            "files": ["config/providers.yaml", *(f"config/resources/{name}" for name in sorted([*catalog.RESOURCE_FILES.values(), catalog.SUBSCRIPTIONS_FILE]))],
            "generator": "scripts/publish_catalogue.py",
            "rules": RULES,
        },
        "counts": {section: len(entries[section]) for section in SECTIONS},
        **entries,
        "changes": changes,
    }
    return doc, fresh


# -------------------------------------------------------------------- guard


def _walk(obj: Any, path: str = ""):
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            yield f"{path}.{key}" if path else str(key), key, value
            yield from _walk(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _walk(value, f"{path}[{i}]")


def _strings(obj: Any, path: str = ""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, Mapping):
        for key, value in obj.items():
            yield from _strings(value, f"{path}.{key}" if path else str(key))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            yield from _strings(value, f"{path}[{i}]")


def guard(doc: Mapping[str, Any]) -> list[str]:
    """Reasons the document must not be published. Empty means it may."""
    problems = [f"{path}: looks like a key or token" for path in catalog.find_secretlike(doc)]
    for path, key, _value in _walk(doc):
        if key in INTERNAL_KEYS:
            problems.append(f"{path}: {key} is an internal field")
    for path, text in _strings(doc):
        if LOCAL_PATH.search(text):
            problems.append(f"{path}: contains a local path")
    return problems


# --------------------------------------------------- a small schema checker
#
# The subset of JSON Schema that catalogue.v1.schema.json uses: type, enum,
# const, required, properties, additionalProperties, items, pattern, minimum,
# anyOf and local $ref. Enough to keep the document and the schema honest
# without adding a dependency.

_TYPES = {
    "object": lambda v: isinstance(v, Mapping),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}
_KNOWN = {"$schema", "$id", "$defs", "$ref", "title", "description", "type", "enum", "const", "required", "properties",
          "additionalProperties", "items", "pattern", "minimum", "anyOf"}


def schema_problems(value: Any, schema: Mapping[str, Any], root: Mapping[str, Any] | None = None, path: str = "$") -> list[str]:
    root = schema if root is None else root
    unknown = set(schema) - _KNOWN
    if unknown:
        return [f"{path}: the schema uses keywords this checker does not know: {sorted(unknown)}"]
    if "$ref" in schema:
        ref = str(schema["$ref"])
        if not ref.startswith("#/$defs/") or ref[8:] not in root.get("$defs", {}):
            return [f"{path}: cannot resolve {ref}"]
        return schema_problems(value, root["$defs"][ref[8:]], root, path)
    problems: list[str] = []
    if "anyOf" in schema:
        if not any(not schema_problems(value, option, root, path) for option in schema["anyOf"]):
            problems.append(f"{path}: matches none of the allowed shapes")
        return problems
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        if not any(_TYPES[k](value) for k in kinds):
            return [f"{path}: must be {' or '.join(kinds)}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if "const" in schema and value != schema["const"]:
        problems.append(f"{path}: must be {schema['const']!r}")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        problems.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in schema and value < schema["minimum"]:
        problems.append(f"{path}: is below {schema['minimum']}")
    if isinstance(value, Mapping):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{path}: {name} is missing")
        extra = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in props:
                problems.extend(schema_problems(item, props[name], root, f"{path}.{name}"))
            elif extra is False:
                problems.append(f"{path}: unexpected field {name}")
            elif isinstance(extra, Mapping):
                problems.extend(schema_problems(item, extra, root, f"{path}.{name}"))
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for i, item in enumerate(value):
            problems.extend(schema_problems(item, schema["items"], root, f"{path}[{i}]"))
    return problems


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def problems_of(doc: Mapping[str, Any]) -> list[str]:
    """Everything that stops publication: the schema, then the public-facts guard."""
    return schema_problems(doc, load_schema()) + guard(doc)


# ------------------------------------------------------------------- output


def render(doc: Mapping[str, Any]) -> tuple[str, str]:
    pretty = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    minified = json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n"
    return pretty, minified


def change_line(record: Mapping[str, Any]) -> str:
    what = f"`{record['id']}` ({str(record['section']).replace('_', ' ')})"
    change = record["change"]
    if change == "added":
        return f"- Added {what}."
    if change == "removed":
        return f"- Removed {what}."
    if change == "limits_changed":
        return f"- Limits changed for {what}: {'; '.join(record['from'])} → {'; '.join(record['to'])}."
    if change == "details_changed":
        return f"- Details changed for {what}: {record['field']}."
    label = {"policy_changed": "Policy fact", "date_changed": "Date checked", "confidence_changed": "Confidence"}.get(change, change)
    return f"- {label} changed for {what}: {record['field']} {record['from']} → {record['to']}."


def changelog_with(existing: str | None, date: str, records: list[dict[str, Any]]) -> str:
    """The changelog with `records` added under `date`. Text already there is kept; a line is never written twice."""
    text = existing if existing and existing.strip() else CHANGELOG_HEAD
    lines = [change_line(r) for r in records]
    lines = [line for line in lines if line not in text.splitlines()]
    if not lines:
        return text
    heading = f"## {date}"
    if heading in text.splitlines():
        parts = text.split(heading + "\n", 1)
        body = parts[1].lstrip("\n")
        return parts[0] + heading + "\n\n" + "\n".join(lines) + "\n" + body
    head, sep, rest = text.partition("\n## ")
    return head.rstrip("\n") + "\n\n" + heading + "\n\n" + "\n".join(lines) + "\n" + (sep + rest if sep else "")


def read_previous(out_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((out_dir / PRETTY).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PublishError(f"{PRETTY} in the output folder cannot be read ({type(exc).__name__}); fix or remove it first") from None
    if not isinstance(data, dict):
        raise PublishError(f"{PRETTY} in the output folder is not a catalogue document")
    return data


def run(*, out_dir: Path = OUT_DIR, check: bool = False, date: str | None = None, release: bool = False,
        cat: catalog.Catalog | None = None, say: Any = print) -> int:
    """Exit code 0: written, or up to date. 1: out of date (--check). 2: refused."""
    try:
        if check and release:
            raise PublishError("--check and --release do not go together")
        previous = read_previous(out_dir)
        doc, fresh = build(cat or catalog.load_catalog(), previous, date=date, release=release)
        problems = problems_of(doc)
        if problems:
            raise PublishError("the document may not be published:\n  " + "\n  ".join(problems[:20]))
    except PublishError as err:
        say(f"publish_catalogue: {err}")
        return 2
    pretty, minified = render(doc)
    if check:
        stale = [name for name, text in ((PRETTY, pretty), (MINIFIED, minified)) if _read(out_dir / name) != text]
        log = _read(out_dir / CHANGELOG) or ""
        missing = sorted({c["date"] for c in doc["changes"]} - set(re.findall(r"^## (\d{4}-\d\d-\d\d)$", log, flags=re.M)))
        if doc["changes"] and missing:
            stale.append(f"{CHANGELOG} (no section for {', '.join(missing)})")
        if stale:
            say("catalogue/ is out of date with config/: " + ", ".join(stale) + ". Run `python scripts/publish_catalogue.py` and commit the result.")
            return 1
        say(f"catalogue ok ({doc['catalogue_date']}, {sum(doc['counts'].values())} entries)")
        return 0
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / PRETTY).write_text(pretty, encoding="utf-8")
    (out_dir / MINIFIED).write_text(minified, encoding="utf-8")
    if fresh or not (out_dir / CHANGELOG).exists():
        (out_dir / CHANGELOG).write_text(changelog_with(_read(out_dir / CHANGELOG), doc["generated_at"], fresh), encoding="utf-8")
    if release:
        dated = out_dir / RELEASES / f"catalogue.v1.{doc['generated_at']}.json"
        dated.parent.mkdir(parents=True, exist_ok=True)
        dated.write_text(pretty, encoding="utf-8")
    say(f"wrote {PRETTY} and {MINIFIED}: {sum(doc['counts'].values())} entries, {len(fresh)} new change(s), "
        f"catalogue date {doc['catalogue_date']}, licence {doc['licence']['spdx'] or 'undecided'}")
    return 0


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish the catalogue as dated JSON.")
    parser.add_argument("--out", type=Path, default=OUT_DIR, metavar="DIR", help="where to write (default: catalogue/)")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if the files are out of date with config/")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="generated_at (default: the newest checked_on)")
    parser.add_argument("--release", action="store_true", help="mark the document released and keep a dated copy (needs a chosen licence)")
    args = parser.parse_args(argv)
    code = run(out_dir=args.out, check=args.check, date=args.date, release=args.release)
    if code == 0 and args.out.resolve() == OUT_DIR.resolve():
        # The app's list of hosts it may open is derived from this document (scripts/export_link_hosts.py).
        sys.path.insert(0, str(ROOT / "scripts"))
        import export_link_hosts

        code = export_link_hosts.main(["--check"] if args.check else [])
    return code


if __name__ == "__main__":
    sys.exit(main())
