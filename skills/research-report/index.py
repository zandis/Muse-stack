"""The index: which files were read, their text in bounded chunks, and (optionally) an embedding per chunk.

One SQLite file in the skill's own data folder. It holds text copied from the
person's files, so it stays on this computer, mode 0600, and nothing in it is
ever put in an event or a note.

Incremental: a file whose size and modification time are unchanged is not
read again. Hard caps on files, bytes read in one run and chunks keep a run
bounded; when one is hit the caller is told so it can say so.

PDFs are read by pdf.py when pypdf is installed (see there for where parsing
runs and the limits). A PDF paragraph remembers its page and its place on the
page, so a citation reads `lease.pdf › page 3, paragraph 2`. A PDF that is a
scan, locked, broken or cut short is remembered as such, so the next run says
so again without reading it again.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

from . import pdf as pdf_text
from .textwork import weight

TEXT_TYPES = {".txt", ".text", ".md", ".markdown", ".rst"}
LINE_TYPES = {".csv", ".tsv", ".jsonl"}
HTML_TYPES = {".html", ".htm"}
JSON_TYPES = {".json"}
PDF_TYPES = {".pdf"}
SUPPORTED = TEXT_TYPES | LINE_TYPES | HTML_TYPES | JSON_TYPES

MAX_FILES = 2000
MAX_FILE_BYTES = 2_000_000
MAX_RUN_BYTES = 50_000_000
MAX_CHUNKS = 20_000
MAX_PDF_BYTES = 20_000_000  # a PDF holds fonts and images besides its text, so it may be larger than a text file
MAX_PDF_PAGES = pdf_text.MAX_PAGES
MAX_PDF_SECONDS = pdf_text.MAX_SECONDS
NAMED = 5  # at most this many file names in one note
CHUNK_WEIGHT = 320  # about this many model tokens a chunk
OVERLAP_WEIGHT = 80  # at most this much of the previous chunk is repeated at the start of the next

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  state TEXT NOT NULL,          -- ok | empty | large | binary | unreadable | scanned | locked | broken
  chunks INTEGER NOT NULL DEFAULT 0,
  info TEXT                     -- JSON, PDFs only: {"pages", "scanned_pages", "bad_pages", "cut"}
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  para_start INTEGER NOT NULL,
  para_end INTEGER NOT NULL,
  text TEXT NOT NULL,
  paras TEXT NOT NULL,          -- JSON: the paragraph number of each blank-line-separated part of text (0 = a repeated table header)
  places TEXT,                  -- JSON, PDFs only: [page, paragraph on that page] for each part of text (null for a header)
  emb BLOB,
  emb_model TEXT
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
"""


@dataclass
class Caps:
    files: int = MAX_FILES
    file_bytes: int = MAX_FILE_BYTES
    run_bytes: int = MAX_RUN_BYTES
    chunks: int = MAX_CHUNKS
    pdf_bytes: int = MAX_PDF_BYTES
    pdf_pages: int = MAX_PDF_PAGES
    pdf_seconds: float = MAX_PDF_SECONDS


@dataclass
class Stats:
    seen: int = 0  # supported files looked at
    read: int = 0  # files read this run (new or changed)
    reused: int = 0  # unchanged files not read again
    removed: int = 0  # files gone from disk, dropped from the index
    chunks: int = 0  # chunks available for this run's folders
    unsupported: int = 0
    pdf_skipped: int = 0  # PDFs not read because no PDF reader is installed
    large: int = 0
    unreadable: int = 0
    pdf_read: int = 0  # PDFs with text, read or reused
    pdf_scanned: int = 0  # no text layer: scanned, not read
    pdf_locked: int = 0  # encrypted with a password: locked, not read
    pdf_broken: int = 0  # not a PDF, truncated, or the parser failed: could not be read
    pdf_empty: int = 0  # no text and no image
    pdf_large: int = 0  # over the PDF byte cap
    pdf_cut_pages: int = 0  # only the first pages were read (page cap)
    pdf_cut_time: int = 0  # reading stopped at the time cap
    pdf_scanned_pages: int = 0  # pages without a text layer inside PDFs that were otherwise read
    pdf_bad_pages: int = 0  # pages the parser failed on inside PDFs that were otherwise read
    hit_files: bool = False
    hit_bytes: bool = False
    hit_chunks: bool = False
    file_ids: set[int] = field(default_factory=set)
    names: dict[str, list[str]] = field(default_factory=dict)  # kind -> paths, for notes; never in events

    def counts(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k not in ("file_ids", "names")}

    def name(self, kind: str, path: str) -> None:
        self.names.setdefault(kind, []).append(path)


@dataclass(frozen=True)
class Chunk:
    id: int
    path: str
    para_start: int
    para_end: int
    text: str
    paras: tuple[int, ...] = ()  # the paragraph number of each part of `text` (parts are separated by a blank line)
    places: tuple[Any, ...] = ()  # PDFs: (page, paragraph on that page) for each part, None for a header; else empty


# ---------------------------------------------------------------- text extraction


def pdf_reader() -> Callable[..., Any] | None:
    """pdf.read when pypdf is installed (the optional `pdf` extra, requirements/pdf.lock), else None.

    The reader takes (data, max_pages, max_seconds) and returns a pdf.Reading. For tests and older callers it may
    instead return plain text, which is read as one page.
    """
    return pdf_text.read if pdf_text.available() else None


def decode(data: bytes) -> str | None:
    """Text of a file, or None when it looks binary. UTF-8 first; Big5 (cp950) for older Traditional Chinese files."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    if b"\x00" in data[:8192]:
        return None
    for encoding in ("utf-8", "cp950"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


class _Strip(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "ul", "ol",
             "blockquote", "pre", "header", "footer", "title", "dd", "dt"}
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n\n" if tag != "br" else "\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Strip()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return "".join(parser.parts)


def extract(suffix: str, data: bytes, pdf: Callable[..., Any] | None = None) -> tuple[str | None, bool]:
    """(text or None, one-paragraph-per-line?). A PDF's pages are joined; `read_pdf` keeps them apart."""
    if suffix in PDF_TYPES:
        reading = read_pdf(data, pdf)
        if reading is None or reading.state != pdf_text.OK:
            return None, False
        return "\n\n".join(text for _, _, text in reading.paragraphs()), False
    text = decode(data)
    if text is None:
        return None, False
    if suffix in HTML_TYPES:
        return html_to_text(text), False
    if suffix in JSON_TYPES:
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=1), True
        except ValueError:
            return text, True
    return text, suffix in LINE_TYPES


def read_pdf(data: bytes, pdf: Callable[..., Any] | None, caps: Caps | None = None) -> pdf_text.Reading | None:
    """The reading of a PDF by `pdf`; None when there is no reader. A reader that fails is a broken file, never a crash."""
    if pdf is None:
        return None
    caps = caps or Caps()
    try:
        got = pdf(data, caps.pdf_pages, caps.pdf_seconds)
    except TypeError:  # a reader that takes only the bytes
        try:
            got = pdf(data)
        except Exception:
            return pdf_text.Reading(pdf_text.BROKEN)
    except Exception:
        return pdf_text.Reading(pdf_text.BROKEN)
    if isinstance(got, pdf_text.Reading) or hasattr(got, "state"):
        return got
    paras = paragraphs(str(got or ""))
    if not paras:
        return pdf_text.Reading(pdf_text.EMPTY, total_pages=1)
    return pdf_text.Reading(pdf_text.OK, pages=[(1, paras)], total_pages=1)


# ---------------------------------------------------------------- chunking

_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])|(?<=[.])\s+|\n+")


def paragraphs(text: str, by_line: bool = False) -> list[str]:
    """Paragraphs in order (blank-line separated; one per line for tables). Paragraph n is item n-1."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    raw = text.split("\n") if by_line else re.split(r"\n[ \t]*\n+", text)
    out = []
    for item in raw:
        item = re.sub(r"[ \t　]+", " ", item).strip()
        item = re.sub(r" ?\n ?", "\n", item)
        if item:
            out.append(item)
    return out


def _pieces(paragraph: str, limit: int) -> list[str]:
    """A paragraph over the limit, cut at sentence ends (or by length when a sentence is itself too long)."""
    if weight(paragraph) <= limit:
        return [paragraph]
    out: list[str] = []
    current = ""
    for sentence in (s for s in _SENTENCE_END.split(paragraph) if s and s.strip()):
        while weight(sentence) > limit:  # no sentence end in reach: cut by length
            cut = max(1, len(sentence) * limit // max(1, weight(sentence)))
            if current:
                out.append(current)
                current = ""
            out.append(sentence[:cut])
            sentence = sentence[cut:]
        joined = f"{current} {sentence}".strip() if current else sentence.strip()
        if current and weight(joined) > limit:
            out.append(current)
            current = sentence.strip()
        else:
            current = joined
    if current:
        out.append(current)
    return out


def chunk(paras: list[str], limit: int = CHUNK_WEIGHT, overlap: int = OVERLAP_WEIGHT,
          header: str | None = None) -> list[tuple[int, int, str, list[int]]]:
    """(first paragraph, last paragraph, text, paragraph number of each part of text) per chunk. Numbers start at 1.

    Paragraphs are packed up to `limit`; each chunk after the first starts with
    the tail of the previous one (whole units, at most `overlap`), so a fact
    that straddles a boundary is still found whole in one chunk. `header` (a
    table's first row) is repeated at the top of later chunks.
    """
    units: list[tuple[int, str]] = [(n, piece) for n, para in enumerate(paras, start=1) for piece in _pieces(para, limit)]
    chunks: list[tuple[int, int, str, list[int]]] = []
    current: list[tuple[int, str]] = []
    fresh = 0  # units in `current` that are not overlap
    size = 0

    def flush() -> None:
        new = current[len(current) - fresh:]
        body = "\n\n".join(text for _, text in current)
        numbers = [n for n, _ in current]
        if header and new[0][0] != 1:
            body, numbers = f"{header}\n\n{body}", [0] + numbers
        chunks.append((new[0][0], new[-1][0], body, numbers))

    for unit in units:
        w = weight(unit[1])
        if fresh and size + w > limit:
            flush()
            tail: list[tuple[int, str]] = []
            carried = 0
            for prev in reversed(current):
                if carried + weight(prev[1]) > overlap:
                    break
                tail.insert(0, prev)
                carried += weight(prev[1])
            current, size, fresh = tail, carried, 0
        current.append(unit)
        size += w
        fresh += 1
    if fresh:
        flush()
    return chunks


# ---------------------------------------------------------------- the database


def open_db(folder: Path) -> sqlite3.Connection:
    path = folder / "index.sqlite"
    db = sqlite3.connect(str(path), timeout=30)
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(SCHEMA)
    for table, column in (("files", "info"), ("chunks", "places")):  # an index made before PDFs were read
        if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return db


def _hidden(path: str, root: str) -> bool:
    return any(part.startswith(".") for part in Path(os.path.relpath(path, root)).parts)


def _shown(path: str, roots: list[str]) -> str:
    for root in roots:
        if path.startswith(root.rstrip(os.sep) + os.sep):
            return os.path.relpath(path, root) if len(roots) == 1 else path
    return path


def _count_pdf(stats: Stats, state: str, info: dict[str, Any], shown: str) -> None:
    """Count a PDF's outcome, read now or remembered from an earlier run."""
    kind = {"ok": "pdf_read", "scanned": "pdf_scanned", "locked": "pdf_locked", "broken": "pdf_broken",
            "empty": "pdf_empty", "large": "pdf_large"}.get(state)
    if kind is None:
        return
    setattr(stats, kind, getattr(stats, kind) + 1)
    if kind != "pdf_read":
        stats.name(kind, shown)
        return
    if info.get("cut") == "pages":
        stats.pdf_cut_pages += 1
        stats.name("pdf_cut_pages", shown)
    elif info.get("cut") == "time":
        stats.pdf_cut_time += 1
        stats.name("pdf_cut_time", shown)
    if info.get("scanned_pages"):
        stats.pdf_scanned_pages += int(info["scanned_pages"])
        stats.name("pdf_scanned_pages", shown)
    if info.get("bad_pages"):
        stats.pdf_bad_pages += int(info["bad_pages"])
        stats.name("pdf_bad_pages", shown)


def _read_file(info: Any, suffix: str, workspace: Any, caps: Caps,
               pdf: Callable[..., Any] | None) -> tuple[str, list[tuple[int, int, str, list[int]]], list[list[Any]], dict[str, Any]]:
    """(state, chunk rows, places per chunk, PDF info) for one file that is to be read now."""
    is_pdf = suffix in PDF_TYPES
    limit = caps.pdf_bytes if is_pdf else caps.file_bytes
    try:
        data = workspace.read_bytes(info.path, limit)
    except (OSError, ValueError):
        return "unreadable", [], [], {}
    if not is_pdf:
        text, by_line = extract(suffix, data)
        if text is None:
            return "binary", [], [], {}
        paras = paragraphs(text, by_line)
        header = paras[0] if suffix in (".csv", ".tsv") and paras and weight(paras[0]) <= OVERLAP_WEIGHT else None
        rows = chunk(paras, header=header)
        return ("ok" if rows else "empty"), rows, [[] for _ in rows], {}
    reading = read_pdf(data, pdf, caps)
    assert reading is not None  # PDFs reach here only when there is a reader
    details = {"pages": reading.total_pages, "scanned_pages": reading.scanned_pages, "bad_pages": reading.bad_pages,
               "cut": reading.cut}
    if reading.state != pdf_text.OK:
        return reading.state, [], [], details
    flat = reading.paragraphs()
    rows = chunk([text for _, _, text in flat])
    places = [[[flat[n - 1][0], flat[n - 1][1]] if n else None for n in numbers] for _, _, _, numbers in rows]
    return ("ok" if rows else "empty"), rows, places, details


def sync(db: sqlite3.Connection, workspace: Any, caps: Caps | None = None,
         pdf: Callable[..., Any] | None = None) -> Stats:
    """Bring the index up to date for the workspace's folders. Returns counts and the ids of the files to search."""
    caps = caps or Caps()
    stats = Stats()
    known = {row[0]: row[1:] for row in db.execute("SELECT path, id, size, mtime_ns, state, chunks, info FROM files")}
    roots = [str(r) for r in workspace.roots()]
    seen_paths: set[str] = set()
    run_bytes = 0
    complete = True
    supported = SUPPORTED | (PDF_TYPES if pdf is not None else set())
    for info in workspace.list():
        if _hidden(info.path, info.root):
            continue
        suffix = Path(info.path).suffix.lower()
        if suffix not in supported:
            if suffix in PDF_TYPES:
                stats.pdf_skipped += 1
                stats.name("pdf_skipped", _shown(info.path, roots))
            else:
                stats.unsupported += 1
            continue
        if stats.seen >= caps.files:
            stats.hit_files, complete = True, False
            break
        stats.seen += 1
        seen_paths.add(info.path)
        shown = _shown(info.path, roots)
        is_pdf = suffix in PDF_TYPES
        old = known.get(info.path)
        if old is not None and old[1] == info.size and old[2] == info.mtime_ns:
            file_id, _, _, state, n, details = old
            if state == "ok" and stats.chunks + n > caps.chunks:
                stats.hit_chunks = True
                continue
            stats.reused += 1
            if is_pdf:
                _count_pdf(stats, state, json.loads(details or "{}"), shown)
            if state == "large" and not is_pdf:
                stats.large += 1
            elif state in ("binary", "unreadable"):
                stats.unreadable += 1
            elif state == "ok":
                stats.chunks += n
                stats.file_ids.add(file_id)
            continue
        if stats.hit_chunks:
            complete = False
            continue
        state, rows, places, details = "ok", [], [], {}
        if info.size > (caps.pdf_bytes if is_pdf else caps.file_bytes):
            state = "large"
            if not is_pdf:
                stats.large += 1
        elif run_bytes + info.size > caps.run_bytes:
            stats.hit_bytes, complete = True, False
            continue
        else:
            run_bytes += info.size
            state, rows, places, details = _read_file(info, suffix, workspace, caps, pdf)
            if state in ("binary", "unreadable"):
                stats.unreadable += 1
            room = caps.chunks - stats.chunks
            if len(rows) > room:
                rows, places = rows[:max(0, room)], places[:max(0, room)]
                stats.hit_chunks, complete = True, False
        if is_pdf:
            _count_pdf(stats, state, details, shown)
        with db:
            if old is not None:
                db.execute("DELETE FROM files WHERE id = ?", (old[0],))
            cursor = db.execute("INSERT INTO files (path, size, mtime_ns, state, chunks, info) VALUES (?,?,?,?,?,?)",
                                (info.path, info.size, info.mtime_ns, state, len(rows), json.dumps(details) if is_pdf else None))
            file_id = cursor.lastrowid
            db.executemany("INSERT INTO chunks (file_id, seq, para_start, para_end, text, paras, places) VALUES (?,?,?,?,?,?,?)",
                           [(file_id, seq, a, b, text, json.dumps(numbers), json.dumps(where) if is_pdf else None)
                            for seq, ((a, b, text, numbers), where) in enumerate(zip(rows, places))])
        stats.read += 1
        if state == "ok":
            stats.chunks += len(rows)
            stats.file_ids.add(int(file_id))
    if complete:  # only a full walk proves a file is gone
        gone = [(path, values[0]) for path, values in known.items()
                if path not in seen_paths and any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in roots)]
        if gone:
            with db:
                db.executemany("DELETE FROM files WHERE id = ?", [(file_id,) for _, file_id in gone])
            stats.removed = len(gone)
    return stats


def load_chunks(db: sqlite3.Connection, file_ids: Iterable[int]) -> list[Chunk]:
    wanted = set(file_ids)
    rows = db.execute(
        "SELECT c.id, c.file_id, f.path, c.para_start, c.para_end, c.text, c.paras, c.places FROM chunks c "
        "JOIN files f ON f.id = c.file_id ORDER BY f.path, c.seq")
    return [Chunk(r[0], r[2], r[3], r[4], r[5], tuple(json.loads(r[6])),
                  tuple(tuple(p) if p else None for p in json.loads(r[7])) if r[7] else ())
            for r in rows if r[1] in wanted]


# ---------------------------------------------------------------- where a passage is, in words


def place_label(numbers: Iterable[int], places: dict[int, Any], zh: bool) -> str | None:
    """`page 3, paragraph 2` (第 3 頁第 2 段) for paragraphs of a PDF; None when the numbers have no page.

    `places` maps a paragraph number of the file to (page, paragraph on that page). Paragraphs on one page are
    listed (`paragraphs 2, 4`, a run as `2-4`); paragraphs on several pages give only the pages (`pages 3-4`).
    """
    spots = sorted({tuple(places[n]) for n in numbers if places.get(n)})
    if not spots:
        return None
    pages = sorted({page for page, _ in spots})
    if len(pages) == 1:
        paras = _span([k for _, k in spots], zh)
        if zh:
            return f"第 {pages[0]} 頁第 {paras} 段"
        return f"page {pages[0]}, paragraph{'s' if len(spots) > 1 else ''} {paras}"
    return f"第 {_span(pages, zh)} 頁" if zh else f"pages {_span(pages, zh)}"


def _span(numbers: list[int], zh: bool) -> str:
    numbers = sorted(set(numbers))
    if len(numbers) > 2 and numbers[-1] - numbers[0] == len(numbers) - 1:
        return f"{numbers[0]}-{numbers[-1]}"
    return ("、" if zh else ", ").join(str(n) for n in numbers)


def places_of(passage: dict[str, Any]) -> dict[int, Any]:
    """{paragraph number: (page, paragraph on the page)} from a passage dict's `paragraphs` and `places`."""
    return {n: p for n, p in zip(passage.get("paragraphs") or [], passage.get("places") or []) if n and p}


def _names(stats: Stats, kind: str) -> str:
    names = stats.names.get(kind, [])
    shown = ", ".join(names[:NAMED]) + (f" and {len(names) - NAMED} more" if len(names) > NAMED else "")
    return f": {shown}" if shown else ""


def pdf_notes(stats: Stats) -> list[str]:
    """What the person is told about PDFs: each kind of PDF not read, or not read whole, counted and named."""
    out = []
    if stats.pdf_skipped:
        out.append(f"PDF reading is not installed; {stats.pdf_skipped} PDF(s) were skipped{_names(stats, 'pdf_skipped')}.")
    for kind, words in (("pdf_scanned", "scanned (no text layer), not read"), ("pdf_locked", "locked (a password is needed), not read"),
                        ("pdf_broken", "could not be read (damaged or not really a PDF)"),
                        ("pdf_empty", "hold no text"),
                        ("pdf_large", f"too large to read (over {MAX_PDF_BYTES // 1_000_000} MB each)")):
        count = getattr(stats, kind)
        if count:
            out.append(f"{count} PDF(s) {words}{_names(stats, kind)}.")
    if stats.pdf_cut_pages:
        out.append(f"{stats.pdf_cut_pages} PDF(s) were read only in part: the limit on pages per PDF was reached"
                   f"{_names(stats, 'pdf_cut_pages')}.")
    if stats.pdf_cut_time:
        out.append(f"{stats.pdf_cut_time} PDF(s) took too long; only their first pages were read{_names(stats, 'pdf_cut_time')}.")
    if stats.pdf_scanned_pages:
        out.append(f"{stats.pdf_scanned_pages} scanned page(s) without text were not read{_names(stats, 'pdf_scanned_pages')}.")
    if stats.pdf_bad_pages:
        out.append(f"{stats.pdf_bad_pages} PDF page(s) could not be read{_names(stats, 'pdf_bad_pages')}.")
    return out


def embeddings(db: sqlite3.Connection, chunk_ids: Iterable[int], model: str) -> dict[int, bytes]:
    wanted = set(chunk_ids)
    rows = db.execute("SELECT id, emb FROM chunks WHERE emb IS NOT NULL AND emb_model = ?", (model,))
    return {r[0]: r[1] for r in rows if r[0] in wanted}


def store_embeddings(db: sqlite3.Connection, items: Iterable[tuple[int, bytes]], model: str) -> None:
    with db:
        db.executemany("UPDATE chunks SET emb = ?, emb_model = ? WHERE id = ?", [(blob, model, cid) for cid, blob in items])
