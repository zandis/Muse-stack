"""The index: which files were read, their text in bounded chunks, and (optionally) an embedding per chunk.

One SQLite file in the skill's own data folder. It holds text copied from the
person's files, so it stays on this computer, mode 0600, and nothing in it is
ever put in an event or a note.

Incremental: a file whose size and modification time are unchanged is not
read again. Hard caps on files, bytes read in one run and chunks keep a run
bounded; when one is hit the caller is told so it can say so.
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

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
CHUNK_WEIGHT = 320  # about this many model tokens a chunk
OVERLAP_WEIGHT = 80  # at most this much of the previous chunk is repeated at the start of the next

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  state TEXT NOT NULL,          -- ok | empty | large | binary | unreadable
  chunks INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  para_start INTEGER NOT NULL,
  para_end INTEGER NOT NULL,
  text TEXT NOT NULL,
  paras TEXT NOT NULL,          -- JSON: the paragraph number of each blank-line-separated part of text (0 = a repeated table header)
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


@dataclass
class Stats:
    seen: int = 0  # supported files looked at
    read: int = 0  # files read this run (new or changed)
    reused: int = 0  # unchanged files not read again
    removed: int = 0  # files gone from disk, dropped from the index
    chunks: int = 0  # chunks available for this run's folders
    unsupported: int = 0
    pdf_skipped: int = 0
    large: int = 0
    unreadable: int = 0
    hit_files: bool = False
    hit_bytes: bool = False
    hit_chunks: bool = False
    file_ids: set[int] = field(default_factory=set)

    def counts(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if k != "file_ids"}


@dataclass(frozen=True)
class Chunk:
    id: int
    path: str
    para_start: int
    para_end: int
    text: str
    paras: tuple[int, ...] = ()  # the paragraph number of each part of `text` (parts are separated by a blank line)


# ---------------------------------------------------------------- text extraction


def pdf_reader() -> Callable[[bytes], str] | None:
    """A pure-Python PDF text extractor if one is already installed; Muse adds no dependency for it."""
    for name in ("pypdf", "PyPDF2"):
        try:
            module = __import__(name)
        except Exception:
            continue

        def read(data: bytes, module: Any = module) -> str:
            reader = module.PdfReader(io.BytesIO(data))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)

        return read
    return None


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


def extract(suffix: str, data: bytes, pdf: Callable[[bytes], str] | None = None) -> tuple[str | None, bool]:
    """(text or None, one-paragraph-per-line?)."""
    if suffix in PDF_TYPES:
        if pdf is None:
            return None, False
        try:
            return pdf(data), False
        except Exception:
            return None, False
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
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return db


def _hidden(path: str, root: str) -> bool:
    return any(part.startswith(".") for part in Path(os.path.relpath(path, root)).parts)


def sync(db: sqlite3.Connection, workspace: Any, caps: Caps | None = None,
         pdf: Callable[[bytes], str] | None = None) -> Stats:
    """Bring the index up to date for the workspace's folders. Returns counts and the ids of the files to search."""
    caps = caps or Caps()
    stats = Stats()
    known = {row[0]: row[1:] for row in db.execute("SELECT path, id, size, mtime_ns, state, chunks FROM files")}
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
            else:
                stats.unsupported += 1
            continue
        if stats.seen >= caps.files:
            stats.hit_files, complete = True, False
            break
        stats.seen += 1
        seen_paths.add(info.path)
        old = known.get(info.path)
        if old is not None and old[1] == info.size and old[2] == info.mtime_ns:
            file_id, _, _, state, n = old
            if state == "ok" and stats.chunks + n > caps.chunks:
                stats.hit_chunks = True
                continue
            stats.reused += 1
            if state == "large":
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
        state, rows = "ok", []
        if info.size > caps.file_bytes:
            state = "large"
            stats.large += 1
        elif run_bytes + info.size > caps.run_bytes:
            stats.hit_bytes, complete = True, False
            continue
        else:
            try:
                data = workspace.read_bytes(info.path, caps.file_bytes)
            except (OSError, ValueError):
                data = None
            run_bytes += info.size
            text, by_line = extract(suffix, data, pdf) if data is not None else (None, False)
            if text is None:
                state = "unreadable" if data is None else "binary"
                stats.unreadable += 1
            else:
                paras = paragraphs(text, by_line)
                header = paras[0] if suffix in (".csv", ".tsv") and paras and weight(paras[0]) <= OVERLAP_WEIGHT else None
                rows = chunk(paras, header=header)
                if not rows:
                    state = "empty"
                room = caps.chunks - stats.chunks
                if len(rows) > room:
                    rows = rows[:max(0, room)]
                    stats.hit_chunks, complete = True, False
        with db:
            if old is not None:
                db.execute("DELETE FROM files WHERE id = ?", (old[0],))
            cursor = db.execute("INSERT INTO files (path, size, mtime_ns, state, chunks) VALUES (?,?,?,?,?)",
                                (info.path, info.size, info.mtime_ns, state, len(rows)))
            file_id = cursor.lastrowid
            db.executemany("INSERT INTO chunks (file_id, seq, para_start, para_end, text, paras) VALUES (?,?,?,?,?,?)",
                           [(file_id, seq, a, b, text, json.dumps(numbers)) for seq, (a, b, text, numbers) in enumerate(rows)])
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
        "SELECT c.id, c.file_id, f.path, c.para_start, c.para_end, c.text, c.paras FROM chunks c JOIN files f ON f.id = c.file_id "
        "ORDER BY f.path, c.seq")
    return [Chunk(r[0], r[2], r[3], r[4], r[5], tuple(json.loads(r[6]))) for r in rows if r[1] in wanted]


def embeddings(db: sqlite3.Connection, chunk_ids: Iterable[int], model: str) -> dict[int, bytes]:
    wanted = set(chunk_ids)
    rows = db.execute("SELECT id, emb FROM chunks WHERE emb IS NOT NULL AND emb_model = ?", (model,))
    return {r[0]: r[1] for r in rows if r[0] in wanted}


def store_embeddings(db: sqlite3.Connection, items: Iterable[tuple[int, bytes]], model: str) -> None:
    with db:
        db.executemany("UPDATE chunks SET emb = ?, emb_model = ? WHERE id = ?", [(blob, model, cid) for cid, blob in items])
