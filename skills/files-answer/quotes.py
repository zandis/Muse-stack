"""Verbatim quotes: the citation rule of both file skills. Standard library only.

A claim is {"quote": words copied from passage n, "n": the passage number, "says": the fact in the answer's language}.
It stands when two things hold, and neither depends on the language of `says`:

1. The quote really is in passage n (`find`). Both sides are folded first: NFKC (full-width digits and letters
   become plain ones), case-folded, runs of white space made one space, curly quotes and the many dashes made
   plain. If the folded quote is not found as it is, white space and punctuation are ignored on both sides and
   it is looked for again. Letters and digits are never ignored, and the separators that give a number its
   value are kept: `.`, `,`, `:`, `/`, `-` or a space standing between two digits ("4.2" is not "42", "30 6" is
   not "306"), `%` and every symbol such as `$`. So a quote whose digits differ from the passage is refused.
   A quote must hold at least MIN_CJK Chinese/Japanese/Korean characters or MIN_WORDS words, and at most
   MAX_CHARS characters: a bounded excerpt, not the passage again.

2. Every number in `says` is in the quote (`unsupported_numbers`): digits compared by value ("1,450" = "1450" =
   "1450.00", "08" = "8"). The quote also vouches for what it spells out: English number words and month names
   ("two", "June" -> 2, 6), Chinese numerals (兩, 三十 -> 2, 30), an afternoon hour in 24-hour form and back
   ("3:30 pm" <-> 15:30, 下午3點 -> 15), and a Republic of China year as its common-era year (民國120年 -> 2031;
   the other way only when `says` itself says 民國). Numbers that are in the question (the `context`) are allowed
   too: "Flat 4B" in a statement is not a fact taken from the passage. Nothing else is converted.

This proves where the words are and that the numbers stated are in them. It does not prove that a name or a
relation was restated correctly; the quote and its place are kept so the person can look.

Results are short codes and character offsets; nothing here puts passage text in a reason.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

MIN_CJK = 2
MIN_WORDS = 2
MAX_CHARS = 300
MAX_SAYS_CHARS = 400

OK = ""
NO_QUOTE = "no-quote"
NO_PASSAGE = "unknown-passage"
TOO_SHORT = "quote-too-short"
TOO_LONG = "quote-too-long"
NOT_FOUND = "quote-not-in-passage"
NUMBER = "number-not-in-quote"
NO_SAYS = "says-nothing"

_QUOTES = dict.fromkeys("“”„‟«»「」『』〝〞＂", '"') | dict.fromkeys("‘’‚‛`´＇", "'")
_DASHES = dict.fromkeys("‐‑‒–—―−－﹣", "-")
_UNIFY = {**_QUOTES, **_DASHES}
_NUM_SEPARATORS = ".,:/-"
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]")
_WORD = re.compile(r"[^\W_぀-ヿ㐀-䶿一-鿿豈-﫿가-힯]+")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_DOTTED = re.compile(r"\d+(?:\.\d+){2,}")  # 2031.04.03, 1.2.3: parts, not a decimal

_EN_NUMBERS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen "
    "eighteen nineteen twenty".split())}
_EN_NUMBERS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
                    "hundred": 100, "thousand": 1000, "once": 1, "twice": 2, "first": 1, "second": 2, "third": 3,
                    "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10})
_MONTHS = {name: i // 2 + 1 for i, name in enumerate(
    "january jan february feb march mar april apr may may june jun july jul august aug september sep october oct "
    "november nov december dec".split())}
_MONTHS["sept"] = 9
_ZH_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_ZH_UNITS = {"十": 10, "百": 100, "千": 1000}
_ZH_RUN = re.compile(r"[零〇一二兩两三四五六七八九十百千]+")
_ROC = re.compile(r"民國\s*(\d{1,3})\s*年")
_PM_EN = re.compile(r"(?<!\d)(\d{1,2})(?::\d{2})?\s*p\.?\s?m\b")
_PM_ZH = re.compile(r"(?:下午|晚上|傍晚)\s*(\d{1,2})")
_CLOCK = re.compile(r"(?<!\d)(1[3-9]|2[0-3]):\d{2}")


# ---------------------------------------------------------------- folding, with a way back to the original


def fold(text: str) -> tuple[str, list[int]]:
    """(the folded text, for each of its characters the index in `text` it came from)."""
    out: list[str] = []
    where: list[int] = []
    space = True  # leading white space is dropped
    for index, original in enumerate(text):
        for ch in unicodedata.normalize("NFKC", original).casefold():
            ch = _UNIFY.get(ch, ch)
            if ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf", "Zs", "Zl", "Zp"):
                if not space:
                    out.append(" ")
                    where.append(index)
                    space = True
                continue
            space = False
            out.append(ch)
            where.append(index)
    if out and out[-1] == " ":
        out.pop()
        where.pop()
    return "".join(out), where


def _skeleton(folded: str) -> tuple[str, list[int]]:
    """Letters, digits and symbols only, plus the separators standing between two digits. Indexes into `folded`."""
    out: list[str] = []
    where: list[int] = []
    last = len(folded) - 1
    for i, ch in enumerate(folded):
        keep = ch.isalnum() or ch == "%" or unicodedata.category(ch).startswith("S")
        if not keep and (ch in _NUM_SEPARATORS or ch == " ") and 0 < i < last:
            keep = folded[i - 1].isdigit() and folded[i + 1].isdigit()
        if keep:
            out.append(ch)
            where.append(i)
    return "".join(out), where


def find(quote: str, passage: str) -> tuple[int, int] | None:
    """Where the quote is in the passage: (start, end) offsets into `passage` as given, or None."""
    q, _ = fold(quote)
    p, back = fold(passage)
    if not q or not p:
        return None
    at = p.find(q)
    if at >= 0:
        return back[at], back[at + len(q) - 1] + 1
    sq, _ = _skeleton(q)
    sp, to_folded = _skeleton(p)
    if not sq:
        return None
    at = sp.find(sq)
    if at < 0:
        return None
    return back[to_folded[at]], back[to_folded[at + len(sq) - 1]] + 1


def size_code(quote: str) -> str:
    """OK, or why this quote is not a usable excerpt (empty, too short, too long)."""
    folded, _ = fold(quote)
    if not folded:
        return NO_QUOTE
    if len(folded) > MAX_CHARS:
        return TOO_LONG
    if len(_CJK.findall(folded)) < MIN_CJK and len(_WORD.findall(folded)) < MIN_WORDS:
        return TOO_SHORT
    return OK


# ---------------------------------------------------------------- numbers


def _canonical(raw: str) -> str:
    whole, _, fraction = raw.partition(".")
    fraction = fraction.rstrip("0")
    return (whole.lstrip("0") or "0") + ("." + fraction if fraction else "")


def numbers(text: str) -> list[str]:
    """Every number written in digits, by value: "1,450.00" -> "1450", "08" -> "8", "2031.04.03" -> 2031, 4, 3."""
    text = _THOUSANDS.sub("", unicodedata.normalize("NFKC", str(text)))
    text = _DOTTED.sub(lambda m: m.group().replace(".", " "), text)
    return [_canonical(raw) for raw in _NUMBER.findall(text)]


def _zh_value(run: str) -> int | None:
    """三十 -> 30, 一百二十 -> 120, 兩 -> 2, 二〇三一 -> 2031. None when the run is not a number."""
    if not any(ch in _ZH_UNITS for ch in run):
        return int("".join(str(_ZH_DIGITS[ch]) for ch in run)) if all(ch in _ZH_DIGITS for ch in run) else None
    total = digit = 0
    for ch in run:
        if ch in _ZH_DIGITS:
            digit = _ZH_DIGITS[ch]
        else:
            total += (digit or 1) * _ZH_UNITS[ch]
            digit = 0
    return total + digit


def vouched(quote: str) -> set[str]:
    """The numbers a quote stands behind: its digits, and the few things it spells out (see the module text)."""
    text = unicodedata.normalize("NFKC", str(quote))
    low = text.casefold()
    out = set(numbers(text))
    for word in _WORD.findall(low):
        if word in _EN_NUMBERS:
            out.add(str(_EN_NUMBERS[word]))
        if word in _MONTHS:
            out.add(str(_MONTHS[word]))
    for run in _ZH_RUN.findall(text):
        value = _zh_value(run)
        if value is not None:
            out.add(str(value))
    for match in _ROC.finditer(text):
        out.add(str(int(match.group(1)) + 1911))
    for pattern in (_PM_EN, _PM_ZH):
        for match in pattern.finditer(low):
            hour = int(match.group(1))
            if hour < 12:
                out.add(str(hour + 12))
    for match in _CLOCK.finditer(low):
        out.add(str(int(match.group(1)) - 12))
    return out


def unsupported_numbers(says: str, quotes: Iterable[str], context: str = "") -> list[str]:
    """The numbers in `says` that none of the quotes (nor the question, `context`) stands behind."""
    allowed: set[str] = set(numbers(context))
    joined = list(quotes)
    for quote in joined:
        allowed |= vouched(quote)
    if "民國" in says:  # a common-era year in the quote may be said as a Republic of China year
        for quote in joined:
            allowed |= {str(int(n) - 1911) for n in numbers(quote) if n.isdigit() and 1912 <= int(n) <= 2200}
    seen: list[str] = []
    for number in numbers(says):
        if number not in allowed and number not in seen:
            seen.append(number)
    return seen


# ---------------------------------------------------------------- one claim, and a model's list of claims


def verify(quote: str, n: Any, says: str, passages: dict[int, str], context: str = "") -> tuple[str, int, int]:
    """(code, start, end). OK ("") with the quote's offsets in passage n; else a code and (0, 0)."""
    if isinstance(n, bool) or not isinstance(n, int) or n not in passages:
        return NO_PASSAGE, 0, 0
    code = size_code(quote)
    if code:
        return code, 0, 0
    if not str(says or "").strip():
        return NO_SAYS, 0, 0
    span = find(quote, passages[n])
    if span is None:
        return NOT_FOUND, 0, 0
    if unsupported_numbers(says, [quote], context):
        return NUMBER, 0, 0
    return OK, span[0], span[1]


def keep(raw_claims: list[dict[str, Any]], passages: dict[int, str], context: str = "",
         limit: int | None = None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """(the claims that stand, why the others were dropped as {code: count}).

    A kept claim: {"says", "quote" (as it is written in the passage), "n", "start", "end"}. A quote that appears
    in another of the given passages than the one named is refused, not moved: the model said where it read it.
    """
    kept: list[dict[str, Any]] = []
    dropped: dict[str, int] = {}
    seen: set[tuple[int, int, str]] = set()
    for claim in raw_claims:
        quote = " ".join(str(claim.get("quote") or "").split())
        says = " ".join(str(claim.get("says") or "").split())[:MAX_SAYS_CHARS]
        n = claim.get("n")
        code, start, end = verify(quote, n, says, passages, context)
        if not code and limit is not None and len(kept) >= limit:
            code = "over-limit"
        if not code and (n, start, says.casefold()) in seen:
            code = "repeat"
        if code:
            dropped[code] = dropped.get(code, 0) + 1
            continue
        seen.add((n, start, says.casefold()))
        kept.append({"says": says, "quote": passages[n][start:end], "n": n, "start": start, "end": end})
    return kept, dropped


def paragraph_of(passage: dict[str, Any], start: int) -> int | None:
    """The paragraph number holding offset `start` of a passage dict ({"text", "paragraphs"}); None when unknown."""
    position = 0
    for part, number in zip(str(passage.get("text", "")).split("\n\n"), passage.get("paragraphs") or []):
        if position <= start < position + len(part) + 2:
            return number or None
        position += len(part) + 2
    return None
