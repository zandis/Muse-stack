"""Words, for search and for the citation check. Standard library only.

One tokeniser serves both, and it needs no word segmentation:

- Latin-script text: lower-cased words (letters and digits), with a trailing
  plural "s" removed and a short list of English stop words dropped.
- Numbers with separators stay whole ("4.2", "1,200").
- Chinese, Japanese and Korean text has no spaces, so every run of such
  characters becomes its overlapping character pairs (bigrams): 營收成長 ->
  營收, 收成, 成長. A run of one character is kept as that character.

Text is NFKC-normalised first, so full-width digits and letters match their
plain forms.
"""

from __future__ import annotations

import re
import unicodedata

_CJK = "぀-ヿ㐀-䶿一-鿿豈-﫿가-힯"
_TOKEN = re.compile(rf"\d+(?:[.,]\d+)+|[{_CJK}]+|[^\W_{_CJK}]+")
_CJK_CHAR = re.compile(rf"[{_CJK}]")
_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

STOP = frozenset(
    "a an and are as at be been but by can could did do does for from had has have he her his how i if in into is it its "
    "may me my no not of on or our she should so than that the their them then there these they this those to was we were "
    "what when where which who whom why will with would you your".split()
)


def normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def tokens(text: str) -> list[str]:
    """Search terms of `text`, in order, with repeats."""
    out: list[str] = []
    for match in _TOKEN.finditer(normalise(text)):
        piece = match.group()
        if _CJK_CHAR.match(piece):
            if len(piece) == 1:
                out.append(piece)
            else:
                out.extend(piece[i:i + 2] for i in range(len(piece) - 1))
        elif piece[0].isdigit():
            out.append(piece.replace(",", "") if re.fullmatch(r"\d+(?:,\d{3})+(?:\.\d+)?", piece) else piece)
        else:
            if len(piece) > 3 and piece.endswith("s") and not piece.endswith("ss"):
                piece = piece[:-1]
            if piece not in STOP:
                out.append(piece)
    return out


def terms(text: str) -> set[str]:
    """The distinct terms of `text`: what the citation check compares."""
    return set(tokens(text))


def overlap(claim: str, passage: str) -> tuple[int, float]:
    """(shared terms, shared / claim terms). (0, 0.0) when the claim has no terms."""
    mine = terms(claim)
    if not mine:
        return 0, 0.0
    shared = len(mine & terms(passage))
    return shared, shared / len(mine)


def weight(text: str) -> int:
    """About how many model tokens `text` is: 4 ASCII characters or 1 other character each."""
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars // 4 + (len(text) - ascii_chars)


def mostly_chinese(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and sum(1 for ch in letters if _HAN.match(ch)) / len(letters) >= 0.3
