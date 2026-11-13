"""Reading what a chore was given: feed files, the files of a watched folder, and its change record.

Everything goes through `ctx.workspace`, so only the folders named for the run
are read. Feed files are read with regular expressions, not an XML parser: a
feed is someone else's file, and a parser that expands entities is a way to
make this process read or allocate what it should not.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from pathlib import Path
from typing import Any

FEED_EXTS = (".xml", ".rss", ".atom", ".rdf", ".json", ".txt", ".md", ".markdown", ".html", ".htm", ".opml")
TEXT_EXTS = FEED_EXTS + (".csv", ".tsv", ".rst", ".log", ".yaml", ".yml", ".ini", ".toml", ".py", ".js", ".ts", ".css", ".tex", ".org")
MAX_FILE_BYTES = 2_000_000
MAX_LISTED = 5000
MAX_ITEM_CHARS = 400

_TAG = re.compile(r"<[^>]{0,2000}>")
_SCRIPT = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_ITEM = re.compile(r"<(item|entry)\b[^>]*>(.*?)</\1\s*>", re.I | re.S)
_URL_LINE = re.compile(r"https?://\S+", re.I)


def shown_name(path: str, root: str, roots: list[str]) -> str:
    """A file as the person knows it: its path inside the folder they named (with the folder's name when there are several)."""
    rel = os.path.relpath(path, root)
    return rel if len(roots) == 1 else f"{Path(root).name}/{rel}"


def _visible(path: str, root: str) -> bool:
    return not any(part.startswith(".") for part in Path(os.path.relpath(path, root)).parts)


def listing(ctx: Any) -> list[tuple[str, Any]]:
    """[(shown name, FileInfo)] for every visible file in the run's folders, sorted by name. Reads no file."""
    roots = ctx.workspace.roots()
    out = []
    for info in ctx.workspace.list():
        if _visible(info.path, info.root):
            out.append((shown_name(info.path, info.root, roots), info))
        if len(out) >= MAX_LISTED:
            break
    return sorted(out, key=lambda pair: pair[0])


def feed_files(ctx: Any) -> tuple[list[tuple[str, Any]], int]:
    """(feed files as (name, FileInfo), how many files of other kinds were left alone)."""
    files = listing(ctx)
    feeds = [(name, info) for name, info in files if Path(info.path).suffix.lower() in FEED_EXTS]
    return feeds, len(files) - len(feeds)


def decode(data: bytes) -> str | None:
    """Text from bytes: UTF-8, then Big5 for older Traditional Chinese files. None when it is not text."""
    if b"\x00" in data[:4096]:
        return None
    for encoding in ("utf-8-sig", "big5"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def read_text(ctx: Any, info: Any) -> tuple[str | None, str]:
    """(text, "") or (None, a reason code: large | unreadable | not-text)."""
    if info.size > MAX_FILE_BYTES:
        return None, "large"
    try:
        data = ctx.workspace.read_bytes(info.path, MAX_FILE_BYTES)
    except (OSError, ValueError):
        return None, "unreadable"
    text = decode(data)
    return (text, "") if text is not None else (None, "not-text")


def _plain(fragment: str) -> str:
    fragment = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", fragment, flags=re.S)
    fragment = html.unescape(_TAG.sub(" ", _SCRIPT.sub(" ", fragment)))
    return " ".join(_TAG.sub(" ", fragment).split())  # escaped mark-up inside a description is mark-up too


def _field(block: str, names: str) -> str:
    match = re.search(rf"<(?:\w+:)?({names})\b[^>]*>(.*?)</(?:\w+:)?\1\s*>", block, re.I | re.S)
    return _plain(match.group(2)) if match else ""


def feed_text(name: str, text: str) -> tuple[str, str]:
    """(the readable text of one feed file, "") or ("", reason: empty | addresses).

    RSS and Atom become one line per item (title, then the start of its summary); JSON Feed likewise; HTML loses
    its tags; a file that only lists web addresses (OPML, a list of links) has nothing to read without fetching.
    """
    ext = Path(name).suffix.lower()
    if ext == ".opml" or (ext in (".xml", ".rdf") and re.search(r"<opml\b", text[:2000], re.I)):
        return "", "addresses"
    lines: list[str] = []
    if ext in (".xml", ".rss", ".atom", ".rdf") or re.search(r"<(rss|feed|rdf:RDF)\b", text[:2000], re.I):
        for match in _ITEM.finditer(text):
            title = _field(match.group(2), "title")
            body = _field(match.group(2), "description|summary|encoded|content")
            line = (title + (": " if title and body else "") + body)[:MAX_ITEM_CHARS].strip()
            if line:
                lines.append("- " + line)
        out = "\n".join(lines) if lines else _plain(text)
    elif ext == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                title = " ".join(str(item.get("title") or "").split())
                body = " ".join(str(item.get("content_text") or item.get("summary") or "").split()) or _plain(str(item.get("content_html") or ""))
                line = (title + (": " if title and body else "") + body)[:MAX_ITEM_CHARS].strip()
                if line:
                    lines.append("- " + line)
            out = "\n".join(lines)
        else:
            out = text.strip()
    elif ext in (".html", ".htm"):
        out = _plain(text)
    else:
        out = text.strip()
        rest = _URL_LINE.sub("", out)
        if out and not re.sub(r"[\s\-*•,;]+", "", rest):
            return "", "addresses"
    return (out, "") if out.strip() else ("", "empty")


# ---------------------------------------------------------------- what changed in a folder


def _record_path(ctx: Any) -> Path:
    key = str(ctx.inputs.get("chore") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{4,16}", key):
        key = "f" + hashlib.sha256("\n".join(sorted(ctx.workspace.roots())).encode()).hexdigest()[:15]
    return ctx.data_dir / f"changes-{key}.json"


def _load_record(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def snapshot(ctx: Any) -> dict[str, list[int]]:
    """{shown name: [size, modified]} for every visible file. Names and numbers only."""
    return {name: [info.size, info.mtime_ns] for name, info in listing(ctx)}


def baseline(ctx: Any) -> tuple[dict[str, list[int]] | None, dict[str, Any]]:
    """(the state of the folder at the last run that delivered a result, the whole record). None on the first run.

    Each run saves the folder's state as a candidate; the next run adopts it as the baseline only if that run
    delivered its digest (`last_ok_run`, set by `muse chores`; a run outside a chore always counts). So a run
    that failed its check does not swallow the changes it saw.
    """
    record = _load_record(_record_path(ctx))
    run_id = str(ctx.inputs.get("run_id") or "")
    candidate, candidate_run = record.get("candidate"), str(record.get("candidate_run") or "")
    earlier = candidate_run != run_id if run_id else getattr(ctx, "attempt", 1) == 1  # a retry of this run keeps its baseline
    if isinstance(candidate, dict) and earlier:
        last_ok = ctx.inputs.get("last_ok_run")
        if last_ok is None or str(last_ok) == candidate_run:
            record = {"baseline": candidate, "baseline_at": record.get("candidate_at"), "baseline_run": candidate_run}
        else:
            record = {k: record[k] for k in ("baseline", "baseline_at", "baseline_run") if k in record}
    base = record.get("baseline")
    return (base if isinstance(base, dict) else None), record


def save_candidate(ctx: Any, record: dict[str, Any], current: dict[str, list[int]], date: str) -> None:
    kept = {k: record[k] for k in ("baseline", "baseline_at", "baseline_run") if k in record}
    kept.update(candidate=current, candidate_at=date, candidate_run=str(ctx.inputs.get("run_id") or ""))
    path = _record_path(ctx)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(kept, handle, ensure_ascii=False)
    os.replace(tmp, path)


def diff(base: dict[str, list[int]], current: dict[str, list[int]]) -> dict[str, list[str]]:
    """New, changed and deleted names; new and changed are newest first."""
    def newest(names: list[str]) -> list[str]:
        return sorted(names, key=lambda n: (-current[n][1], n))

    return {
        "new": newest([n for n in current if n not in base]),
        "changed": newest([n for n in current if n in base and list(base[n]) != list(current[n])]),
        "deleted": sorted(n for n in base if n not in current),
    }
