"""PDF reading in the file skills: pdf.py (layout, the one normalisation, limits, failure states), the index, the
citation `file › page n, paragraph k`, the quote check on PDF text, and what the person is told.

The PDFs are built here by scripts/pdf_fixtures.py (standard library only); nothing is downloaded. Tests that parse a
PDF need the optional pypdf (requirements/pdf.lock) and are skipped without it; the rest run everywhere, including
the plain `pip install -e ".[dev]"` of CI.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

import pdf_fixtures as pf
from wardmuse.skills import loader, runner
from wardmuse.skills.workspace import Workspace
from test_skills_support import chat, env, proxy  # noqa: F401  (fixtures)

try:
    import pypdf  # noqa: F401
    HAVE_PYPDF = True
except Exception:  # pragma: no cover - depends on the install
    HAVE_PYPDF = False

needs_pypdf = pytest.mark.skipif(not HAVE_PYPDF, reason="pypdf (the optional pdf extra) is not installed")
SPECS = pf.specs()


def module(name: str, skill: str = "files-answer"):
    return loader.import_skill(loader.find(skill), name)


def fixture(folder: str, name: str) -> bytes:
    return pf.build(SPECS[(folder, name)])


LEASE = ("harbour-pdf-en", "mooring-lease.pdf")
NEWS = ("harbour-pdf-en", "club-newsletter.pdf")
PRICES = ("harbour-pdf-en", "fuel-prices.pdf")
SCAN = ("harbour-pdf-en", "old-notice-scan.pdf")
ZH_LEASE = ("songyuan-pdf-zh", "活動中心租借合約.pdf")
ZH_MINUTES = ("songyuan-pdf-zh", "里民大會紀錄.pdf")
ZH_TABLE = ("songyuan-pdf-zh", "課程價目表.pdf")
LOCKED = ("songyuan-pdf-zh", "locked-minutes.pdf")


def answer_json(answer: str, claims: list[tuple], not_found: bool = False) -> str:
    items = [{"quote": c[0], "n": c[1], "says": c[2] if len(c) > 2 else c[0]} for c in claims]
    return json.dumps({"claims": items, "answer": answer, "not_found": not_found}, ensure_ascii=False)


# ================================================================ the one normalisation (no pypdf needed)


def test_tidy_joins_lines_undoes_hyphenation_and_cjk_spacing_and_nothing_else():
    tidy = module("pdf").tidy
    assert tidy(["The club re-", "turns it within 14 days."]) == "The club returns it within 14 days."
    assert tidy(["well-", "Known"]) == "well- Known"  # only lower-case on both sides is a hyphenated word
    assert tidy(["dates 2031-", "2033"]) == "dates 2031- 2033"  # never between digits
    assert tidy(["每月租借費為新臺幣", "8,400 元，應於每月", "10 日以前繳交。"]) == "每月租借費為新臺幣 8,400 元，應於每月 10 日以前繳交。"
    assert tidy(["租 借 期 間 自 2032 年", "7 月 1 日 起 。"]) == "租借期間自 2032 年 7 月 1 日起。"
    assert tidy(["the ﬁrst oﬃce", "and a soft­hyphen​ here"]) == "the first office and a softhyphen here"
    assert tidy(["full-width １２３ stays", "as it is"]) == "full-width １２３ stays as it is"
    assert tidy(["rent 1,", "450 dollars"]) == "rent 1, 450 dollars"  # two lines never merge into one number


@pytest.mark.parametrize("lines", [
    ["12-", "34"], ["1,", "450"], ["第 ３ 條", "第 4 條"], ["2 0 3 1 年", "4 月"], ["aﬁ12", "3­4"], ["4.", "2%"],
    ["電話 02-2345", "6789"], ["9", "", " ", "9"], ["x" * 3 + "-", "7y"],
])
def test_tidy_never_adds_drops_or_merges_a_digit(lines):
    tidy = module("pdf").tidy
    import unicodedata
    out = tidy(lines)
    digits = lambda text: [unicodedata.digit(ch) for ch in text if ch.isdigit()]  # noqa: E731
    assert digits(out) == digits("".join(lines))
    # each original line's digit runs stay whole and apart: no run in the output spans two input lines
    runs = re.findall(r"\d+", unicodedata.normalize("NFKC", out))
    expected = [run for line in lines for run in re.findall(r"\d+", unicodedata.normalize("NFKC", module("pdf")._line(line)))]
    assert runs == expected


def test_place_labels_name_the_page_and_the_paragraph_on_it():
    index = module("index")
    places = {1: (1, 1), 2: (1, 2), 3: (2, 1), 4: (2, 2), 5: (2, 3), 6: (3, 1)}
    assert index.place_label([2], places, zh=False) == "page 1, paragraph 2"
    assert index.place_label([3, 5], places, zh=False) == "page 2, paragraphs 1, 3"
    assert index.place_label([3, 4, 5], places, zh=False) == "page 2, paragraphs 1-3"
    assert index.place_label([2, 3], places, zh=False) == "pages 1, 2"
    assert index.place_label([1, 3, 6], places, zh=False) == "pages 1-3"
    assert index.place_label([4], places, zh=True) == "第 2 頁第 2 段"
    assert index.place_label([2, 6], places, zh=True) == "第 1、3 頁"
    assert index.place_label([7], places, zh=False) is None and index.place_label([1], {}, zh=False) is None


def test_the_fixture_pdfs_are_built_byte_for_byte_the_same():
    for (folder, name), spec in SPECS.items():
        assert pf.build(spec) == pf.build(spec), name
        assert (pf.FIXTURES / folder / name).read_bytes() == pf.build(spec), f"{name}: run python scripts/pdf_fixtures.py --write"
        text, problem = pf.source_text(pf.FIXTURES / folder / name)
        assert problem == "" and (bool(text) or name == "old-notice-scan.pdf")  # the scan has no text, by design


def test_without_pypdf_pdfs_are_skipped_and_said(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)  # `import pypdf` now fails, as on an install without the extra
    pdf, index = module("pdf"), module("index")
    assert pdf.available() is False and index.pdf_reader() is None
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "lease.pdf").write_bytes(fixture(*LEASE))
    (docs / "notes.txt").write_text("A note.\n", encoding="utf-8")
    db = index.open_db(tmp_path)
    stats = index.sync(db, Workspace([docs]), None, index.pdf_reader())
    db.close()
    assert (stats.pdf_skipped, stats.seen) == (1, 1)
    assert index.pdf_notes(stats) == ["PDF reading is not installed; 1 PDF(s) were skipped: lease.pdf."]
    assert "names" not in stats.counts() and stats.counts()["pdf_skipped"] == 1


# ================================================================ reading PDFs (pypdf)


def read(key, **kw):
    return module("pdf").read(fixture(*key), **kw)


@needs_pypdf
def test_english_pages_paragraphs_and_hyphenation():
    reading = read(LEASE)
    assert (reading.state, reading.total_pages, reading.cut) == ("ok", 3, "")
    paragraphs = {(page, k): text for page, k, text in reading.paragraphs()}
    assert paragraphs[(2, 2)].startswith("The monthly mooring fee is 380 dollars")
    assert "The club returns it within 14 days" in paragraphs[(2, 3)]  # "re-" + "turns" across a line end
    assert paragraphs[(3, 2)].startswith("The lease starts on 1 May 2032 and ends on 30 April 2034.")
    assert len(paragraphs) == 11


@needs_pypdf
def test_traditional_chinese_through_a_cid_font_and_its_tounicode_map():
    reading = read(ZH_LEASE)
    texts = [text for _, _, text in reading.paragraphs()]
    assert "每月租借費為新臺幣 8,400 元，應於每月 10 日以前繳交。逾期繳交者，每次加收 300 元。" in texts
    assert "承租方應繳交押金新臺幣 16,800 元，相當於兩個月的租借費，於合約結束後 15 日內無息退還。" in texts
    assert [(p, k) for p, k, t in reading.paragraphs() if t.startswith("租借期間")] == [(3, 2)]


@needs_pypdf
def test_two_columns_are_read_column_by_column():
    for key, first, second in ((NEWS, "Summer regatta.", "New harbour master."), (ZH_MINUTES, "會議時間", "第二案")):
        texts = [text for _, _, text in read(key).paragraphs()]
        a = next(i for i, t in enumerate(texts) if t.startswith(first))
        b = next(i for i, t in enumerate(texts) if t.startswith(second))
        assert a < b and texts[a].endswith(("16:00.", "86 人。")), texts
    news = [text for _, _, text in read(NEWS).paragraphs()]
    assert "Summer regatta. The summer regatta is on 14 June 2032. The first start is at 09:30 from the line off the harbour wall, and the last boat must finish by 16:00." in news


@needs_pypdf
def test_a_table_row_is_one_paragraph_with_its_cells_apart():
    en = [text for _, _, text in read(PRICES).paragraphs()]
    zh = [text for _, _, text in read(ZH_TABLE).paragraphs()]
    assert "Diesel per litre 1.84" in en and "Petrol (unleaded) per litre 2.07" in en
    assert "書法班 週一 19:00 3,200" in zh and "課程 上課時間 每期學費（元）" in zh  # CJK cells keep their separating space


@needs_pypdf
def test_scanned_locked_open_encrypted_broken_and_truncated():
    pdf = module("pdf")
    scan = read(SCAN)
    assert (scan.state, scan.scanned_pages, scan.pages) == ("scanned", 1, [])
    assert read(LOCKED).state == "locked"
    owner_only = pf.Spec([pf.Page([("p", "Readable with no password, though encrypted.")])], user_password="")
    opened = pdf.read(pf.build(owner_only))
    assert opened.state == "ok" and opened.paragraphs()[0][2] == "Readable with no password, though encrypted."
    assert pdf.read(b"this is not a PDF at all").state == "broken"
    assert pdf.read(b"").state == "broken"
    assert pdf.read(pf.truncated(fixture(*LEASE), 0.3)).state in ("broken", "ok")  # never a crash
    cut = pdf.read(pf.truncated(fixture(*LEASE), 0.05))
    assert cut.state == "broken"
    mixed = pf.Spec([pf.Page([("p", "Page one has text.")]), pf.Page([("image",)])])
    both = pdf.read(pf.build(mixed))
    assert (both.state, both.scanned_pages, [p for p, _ in both.pages]) == ("ok", 1, [1])
    empty = pdf.read(pf.build(pf.Spec([pf.Page([])])))
    assert empty.state == "empty"


@needs_pypdf
def test_a_stream_that_inflates_past_the_limit_is_refused_not_read():
    pdf = module("pdf")
    bomb = pf.build(pf.Spec([pf.Page([("p", "A page with a hidden second content stream.")])]), bomb_bytes=pdf.MAX_INFLATED * 3)
    assert len(bomb) < 2_000_000  # small on disk: under the skill's byte cap, so only the inflate limit stops it
    reading = pdf.read(bomb)
    assert reading.state == "broken" and reading.bad_pages == 1


@needs_pypdf
def test_page_and_time_caps_cut_the_reading_and_say_so():
    pdf = module("pdf")
    first = read(LEASE, max_pages=1)
    assert (first.cut, [p for p, _ in first.pages], first.total_pages) == ("pages", [1], 3)
    ticks = iter(range(0, 1000, 10))  # a fake clock: 10 seconds pass at each look
    slow = pdf.read(fixture(*LEASE), max_seconds=15, clock=lambda: next(ticks))
    assert slow.cut == "time" and [p for p, _ in slow.pages] == [1, 2]


# ================================================================ the index, labels, quotes


def folder_with_pdfs(tmp_path: Path) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    for key, name in ((LEASE, "lease.pdf"), (ZH_LEASE, "合約.pdf"), (SCAN, "scan.pdf"), (LOCKED, "locked.pdf")):
        (docs / name).write_bytes(fixture(*key))
    (docs / "broken.pdf").write_bytes(b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog")
    (docs / "notes.md").write_text("# Notes\n\nThe boat is called Petrel.\n", encoding="utf-8")
    return docs


@needs_pypdf
def test_the_index_keeps_pages_and_remembers_why_a_pdf_was_not_read(tmp_path, monkeypatch):
    index = module("index")
    docs = folder_with_pdfs(tmp_path)
    reads: list[str] = []
    original = Workspace.read_bytes
    monkeypatch.setattr(Workspace, "read_bytes", lambda self, path, limit=0: (reads.append(Path(path).name), original(self, path, limit))[1])
    db = index.open_db(tmp_path)
    first = index.sync(db, Workspace([docs]), None, index.pdf_reader())
    counts = first.counts()
    assert (counts["pdf_read"], counts["pdf_scanned"], counts["pdf_locked"], counts["pdf_broken"]) == (2, 1, 1, 1)
    assert first.names == {"pdf_scanned": ["scan.pdf"], "pdf_locked": ["locked.pdf"], "pdf_broken": ["broken.pdf"]}
    chunks = index.load_chunks(db, first.file_ids)
    lease = [c for c in chunks if c.path.endswith("lease.pdf")]
    assert lease and all(len(c.places) == len(c.paras) for c in lease)
    places = {n: p for c in lease for n, p in zip(c.paras, c.places)}
    fee = next(n for c in lease for n, part in zip(c.paras, c.text.split("\n\n")) if "mooring fee is 380" in part)
    assert places[fee] == (2, 2) and index.place_label([fee], places, zh=False) == "page 2, paragraph 2"
    assert all(c.places == () for c in chunks if c.path.endswith(".md"))
    reads.clear()
    again = index.sync(db, Workspace([docs]), None, index.pdf_reader())
    assert reads == [] and again.counts() == {**counts, "read": 0, "reused": 6}  # remembered, said again, not re-read
    assert again.names == first.names
    db.close()
    notes = index.pdf_notes(again)
    assert notes == ["1 PDF(s) scanned (no text layer), not read: scan.pdf.",
                     "1 PDF(s) locked (a password is needed), not read: locked.pdf.",
                     "1 PDF(s) could not be read (damaged or not really a PDF): broken.pdf."]


@needs_pypdf
def test_a_large_pdf_and_the_page_cap_are_counted_and_named(tmp_path):
    index = module("index")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "lease.pdf").write_bytes(fixture(*LEASE))
    caps = index.Caps(pdf_bytes=len(fixture(*LEASE)) + 1, pdf_pages=2)
    (docs / "big.pdf").write_bytes(fixture(*NEWS) + b" " * caps.pdf_bytes)  # trailing white space: still a PDF, now too big
    db = index.open_db(tmp_path)
    stats = index.sync(db, Workspace([docs]), caps, index.pdf_reader())
    db.close()
    assert (stats.pdf_large, stats.pdf_cut_pages, stats.pdf_read) == (1, 1, 1)
    assert stats.large == 0  # the text-file note ("over 2 MB") is not used for a PDF
    assert index.pdf_notes(stats) == ["1 PDF(s) too large to read (over 20 MB each): big.pdf.",
                                      "1 PDF(s) were read only in part: the limit on pages per PDF was reached: lease.pdf."]


def test_an_index_made_before_pdfs_were_read_gains_the_new_columns(tmp_path):
    db = sqlite3.connect(tmp_path / "index.sqlite")
    db.executescript("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL, "
                     "mtime_ns INTEGER NOT NULL, state TEXT NOT NULL, chunks INTEGER NOT NULL DEFAULT 0);"
                     "CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, seq INTEGER NOT NULL, "
                     "para_start INTEGER NOT NULL, para_end INTEGER NOT NULL, text TEXT NOT NULL, paras TEXT NOT NULL, "
                     "emb BLOB, emb_model TEXT);")
    db.close()
    index = module("index")
    db = index.open_db(tmp_path)
    assert "info" in {r[1] for r in db.execute("PRAGMA table_info(files)")}
    assert "places" in {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
    db.close()


@needs_pypdf
def test_quotes_from_pdf_text_are_found_and_a_changed_digit_is_not(tmp_path):
    quotes = module("quotes")
    texts = {k: t for _, k, t in [(p, (p, k), t) for p, k, t in read(LEASE).paragraphs()]}
    deposit = texts[(2, 3)]
    assert quotes.find("The club returns it within 14 days", deposit)  # across the hyphenated line end
    assert quotes.find("The club re-turns it within １４ days", deposit)  # a hyphen kept by the model, full-width digits
    assert quotes.find("The club returns it within 41 days", deposit) is None
    zh = {k: t for _, k, t in [(p, (p, k), t) for p, k, t in read(ZH_LEASE).paragraphs()]}
    fee = zh[(2, 2)]
    assert quotes.find("每月租借費為新臺幣 8,400 元", fee) and quotes.find("每 月 租 借 費 為 新 臺 幣 ８,４００ 元", fee)
    assert quotes.find("每月租借費為新臺幣 8400 元", fee) is None  # a separator inside a number is part of its value
    assert quotes.find("每月租借費為新臺幣 8,401 元", fee) is None


# ================================================================ the skill, end to end (in process)


@needs_pypdf
def test_files_answer_cites_a_pdf_by_page_and_says_what_it_could_not_read(env, proxy, tmp_path):  # noqa: F811
    docs = folder_with_pdfs(tmp_path)
    proxy.push(*chat(answer_json("The monthly mooring fee is 380 dollars [1].",
                                 [("The monthly mooring fee is 380 dollars", 1, "The monthly mooring fee is 380 dollars")]),
                     task="everyday"))
    result = runner.run("files-answer", {"question": "What is the monthly mooring fee?", "embeddings": "off", "top_k": 1},
                        folders=[docs], environ=env, privacy="standard")
    assert result.status == "partial" and result.check["status"] == "passed", result.check
    claim = result.output["claims"][0]
    assert claim["source"] == "lease.pdf › page 2, paragraph 2"
    citation = result.output["citations"][0]
    assert citation["label"] == "lease.pdf › page 2, paragraph 2" and citation["pages"] == [2] and citation["page_paragraphs"] == [2]
    assert "[1] lease.pdf › page 2, paragraph 2: “The monthly mooring fee is 380 dollars”" in result.output["text"]
    assert result.notes == ["1 PDF(s) scanned (no text layer), not read: scan.pdf.",
                            "1 PDF(s) locked (a password is needed), not read: locked.pdf.",
                            "1 PDF(s) could not be read (damaged or not really a PDF): broken.pdf."]
    index_event = next(e for e in result.events if e["kind"] == "skill.index")
    assert (index_event["pdf_read"], index_event["pdf_scanned"], index_event["pdf_locked"], index_event["pdf_broken"]) == (2, 1, 1, 1)
    assert "scan.pdf" not in json.dumps(result.events)  # names go to the person in notes, never into events


@needs_pypdf
def test_files_answer_in_chinese_cites_the_page_in_chinese(env, proxy, tmp_path):  # noqa: F811
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "合約.pdf").write_bytes(fixture(*ZH_LEASE))
    proxy.push(*chat(answer_json("押金是新臺幣 16,800 元 [1]。", [("承租方應繳交押金新臺幣 16,800 元", 1, "押金是新臺幣 16,800 元")]),
                     task="everyday"))
    result = runner.run("files-answer", {"question": "押金是多少？", "embeddings": "off", "top_k": 1},
                        folders=[docs], environ=env, privacy="standard")
    assert result.status == "done" and result.check["status"] == "passed", result.check
    assert result.output["claims"][0]["source"] == "合約.pdf › 第 2 頁第 3 段"


@needs_pypdf
def test_research_report_reads_the_same_pdfs(tmp_path):
    index = module("index", "research-report")
    db = index.open_db(tmp_path)
    stats = index.sync(db, Workspace([folder_with_pdfs(tmp_path)]), None, index.pdf_reader())
    db.close()
    assert (stats.pdf_read, stats.pdf_scanned, stats.pdf_locked, stats.pdf_broken) == (2, 1, 1, 1)
