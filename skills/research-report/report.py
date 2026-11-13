"""Step 4, synthesise, and everything about the report as text: the prompt, reading it back, the hand-off prompt.

The heavy model sees the verified claims and their source numbers only, never
the documents again. It writes the sections; the lists at the end ("what the
sources did not answer", "Sources") are built here from data, not by the model.
"""

from __future__ import annotations

import re
from typing import Any

from . import answer as reader
from .textwork import overlap, terms

LENGTHS = {  # words asked for, output tokens allowed, most weight (about tokens) the body may have
    "short": {"ask": "150 to 250 words", "ask_zh": "250 到 400 個字", "max_tokens": 700, "max_weight": 700},
    "medium": {"ask": "400 to 600 words", "ask_zh": "600 到 900 個字", "max_tokens": 1400, "max_weight": 1500},
}
COVER_SHARE = 0.5  # of a claim's terms found in a sentence that cites the same source: the claim is in the report
MIN_FACT_TERMS = 3  # a sentence with fewer terms is a heading or a connective, not a statement of fact

WORDS = {
    False: {"unanswered": "What the sources did not answer", "not_done": "Not done", "sources": "Sources",
            "report_missing": "Not done yet: the written report.", "the_report": "the written report",
            "unread": "not read, the budget for this job ran out: ", "nothing": "The files you named do not answer this.",
            "notes_title": "Verified notes", "language": "English"},
    True: {"unanswered": "資料沒有回答的問題", "not_done": "尚未完成", "sources": "來源",
           "report_missing": "尚未完成：書面報告。", "the_report": "書面報告",
           "unread": "尚未閱讀（這項工作的額度已用完）：", "nothing": "你指定的資料夾裡沒有這個問題的答案。",
           "notes_title": "已查證的筆記", "language": "Traditional Chinese (繁體中文)"},
}

SYSTEM = """You write a short report from verified notes that were taken from the person's own documents.

Rules:
- Use only the notes. No outside knowledge, no guesses, no advice of your own.
- Every sentence that states a fact ends with the source numbers of the notes it uses, like [3] or [3][5].
- Keep names, numbers, dates and key words exactly as they are written in the notes.
- One section per question, in the order given, each starting with a line "## " and a short heading.
- No introduction, no conclusion, no list of sources: those are added afterwards.
- Write plain Markdown, not JSON."""

HANDOFF = {
    False: ("Write a short report that answers the question below. Use only the notes: they were checked against my own "
            "documents. Keep the source numbers like [3] after every sentence that states a fact, keep names, numbers and "
            "dates exactly as written, use one section per sub-question, and end with the Sources list as given."),
    True: ("請根據下面的筆記寫一份簡短的報告來回答問題。只能使用筆記的內容：這些筆記已經和我自己的文件核對過。"
           "每一句陳述事實的句子後面都要保留來源編號，例如 [3]；姓名、數字和日期要和筆記完全一樣；"
           "每個子問題寫一節，最後附上下面的來源清單。請用繁體中文寫。"),
}

_MARK = reader._MARK
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*•]\s+|\d{1,2}[.)]\s+)")
_SENTENCE = re.compile(r"(?<=[。！？!?])|(?<=\.)\s+(?=[^a-z0-9\s])")
_LEADING = re.compile(r"^(?:\s*\[\s*\d{1,3}(?:\s*[,、]\s*\d{1,3})*\s*\])+\s*[.。]?")
_SOURCE_LINE = re.compile(r"^\s*\[(\d{1,3})\]\s+\S")
_OWN_SOURCES = re.compile(r"^\s*(?:#{1,6}\s*|\*\*)?(?:sources?|references?|來源|参考資料|參考資料)\s*(?:\*\*)?\s*[:：]?\s*$", re.IGNORECASE)


def _brackets(text: str) -> str:
    return text.replace("【", "[").replace("】", "]").replace("［", "[").replace("］", "]")


def cites_of(text: str) -> list[int]:
    out: list[int] = []
    for match in _MARK.finditer(text):
        for n in re.findall(r"\d{1,3}", match.group(1)):
            if int(n) not in out:
                out.append(int(n))
    return out


# ---------------------------------------------------------------- the prompt


def of_sub(claims: list[dict[str, Any]], number: int) -> list[dict[str, Any]]:
    """The claims shown under a sub-question: the ones first found for it, else the ones that also answer it."""
    return [c for c in claims if c["sub"] == number] or [c for c in claims if number in c.get("subs", [])]


def claim_lines(subs: list[str], claims: list[dict[str, Any]], only: int | None = None) -> str:
    """The verified claims under their sub-questions, each with its source numbers."""
    parts = []
    for number, sub in enumerate(subs, start=1):
        mine = of_sub(claims, number) if only in (None, number) else []
        if mine:
            parts.append(f"## {sub}\n" + "\n".join(f"- {c['text']} " + "".join(f"[{n}]" for n in c["cites"]) for c in mine))
    return "\n\n".join(parts)


def messages(question: str, subs: list[str], claims: list[dict[str, Any]], length: str, zh: bool) -> list[dict[str, str]]:
    size = LENGTHS[length]
    user = (f"Report question: {question}\n\nWrite in {WORDS[zh]['language']}, about {size['ask_zh' if zh else 'ask']}.\n\n"
            f"Notes, by question:\n\n{claim_lines(subs, claims)}\n\nReport:")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def handoff_prompt(question: str, subs: list[str], claims: list[dict[str, Any]], sources: list[dict[str, Any]], zh: bool) -> str:
    """Ready to paste into the person's own ChatGPT, Claude or Gemini app: the verified claims and where they came from."""
    w = WORDS[zh]
    listed = "\n".join(f"[{s['n']}] {s['label']}" for s in sources)
    return (f"{HANDOFF[zh]}\n\n{'問題' if zh else 'Question'}: {question}\n\n{w['notes_title']}:\n\n{claim_lines(subs, claims)}\n\n"
            f"{w['sources']}:\n{listed}\n")


# ---------------------------------------------------------------- reading a report


def clean_body(raw: str) -> str:
    """The model's sections: code fences off, its own title and any source list of its own removed."""
    text = _brackets(re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (raw or "").strip()))
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _OWN_SOURCES.match(line):
            lines = lines[:i]
            break
    while lines and (not lines[0].strip() or re.match(r"^\s{0,3}#\s", lines[0])):
        lines.pop(0)
    return "\n".join(lines).strip()


def sections(text: str) -> list[tuple[str, list[str]]]:
    """(heading, lines) for each `## ` section; text before the first heading has the heading ""."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match and line.lstrip().startswith("#"):
            out.append((match.group(1).strip(), []))
        else:
            out[-1][1].append(line)
    return out


def sentences(lines: list[str]) -> list[dict[str, Any]]:
    """Every sentence of some body lines: {"text" without markers, "cites", "fact": needs a source}."""
    out: list[dict[str, Any]] = []
    for line in lines:
        line = _BULLET.sub("", _brackets(line)).strip()
        if not line or re.fullmatch(r"\*\*[^*]+\*\*:?", line):
            continue
        for piece in _SENTENCE.split(line):
            if not piece or not piece.strip():
                continue
            lead = _LEADING.match(piece)
            if lead and out:  # "... the 5th. [2] Next ...": the marker belongs to the sentence before it
                for n in cites_of(lead.group()):
                    if n not in out[-1]["cites"]:
                        out[-1]["cites"].append(n)
                piece = piece[lead.end():]
            text = " ".join(_MARK.sub(" ", piece).split()).strip("*_ ")
            if not text:
                continue
            ending = text.rstrip("\"'”」)） ")[-1:]
            fact = len(terms(text)) >= MIN_FACT_TERMS and ending not in ":：?？"
            out.append({"text": text, "cites": cites_of(piece), "fact": fact})
    return out


def split_report(text: str, zh: bool) -> dict[str, Any]:
    """The parts of a finished report: body sentences, the listed unanswered lines, the Sources ids."""
    w = WORDS[zh]
    body: list[str] = []
    listed: list[str] = []
    ids: list[int] = []
    for heading, lines in sections(text):
        if heading == w["sources"]:
            ids += [int(m.group(1)) for m in (_SOURCE_LINE.match(line) for line in lines) if m]
        elif heading in (w["unanswered"], w["not_done"]):
            listed += [_BULLET.sub("", line).strip() for line in lines if line.strip()]
        else:
            body += lines
    return {"sentences": sentences(body), "listed": listed, "source_ids": ids, "body": "\n".join(body)}


def body_sentences(body: str) -> list[dict[str, Any]]:
    return sentences([line for line in body.splitlines() if not line.lstrip().startswith("#")])


def covers(claims: list[dict[str, Any]], found: list[dict[str, Any]]) -> bool:
    """Does some sentence restate one of these claims: it cites one of the claim's sources and shares its words."""
    return any(set(c["cites"]) & set(s["cites"]) and overlap(c["text"], s["text"])[1] >= COVER_SHARE for c in claims for s in found)


# ---------------------------------------------------------------- writing the final text


def assemble(title: str, body: str, unanswered: list[str], not_done: list[str], sources: list[dict[str, Any]], zh: bool) -> str:
    w = WORDS[zh]
    parts = [f"# {title}"]
    if body.strip():
        parts.append(body.strip())
    if unanswered:
        parts.append(f"## {w['unanswered']}\n" + "\n".join(f"- {item}" for item in unanswered))
    if not_done:
        parts.append(f"## {w['not_done']}\n" + "\n".join(f"- {item}" for item in not_done))
    if sources:
        parts.append(f"## {w['sources']}\n" + "\n".join(f"[{s['n']}] {s['label']}" for s in sources))
    return "\n\n".join(parts) + "\n"
