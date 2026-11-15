"""PDF text for the file skills: pages, paragraphs, and why a PDF was not read. Uses pypdf when it is installed.

Where this runs, and why: in the skill, in the same process that reads the person's files. With the sandbox on,
that is the locked-down child (no network, no keys, no Muse state, memory and time limits), so a bug in the PDF
parser can reach nothing but the folders named for the run. The parent never parses a PDF. pypdf is pure Python:
a malformed file can make it raise, loop or allocate, but not corrupt memory. Those are bounded here (pages, bytes,
seconds, and pypdf's own decoding limits, tightened) and by the sandbox's limits when it is on.

`read(data)` returns a `Reading`:

    ok        text was found; `pages` holds (page number, [paragraphs]) for the pages that have text
    scanned   no page has a text layer, and at least one page holds an image: a scan. Not read (no OCR here)
    empty     no page has text and none holds an image
    locked    encrypted, and it does not open without a password (or its cipher needs a package not installed)
    broken    not a PDF, truncated past repair, or the parser failed on every page

Partial reads are said, never hidden: `cut` is "pages" when only the first `max_pages` pages were read and "time"
when reading stopped at `max_seconds` (checked between pages; one page is not interrupted); `scanned_pages` counts
pages without a text layer inside a PDF that was otherwise read; `bad_pages` pages the parser failed on.

Text normalisation for PDFs happens in one place, `tidy`, and only undoes what PDF layout does to text:

1. Lines of one paragraph are joined. A hyphen at the end of a line is dropped when a lower-case letter stands on
   both sides ("re-" + "turns" -> "returns"); between two CJK characters the join adds nothing; anywhere else it
   adds one space. A line break between two digits is a space: two numbers are never merged into one.
2. Spaces that extraction puts between two CJK characters (characters and CJK punctuation) are removed. A space
   next to a digit or a Latin letter is kept.
3. The Latin ligatures U+FB00-U+FB06 become their letters (U+FB01 -> "fi"); soft hyphens and zero-width
   characters are removed; no-break and other spaces become a plain space.

Nothing else is changed: no digit is added, removed or altered, and full-width forms are left as the document has
them (the quote check in quotes.py folds width and case when it compares, for every kind of file alike). The text
that is indexed, sent to the model and checked against its quotes is this tidied text, so a quote copied from a
passage is found in it exactly as for a Markdown file.

Paragraphs are found from the layout: a gap between lines clearly larger than the page's usual line spacing, a
jump up the page (the next column), a line that starts far from where the previous one started, or a table row
(text on one line in cells far apart) starts a new paragraph.
"""

from __future__ import annotations

import io
import logging
import re
import time
import unicodedata
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable

MAX_PAGES = 300  # pages read from one PDF
MAX_SECONDS = 20.0  # seconds spent on one PDF (checked between pages)
MAX_INFLATED = 32_000_000  # bytes one stream may inflate to (pypdf's default is 75 MB)
MAX_DECODING_WORK = 64_000_000  # accumulated decoding work for one stream (pypdf's default is 300 MB)

OK, SCANNED, EMPTY, LOCKED, BROKEN = "ok", "scanned", "empty", "locked", "broken"

_CJK = "\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef"
_CJK_CHAR = re.compile(f"[{_CJK}]")
_CJK_SPACE = re.compile(f"(?<=[{_CJK}]) +(?=[{_CJK}])")
_LIGATURES = {chr(c): unicodedata.normalize("NFKC", chr(c)) for c in range(0xFB00, 0xFB07)}
_INVISIBLE = dict.fromkeys(map(ord, "\u00ad\u200b\u200c\u200d\u2060\ufeff"), None)
_SPACES = re.compile("[ \u00a0\u2000-\u200a\u202f\u205f]+")


@dataclass
class Reading:
    state: str
    pages: list[tuple[int, list[str]]] = field(default_factory=list)
    total_pages: int = 0
    scanned_pages: int = 0
    bad_pages: int = 0
    cut: str = ""  # "" | "pages" | "time"

    def paragraphs(self) -> list[tuple[int, int, str]]:
        """(page, paragraph within the page, text) for every paragraph, in order. Numbers start at 1."""
        return [(page, k, text) for page, paras in self.pages for k, text in enumerate(paras, start=1)]


def available() -> bool:
    try:
        import pypdf  # noqa: F401
    except Exception:
        return False
    return True


# ---------------------------------------------------------------- the one place PDF text is normalised


def _line(text: str) -> str:
    """One line: ligatures, invisible characters and spaces as in the module text. A tab separates table cells."""
    text = "".join(_LIGATURES.get(ch, ch) for ch in text).translate(_INVISIBLE)
    cells = (_CJK_SPACE.sub("", _SPACES.sub(" ", cell).strip()) for cell in text.split("\t"))
    return " ".join(cell for cell in cells if cell)


def tidy(lines: list[str]) -> str:
    """One paragraph from its lines, by the rules in the module text."""
    out = ""
    for raw in lines:
        line = _line(raw)
        if not line:
            continue
        if not out:
            out = line
        elif out.endswith("-") and len(out) > 1 and out[-2].isalpha() and out[-2].islower() and line[0].islower():
            out = out[:-1] + line
        elif _CJK_CHAR.match(out[-1]) and _CJK_CHAR.match(line[0]):
            out += line
        else:
            out += " " + line
    return out


# ---------------------------------------------------------------- layout: lines, then paragraphs


def _estimate(text: str, size: float) -> float:
    return sum(size if _CJK_CHAR.match(ch) else 0.5 * size for ch in text)


class _Page:
    """Collects pypdf's text pieces with their position, then makes lines and paragraphs."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []  # {"y", "x", "start", "size", "text", "end", "cells"}

    def visit(self, text: str, cm: Any, tm: Any, font: Any, size: Any) -> None:
        if not text or text == "\n":
            return
        try:
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            size = abs(float(size or 0)) * (abs(tm[3]) or 1.0) * (abs(cm[3]) or 1.0) or 10.0
        except (TypeError, ValueError, IndexError):
            x, y, size = 0.0, 0.0, 10.0
        for number, piece in enumerate(text.split("\n")):
            if number:  # a line break inside one piece: the rest goes one line lower
                y -= size * 1.2
            if piece:
                self._add(piece, float(x), float(y), float(size))

    def _add(self, text: str, x: float, y: float, size: float) -> None:
        last = self.lines[-1] if self.lines else None
        if last is not None and abs(last["y"] - y) <= 0.4 * max(size, last["size"]):
            if not text.strip():  # a space pypdf put between two pieces: it takes no room of its own
                last["text"] += text
                return
            if x > last["x"] + 0.5:  # a piece placed by itself further right on the same line
                if x - last["end"] > 1.2 * size:  # far from the text before it: a table cell
                    last["cells"] = True
                    text = "\t" + text.lstrip()
                last["x"], last["end"] = x, x
            last["text"] += text
            last["end"] += _estimate(text, size)
            return
        self.lines.append({"y": y, "x": x, "start": x, "size": size, "text": text, "end": x + _estimate(text, size),
                           "cells": False})

    def paragraphs(self) -> list[str]:
        lines = [line for line in self.lines if _line(line["text"])]
        if not lines:
            return []
        gaps = sorted(a["y"] - b["y"] for a, b in zip(lines, lines[1:]) if 0.8 * a["size"] < a["y"] - b["y"] < 2.5 * a["size"])
        leading = gaps[len(gaps) // 4] if gaps else 1.2 * lines[0]["size"]  # the usual line spacing: a low quartile
        groups: list[list[str]] = []
        previous: dict[str, Any] | None = None
        for line in lines:
            new = previous is None or previous["cells"] or line["cells"]
            if not new and previous is not None:
                gap = previous["y"] - line["y"]
                new = (gap < 0.3 * line["size"] or gap > 1.3 * leading or gap > 1.7 * line["size"]
                       or abs(line["start"] - previous["start"]) > 6 * line["size"])
            if new:
                groups.append([])
            groups[-1].append(line["text"])
            previous = line
        return [p for p in (tidy(g) for g in groups) if p]


def _has_image(page: Any) -> bool:
    try:
        resources = page.get("/Resources")
        objects = resources.get_object().get("/XObject") if resources is not None else None
        if objects is None:
            return False
        for ref in objects.get_object().values():
            if ref.get_object().get("/Subtype") == "/Image":
                return True
    except Exception:
        return False
    return False


# ---------------------------------------------------------------- reading a file


def read(data: bytes, max_pages: int = MAX_PAGES, max_seconds: float = MAX_SECONDS,
         clock: Callable[[], float] = time.monotonic) -> Reading:
    """The text of a PDF, page by page. Never raises for a bad file: the state says what happened."""
    import pypdf  # the caller checks `available()` first

    logging.getLogger("pypdf").setLevel(logging.CRITICAL)  # its warnings could quote the file
    limits = {"zlib_maximum_output_length": MAX_INFLATED, "lzw_maximum_output_length": MAX_INFLATED,
              "run_length_maximum_output_length": MAX_INFLATED, "stream_decoding_work_maximum_length": MAX_DECODING_WORK,
              "jbig2dec_binary": None}
    apply = getattr(pypdf, "apply_configuration", None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if apply is None:  # an older pypdf: its built-in limits only
            return _read(pypdf, data, max_pages, max_seconds, clock)
        with apply(**limits):
            return _read(pypdf, data, max_pages, max_seconds, clock)


def _read(pypdf: Any, data: bytes, max_pages: int, max_seconds: float, clock: Callable[[], float]) -> Reading:
    started = clock()
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception:  # a cipher pypdf cannot run without an extra package, or a damaged dictionary
                return Reading(LOCKED)
            if not opened:
                return Reading(LOCKED)
        total = len(reader.pages)
    except Exception:
        return Reading(BROKEN)
    if total <= 0:
        return Reading(BROKEN)
    reading = Reading(OK, total_pages=total)
    images = 0
    for index in range(total):
        if index >= max_pages:
            reading.cut = "pages"
            break
        if index and clock() - started > max_seconds:
            reading.cut = "time"
            break
        collector = _Page()
        try:
            page = reader.pages[index]
            page.extract_text(visitor_text=collector.visit)
            paragraphs = collector.paragraphs()
        except Exception:
            reading.bad_pages += 1
            continue
        if paragraphs:
            reading.pages.append((index + 1, paragraphs))
        elif _has_image(page):
            reading.scanned_pages += 1
            images += 1
    if not reading.pages:
        if reading.bad_pages and not images:
            return Reading(BROKEN, total_pages=total, bad_pages=reading.bad_pages, cut=reading.cut)
        return Reading(SCANNED if images else EMPTY, total_pages=total, scanned_pages=reading.scanned_pages,
                       bad_pages=reading.bad_pages, cut=reading.cut)
    return reading
