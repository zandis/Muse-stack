"""Step 4, synthesise, and everything about the report as text: the prompt, reading it back, the hand-off prompt.

The heavy model sees the verified claims (what each says, and its claim number) only, never the documents
again. Every sentence it writes cites claim numbers like [3]. The lists at the end ("what the sources did not
answer", "Not done", "Sources") are built here from data, not by the model; the Sources list gives, for every
claim the report cites, the file and paragraph and the exact words quoted from it.
"""

from __future__ import annotations

import re
from typing import Any

from . import quotes

LENGTHS = {  # words asked for, output tokens allowed, most weight (about tokens) the body may have
    "short": {"ask": "150 to 250 words", "ask_zh": "250 到 400 個字", "max_tokens": 700, "max_weight": 700},
    "medium": {"ask": "400 to 600 words", "ask_zh": "600 到 900 個字", "max_tokens": 1400, "max_weight": 1500},
}
MIN_FACT_TERMS = quotes.MIN_FACT_TERMS

WORDS = {
    False: {"unanswered": "What the sources did not answer", "not_done": "Not done", "sources": "Sources",
            "report_missing": "Not done yet: the written report.", "the_report": "the written report",
            "unread": "not read, the budget for this job ran out: ",
            "no_text": "not finished, the model returned no text for some of its passages: ",
            "no_report_text": "the written report (the model returned no text)",
            "nothing": "The files you named do not answer this.",
            "notes_title": "Verified notes", "language": "English"},
    True: {"unanswered": "資料沒有回答的問題", "not_done": "尚未完成", "sources": "來源",
           "report_missing": "尚未完成：書面報告。", "the_report": "書面報告",
           "unread": "尚未閱讀（這項工作的額度已用完）：",
           "no_text": "尚未完成（模型沒有傳回文字，部分段落未讀）：",
           "no_report_text": "書面報告（模型沒有傳回文字）",
           "nothing": "你指定的資料夾裡沒有這個問題的答案。",
           "notes_title": "已查證的筆記", "language": "Traditional Chinese (繁體中文)"},
}

SYSTEM = """You write a short report from verified notes that were taken from the person's own documents.

Rules:
- Use only the notes. No outside knowledge, no guesses, no advice of your own.
- Every sentence that states a fact ends with the numbers of the notes it uses, like [3] or [3][5].
- Keep names, numbers, dates and key words exactly as they are written in the notes.
- One section per question, in the order given, each starting with a line "## " and a short heading.
- No introduction, no conclusion, no list of sources: those are added afterwards.
- Write plain Markdown, not JSON."""

HANDOFF = {
    False: ("Write a short report that answers the question below. Use only the notes: each was checked against an exact "
            "quote from my own documents (listed under Sources). Keep the note numbers like [3] after every sentence that "
            "states a fact, keep names, numbers and dates exactly as written, use one section per sub-question, and end "
            "with the Sources list as given."),
    True: ("請根據下面的筆記寫一份簡短的報告來回答問題。只能使用筆記的內容：每一條筆記都已經和我自己文件中的原文核對過（列在來源）。"
           "每一句陳述事實的句子後面都要保留筆記編號，例如 [3]；姓名、數字和日期要和筆記完全一樣；"
           "每個子問題寫一節，最後附上下面的來源清單。請用繁體中文寫。"),
}

_MARK = quotes._MARK
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*•]\s+|\d{1,2}[.)]\s+)")
_SOURCE_LINE = re.compile(r"^\s*\[(\d{1,3})\]\s+\S")
_OWN_SOURCES = re.compile(r"^\s*(?:#{1,6}\s*|\*\*)?(?:sources?|references?|來源|参考資料|參考資料)\s*(?:\*\*)?\s*[:：]?\s*$", re.IGNORECASE)

cites_of = quotes.cites_of
sentences = quotes.sentences


# ---------------------------------------------------------------- the prompt


def of_sub(claims: list[dict[str, Any]], number: int) -> list[dict[str, Any]]:
    """The claims shown under a sub-question: the ones first found for it, else the ones that also answer it."""
    return [c for c in claims if c["sub"] == number] or [c for c in claims if number in c.get("subs", [])]


def claim_lines(subs: list[str], claims: list[dict[str, Any]], only: int | None = None) -> str:
    """The verified claims under their sub-questions, each with its claim number."""
    parts = []
    for number, sub in enumerate(subs, start=1):
        mine = of_sub(claims, number) if only in (None, number) else []
        if mine:
            parts.append(f"## {sub}\n" + "\n".join(f"- {c['text']} [{c['id']}]" for c in mine))
    return "\n\n".join(parts)


def messages(question: str, subs: list[str], claims: list[dict[str, Any]], length: str, zh: bool) -> list[dict[str, str]]:
    size = LENGTHS[length]
    user = (f"Report question: {question}\n\nWrite in {WORDS[zh]['language']}, about {size['ask_zh' if zh else 'ask']}.\n\n"
            f"Notes, by question:\n\n{claim_lines(subs, claims)}\n\nReport:")
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def source_lines(sources: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{s['n']}] {s['label']}: “{s['quote']}”" if s.get("quote") else f"[{s['n']}] {s['label']}"
                     for s in sources)


def handoff_prompt(question: str, subs: list[str], claims: list[dict[str, Any]], sources: list[dict[str, Any]], zh: bool) -> str:
    """Ready to paste into the person's own ChatGPT, Claude or Gemini app: the verified claims and where they came from."""
    w = WORDS[zh]
    return (f"{HANDOFF[zh]}\n\n{'問題' if zh else 'Question'}: {question}\n\n{w['notes_title']}:\n\n{claim_lines(subs, claims)}\n\n"
            f"{w['sources']}:\n{source_lines(sources)}\n")


# ---------------------------------------------------------------- reading a report


def clean_body(raw: str) -> str:
    """The model's sections: code fences off, its own title and any source list of its own removed."""
    text = quotes.brackets(re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (raw or "").strip()))
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


def split_report(text: str, zh: bool) -> dict[str, Any]:
    """The parts of a finished report: body sentences, the listed unanswered / not-done lines, the Sources ids."""
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
    """Does some sentence rest on one of these claims: it cites the claim's number. No words are compared."""
    ids = {c["id"] for c in claims}
    return any(ids & set(s["cites"]) for s in found)


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
        parts.append(f"## {w['sources']}\n" + source_lines(sources))
    return "\n\n".join(parts) + "\n"
