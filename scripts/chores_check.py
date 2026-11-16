#!/usr/bin/env python3
"""Run the shipped skills many times on FREE allowances and say how often they pass (gate G1).

Run it on your own machine, after `muse up`. It runs each task in
evals/chores/<skill>/tasks.jsonl through the real skill runner against the local
router, with the runner's own never-paid app (`skills`) and a money budget of 0.

Before anything is sent it prints the plan and the worst case (runs, requests,
tokens) and asks you to confirm unless you pass --yes. It is slow on purpose:
one run at a time, a pause between runs, a short Retry-After is sat out and a
long one records the run as `deferred`. If the router ever reports a cost above
zero it stops at once.

Two scores per run:
  right         the expected facts in the task file (what G1 counts since the
                first full real run, 2026-11-13: an 82% own-check rate "passed"
                while 41% of the reports were right). A typed fact (date, time,
                amount, count) takes every way of writing the same value.
  own check     the skill's own check, as the runner reports it. Shown second,
                with the gap to "right" called out.

Per run it also records, as counts and milliseconds only: empty answers and
recoveries, the status (done / partial / needs_choice), requests per skill step,
each call's provider, task and duration (the slowest are shown), waits, and
whether the router is a build that sends the newer headers (an old one gets a
loud WARNING). At the start and the end it records the day's allowance state
from the local ledger (`muse status`), so a batch that used up a free allowance
says so.

What it writes:
  $MUSE_HOME/chores-report.json   the report (resume with --resume)
  $MUSE_HOME/chores-runs.csv      one line per run, appended, to compare nights
  $MUSE_HOME/chores-failures.jsonl  only with --keep-failures: for each wrong or
                failed run, the answer text, its claims (the skill's own quotes
                and what they say) and which accepted forms of each expected fact
                were missing. Mode 0600. Never passages or prompts.
By default none holds document text, a prompt, a model's answer or a check
reason's wording: ids, counts, provider names, short codes. --keep-outputs adds
the answers and reasons to the JSON report for debugging.
tests/test_chores_check.py runs this script against a fake router with canary
values and fails if any of them reach the report or the CSV.

Exit code: 0 every skill met the G1 line; 1 one did not, or the router could not
be reached; 2 a usage problem; 3 stopped because something was charged.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

TASKS_DIR = ROOT / "evals" / "chores"
FIXTURES = "fixtures"
TASK_FILE = "tasks.jsonl"
G1_RATE = 0.80
G1_RUNS = 50
DEFAULT_DELAY = 2.0
DEFAULT_MAX_WAIT = 60.0
DEFAULT_STOP_AFTER = 5
UNREACHABLE_STOP = 2
Z95 = 1.959963984540054
EXIT_OK, EXIT_NOT_MET, EXIT_USAGE, EXIT_SPENT = 0, 1, 2, 3
CSV_FIELDS = ("time", "run_id", "set", "skill", "version", "task", "kind", "lang", "repeat", "privacy", "outcome", "status",
              "own_check", "independent", "facts_found", "facts", "requests", "tokens", "cost_usd", "providers", "deferrals",
              "empty_answers", "seconds", "failure", "recoveries", "unanswered", "slowest_ms", "router")
FAILURES_FILE = "chores-failures.jsonl"
SLOWEST = 3  # calls listed per run
NEW_HEADERS = ("x-muse-reasoning", "x-muse-min-output", "x-muse-answer")
OLD_ROUTER_WARNING = ("WARNING: your router is older than these skills: it did not send the newer headers "
                      "(x-muse-min-output / x-muse-reasoning / x-muse-answer) where a current build would. "
                      "Update it and restart it, then run this again.")

_sleep = time.sleep


# ---------------------------------------------------------------- tasks


def norm(text: str) -> str:
    """For comparing: NFKC, case folded, no white space, no thousands commas."""
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)
    return re.sub(r"\s+", "", text)


def numbers(text: str) -> list[float]:
    """Every number in the text, thousands commas removed. `1,450` and `1450.00` are both 1450."""
    out = []
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?", unicodedata.normalize("NFKC", str(text))):
        try:
            out.append(float(raw.replace(",", "")))
        except ValueError:
            continue
    return out


def _contains(haystack: str, needle: str) -> bool:
    """`needle` in `haystack`, but a needle that starts or ends with a digit must not sit inside a longer number."""
    if not needle:
        return False
    pattern = ("(?<![0-9])" if needle[0].isdigit() else "") + re.escape(needle) + ("(?![0-9])" if needle[-1].isdigit() else "")
    return re.search(pattern, haystack) is not None


# Typed facts: one VALUE, many ways to write it. Strict on the value, lenient on the form.
#   {"date": "2031-04-03"}                  2031-04-03 = 2031/4/3 = 3 April 2031 = 03 Apr 2031 = April 3, 2031
#                                           = 2031年4月3日 = 民國120年4月3日. "year_optional": true also takes
#                                           a month and day without a year ("3 April", "4 月 3 日").
#   {"time": "08:40"}                       08:40 = 8:40 = 8:40 am = 8.40am = 上午8點40分 = 8點40分; 13:15 = 1:15 pm =
#                                           下午1點15分 = 1:15 (no am/pm said: either half of the day).
#   {"amount": 1450}                        1,450 = 1450.00 = US$1,450 = NT$1,450 = 1,450 元: the number's value.
#   {"count": 3, "unit": "year"}            3 years = three years = 3-year = 3 年 = 三年 = 兩年 (for 2).
FACT_KINDS = ("any", "number", "regex", "date", "time", "amount", "count")
COUNT_UNITS = {
    "year": (r"years?", r"年"), "month": (r"months?", r"個月|个月"), "week": (r"weeks?", r"週|周|星期|個星期|個禮拜"),
    "day": (r"days?|working days?", r"天|個工作天|個工作日"), "hour": (r"hours?", r"小時|個小時"),
    "minute": (r"minutes?", r"分鐘"), "time": (r"times?", r"次"), "person": (r"people|persons?", r"人|位"),
}
_MONTHS = {name: i // 2 + 1 for i, name in enumerate(
    "january jan february feb march mar april apr may may june jun july jul august aug september sep october oct "
    "november nov december dec".split())}
_MONTHS["sept"] = 9
_MONTH = r"(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec)"
_EN_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}
_EN_WORDS.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90})
_ZH_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text)).casefold()


def _zh_number(run: str) -> int | None:
    if run.isdigit():
        return int(run)
    if not run or any(ch not in _ZH_DIGITS and ch not in "十百" for ch in run):
        return None
    total = digit = 0
    for ch in run:
        if ch in _ZH_DIGITS:
            digit = _ZH_DIGITS[ch]
        else:
            total += (digit or 1) * (10 if ch == "十" else 100)
            digit = 0
    return total + digit


def dates_in(text: str) -> set[tuple[int | None, int, int]]:
    """Every (year or None, month, day) written in `text`, in any of the forms the date fact accepts."""
    t = _fold(text)
    out: set[tuple[int | None, int, int]] = set()

    def add(y: Any, m: Any, d: Any) -> None:
        try:
            year, month, day = (int(y) if y is not None else None), int(m), int(d)
        except (TypeError, ValueError):
            return
        if 1 <= month <= 12 and 1 <= day <= 31:
            out.add((year, month, day))

    for y, m, d in re.findall(r"(?<!\d)(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})(?!\d)", t):
        add(y, m, d)
    for roc, m, d in re.findall(r"民國\s*(\d{1,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日號号]", t):
        add(int(roc) + 1911, m, d)
    for y, m, d in re.findall(r"(?<!民國)(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日號号]", t):
        add(y, m, d)
    for m, d in re.findall(r"(?<![\d年])\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日號号]", t):
        add(None, m, d)
    for d, name, y in re.findall(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\.?(?:,?\s+(\d{{4}}))?(?!\w)", t):
        add(y or None, _MONTHS[name], d)
    for name, d, y in re.findall(rf"(?<!\w){_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)(?:,?\s+(\d{{4}}))?", t):
        add(y or None, _MONTHS[name], d)
    return out


def times_in(text: str) -> set[tuple[int, int, str]]:
    """Every (hour, minute, half) in `text`; half is "am", "pm" or "" when the text does not say."""
    t = _fold(text)
    out: set[tuple[int, int, str]] = set()
    for h, m, ap in re.findall(r"(?<![\d:.])(\d{1,2})\s*[:：]\s*(\d{2})(?![\d:])\s*(a\.?\s?m\b\.?|p\.?\s?m\b\.?)?", t):
        out.add((int(h), int(m), ("pm" if ap.startswith("p") else "am") if ap else ""))
    for h, m, ap in re.findall(r"(?<![\d:.])(\d{1,2})\.(\d{2})\s*(a\.?\s?m\b\.?|p\.?\s?m\b\.?)", t):  # 8.40am: only with am/pm
        out.add((int(h), int(m), "pm" if ap.startswith("p") else "am"))
    for h, ap in re.findall(r"(?<![\d:.])(\d{1,2})\s*(a\.?\s?m\b\.?|p\.?\s?m\b\.?)", t):
        out.add((int(h), 0, "pm" if ap.startswith("p") else "am"))
    for half, h, rest in re.findall(r"(上午|早上|清晨|凌晨|中午|下午|傍晚|晚上)?\s*([\d一二兩两三四五六七八九十]{1,3})\s*[點点時时]\s*(半|\d{1,2}\s*分|[一二三四五六七八九十]{1,3}\s*分)?", t):
        hour = _zh_number(h)
        if hour is None:
            continue
        minute = 30 if rest == "半" else (_zh_number(rest.replace("分", "").strip()) or 0) if rest else 0
        side = "pm" if half in ("下午", "傍晚", "晚上") else ("am" if half in ("上午", "早上", "清晨", "凌晨") else "")
        if half == "中午":
            side = "pm" if hour == 12 or hour < 6 else ""
        out.add((hour, minute, side))
    return out


def _time_matches(want: tuple[int, int], got: tuple[int, int, str]) -> bool:
    h, m, side = got
    if m != want[1] or not 0 <= h <= 23:
        return False
    if side == "pm" and h < 12:
        h += 12
    elif side == "am" and h == 12:
        h = 0
    elif not side and h <= 12:
        return h % 12 == want[0] % 12  # "1:15" with no am/pm said: either half of the day
    return h == want[0]


def _count_values(text: str, unit: str) -> set[int]:
    en, zh = COUNT_UNITS[unit]
    t = _fold(text)
    out: set[int] = set()
    for raw in re.findall(rf"(?<![\d.])(\d+|{'|'.join(_EN_WORDS)})\s*-?\s*(?:{en})\b", t):
        out.add(int(raw) if raw.isdigit() else _EN_WORDS[raw])
    for raw in re.findall(rf"([\d一二兩两三四五六七八九十百]+)\s*(?:{zh})", t):
        value = _zh_number(raw)
        if value is not None:
            out.add(value)
    return out


def fact_in(fact: Mapping[str, Any], text: str) -> bool:
    """Is this expected fact present in `text`? Untyped: `any` (substrings), `number`, `regex`. Typed: see FACT_KINDS."""
    if "any" in fact:
        squashed = norm(text)
        return any(_contains(squashed, norm(alt)) for alt in fact["any"])
    if "number" in fact or "amount" in fact:
        want = float(fact.get("number", fact.get("amount")))
        return any(abs(n - want) < 1e-9 for n in numbers(text))
    if "regex" in fact:
        return re.search(fact["regex"], unicodedata.normalize("NFKC", str(text)), re.IGNORECASE) is not None
    if "date" in fact:
        y, m, d = (int(x) for x in str(fact["date"]).split("-"))
        return any((year == y or (year is None and fact.get("year_optional") is True)) and (month, day) == (m, d)
                   for year, month, day in dates_in(text))
    if "time" in fact:
        h, m = (int(x) for x in str(fact["time"]).split(":"))
        return any(_time_matches((h, m), got) for got in times_in(text))
    if "count" in fact:
        return int(fact["count"]) in _count_values(text, str(fact.get("unit", "")))
    return False


def accepted_forms(fact: Mapping[str, Any]) -> list[str]:
    """Some of the ways this fact may be written, for a person reading why an answer was scored wrong."""
    if "any" in fact:
        return list(fact["any"])
    if "number" in fact or "amount" in fact:
        value = fact.get("number", fact.get("amount"))
        return [f"{value:,}" if isinstance(value, int) else str(value), "(any form with the same value)"]
    if "date" in fact:
        y, m, d = (int(x) for x in str(fact["date"]).split("-"))
        name = "January February March April May June July August September October November December".split()[m - 1]
        forms = [f"{y:04d}-{m:02d}-{d:02d}", f"{d} {name} {y}", f"{name} {d}, {y}", f"{y}/{m}/{d}", f"{y}年{m}月{d}日",
                 f"民國{y - 1911}年{m}月{d}日"]
        return forms + ([f"{d} {name}", f"{m}月{d}日"] if fact.get("year_optional") else [])
    if "time" in fact:
        h, m = (int(x) for x in str(fact["time"]).split(":"))
        twelve = h % 12 or 12
        return [f"{h:02d}:{m:02d}", f"{twelve}:{m:02d} {'pm' if h >= 12 else 'am'}", f"{'下午' if h >= 12 else '上午'}{twelve}點{m}分"]
    if "count" in fact:
        en, zh = COUNT_UNITS.get(str(fact.get("unit")), ("", ""))
        return [f"{fact['count']} {str(fact.get('unit'))}(s)", f"{fact['count']}{zh.split('|')[0]}"]
    if "regex" in fact:
        return [f"/{fact['regex']}/"]
    return []


def sources_of(fact: Mapping[str, Any]) -> list[str]:
    source = fact.get("source")
    return [source] if isinstance(source, str) else list(source or [])


def task_problems(task: Any, tasks_dir: Path, skill: str) -> list[str]:
    """What is wrong with one task definition; [] when it can be used."""
    if not isinstance(task, dict):
        return ["not a JSON object"]
    out: list[str] = []
    for key in ("id", "kind", "lang"):
        if not isinstance(task.get(key), str) or not task[key]:
            out.append(f"`{key}` must be a non-empty string")
    if task.get("skill") != skill:
        out.append(f"`skill` must be {skill!r}, the folder the file is in")
    if not isinstance(task.get("inputs"), dict) or not task.get("inputs"):
        out.append("`inputs` must be a non-empty object")
    folders = task.get("folders", [])
    if not isinstance(folders, list) or not all(isinstance(f, str) and f and "/" not in f and ".." not in f for f in folders):
        out.append("`folders` must be a list of folder names under fixtures/")
        folders = []
    for folder in folders:
        if not (tasks_dir / FIXTURES / folder).is_dir():
            out.append(f"fixture folder not found: {folder}")
    expect = task.get("expect")
    if not isinstance(expect, dict):
        return out + ["`expect` must be an object"]
    facts = expect.get("facts", [])
    if not isinstance(facts, list) or not all(isinstance(f, dict) for f in facts):
        return out + ["`expect.facts` must be a list of objects"]
    if expect.get("not_found") is not True and not facts:
        out.append("`expect` needs `facts`, or `not_found: true`")
    if expect.get("not_found") is True and facts:
        out.append("`expect` has both `not_found` and `facts`")
    for number, fact in enumerate(facts, start=1):
        kinds = [k for k in FACT_KINDS if k in fact]
        if len(kinds) != 1:
            out.append(f"fact {number} needs exactly one of " + ", ".join(f"`{k}`" for k in FACT_KINDS))
            continue
        if "date" in fact and not (isinstance(fact["date"], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fact["date"])):
            out.append(f"fact {number}: `date` must be YYYY-MM-DD")
        if "time" in fact and not (isinstance(fact["time"], str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", fact["time"])):
            out.append(f"fact {number}: `time` must be HH:MM (24-hour)")
        if "amount" in fact and (isinstance(fact["amount"], bool) or not isinstance(fact["amount"], (int, float))):
            out.append(f"fact {number}: `amount` must be a number")
        if "count" in fact and (isinstance(fact["count"], bool) or not isinstance(fact["count"], int) or fact.get("unit") not in COUNT_UNITS):
            out.append(f"fact {number}: `count` must be a whole number with a `unit` from {', '.join(COUNT_UNITS)}")
        if "any" in fact and not (isinstance(fact["any"], list) and fact["any"] and all(isinstance(a, str) and a for a in fact["any"])):
            out.append(f"fact {number}: `any` must be a list of strings")
        if "number" in fact and (isinstance(fact["number"], bool) or not isinstance(fact["number"], (int, float))):
            out.append(f"fact {number}: `number` must be a number")
        if "regex" in fact:
            try:
                re.compile(fact["regex"])
            except (re.error, TypeError):
                out.append(f"fact {number}: `regex` does not compile")
    return out


def fixture_text(path: Path) -> tuple[str | None, str]:
    """(the text of a fixture file, problem). A PDF fixture is checked without a PDF parser: its bytes must be what
    scripts/pdf_fixtures.py builds from the source text kept there, and that source text is what is searched."""
    if path.suffix.lower() != ".pdf":
        return path.read_text(encoding="utf-8"), ""
    import pdf_fixtures  # scripts/, standard library only

    return pdf_fixtures.source_text(path)


def fixture_problems(task: Mapping[str, Any], tasks_dir: Path) -> list[str]:
    """Expected facts that are not in the file they should come from: such a task could not be answered."""
    out: list[str] = []
    for number, fact in enumerate(task.get("expect", {}).get("facts", []), start=1):
        names = sources_of(fact)
        if not names:
            continue
        texts = []
        for name in names:
            found = [tasks_dir / FIXTURES / folder / name for folder in task.get("folders", [])]
            found = [p for p in found if p.is_file()]
            if not found:
                out.append(f"fact {number}: source file {name} is not in the task's folders")
            for path in found:
                text, problem = fixture_text(path)
                if problem:
                    out.append(f"fact {number}: {problem}")
                if text is not None:
                    texts.append(text)
        if texts and not any(fact_in(fact, text) for text in texts):
            out.append(f"fact {number} is not in {' or '.join(names)}")
    return out


def load_tasks(tasks_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """({skill folder: tasks}, problems). A skill folder is any folder with a tasks.jsonl."""
    found: dict[str, list[dict[str, Any]]] = {}
    problems: list[str] = []
    if not tasks_dir.is_dir():
        return {}, [f"no such folder: {tasks_dir}"]
    for folder in sorted(p for p in tasks_dir.iterdir() if p.is_dir() and p.name != FIXTURES and (p / TASK_FILE).is_file()):
        tasks: list[dict[str, Any]] = []
        seen: set[str] = set()
        for number, line in enumerate((folder / TASK_FILE).read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            where = f"{folder.name}/{TASK_FILE} line {number}"
            try:
                task = json.loads(line)
            except ValueError:
                problems.append(f"{where}: not JSON")
                continue
            issues = task_problems(task, tasks_dir, folder.name)
            if not issues and task["id"] in seen:
                issues = [f"duplicate id {task['id']}"]
            if not issues:
                issues = fixture_problems(task, tasks_dir)
            if issues:
                problems += [f"{where}: {issue}" for issue in issues]
                continue
            seen.add(task["id"])
            tasks.append(task)
        found[folder.name] = tasks
    return found, problems


def set_hash(tasks: Iterable[Mapping[str, Any]], tasks_dir: Path) -> str:
    """Changes when a task or a fixture file changes, so two nights are only compared on the same set."""
    digest = hashlib.sha256()
    folders: set[str] = set()
    for task in sorted(tasks, key=lambda t: t["id"]):
        digest.update(json.dumps(task, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        folders.update(task.get("folders", []))
    for folder in sorted(folders):
        for path in sorted((tasks_dir / FIXTURES / folder).rglob("*")):
            if path.is_file():
                digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    return digest.hexdigest()[:12]


def choose(tasks: list[dict[str, Any]], sample: int | None, seed: int) -> list[dict[str, Any]]:
    """All tasks, or a random `sample` of them spread over the kinds (the same for the same seed)."""
    if not sample or sample >= len(tasks):
        return list(tasks)
    rng = random.Random(seed)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        by_kind.setdefault(task["kind"], []).append(task)
    for group in by_kind.values():
        rng.shuffle(group)
    picked: list[dict[str, Any]] = []
    while len(picked) < sample:
        for kind in sorted(by_kind):
            if by_kind[kind] and len(picked) < sample:
                picked.append(by_kind[kind].pop())
    order = {task["id"]: i for i, task in enumerate(tasks)}
    return sorted(picked, key=lambda t: order[t["id"]])


# ---------------------------------------------------------------- scoring


def answer_text(output: Any) -> str:
    if isinstance(output, dict):
        for key in ("text", "answer", "report"):
            if isinstance(output.get(key), str) and output[key].strip():
                return output[key]
        return ""
    return output if isinstance(output, str) else ""


def cited_files(output: Any) -> set[str] | None:
    """Names of the files the answer cites; None when the skill's output has no citation list at all."""
    if not isinstance(output, dict):
        return None
    lists = [output[key] for key in ("citations", "sources") if isinstance(output.get(key), list)]
    if not lists:
        return None
    return {Path(str(item["path"])).name for items in lists for item in items if isinstance(item, dict) and item.get("path")}


def score(task: Mapping[str, Any], output: Any) -> dict[str, Any]:
    """The independent score: are the expected facts in the answer, citing the right file?"""
    expect = task["expect"]
    text = answer_text(output)
    if expect.get("not_found") is True:
        said = isinstance(output, dict) and output.get("not_found") is True
        return {"pass": bool(said), "facts_found": 0, "facts": 0, "sources_ok": None, "not_found": bool(said)}
    facts = expect.get("facts", [])
    found = [fact_in(fact, text) for fact in facts]
    cited = cited_files(output)
    wanted = [sources_of(fact) for fact in facts if sources_of(fact)]
    sources_ok = None if cited is None or not wanted else all(any(name in cited for name in names) for names in wanted)
    claimed_nothing = isinstance(output, dict) and output.get("not_found") is True
    return {"pass": bool(text) and all(found) and sources_ok is not False and not claimed_nothing,
            "facts_found": sum(found), "facts": len(facts), "sources_ok": sources_ok, "not_found": claimed_nothing}


def wilson(passed: int, total: int, z: float = Z95) -> tuple[float, float] | None:
    """The 95% Wilson score interval for a pass rate; None when nothing was counted."""
    if total <= 0:
        return None
    p = passed / total
    denom = 1.0 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


REASON_CODES = (
    ("were not read", "not-read"), ("gave no answer", "unanswered"), ("not in the passage", "quote-not-found"),
    ("number that is not in the quote", "number-not-quoted"), ("quote is too", "quote-size"),
    ("does not mention what the question names", "off-subject"), ("asks for", "missing-part"),
    ("no verified claims", "empty"),
    ("without a source", "uncited"), ("cites no", "uncited"), ("no cited claims", "empty"), ("no statement", "empty"),
    ("is empty", "empty"), ("does not exist", "unknown-source"), ("was not given", "unknown-source"),
    ("shares too little", "weak-overlap"), ("nothing that can be checked", "weak-overlap"), ("too long", "length"),
    ("language", "language"), ("sub-question", "coverage"), ("sources list", "source-list"),
    ("not found but", "not-found-with-claims"), ("nothing was found but", "not-found-with-claims"),
    ("check itself stopped", "check-error"),
)


def reason_code(reason: str) -> str:
    """A check reason as a short code. The wording stays out of the report."""
    low = str(reason).lower()
    return next((code for needle, code in REASON_CODES if needle in low), "other")


def ended_by_deferral(events: list[dict[str, Any]]) -> bool:
    """Was the last thing the router did in this run a deferral that nothing came after?"""
    for event in reversed(events):
        if event.get("kind") == "deferred":
            return True
        if event.get("kind") in ("call", "wait"):
            return False
    return False


def steps_of(output: Any) -> dict[str, int]:
    """Requests per skill step, from the skill's own usage table (research-report); {} when it has none."""
    usage = output.get("usage") if isinstance(output, dict) else None
    if not isinstance(usage, dict):
        return {}
    return {str(step): int(row.get("requests", 0)) for step, row in usage.items() if isinstance(row, dict)}


def router_build(answered: list[dict[str, Any]]) -> str:
    """current | old | unknown, from what the router said on the calls that came back.

    current: it sent one of the newer headers (x-muse-reasoning, x-muse-min-output, x-muse-answer).
    old: an empty answer came back without x-muse-answer, or a gpt-oss deployment answered a light or everyday call
    with neither x-muse-reasoning nor x-muse-min-output (a current build sets one of them there).
    unknown: nothing in these calls tells the two apart (e.g. every answer came from a model without a floor).
    """
    if any(e.get("reasoning") or e.get("min_output") or e.get("flagged") for e in answered):
        return "current"
    for e in answered:
        if e.get("empty") and not e.get("flagged"):
            return "old"
        if "gpt-oss" in str(e.get("deployment", "")) and e.get("task") in ("light", "everyday"):
            return "old"
    return "unknown"


def failure_entry(task: Mapping[str, Any], repeat: int, record: Mapping[str, Any], output: Any) -> dict[str, Any]:
    """What --keep-failures writes for one wrong or failed run: the answer, the skill's own quotes, the missing forms.

    Never a passage, a prompt or a file's text beyond what the skill quoted in its claims.
    """
    claims = []
    for c in (output.get("claims") if isinstance(output, dict) else None) or []:
        if isinstance(c, dict):
            claims.append({"id": c.get("id"), "says": c.get("says", c.get("text", "")), "quote": c.get("quote", ""),
                           "passage": c.get("n"), "source": (c.get("sources") or [c.get("source", "")])[0]})
    text = answer_text(output)
    missing = []
    for number, fact in enumerate(task["expect"].get("facts", []), start=1):
        if not fact_in(fact, text):
            kind = next((k for k in FACT_KINDS if k in fact), "?")
            missing.append({"fact": number, "kind": kind, "accepted": accepted_forms(fact), "source": sources_of(fact)})
    scored = record.get("independent") or {}
    return {"skill": task["skill"], "task": task["id"], "repeat": repeat, "outcome": record["outcome"],
            "status": record["status"], "own_check": record["own_check"], "right": bool(scored.get("pass")),
            "sources_ok": scored.get("sources_ok"), "not_found": bool(isinstance(output, dict) and output.get("not_found")),
            "expected_not_found": task["expect"].get("not_found") is True, "answer": text, "claims": claims,
            "missing_facts": missing, "check_reasons": record["check_reasons"], "empty_answers": record["empty_answers"],
            "recoveries": record["recoveries"]}


def append_failure(path: Path, entry: Mapping[str, Any]) -> None:
    """One JSON line, in a file only its owner can read (0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):
        pass
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def record_of(task: Mapping[str, Any], repeat: int, result: Any, seconds: float, keep: bool) -> dict[str, Any]:
    """One run as plain facts: no document text, no prompt, no answer (unless `keep`)."""
    events = list(result.events or [])
    used = result.used or {}
    calls = [e for e in events if e.get("kind") == "call"]
    deferrals = [e for e in events if e.get("kind") == "deferred"]
    own = (result.check or {}).get("status", "not-run")
    reasons = [reason_code(r) for r in (result.check or {}).get("reasons", [])]
    unreachable = any(e.get("status") == 0 and not e.get("ok") for e in calls)
    failure = ""
    if result.status == "needs_choice" and ended_by_deferral(events):
        outcome = "deferred"
    elif result.status == "failed" and unreachable:
        outcome = "no-router"
    elif result.status in ("done", "partial") and own == "passed":
        outcome = "passed"
    else:
        outcome = "failed"
        if own == "failed":
            failure = "check-failed"
        elif result.refusal:
            failure = "privacy-refused"
        elif any(e.get("kind") == "error" for e in events):
            failure = "skill-error"
        elif any("budget of" in note for note in result.notes or []):
            failure = "budget"
        elif any(not e.get("ok") for e in calls):
            failure = "router-error"
        elif not events:
            failure = "not-ready"
        else:
            failure = "stopped"
    scored = score(task, result.output) if outcome in ("passed", "failed") else None
    answered = [e for e in calls if e.get("ok") and e.get("task") != "embed"]
    skill_events = [e for e in events if str(e.get("kind", "")).startswith("skill.")]
    per_call = [{"provider": str(e.get("provider", "") or ""), "task": str(e.get("task", "") or ""), "ms": int(e.get("ms") or 0),
                 "status": int(e.get("status") or 0), "empty": bool(e.get("empty"))} for e in calls]
    record: dict[str, Any] = {
        "skill": task["skill"], "task": task["id"], "kind": task["kind"], "lang": task["lang"], "repeat": repeat,
        "outcome": outcome, "status": result.status, "own_check": own, "partial": result.status == "partial",
        "failure": failure, "check_reasons": {code: reasons.count(code) for code in sorted(set(reasons))},
        "independent": scored,
        "requests": int(used.get("requests", 0)), "tokens": int(used.get("tokens", 0)), "cost_usd": float(used.get("cost_usd", 0.0)),
        "providers": {name: int(row.get("requests", 0)) for name, row in (used.get("providers") or {}).items()},
        "deferrals": len(deferrals),
        "offered": ["+".join(o.get("choice", "?") for o in e.get("options") or []) or "none" for e in deferrals],
        "options": [o.get("choice", "?") for o in result.options or []] if outcome == "deferred" else [],
        "waits": sum(1 for e in events if e.get("kind") == "wait"),
        "empty_answers": sum(1 for e in answered if e.get("empty") or e.get("answer") == "empty-reasoning"),
        "recoveries": sum(1 for e in skill_events if e.get("recovered") is True),
        "unanswered": sum(1 for e in skill_events if e.get("unanswered") is True),
        "steps": steps_of(result.output),
        "calls": per_call,
        "slowest": sorted(per_call, key=lambda c: -c["ms"])[:SLOWEST],
        "call_ms": sum(c["ms"] for c in per_call),
        "wait_seconds": round(sum(float(e.get("seconds") or 0) for e in events if e.get("kind") == "wait"), 2),
        "router": router_build(answered),
        "seconds": round(seconds, 2),
    }
    if keep:
        record["kept"] = {"text": answer_text(result.output), "notes": list(result.notes or []),
                          "reasons": list((result.check or {}).get("reasons", []))}
    return record


# ---------------------------------------------------------------- summary


def _rate(passed: int, total: int) -> float | None:
    return round(passed / total, 4) if total else None


def _interval(passed: int, total: int) -> list[float] | None:
    got = wilson(passed, total)
    return [round(got[0], 4), round(got[1], 4)] if got else None


def summarise(records: list[dict[str, Any]], planned: int) -> dict[str, Any]:
    completed = [r for r in records if r["outcome"] in ("passed", "failed")]
    deferred = [r for r in records if r["outcome"] == "deferred"]
    own = [r for r in completed if r["outcome"] == "passed"]
    right = [r for r in completed if r["independent"] and r["independent"]["pass"]]
    both = [r for r in own if r["independent"] and r["independent"]["pass"]]
    failures: dict[str, int] = {}
    reasons: dict[str, int] = {}
    providers: dict[str, int] = {}
    offered: dict[str, int] = {}
    for r in records:
        if r["failure"]:
            failures[r["failure"]] = failures.get(r["failure"], 0) + 1
        for code, n in r["check_reasons"].items():
            if r["outcome"] == "failed":
                reasons[code] = reasons.get(code, 0) + n
        for name, n in r["providers"].items():
            providers[name] = providers.get(name, 0) + n
        for item in r["offered"]:
            offered[item] = offered.get(item, 0) + 1

    def group(key: str) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in completed:
            row = out.setdefault(r[key], {"completed": 0, "passed_own": 0, "passed_independent": 0})
            row["completed"] += 1
            row["passed_own"] += r["outcome"] == "passed"
            row["passed_independent"] += bool(r["independent"] and r["independent"]["pass"])
        return dict(sorted(out.items()))

    cost = round(sum(r["cost_usd"] for r in records), 6)
    n = len(completed)
    met = n > 0 and len(right) / n >= G1_RATE and cost == 0  # the gate counts runs that are RIGHT by the expected facts
    statuses: dict[str, int] = {}
    steps: dict[str, int] = {}
    routers: dict[str, int] = {}
    for r in records:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
        for step, count in (r.get("steps") or {}).items():
            steps[step] = steps.get(step, 0) + count
        routers[r.get("router", "unknown")] = routers.get(r.get("router", "unknown"), 0) + 1
    slow = sorted(({"run": r["task"], **c} for r in records for c in r.get("slowest") or []), key=lambda c: -c["ms"])[:5]
    by_empties: dict[str, dict[str, int]] = {}
    for r in completed:
        band = "0" if r["empty_answers"] == 0 else ("1-2" if r["empty_answers"] <= 2 else "3+")
        row = by_empties.setdefault(band, {"completed": 0, "right": 0})
        row["completed"] += 1
        row["right"] += bool(r["independent"] and r["independent"]["pass"])
    extra = {"statuses": dict(sorted(statuses.items())), "recoveries": sum(r.get("recoveries", 0) for r in records),
             "unanswered": sum(r.get("unanswered", 0) for r in records), "steps": dict(sorted(steps.items())),
             "routers": dict(sorted(routers.items())), "slowest_calls": slow, "right_by_empty_answers": dict(sorted(by_empties.items())),
             "call_ms": sum(r.get("call_ms", 0) for r in records), "wait_seconds": round(sum(r.get("wait_seconds", 0) for r in records), 2),
             "right_but_own_failed": sum(1 for r in completed if r["outcome"] != "passed" and r["independent"] and r["independent"]["pass"])}
    return {
        "planned": planned, "runs": len(records), "completed": n, "deferred": len(deferred),
        "no_router": sum(1 for r in records if r["outcome"] == "no-router"), "not_run": max(0, planned - len(records)),
        "passed_own": len(own), "passed_independent": len(right), "passed_both": len(both),
        "own_passed_but_wrong": len(own) - len(both), "partial": sum(1 for r in own if r["partial"]),
        "failed": n - len(own), "failures": dict(sorted(failures.items())), "check_reasons": dict(sorted(reasons.items())),
        "rate_own": _rate(len(own), n), "interval_own": _interval(len(own), n),
        "rate_independent": _rate(len(right), n), "interval_independent": _interval(len(right), n),
        "requests": sum(r["requests"] for r in records), "tokens": sum(r["tokens"] for r in records), "cost_usd": cost,
        "providers": dict(sorted(providers.items())), "deferrals": sum(r["deferrals"] for r in records),
        "offered": dict(sorted(offered.items())), "empty_answers": sum(r["empty_answers"] for r in records),
        "seconds": round(sum(r["seconds"] for r in records), 1), "by_kind": group("kind"), "by_lang": group("lang"),
        "g1": {"met": met, "completed": n, "enough_runs": n >= G1_RUNS, "nothing_spent": cost == 0, "counts": "right",
               "own_check_would_say": n > 0 and len(own) / n >= G1_RATE and cost == 0},
        **extra,
    }


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate * 100:.1f}%"


def _span(interval: list[float] | None) -> str:
    return "" if not interval else f"  (95% interval {interval[0] * 100:.1f}% to {interval[1] * 100:.1f}%)"


def g1_line(summary: Mapping[str, Any]) -> str:
    g1 = summary["g1"]
    line = (f"G1: at least {G1_RATE * 100:.0f}% of completed runs RIGHT by the expected facts with nothing spent: "
            f"{'yes' if g1['met'] else 'no'} ({summary['passed_independent']} of {g1['completed']}")
    line += ")" if g1["nothing_spent"] else f"; US${summary['cost_usd']:.6f} WAS SPENT)"
    if not g1["enough_runs"]:
        line += f" - only {g1['completed']} completed runs; the gate asks for {G1_RUNS} per skill"
    return line


def own_line(summary: Mapping[str, Any]) -> str:
    """The own-check rate, second, with the gap to the right-rate called out."""
    own, right = summary["rate_own"], summary["rate_independent"]
    line = f"Own check (second line, not the gate): {summary['passed_own']} of {summary['completed']} passed ({_pct(own)})"
    if own is not None and right is not None:
        gap = own - right
        if gap > 0:
            line += f"; it overstates the right-rate by {gap * 100:.1f} points"
        line += (f"; passed but wrong {summary['own_passed_but_wrong']}, right but failed its own check "
                 f"{summary.get('right_but_own_failed', 0)}")
    if summary["g1"].get("own_check_would_say") and not summary["g1"]["met"]:
        line += ". By the own check alone this skill would have looked like it met the line; it did not."
    return line


def render(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for name, entry in report["skills"].items():
        s = entry.get("summary")
        if not s:
            continue
        n = s["completed"]
        lines.append(f"{name} {entry.get('version', '')}: {s['planned']} planned, {n} completed, {s['deferred']} deferred, "
                     f"{s['not_run'] + s['no_router']} not run")
        lines.append(f"  right by expected facts  {s['passed_independent']:>4} of {n}  {_pct(s['rate_independent'])}{_span(s['interval_independent'])}")
        lines.append(f"  passed own check         {s['passed_own']:>4} of {n}  {_pct(s['rate_own'])}{_span(s['interval_own'])}")
        lines.append(f"  both                     {s['passed_both']:>4} of {n}")
        if s.get("statuses"):
            lines.append("  statuses: " + ", ".join(f"{k} {v}" for k, v in s["statuses"].items())
                         + f"; empty answers {s['empty_answers']}, recovered {s.get('recoveries', 0)}, "
                           f"still unanswered {s.get('unanswered', 0)}")
        if s.get("right_by_empty_answers"):
            lines.append("  right by empty answers in the run: "
                         + ", ".join(f"{k} empties {v['right']}/{v['completed']}" for k, v in s["right_by_empty_answers"].items()))
        if s.get("steps"):
            lines.append("  requests per step: " + ", ".join(f"{k} {v}" for k, v in s["steps"].items()))
        if s.get("slowest_calls"):
            lines.append("  slowest calls: " + ", ".join(f"{c['run']} {c['task']} by {c['provider'] or '?'} {c['ms']} ms"
                                                          for c in s["slowest_calls"][:3])
                         + f" (call time {s.get('call_ms', 0) / 1000:.0f} s in all, waits for Retry-After {s.get('wait_seconds', 0):.0f} s;"
                           " a long call includes the router's own retry and fallbacks inside that one request)")
        if s.get("routers"):
            lines.append("  router build seen: " + ", ".join(f"{k} {v}" for k, v in s["routers"].items()))
        if s["partial"]:
            lines.append(f"  of the passes, {s['partial']} were partial (something could not be done; the notes said what)")
        if s["failed"]:
            why = ", ".join(f"{k} {v}" for k, v in s["failures"].items())
            detail = ", ".join(f"{k} {v}" for k, v in s["check_reasons"].items())
            lines.append(f"  failed                   {s['failed']:>4}: {why}" + (f"  [check reasons: {detail}]" if detail else ""))
        if s["deferred"] or s["deferrals"]:
            offered = ", ".join(f"{k} x{v}" for k, v in s["offered"].items()) or "nothing"
            lines.append(f"  deferred                 {s['deferred']:>4}  (reported apart: neither passes nor failures; --resume tries them again)"
                         f"  [{s['deferrals']} deferrals in all; offered: {offered}]")
        answered = ", ".join(f"{k} {v}" for k, v in s["providers"].items()) or "nobody"
        lines.append(f"  used {s['requests']} requests, {s['tokens']} tokens, US${s['cost_usd']:.6f}, {s['seconds']:.0f} s; answered by: {answered}"
                     + (f"; {s['empty_answers']} empty answers (reasoning used the whole output)" if s["empty_answers"] else ""))
        if s["by_kind"]:
            lines.append("  own check by kind: " + ", ".join(f"{k} {v['passed_own']}/{v['completed']}" for k, v in s["by_kind"].items()))
            lines.append("  own check by language: " + ", ".join(f"{k} {v['passed_own']}/{v['completed']}" for k, v in s["by_lang"].items()))
        if entry.get("stopped"):
            lines.append(f"  stopped early: {entry['stopped']}")
        lines.append("  " + g1_line(s))
        lines.append("  " + own_line(s))
        if s["own_passed_but_wrong"]:
            much = s["rate_own"] is not None and s["rate_independent"] is not None and s["rate_own"] - s["rate_independent"] >= 0.10
            lines.append(f"  {'CAVEAT' if much else 'Note'}: the skill's own check passed answers that were wrong "
                         f"{s['own_passed_but_wrong']} time(s) by the expected facts"
                         + ("; the own-check rate overstates how often it is right." if much else "."))
        lines.append("")
    for item in report.get("skipped", []):
        lines.append(f"{item['skill']}: skipped - {item['reason']}")
    router = report.get("router") or {}
    if router:
        lines.append(f"Router build: {router.get('build', 'unknown')}"
                     + (" - " + OLD_ROUTER_WARNING if router.get("build") == "old" else
                        " (no call in this batch could tell an old router from a current one)" if router.get("build") == "unknown" else ""))
    cap = (report.get("options") or {}).get("max_requests")
    if cap is not None:
        lines.append(f"Requests used: {report.get('requests_used', 0)} of the cap of {cap}"
                     + (" (stopped at the cap)" if report.get("stopped") == "request-cap" else ""))
    spans = report.get("allowance") or {}
    if spans.get("start") is not None or spans.get("end") is not None:
        lines.append("Free allowance at the start: " + allowance_line(spans.get("start")))
        lines.append("Free allowance at the end:   " + allowance_line(spans.get("end")))
        gone = sorted(n for n, row in (spans.get("end") or {}).items()
                      if row.get("state") in ("exhausted", "cooling") and ((spans.get("start") or {}).get(n) or {}).get("state") == "ok")
        if gone:
            lines.append("  used up or cooling during this batch: " + ", ".join(gone)
                         + ". Runs after that point had fewer providers; compare them with care.")
    overall = report.get("verdict", {})
    if overall:
        lines.append("")
        lines.append(f"Overall G1 line (right by expected facts, every skill that ran): {'yes' if overall['met'] else 'no'}"
                     + ("" if overall["enough_runs"] else f" - not every skill has {G1_RUNS} completed runs yet")
                     + f". Spent: US${overall['cost_usd']:.6f}.")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- files


def write_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_csv(path: Path, report: Mapping[str, Any], record: Mapping[str, Any], version: str, set_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    scored = record.get("independent") or {}
    row = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run_id": report["run_id"], "set": set_id,
        "skill": record["skill"], "version": version, "task": record["task"], "kind": record["kind"], "lang": record["lang"],
        "repeat": record["repeat"], "privacy": report["privacy"], "outcome": record["outcome"], "status": record["status"],
        "own_check": record["own_check"], "independent": "" if not scored else ("pass" if scored["pass"] else "fail"),
        "facts_found": scored.get("facts_found", ""), "facts": scored.get("facts", ""), "requests": record["requests"],
        "tokens": record["tokens"], "cost_usd": f"{record['cost_usd']:.6f}",
        "providers": "+".join(sorted(record["providers"])), "deferrals": record["deferrals"],
        "empty_answers": record["empty_answers"], "seconds": record["seconds"], "failure": record["failure"],
        "recoveries": record.get("recoveries", 0), "unanswered": record.get("unanswered", 0),
        "slowest_ms": max([c["ms"] for c in record.get("slowest") or []] or [0]), "router": record.get("router", ""),
    }
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def allowance(env: Mapping[str, str]) -> dict[str, Any] | None:
    """The day's allowance per connected provider, from the local ledger (what `muse status` shows): counts only.

    {provider: {"state", "requests_today", "tokens_today", "remaining_today": {metric: n}, "source"}}. Nothing is sent
    to a provider. None when the status cannot be read here (no ledger, another machine).
    """
    try:
        from wardmuse import status

        snap = status.build_snapshot(environ=dict(env))
    except Exception:
        return None
    out: dict[str, Any] = {}
    for p in snap.get("providers", []):
        if not p.get("configured"):
            continue
        day = (p.get("usage") or {}).get("day") or {}
        remaining = {str(lim.get("metric")): lim.get("remaining") for lim in p.get("limits") or []
                     if lim.get("window") == "day" and lim.get("remaining") is not None}
        out[str(p["id"])] = {"state": p.get("state"), "requests_today": int(day.get("requests") or 0),
                             "tokens_today": int(day.get("tokens") or 0), "remaining_today": remaining,
                             "blocked_until": p.get("blocked_until")}
    return out


def allowance_line(snapshot: Mapping[str, Any] | None) -> str:
    if not snapshot:
        return "not known here"
    parts = []
    for name, row in sorted(snapshot.items()):
        left = ", ".join(f"{v} {k} left" for k, v in row.get("remaining_today", {}).items())
        parts.append(f"{name} {row.get('state')} ({row.get('requests_today', 0)} requests today" + (f"; {left}" if left else "") + ")")
    return "; ".join(parts) or "no connected provider"


def merged_env(environ: Mapping[str, str] | None) -> dict[str, str]:
    """Environment, .env and key store merged the way the other real-provider scripts do it."""
    try:
        from wardmuse import keys

        return dict(keys.export_env(environ))
    except Exception:
        return dict(os.environ if environ is None else environ)


# ---------------------------------------------------------------- the run


def key_of(task: Mapping[str, Any], repeat: int) -> str:
    return f"{task['skill']}/{task['id']}#{repeat}"


def run(args: argparse.Namespace, env: dict[str, str]) -> int:
    from wardmuse import __version__, paths
    from wardmuse.skills import loader, runner

    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else TASKS_DIR
    found, problems = load_tasks(tasks_dir)
    if problems:
        print("The task files have problems; nothing was sent:", file=sys.stderr)
        for problem in problems[:40]:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_USAGE
    if args.check_tasks:
        for name, tasks in found.items():
            kinds: dict[str, int] = {}
            for task in tasks:
                kinds[task["kind"]] = kinds.get(task["kind"], 0) + 1
            print(f"{name}: {len(tasks)} tasks ok (" + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) + ")")
        return EXIT_OK
    only = {name.strip() for name in (args.only or "").split(",") if name.strip()}
    kinds_wanted = {kind.strip() for kind in (args.only_kinds or "").split(",") if kind.strip()}
    if kinds_wanted:
        known_kinds = {t["kind"] for tasks in found.values() for t in tasks}
        if kinds_wanted - known_kinds:
            print(f"No task of kind: {', '.join(sorted(kinds_wanted - known_kinds))} (have: {', '.join(sorted(known_kinds))})",
                  file=sys.stderr)
            return EXIT_USAGE
        found = {name: [t for t in tasks if t["kind"] in kinds_wanted] for name, tasks in found.items()}
    unknown = only - set(found)
    if unknown:
        print(f"No task file for: {', '.join(sorted(unknown))} (have: {', '.join(sorted(found)) or 'none'})", file=sys.stderr)
        return EXIT_USAGE

    report_path = Path(args.report) if args.report else paths.home(env) / "chores-report.json"
    csv_path = Path(args.csv) if args.csv else paths.home(env) / "chores-runs.csv"
    previous: dict[str, Any] = {}
    if args.resume and report_path.is_file():
        try:
            previous = json.loads(report_path.read_text(encoding="utf-8")).get("runs", {})
        except (ValueError, AttributeError):
            previous = {}
    kept = {key: record for key, record in previous.items() if record.get("outcome") in ("passed", "failed")}

    report: dict[str, Any] = {
        "version": 1, "tool": "chores_check", "muse_version": __version__,
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "finished": None,
        "privacy": args.privacy, "paid_usd": 0, "app": "skills",
        "options": {"runs": args.runs, "sample": args.sample, "seed": args.seed, "only": sorted(only), "delay": args.delay,
                    "max_wait": args.max_wait, "resume": bool(args.resume), "keep_outputs": bool(args.keep_outputs),
                    "keep_failures": bool(args.keep_failures), "only_kinds": sorted(kinds_wanted), "max_requests": args.max_requests,
                    "replay": args.replay, "record": args.record},
        "skills": {}, "skipped": [], "runs": dict(kept), "stopped": None, "verdict": {},
        "router": {"build": "unknown", "warned": False}, "allowance": {"start": allowance(env), "end": None},
        "requests_used": 0,
    }
    failures_path = (Path(args.report).parent if args.report else paths.home(env)) / FAILURES_FILE

    # ---- which skills can run, and the plan
    work: dict[str, list[tuple[dict[str, Any], int]]] = {}
    planned: dict[str, int] = {}
    for name, tasks in found.items():
        if only and name not in only:
            continue
        try:
            skill = loader.find(name, env)
            loader.require_ready(skill)
        except loader.LoaderError as exc:
            why = "this skill is not installed here" if "no skill named" in str(exc) else "this skill is not ready to run here"
            report["skipped"].append({"skill": name, "reason": f"{why}, so its {len(tasks)} tasks were not run"})
            continue
        if not tasks:
            report["skipped"].append({"skill": name, "reason": "its task file is empty"})
            continue
        chosen = choose(tasks, args.sample, args.seed)
        every = [(task, repeat) for task in chosen for repeat in range(1, args.runs + 1)]
        work[name] = [(task, repeat) for task, repeat in every if key_of(task, repeat) not in kept]
        planned[name] = len(every)
        report["skills"][name] = {
            "version": skill.version, "tasks": len(chosen), "set": set_hash(chosen, tasks_dir),
            "budget": {"requests": skill.budget.requests, "tokens": skill.budget.tokens, "seconds": skill.budget.seconds}}

    replay_proxy = None
    record_post = None
    if args.replay:
        from wardmuse import cassette

        replay_dir = Path(args.replay)
        if not replay_dir.is_dir():
            print(f"No cassette directory: {replay_dir}", file=sys.stderr)
            return EXIT_USAGE
        try:
            replay_proxy = cassette.Proxy("replay", replay_dir).start()
        except OSError as exc:
            print(f"The replay proxy did not start ({type(exc).__name__}).", file=sys.stderr)
            return EXIT_USAGE
        env = dict(env)
        env["MUSE_PROXY_URL"] = replay_proxy.origin
        env["MUSE_PROVIDER_MODE"] = "replay"
        env["MUSE_CASSETTE_DIR"] = str(replay_dir)
    if args.record:
        from wardmuse import cassette
        from wardmuse.skills.llm import http_post

        record_dir = Path(args.record)
        store = cassette.Store(record_dir)

        def record_post(url: str, headers: Mapping[str, str], body: bytes, timeout: float, _post=http_post, _store=store):  # type: ignore[no-redef]
            started = time.perf_counter()
            reply = _post(url, headers, body, timeout)
            if reply.status != 0:
                path = url.split("://", 1)[-1]
                path = "/" + path.split("/", 1)[1] if "/" in path else "/"
                _store.save_call("POST", path, body, reply.status, reply.headers, reply.body, int((time.perf_counter() - started) * 1000))
            return reply

    def _close(code: int) -> int:
        if replay_proxy is not None:
            replay_proxy.stop()
        return code

    if args.replay:
        print(f"Muse chores check: replaying cassettes from {args.replay}")
        print("  no provider is called, and nothing is added to a free-tier quota")
    else:
        print("Muse chores check: what this will send")
    print(f"  router: {runner.proxy_url(env)}   app: skills (never paid)   privacy: {args.privacy}")
    if args.record:
        print(f"  recording each router exchange to {args.record} (request fingerprints and responses; no API key, no auth header)")
    total_runs = total_requests = total_tokens = 0
    for name, todo in work.items():
        budget = report["skills"][name]["budget"]
        requests, tokens = len(todo) * budget["requests"], len(todo) * budget["tokens"]
        total_runs, total_requests, total_tokens = total_runs + len(todo), total_requests + requests, total_tokens + tokens
        done = planned[name] - len(todo)
        print(f"  {name:<20} {len(todo):>4} runs ({report['skills'][name]['tasks']} tasks x {args.runs})"
              + (f", {done} already done" if done else "")
              + f": at most {requests} requests and {tokens} tokens ({budget['requests']} and {budget['tokens']} a run, the skill's own budget)")
    for item in report["skipped"]:
        print(f"  {item['skill']:<20} skipped: {item['reason']}")
    print(f"  worst case in all: {total_runs} runs, {total_requests} requests, {total_tokens} tokens. These are ceilings (each skill's own budget), not estimates.")
    print(f"  One run at a time, {args.delay:g} s apart. A Retry-After up to {args.max_wait:g} s is waited for; a longer one records the run as deferred.")
    print("  Paid providers are not used; the run stops if anything is charged.")
    print(f"  report: {report_path}" + ("  (WITH answers and check reasons: they hold text from the test documents)" if args.keep_outputs
                                         else "  (ids, counts and codes; no document text, prompts or answers)"))
    print(f"  one line per run is appended to: {csv_path}")
    if args.keep_failures:
        print(f"  wrong or failed runs are written to: {failures_path} (mode 0600: answers and the skills' own quotes, "
              "no passages)")
    if args.max_requests is not None:
        print(f"  request cap: {args.max_requests}. A run is not started when its budget could take the total over the cap.")
    start = report["allowance"]["start"]
    if start:
        print("  free allowance today (from the local ledger): " + allowance_line(start))
    if total_runs == 0:
        print("Nothing to run.")
        return _close(finish(report, report_path, planned, found, {}, env))
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not a terminal: pass --yes to confirm.", file=sys.stderr)
            return _close(EXIT_USAGE)
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was sent.")
            return _close(EXIT_OK)
    print()

    # ---- the runs: one at a time
    first = True
    for name, todo in work.items():
        entry = report["skills"][name]
        deferred_streak = unreachable_streak = 0
        for index, (task, repeat) in enumerate(todo, start=1):
            if args.max_requests is not None and report["requests_used"] + entry["budget"]["requests"] > args.max_requests:
                report["stopped"] = "request-cap"
                print(f"  stopped: {report['requests_used']} of the {args.max_requests}-request cap used; the next run could "
                      f"take up to {entry['budget']['requests']} more. --resume continues later.")
                return _close(finish(report, report_path, planned, found, work, env))
            if not first and args.delay > 0:
                _sleep(args.delay)
            first = False
            folders = [tasks_dir / FIXTURES / folder for folder in task.get("folders", [])]
            began = time.monotonic()
            result = runner.run(name, task["inputs"], folders=folders, privacy=args.privacy, budget={"paid_usd": 0},
                                environ=env, wait_up_to=args.max_wait, **({"post": record_post} if record_post else {}))
            record = record_of(task, repeat, result, time.monotonic() - began, args.keep_outputs)
            report["runs"][key_of(task, repeat)] = record
            report["requests_used"] += record["requests"]
            append_csv(csv_path, report, record, entry["version"], entry["set"])
            if record["router"] != "unknown" and report["router"]["build"] == "unknown":
                report["router"]["build"] = record["router"]
                if record["router"] == "old":
                    report["router"]["warned"] = True
                    print("\n" + "!" * 78, file=sys.stderr)
                    print("!! " + OLD_ROUTER_WARNING, file=sys.stderr)
                    print("!" * 78 + "\n", file=sys.stderr)
            if args.keep_failures and record["outcome"] in ("passed", "failed") and not (record["independent"] or {}).get("pass"):
                append_failure(failures_path, failure_entry(task, repeat, record, result.output))
            elif args.keep_failures and record["outcome"] == "failed":
                append_failure(failures_path, failure_entry(task, repeat, record, result.output))
            if not args.quiet:
                scored = record["independent"]
                facts = "" if not scored else f", facts {scored['facts_found']}/{scored['facts']}" + ("" if scored["pass"] else " WRONG")
                print(f"  {name:<18} {index:>3}/{len(todo)}  {task['id']:<10} {record['outcome']:<9}"
                      f" own check {record['own_check']}{facts}, {record['requests']} requests"
                      + (f", {record['failure']}" if record["failure"] else ""))
            if record["cost_usd"] > 0:
                report["stopped"] = "spend"
                write_report(report, report_path)
                print("\n" + "!" * 78, file=sys.stderr)
                print(f"!! STOPPED: the router reported a cost of US${record['cost_usd']:.6f} for {name} {task['id']}.", file=sys.stderr)
                print("!! This check must cost nothing. No further run was started. Look at `muse status` and the", file=sys.stderr)
                print("!! `skills` app (it must not be allowed to pay) before running this again.", file=sys.stderr)
                print("!" * 78, file=sys.stderr)
                finish(report, report_path, planned, found, work, env)
                return _close(EXIT_SPENT)
            deferred_streak = deferred_streak + 1 if record["outcome"] == "deferred" else 0
            unreachable_streak = unreachable_streak + 1 if record["outcome"] == "no-router" else 0
            if unreachable_streak >= UNREACHABLE_STOP:
                report["stopped"] = "no-router"
                print("\nThe router could not be reached. Start it with `muse up`, then run this again with --resume.", file=sys.stderr)
                finish(report, report_path, planned, found, work, env)
                return _close(EXIT_NOT_MET)
            if deferred_streak >= args.stop_after:
                entry["stopped"] = f"{deferred_streak} runs in a row were deferred; the free allowance for this skill is used up for now"
                print(f"  {name}: stopped after {deferred_streak} deferred runs in a row; --resume continues it later.")
                break
            if index % 5 == 0:
                write_report(report, report_path)
    return _close(finish(report, report_path, planned, found, work, env))


def finish(report: dict[str, Any], path: Path, planned: Mapping[str, int], found: Mapping[str, list[dict[str, Any]]],
           work: Mapping[str, Any], env: Mapping[str, str] | None = None) -> int:
    if env is not None and "allowance" in report:
        report["allowance"]["end"] = allowance(env)
    for name, entry in report["skills"].items():
        records = [r for r in report["runs"].values() if r["skill"] == name]
        entry["summary"] = summarise(records, planned.get(name, len(records)))
    summaries = [e["summary"] for e in report["skills"].values()]
    report["verdict"] = {
        "met": bool(summaries) and all(s["g1"]["met"] for s in summaries),
        "enough_runs": bool(summaries) and all(s["g1"]["enough_runs"] for s in summaries),
        "cost_usd": round(sum(s["cost_usd"] for s in summaries), 6),
        "per_skill": {name: e["summary"]["g1"] for name, e in report["skills"].items()},
    }
    report["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_report(report, path)
    print()
    print(render(report), end="")
    print(f"Report: {path}")
    if report["stopped"] == "spend":
        return EXIT_SPENT
    return EXIT_OK if report["verdict"]["met"] else EXIT_NOT_MET


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run each shipped skill many times on free allowances and report how often it passes (gate G1).")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--only", metavar="SKILLS", help="comma-separated skill names (default: every skill with a task file)")
    parser.add_argument("--runs", type=int, default=1, metavar="N", help="times each task is run (default 1)")
    parser.add_argument("--sample", type=int, metavar="K", help="a random K tasks per skill, spread over the kinds")
    parser.add_argument("--seed", type=int, default=0, help="seed for --sample (default 0)")
    parser.add_argument("--resume", action="store_true", help="continue the report at --report: runs that passed or failed are kept, the rest are run")
    parser.add_argument("--privacy", default="standard", choices=("public", "standard", "private", "local"),
                        help="the privacy class of every run (default standard; the test documents are fictional)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"seconds between runs (default {DEFAULT_DELAY:g})")
    parser.add_argument("--max-wait", type=float, default=DEFAULT_MAX_WAIT,
                        help=f"longest Retry-After to sit out, in seconds; a longer one records the run as deferred (default {DEFAULT_MAX_WAIT:g})")
    parser.add_argument("--stop-after", type=int, default=DEFAULT_STOP_AFTER,
                        help=f"stop a skill after this many deferred runs in a row (default {DEFAULT_STOP_AFTER})")
    parser.add_argument("--report", metavar="PATH", help="default: $MUSE_HOME/chores-report.json")
    parser.add_argument("--csv", metavar="PATH", help="default: $MUSE_HOME/chores-runs.csv (one line per run, appended)")
    parser.add_argument("--keep-outputs", action="store_true", help="store answers, notes and check reasons in the report (default: none of them)")
    parser.add_argument("--keep-failures", action="store_true",
                        help=f"write each wrong or failed run's answer, its claims (quotes) and the missing fact forms to {FAILURES_FILE} (mode 0600)")
    parser.add_argument("--only-kinds", metavar="KINDS", help="comma-separated task kinds (lookup, date, multi-hop, ...); default every kind")
    parser.add_argument("--max-requests", type=int, metavar="N",
                        help="a cap on requests for the whole batch, well below the day's free allowance; a run that could cross it is not started")
    parser.add_argument("--tasks-dir", metavar="DIR", help="read task files and fixtures from here instead of evals/chores")
    parser.add_argument("--check-tasks", action="store_true", help="only validate the task files against the fixtures; sends nothing")
    parser.add_argument("--quiet", action="store_true", help="no line per run")
    parser.add_argument("--replay", metavar="DIR", help="answer every model call from cassettes in DIR; no provider and no router is called")
    parser.add_argument("--record", metavar="DIR", help="write a cassette for each router exchange into DIR, then continue against the live router")
    return parser


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    if args.runs < 1 or (args.sample is not None and args.sample < 1) or args.delay < 0 or args.max_wait < 0 or args.stop_after < 1:
        print("--runs, --sample and --stop-after must be 1 or more; --delay and --max-wait 0 or more.", file=sys.stderr)
        return EXIT_USAGE
    if args.max_requests is not None and args.max_requests < 1:
        print("--max-requests must be 1 or more.", file=sys.stderr)
        return EXIT_USAGE
    if args.replay and args.record:
        print("Pass either --replay or --record, not both. Record first, then replay that directory.", file=sys.stderr)
        return EXIT_USAGE
    return run(args, merged_env(environ))


if __name__ == "__main__":
    raise SystemExit(main())
