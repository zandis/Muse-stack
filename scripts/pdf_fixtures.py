"""Small PDFs written by hand: the PDF fixtures of the chores check and the test suite. Standard library only.

Nothing here downloads anything or needs a PDF library. Every PDF is built from text in this file, byte for byte
the same on every machine (no dates, a fixed file identifier), so the fixture guard can check a committed PDF
without parsing it: it rebuilds the file from its source here and compares the bytes, then checks the expected
facts against the source text (`source_text`).

What the writer can make:

- text in Helvetica (a standard font, WinAnsi encoding) for ASCII, and in a Type0/CID font with Identity-H
  encoding and a ToUnicode CMap for everything else (Traditional Chinese, accented letters). The CID font is
  not embedded: text extraction needs only the ToUnicode map;
- flowing paragraphs wrapped to the page, explicit line breaks ("\\n") and a word hyphenated at a line end
  ("ad-\\njustment");
- a page in two columns, a table-like page (cells placed at fixed x positions), a page that is only an image
  (a "scanned" page with no text layer);
- the Standard security handler, revision 2 (RC4, 40-bit), with or without a user password;
- for tests: a truncated file, a file that is not a PDF, and a page whose content stream inflates to many
  megabytes from a few hundred kilobytes (a "zip bomb").

    python scripts/pdf_fixtures.py --check    # the committed fixture PDFs equal what this file builds
    python scripts/pdf_fixtures.py --write    # (re)write them
"""

from __future__ import annotations

import hashlib
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "evals" / "chores" / "fixtures"

PAGE_W, PAGE_H = 595, 842  # A4 in points
MARGIN = 72
SIZE = 11  # font size
LEADING = 15  # line to line
PARA_GAP = 11  # extra space between paragraphs
FILE_ID = hashlib.md5(b"muse pdf fixture").digest()  # fixed: the same bytes on every machine
_PAD = bytes.fromhex("28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")


def _is_ascii(ch: str) -> bool:
    return ord(ch) < 128


def _width(text: str, size: float = SIZE) -> float:
    """A rough width, for wrapping only: ASCII about half an em, anything else a full em."""
    return sum(0.52 * size if _is_ascii(ch) else size for ch in text)


def _wrap(text: str, width: float) -> list[str]:
    """Lines of at most `width` points. "\\n" in `text` is a forced break. Words wrap at spaces; CJK at any char."""
    lines: list[str] = []
    for forced in text.split("\n"):
        line = ""
        for token in _tokens(forced):
            if line and _width(line + token) > width:
                lines.append(line.rstrip())
                line = token.lstrip()
            else:
                line += token
        lines.append(line.rstrip())
    return lines


def _tokens(text: str) -> Iterable[str]:
    """Words with the space before them; each CJK character on its own."""
    word = ""
    for ch in text:
        if not _is_ascii(ch) and not ch.isspace():
            if word:
                yield word
                word = ""
            yield ch
        elif ch == " ":
            if word.strip():
                yield word
                word = ""
            word += ch
        else:
            word += ch
    if word:
        yield word


def logical(text: str) -> str:
    """The words of a paragraph as the author wrote them: a hyphen at a forced break between letters is dropped,
    a forced break is a space (none between two CJK characters)."""
    out = ""
    for i, piece in enumerate(text.split("\n")):
        if i == 0:
            out = piece
        elif out.endswith("-") and out[-2:-1].isalpha() and piece[:1].islower():
            out = out[:-1] + piece
        elif out and piece and not _is_ascii(out[-1]) and not _is_ascii(piece[0]):
            out += piece
        else:
            out += " " + piece
    return out


# ---------------------------------------------------------------- the document model


@dataclass
class Page:
    """Blocks, top to bottom: ("p", text) | ("cols", [texts], [texts]) | ("table", [[cell, ...], ...]) | ("image",)."""
    blocks: list[tuple[Any, ...]] = field(default_factory=list)

    def text(self) -> str:
        parts: list[str] = []
        for block in self.blocks:
            if block[0] == "p":
                parts.append(logical(block[1]))
            elif block[0] == "cols":
                parts += [logical(t) for t in block[1]] + [logical(t) for t in block[2]]
            elif block[0] == "table":
                parts += ["  ".join(row) for row in block[1]]
        return "\n\n".join(parts)


@dataclass
class Spec:
    pages: list[Page]
    user_password: str | None = None  # set: encrypted (RC4 40-bit) with this user password ("" opens freely)
    owner_password: str = "owner"

    def text(self) -> str:
        return "\n\n".join(p.text() for p in self.pages)


class _Fonts:
    """Helvetica for ASCII; one CID font (Identity-H, ToUnicode) for every other character used in the document."""

    def __init__(self, spec: Spec) -> None:
        chars: set[str] = set()
        for page in spec.pages:
            for block in page.blocks:
                for text in _block_texts(block):
                    chars.update(ch for ch in text if not _is_ascii(ch) and ch != "\n")
        if any(ord(ch) > 0xFFFF for ch in chars):
            raise ValueError("characters outside the Basic Multilingual Plane are not supported")
        self.cids = {ch: n for n, ch in enumerate(sorted(chars), start=1)}

    def show(self, text: str) -> bytes:
        """Content-stream operators that show `text`, switching fonts between runs."""
        out: list[bytes] = []
        run, ascii_run = "", None
        for ch in text + "\0":
            is_ascii = ch == "\0" or _is_ascii(ch)
            if ascii_run is None:
                ascii_run = is_ascii
            if ch == "\0" or is_ascii != ascii_run:
                if run:
                    if ascii_run:
                        escaped = run.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                        out.append(b"/F1 %d Tf (" % SIZE + escaped.encode("latin-1") + b") Tj")
                    else:
                        out.append(b"/F2 %d Tf <" % SIZE + "".join(f"{self.cids[c]:04X}" for c in run).encode() + b"> Tj")
                run, ascii_run = "", is_ascii
            if ch != "\0":
                run += ch
        return b" ".join(out)

    def to_unicode(self) -> bytes:
        items = sorted(self.cids.items(), key=lambda item: item[1])
        body = [b"/CIDInit /ProcSet findresource begin", b"12 dict begin", b"begincmap",
                b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def",
                b"/CMapName /Adobe-Identity-UCS def", b"/CMapType 2 def",
                b"1 begincodespacerange", b"<0000> <FFFF>", b"endcodespacerange"]
        for start in range(0, len(items), 100):
            group = items[start:start + 100]
            body.append(b"%d beginbfchar" % len(group))
            body += [f"<{cid:04X}> <{ord(ch):04X}>".encode() for ch, cid in group]
            body.append(b"endbfchar")
        body += [b"endcmap", b"CMapName currentdict /CMap defineresource pop", b"end", b"end"]
        return b"\n".join(body)


def _block_texts(block: tuple[Any, ...]) -> list[str]:
    if block[0] == "p":
        return [block[1]]
    if block[0] == "cols":
        return list(block[1]) + list(block[2])
    if block[0] == "table":
        return [cell for row in block[1] for cell in row]
    return []


def _content(page: Page, fonts: _Fonts) -> bytes:
    ops: list[bytes] = []

    def line(x: float, y: float, text: str) -> None:
        if text:
            ops.append(b"BT 1 0 0 1 %.2f %.2f Tm " % (x, y) + fonts.show(text) + b" ET")

    def flow(texts: Sequence[str], x: float, y: float, width: float) -> float:
        for number, text in enumerate(texts):
            if number:
                y -= PARA_GAP
            for piece in _wrap(text, width):
                line(x, y, piece)
                y -= LEADING
        return y

    y = PAGE_H - MARGIN
    for block in page.blocks:
        if block[0] == "p":
            y = flow([block[1]], MARGIN, y, PAGE_W - 2 * MARGIN) - PARA_GAP
        elif block[0] == "cols":
            half = (PAGE_W - 2 * MARGIN - 30) / 2
            left = flow(block[1], MARGIN, y, half)
            right = flow(block[2], MARGIN + half + 30, y, half)
            y = min(left, right) - PARA_GAP
        elif block[0] == "table":
            rows = block[1]
            columns = max(len(r) for r in rows)
            widths = [max(_width(r[c]) for r in rows if c < len(r)) + 24 for c in range(columns)]
            for row in rows:
                x = MARGIN
                for c, cell in enumerate(row):
                    line(x, y, cell)
                    x += widths[c]
                y -= LEADING + 4
            y -= PARA_GAP
        elif block[0] == "image":
            ops.append(b"q 451 0 0 600 72 120 cm /Im1 Do Q")
    return b"\n".join(ops)


def _image() -> bytes:
    """A 32 x 32 grey "scan": a pattern, no text."""
    return bytes((x * 7 + y * 3) % 256 for y in range(32) for x in range(32))


# ---------------------------------------------------------------- writing


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) % 256
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) % 256])
    return bytes(out)


def _padded(password: str) -> bytes:
    return (password.encode("latin-1") + _PAD)[:32]


class _Security:
    """Standard security handler, revision 2: RC4 with a 40-bit key (PDF 1.7, 7.6.3, algorithms 2, 3 and 4)."""

    P = -44  # print and copy refused: a typical "protected" file

    def __init__(self, user: str, owner: str) -> None:
        owner_key = hashlib.md5(_padded(owner or user)).digest()[:5]
        self.o = _rc4(owner_key, _padded(user))
        digest = hashlib.md5(_padded(user) + self.o + (self.P & 0xFFFFFFFF).to_bytes(4, "little") + FILE_ID).digest()
        self.key = digest[:5]
        self.u = _rc4(self.key, _PAD)

    def encrypt(self, number: int, data: bytes) -> bytes:
        key = hashlib.md5(self.key + number.to_bytes(3, "little") + b"\0\0").digest()[:10]
        return _rc4(key, data)

    def dictionary(self) -> bytes:
        return (b"<< /Filter /Standard /V 1 /R 2 /Length 40 /P %d /O <" % self.P + self.o.hex().encode()
                + b"> /U <" + self.u.hex().encode() + b"> >>")


class _Writer:
    def __init__(self, security: _Security | None = None) -> None:
        self.objects: list[bytes] = []
        self.security = security

    def reserve(self) -> int:
        self.objects.append(b"")
        return len(self.objects)

    def put(self, number: int, body: bytes) -> int:
        self.objects[number - 1] = body
        return number

    def add(self, body: bytes) -> int:
        return self.put(self.reserve(), body)

    def stream(self, data: bytes, extra: bytes = b"", compress: bool = True, number: int | None = None) -> int:
        number = number or self.reserve()
        if compress:
            data, extra = zlib.compress(data, 9), extra + b" /Filter /FlateDecode"
        if self.security is not None:
            data = self.security.encrypt(number, data)
        return self.put(number, b"<< /Length %d%s >>\nstream\n" % (len(data), extra) + data + b"\nendstream")

    def raw_stream(self, compressed: bytes, extra: bytes = b"") -> int:
        """A stream whose bytes are given already compressed (the zip bomb)."""
        return self.add(b"<< /Length %d /Filter /FlateDecode%s >>\nstream\n" % (len(compressed), extra) + compressed + b"\nendstream")

    def bytes(self, root: int) -> bytes:
        trailer = b""
        if self.security is not None:  # the encryption dictionary itself is never encrypted
            trailer = b" /Encrypt %d 0 R" % self.add(self.security.dictionary())
        out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(self.objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(self.objects) + 1)
        out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        out += (b"trailer\n<< /Size %d /Root %d 0 R /ID [<%s> <%s>]%s >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(self.objects) + 1, root, FILE_ID.hex().encode(), FILE_ID.hex().encode(), trailer, xref))
        return bytes(out)


def build(spec: Spec, bomb_bytes: int = 0) -> bytes:
    """The PDF for `spec`. `bomb_bytes` > 0 adds, to the first page, a content stream inflating to that many bytes."""
    security = _Security(spec.user_password, spec.owner_password) if spec.user_password is not None else None
    fonts = _Fonts(spec)
    if security is not None and fonts.cids:
        raise ValueError("encrypted fixtures use ASCII text only (strings in the CID font objects are not encrypted)")
    w = _Writer(security)
    catalog, pages_id = w.reserve(), w.reserve()
    font_refs = [b"/F1 %d 0 R" % w.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")]
    if fonts.cids:
        descriptor = w.add(b"<< /Type /FontDescriptor /FontName /MingLiU /Flags 32 /FontBBox [0 -200 1000 900] /ItalicAngle 0 "
                           b"/Ascent 880 /Descent -120 /CapHeight 700 /StemV 80 >>")
        cid = w.add(b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /MingLiU /CIDSystemInfo << /Registry (Adobe) "
                    b"/Ordering (Identity) /Supplement 0 >> /FontDescriptor %d 0 R /DW 1000 /CIDToGIDMap /Identity >>" % descriptor)
        to_unicode = w.stream(fonts.to_unicode())
        font_refs.append(b"/F2 %d 0 R" % w.add(b"<< /Type /Font /Subtype /Type0 /BaseFont /MingLiU /Encoding /Identity-H "
                                                 b"/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>" % (cid, to_unicode)))
    image = None
    if any(block[0] == "image" for page in spec.pages for block in page.blocks):
        image = w.stream(_image(), b" /Type /XObject /Subtype /Image /Width 32 /Height 32 /ColorSpace /DeviceGray /BitsPerComponent 8")
    kids = []
    for number, page in enumerate(spec.pages):
        contents = [w.stream(_content(page, fonts))]
        if bomb_bytes and number == 0:
            contents.append(w.raw_stream(_bomb(bomb_bytes)))
        resources = b"<< /Font << " + b" ".join(font_refs) + b" >>"
        if image is not None:
            resources += b" /XObject << /Im1 %d 0 R >>" % image
        resources += b" >>"
        kids.append(w.add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources %s /Contents [%s] >>"
                          % (pages_id, PAGE_W, PAGE_H, resources, b" ".join(b"%d 0 R" % c for c in contents))))
    w.put(pages_id, b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % k for k in kids), len(kids)))
    w.put(catalog, b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    return w.bytes(catalog)


def _bomb(size: int) -> bytes:
    """A deflate stream of `size` spaces, compressed in pieces so building it needs little memory."""
    packer = zlib.compressobj(9)
    piece = b" " * (1 << 20)
    out = bytearray()
    left = size
    while left > 0:
        out += packer.compress(piece[:min(left, len(piece))])
        left -= len(piece)
    return bytes(out + packer.flush())


def truncated(data: bytes, keep: float = 0.5) -> bytes:
    """The first part of a PDF, cut inside its objects: no cross-reference table, no trailer."""
    return data[:max(16, int(len(data) * keep))]


# ---------------------------------------------------------------- the chores-check fixtures

_EN_NOTE = "This is a fictional document written for testing. No person, place or company in it is real."
_ZH_NOTE = "這是為了測試而寫的虛構文件，裡面的人名、地址與公司都不是真的。"

FIXTURE_SPECS: dict[str, dict[str, Spec]] = {
    "harbour-pdf-en": {
        "mooring-lease.pdf": Spec([
            Page([("p", "Mooring lease: Berth C-12, Saltmarrow Boat Club"), ("p", _EN_NOTE),
                  ("p", "The club is the Saltmarrow Boat Club at 3 Wharf Row, Port Ennick. The member is Dorian Halvaros. "
                        "The berth is C-12 on the east pontoon, for one sailing boat of at most 9.5 metres."),
                  ("p", "This lease covers the berth only. Winter storage on land is a separate agreement and is not "
                        "part of this lease.")]),
            Page([("p", "Fees"),
                  ("p", "The monthly mooring fee is 380 dollars, paid on the 1st day of each month. A fee paid more than "
                        "10 days late carries a charge of 25 dollars."),
                  ("p", "The member pays a deposit of 760 dollars, which equals two months of mooring fees. The club re-\n"
                        "turns it within 14 days after the boat leaves the berth."),
                  ("p", "Electricity on the pontoon is metered and billed every quarter at the club's posted rate.")]),
            Page([("p", "Term and notice"),
                  ("p", "The lease starts on 1 May 2032 and ends on 30 April 2034. Either side must give 45 days of "
                        "written notice before the end date if they do not want to renew."),
                  ("p", "The club may move the boat to another berth of the same size for dredging, with 7 days of notice.")]),
        ]),
        "club-newsletter.pdf": Spec([
            Page([("p", "Saltmarrow Boat Club newsletter, spring issue"), ("p", _EN_NOTE),
                  ("cols",
                   ["Summer regatta. The summer regatta is on 14 June 2032. The first start is at 09:30 from the line "
                    "off the harbour wall, and the last boat must finish by 16:00.",
                    "Entries close on 7 June 2032. The entry fee is 35 dollars a boat."],
                   ["New harbour master. Annika Brask-Olu takes over as harbour master on 1 April 2032. Her office "
                    "is in the green hut at the end of the north pontoon.",
                    "The fuel dock now opens at 07:00 on weekdays."])]),
        ]),
        "fuel-prices.pdf": Spec([
            Page([("p", "Saltmarrow fuel dock: prices from 1 March 2032"), ("p", _EN_NOTE),
                  ("table", [["Item", "Unit", "Price (dollars)"],
                             ["Diesel", "per litre", "1.84"],
                             ["Petrol (unleaded)", "per litre", "2.07"],
                             ["Two-stroke oil", "per litre", "11.50"],
                             ["Fresh water", "per 100 litres", "3.20"],
                             ["Pump-out", "per visit", "18.00"]]),
                  ("p", "Members get 5 percent off diesel and petrol when they show their club card.")]),
        ]),
        "old-notice-scan.pdf": Spec([Page([("image",)])]),
    },
    "songyuan-pdf-zh": {
        "活動中心租借合約.pdf": Spec([
            Page([("p", "松園社區活動中心租借合約"), ("p", _ZH_NOTE),
                  ("p", "出租方為松園社區發展協會，承租方為晨星書法社。租借場地為松園社區活動中心二樓多功能教室，"
                        "可容納 40 人。"),
                  ("p", "教室內的桌椅、白板與投影機可以使用，音響設備另外申請。")]),
            Page([("p", "費用"),
                  ("p", "每月租借費為新臺幣 8,400 元，應於每月 10 日以前繳交。逾期繳交者，每次加收 300 元。"),
                  ("p", "承租方應繳交押金新臺幣 16,800 元，相當於兩個月的租借費，於合約結束後 15 日內無息退還。"),
                  ("p", "水電費已包含在租借費內，冷氣每小時另收 50 元。")]),
            Page([("p", "期間與終止"),
                  ("p", "租借期間自 2032 年 7 月 1 日起，至 2033 年 6 月 30 日止。任何一方要提前終止，"
                        "必須在 30 天前以書面通知。"),
                  ("p", "颱風或停電而無法使用場地時，當次不收費。")]),
        ]),
        "里民大會紀錄.pdf": Spec([
            Page([("p", "松園里第三次里民大會紀錄"), ("p", _ZH_NOTE),
                  ("cols",
                   ["會議時間：2032 年 3 月 8 日晚上 7 點，地點在松園國小禮堂，出席里民 86 人。",
                    "第一案：巷道路燈老舊。決議全部更換為 LED 燈，共 42 盞，由里辦公處向區公所申請經費。"],
                   ["第二案：資源回收時間。決議自 4 月起改為每週二與週五晚上 8 點收運。",
                    "第三案：公園兒童遊戲區的地墊破損，請廠商於 5 月底前修復。"])]),
        ]),
        "課程價目表.pdf": Spec([
            Page([("p", "松園社區大學 2032 年秋季課程價目表"), ("p", _ZH_NOTE),
                  ("table", [["課程", "上課時間", "每期學費（元）"],
                             ["書法班", "週一 19:00", "3,200"],
                             ["太極拳", "週三 07:00", "2,400"],
                             ["英語會話", "週四 19:30", "4,500"],
                             ["手機攝影", "週六 14:00", "2,800"]]),
                  ("p", "每期 12 週。65 歲以上長者學費打八折。")]),
        ]),
        "locked-minutes.pdf": Spec([Page([("p", "Board minutes. This file is protected with a password.")])],
                                   user_password="pine-2032"),
    },
}


def specs() -> dict[tuple[str, str], Spec]:
    return {(folder, name): spec for folder, files in FIXTURE_SPECS.items() for name, spec in files.items()}


def source_text(path: Path) -> tuple[str | None, str]:
    """(the source text of a fixture PDF, problem). The problem is "" when the committed bytes are what this file builds."""
    spec = specs().get((path.parent.name, path.name))
    if spec is None:
        return None, f"{path.name} has no source in scripts/pdf_fixtures.py"
    if not path.is_file() or path.read_bytes() != build(spec):
        return spec.text(), f"{path.name} differs from its source in scripts/pdf_fixtures.py (run it with --write)"
    return spec.text(), ""


def main(argv: list[str]) -> int:
    write = "--write" in argv
    bad = 0
    for (folder, name), spec in specs().items():
        path = FIXTURES / folder / name
        data = build(spec)
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"wrote {path.relative_to(ROOT)} ({len(data)} bytes)")
        elif not path.is_file() or path.read_bytes() != data:
            print(f"differs: {path.relative_to(ROOT)}")
            bad += 1
    if not write:
        print("ok" if not bad else f"{bad} fixture PDF(s) differ; run with --write")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
