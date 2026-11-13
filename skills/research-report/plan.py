"""Step 1, plan: turn the question into a few sub-questions.

One `light` call asking for a small JSON object. The reply is read defensively
(an object, a bare list, or numbered lines); when nothing usable comes back the
question is split by rules instead, so the job never depends on this call.
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import answer as reader

MIN_SUBS = 2  # the model is asked for 3 to 6; 2 good ones are still used
MAX_SUBS = 6
MAX_CHARS = 200
MAX_OUTPUT_TOKENS = 300  # the ceiling of a light step

SYSTEM = """You plan a short research report that will be written only from the person's own documents.

Break the question into 3 to 6 sub-questions. Each one asks for one kind of fact that a document could state
(an amount, a date, a name, a rule, a step). Keep the names and key words of the question. Do not answer them.
Write the sub-questions in the language of the question.
Reply with one JSON object and nothing else, in this shape:
{"subquestions": ["first sub-question", "second sub-question", "third sub-question"]}"""

_LINE = re.compile(r"^\s*(?:[-*•]|\(?\d{1,2}[.)、:：]|[一二三四五六]、)\s*(.+?)\s*$")
_SPLIT_EN = re.compile(r"\s*(?:;|\?|\band\b|\bas well as\b|, )\s*", re.IGNORECASE)
_SPLIT_ZH = re.compile(r"[；;？?、，,]|以及|還有|並且|和|與|及")
_HINTS = re.compile(r"[\n;；|]+")

FACETS_EN = ("amounts, numbers and costs", "dates, deadlines and the people involved")
FACETS_ZH = ("金額、數字與費用", "日期、期限與相關人員")


def hints(outline: Any) -> list[str]:
    """The person's outline hints: a list, or text split on new lines, semicolons and bars."""
    items = outline if isinstance(outline, (list, tuple)) else _HINTS.split(str(outline or ""))
    return _clean([re.sub(r"^\s*(?:[-*•]|\d{1,2}[.)、])\s*", "", str(item)) for item in items])


def messages(question: str, outline: list[str]) -> list[dict[str, str]]:
    user = f"Question: {question}\n"
    if outline:
        user += "The person asked for these parts, in this order:\n" + "\n".join(f"- {h}" for h in outline) + "\n"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user + "\nJSON:"}]


def _clean(items: list[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            continue
        text = " ".join(item.split()).strip(" \"'“”「」")
        key = text.casefold()
        if len(text) < 2 or key in seen:
            continue
        seen.add(key)
        out.append(text[:MAX_CHARS])
    return out[:MAX_SUBS]


def parse(raw: str) -> list[str]:
    """The sub-questions in a model's reply; [] when it is not usable."""
    raw = (raw or "").strip()
    if not raw:
        return []
    items: list[Any] = []
    obj, _ = reader.find_object(raw)
    if obj is not None:
        for key in ("subquestions", "sub_questions", "questions", "plan", "items"):
            if isinstance(obj.get(key), list):
                items = [v.get("question") or v.get("text") if isinstance(v, dict) else v for v in obj[key]]
                break
    if not items and "[" in raw:
        try:
            value = json.loads(raw[raw.index("["):raw.rindex("]") + 1])
            items = value if isinstance(value, list) else []
        except ValueError:
            items = []
    if not items:
        items = [m.group(1) for m in (_LINE.match(line) for line in raw.splitlines()) if m]
    found = _clean(items)
    return found if len(found) >= MIN_SUBS else []


def by_rules(question: str, outline: list[str], zh: bool) -> list[str]:
    """Sub-questions without a model: the outline hints, else the question's own parts, else a few fixed angles."""
    if outline:
        return _clean([f"{question}：{h}" if zh else f"{question}: {h}" for h in outline])
    parts = _clean([p for p in (_SPLIT_ZH if zh else _SPLIT_EN).split(question) if len(p.strip()) >= (2 if zh else 4)])
    if len(parts) >= MIN_SUBS:
        return parts
    facets = FACETS_ZH if zh else FACETS_EN
    return _clean([question] + [f"{question}：{f}" if zh else f"{question}: {f}" for f in facets])
