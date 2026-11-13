"""Recurring chores: one sentence becomes a schedule, and a small store says what is due.

Three parts, all usable without a model or a network:

  understand   a sentence ("Every Monday 08:00, summarise the feeds in ~/feeds
               into Documents/Muse", 「每週一早上8點把 ~/feeds 的訂閱摘要存到
               ~/Documents/Muse」) is read by RULES into a `Spec`: how often, at what
               time, which template, which folders, where the result goes. What the
               rules cannot read comes back as a question, never as a guess. A model
               may PROPOSE the missing fields (`validate_proposal`); the proposal
               passes through the same validation and never supplies a path.
  next_after   the next time a spec is due, in wall-clock time of the chore's zone
               (MUSE_TZ, as rationing uses): 08:00 stays 08:00 across a clock
               change, a time that does not exist on the spring-forward day runs
               just after the gap, a time that happens twice runs once, and "the
               31st" runs on the last day of a shorter month.
  ChoreStore   MUSE_HOME/chores.sqlite (0600). `tick(now)` turns every due chore
               into exactly one queued run and moves its next_run forward, in one
               transaction, so two processes ticking at once cannot enqueue twice.

Running a queued run (the skill, the confirm card, the allowance hints) is in
muse/skills/chores.py. The database holds the person's sentence and folder
names, and counts about each run: never file contents, prompts or answers.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from muse import paths
from muse.ration import resolve_tz

SKILL = "recurring-chore"
FREQS = ("daily", "weekdays", "weekly", "monthly")
LANGS = ("en", "zh-TW")
MAX_SENTENCE = 500
MAX_SOURCES = 8
DEFAULT_TIME = "08:00"
LAST_DAY = -1

# What each template may use by default. Shown before the first run; a run that would exceed it stops with a partial result.
TEMPLATES: dict[str, dict[str, Any]] = {
    "summarise-feeds": {"budget": {"requests": 40, "tokens": 120_000, "seconds": 600.0, "paid_usd": 0.0}, "sources": True},
    "summarise-folder-changes": {"budget": {"requests": 20, "tokens": 60_000, "seconds": 300.0, "paid_usd": 0.0}, "sources": True},
    "daily-brief": {"budget": {"requests": 4, "tokens": 12_000, "seconds": 120.0, "paid_usd": 0.0}, "sources": False},
}

RUN_STATUSES = ("queued", "running", "waiting_card", "done", "partial", "failed", "needs_choice", "skipped", "declined", "expired")
RUN_OPEN = ("queued", "running")
RUN_OK = ("done", "partial", "waiting_card", "declined", "expired")  # the run produced its result; what the person did with it is separate
STALE_RUNNING_S = 15 * 60.0


class ChoreError(ValueError):
    """A user-fixable problem with a chore. The message never holds file contents."""


# ================================================================ the spec


@dataclass(frozen=True)
class Spec:
    freq: str  # daily | weekdays | weekly | monthly
    template: str
    time: str = DEFAULT_TIME  # "HH:MM" local; "" = any time of day (spare-allowance chores only)
    days: tuple[int, ...] = ()  # weekly: 0 = Monday ... 6 = Sunday
    day_of_month: int = 0  # monthly: 1..31, or -1 for the last day
    sources: tuple[str, ...] = ()  # folders the chore reads
    urls: tuple[str, ...] = ()  # web addresses named in the sentence: kept, not fetched (needs the sandbox)
    dest: str = ""  # the output folder; "" = Muse's own
    lang: str = "en"
    spare_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("days", "sources", "urls"):
            data[key] = list(data[key])
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Spec":
        problems = spec_problems(data)
        if problems:
            raise ChoreError("; ".join(problems))
        return cls(freq=data["freq"], template=data["template"], time=data.get("time", DEFAULT_TIME),
                   days=tuple(sorted(set(data.get("days") or ()))), day_of_month=int(data.get("day_of_month") or 0),
                   sources=tuple(data.get("sources") or ()), urls=tuple(data.get("urls") or ()), dest=str(data.get("dest") or ""),
                   lang=data.get("lang", "en"), spare_only=bool(data.get("spare_only", False)))


def valid_time(text: Any) -> bool:
    match = re.fullmatch(r"(\d\d):(\d\d)", text) if isinstance(text, str) else None
    return bool(match) and int(match.group(1)) <= 23 and int(match.group(2)) <= 59


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def spec_problems(data: Any) -> list[str]:
    """Why `data` is not a spec Muse will schedule; empty when it is. The one set of rules every spec passes through."""
    if not isinstance(data, Mapping):
        return ["the schedule is not an object"]
    out: list[str] = []
    freq, days, dom = data.get("freq"), data.get("days") or [], data.get("day_of_month") or 0
    if freq not in FREQS:
        out.append("how often it runs is not one of daily, weekdays, weekly, monthly")
    if data.get("template") not in TEMPLATES:
        out.append("the kind of chore is not one Muse has")
    spare = data.get("spare_only", False)
    if not isinstance(spare, bool):
        out.append("spare_only must be true or false")
    when = data.get("time", DEFAULT_TIME)
    if not (valid_time(when) or (when == "" and spare is True)):
        out.append("the time is not a time of day such as 08:00")
    if not isinstance(days, (list, tuple)) or not all(_is_int(d) and 0 <= d <= 6 for d in days):
        out.append("the days of the week must be numbers from 0 (Monday) to 6 (Sunday)")
    elif (freq == "weekly") != bool(days):
        out.append("a weekly chore needs its days of the week, and only a weekly chore has them")
    if not _is_int(dom) or not (dom == LAST_DAY or 0 <= dom <= 31):
        out.append("the day of the month must be 1 to 31, or -1 for the last day")
    elif (freq == "monthly") != (dom != 0):
        out.append("a monthly chore needs its day of the month, and only a monthly chore has one")
    for key, limit in (("sources", MAX_SOURCES), ("urls", 50)):
        value = data.get(key) or []
        if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v and len(v) <= 1000 for v in value) or len(value) > limit:
            out.append(f"{key} must be a short list of text")
    if not isinstance(data.get("dest") or "", str):
        out.append("the output folder must be text")
    if data.get("lang", "en") not in LANGS:
        out.append("the language must be en or zh-TW")
    return out


# ================================================================ reading a sentence (rules)

_EN_DAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
_EN_ABBR = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6}
_ZH_DAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_ZH_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_WEEK = r"(?:週|周|星期|禮拜)"
_ZH_NUM = r"(?:\d{1,2}|[零〇一二兩三四五六七八九十廿卅]{1,3})"
_ZH_AM = ("凌晨", "清晨", "早上", "早晨", "上午")
_ZH_PM = ("下午", "傍晚", "晚上", "晚間")
_ZH_PERIOD = r"(凌晨|清晨|早上|早晨|上午|中午|下午|傍晚|晚上|晚間|半夜|午夜)"

_URL = re.compile(r"https?://[^\s<>\"'，。、；「」（）()]+", re.I)
_QUOTED = re.compile(r"\"([^\"\n]{1,300})\"|'([^'\n]{1,300})'|「([^」\n]{1,300})」|“([^”\n]{1,300})”")
_PATH = re.compile(r"(?<![A-Za-z0-9_./~\-])(?:~/|\./|/)?[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?|(?<![A-Za-z0-9_./~\-])(?:~/|\./|/)[A-Za-z0-9_.\-]+/?|(?<![A-Za-z0-9_./~\-])~(?![A-Za-z0-9_])")
_NOT_PATHS = {"am/pm", "and/or", "a.m./p.m.", "a.m./p.m", "n/a", "24/7", "w/o", "either/or", "he/she", "yes/no"}
_DOMAIN = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}$", re.I)

_DEST_BEFORE = re.compile(
    r"(?:\b(?:in ?to|to|save[ds]? (?:it |them |the result |the digest |the summary )?in|"
    r"put (?:it |them |the result |the digest |the summary )?in|store[ds]? (?:it |them )?in|output (?:to|in)|write (?:it |them )?(?:to|in))"
    r"\s+(?:the |my )?(?:folder )?|"
    r"(?:存到|存在|存進|存入|存放到|放到|放在|放進|寫到|寫入|寫進|輸出到|儲存到|儲存在|儲存至|到|至|->|→)\s*)$")

_T: dict[str, dict[str, str]] = {
    "en": {
        "q_empty": "What should the chore do, and when? For example: Every Monday 08:00, summarise the feeds in ~/feeds into ~/Documents/Muse.",
        "q_too_long": "That sentence is too long. Say it in one or two lines.",
        "q_when": "How often should it run? Say every day, every weekday, a day of the week (every Monday), a day of the month "
                  "(every month on the 1st), or \"when there is allowance to spare\".",
        "q_interval": "Muse can repeat a chore every day, every weekday, on days of the week, or on a day of the month. "
                      "It cannot do \"every other\", hourly, twice a day or yearly yet. Which of those would do?",
        "q_one_off": "That reads like something to do once. A chore repeats: how often should it run?",
        "q_which_day": "Which day should it run on? For example \"every Monday\" or \"every month on the 1st\".",
        "q_conflict": "The sentence names more than one schedule. Which one do you mean?",
        "q_time": "What time of day? Use a time such as 08:00, 8am or 3:30pm.",
        "q_two_times": "The sentence names more than one time. A chore runs at one time of day: which one?",
        "q_what": "What should it do? Muse has three chores: summarise feeds saved in a folder, summarise what changed in a folder, "
                  "or write a daily brief of what Muse did.",
        "q_sources": "Which folder should it read? Name a folder, for example ~/feeds.",
        "q_folder_missing": "This folder does not exist: {path}. Which folder should it read?",
        "q_too_many": "That is more than {n} folders. Name at most {n}.",
        "what_feeds": "Summarise the feed files in {sources}",
        "what_changes": "Summarise what changed in {sources} since the last run",
        "what_brief": "Write a short brief of what Muse did the day before",
        "when_daily": "Every day at {time}",
        "when_weekdays": "Every weekday (Monday to Friday) at {time}",
        "when_weekly": "Every {days} at {time}",
        "when_monthly": "Every month on day {day} at {time}",
        "when_monthly_short": "Every month on day {day} at {time} (on the last day when the month is shorter)",
        "when_last": "On the last day of every month at {time}",
        "when_spare": "{when}, as soon after that as there is allowance to spare",
        "when_spare_any": "{when}, when there is allowance to spare",
        "strip_at": " at ",
        "tz": "{when} ({tz} time)",
        "where": "A new dated Markdown file in {dest}, saved only after you approve it",
        "where_default": "Muse's own folder ({path})",
        "budget": "up to {requests} requests, nothing paid",
        "budget_paid": "up to {requests} requests, at most US${paid} paid",
        "perm_read": "Read the files in {sources}",
        "perm_brief": "Read Muse's own counts of jobs, chores and cards on this home",
        "perm_write": "propose one new file in {dest} each run (each is saved only after you approve it)",
        "join": "; ",
        "list": ", ",
        "and": " and ",
        "days": "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday",
        "n_urls": "Muse cannot fetch web addresses yet: that needs the sandbox, which is not built. The {n} address(es) in your "
                  "sentence are kept with the chore and each run lists them as not read.",
        "n_feed_files": "In this version the feeds are read from files you save in that folder (RSS, Atom, JSON Feed, text or HTML).",
        "n_no_old": "Muse keeps no old copies of your files, so it describes each new or changed file as it is now.",
        "n_assumed_time": "No time was given, so {time} is assumed.",
        "n_assumed_weekly": "A day of the week without \"every\" is read as every week.",
        "n_assumed_what": "\"Summarise\" with a folder is read as: summarise what changed in it.",
        "n_spare": "It runs in the background and waits while your allowance is needed for what you are doing.",
        "n_model": "Part of this was read by a model, not by Muse's own rules. Check it before you confirm.",
    },
    "zh-TW": {
        "q_empty": "這件例行工作要做什麼？什麼時候做？例如：每週一早上8點，把 ~/feeds 裡的訂閱摘要存到 ~/Documents/Muse。",
        "q_too_long": "這句話太長了，請用一兩行說明。",
        "q_when": "多久執行一次？可以說每天、每個工作天、每週幾（每週一）、每月幾號（每月1號），或「有剩餘額度時」。",
        "q_interval": "Muse 可以每天、每個工作天、每週固定幾天，或每月固定一天執行；還不能「每隔一週」、每小時、一天兩次或每年。請問要改成哪一種？",
        "q_one_off": "這聽起來是只做一次的事。例行工作會重複執行：請問多久一次？",
        "q_which_day": "要在哪一天執行？例如「每週一」或「每月1號」。",
        "q_conflict": "這句話提到不只一種排程，請問是哪一種？",
        "q_time": "幾點執行？請用 08:00、早上8點、下午三點半 這樣的時間。",
        "q_two_times": "這句話提到不只一個時間。例行工作一天只在一個時間執行：請問是哪一個？",
        "q_what": "要做什麼？Muse 有三種例行工作：摘要某個資料夾裡存好的訂閱內容、摘要某個資料夾裡的變動，或寫一份 Muse 前一天做了什麼的每日簡報。",
        "q_sources": "要讀哪個資料夾？請指定一個資料夾，例如 ~/feeds。",
        "q_folder_missing": "這個資料夾不存在：{path}。請問要讀哪個資料夾？",
        "q_too_many": "超過 {n} 個資料夾了，最多 {n} 個。",
        "what_feeds": "摘要 {sources} 裡的訂閱檔案",
        "what_changes": "摘要 {sources} 從上次執行以來的變動",
        "what_brief": "寫一份 Muse 前一天做了什麼的簡報",
        "when_daily": "每天 {time}",
        "when_weekdays": "每個工作天（週一到週五）{time}",
        "when_weekly": "每{days} {time}",
        "when_monthly": "每月 {day} 號 {time}",
        "when_monthly_short": "每月 {day} 號 {time}（該月沒有這一天時改在最後一天）",
        "when_last": "每月最後一天 {time}",
        "when_spare": "{when}之後，一有剩餘額度就執行",
        "when_spare_any": "{when}，有剩餘額度時執行",
        "strip_at": " ",
        "tz": "{when}（{tz} 時間）",
        "where": "在 {dest} 新增一個帶日期的 Markdown 檔案，要你同意後才會存檔",
        "where_default": "Muse 自己的資料夾（{path}）",
        "budget": "最多 {requests} 次請求，不會付費",
        "budget_paid": "最多 {requests} 次請求，最多付費 US${paid}",
        "perm_read": "讀取 {sources} 裡的檔案",
        "perm_brief": "讀取這個 Muse 自己的工作、例行工作與確認卡的數量",
        "perm_write": "每次執行在 {dest} 提出一個新檔案（每個都要你同意後才會存檔）",
        "join": "；",
        "list": "、",
        "and": "、",
        "days": "週一,週二,週三,週四,週五,週六,週日",
        "n_urls": "Muse 目前還不能抓取網址：那需要尚未完成的沙盒。你句子裡的 {n} 個網址會跟著這件工作保存，每次執行都會列為「未讀取」。",
        "n_feed_files": "這個版本會從你存在該資料夾的檔案讀取訂閱內容（RSS、Atom、JSON Feed、文字或 HTML）。",
        "n_no_old": "Muse 不會保留你檔案的舊版本，所以只會描述每個新增或變動的檔案現在的內容。",
        "n_assumed_time": "沒有指定時間，所以先假設 {time}。",
        "n_assumed_weekly": "只提到星期幾、沒有說「每」，會當成每週。",
        "n_assumed_what": "「摘要」加上一個資料夾，會當成：摘要那個資料夾裡的變動。",
        "n_spare": "它在背景執行，你正在使用額度時會先等待。",
        "n_model": "其中一部分是由模型判讀的，不是 Muse 自己的規則。確認前請先檢查。",
    },
}


def tr(key: str, lang: str, **fmt: Any) -> str:
    text = (_T.get(lang) or _T["en"]).get(key) or _T["en"][key]
    return text.format(**fmt) if fmt else text


def _zh_number(text: str) -> int | None:
    """`8`, `八`, `十五`, `二十三`, `廿`, `卅一` as a number; None when it is not one."""
    if text.isdigit():
        return int(text)
    text = text.replace("廿", "二十").replace("卅", "三十")
    if not text or any(ch not in _ZH_DIGITS and ch != "十" for ch in text):
        return None
    if "十" not in text:
        return _ZH_DIGITS[text] if len(text) == 1 else None
    tens, _, ones = text.partition("十")
    if len(tens) > 1 or len(ones) > 1:
        return None
    return (_ZH_DIGITS[tens] if tens else 1) * 10 + (_ZH_DIGITS[ones] if ones else 0)


class _Text:
    """The sentence being read. `take` removes what a rule matched, so a later rule cannot read it again."""

    def __init__(self, text: str) -> None:
        self.s = text

    def take(self, pattern: str) -> list[re.Match[str]]:
        found = list(re.finditer(pattern, self.s))
        for match in reversed(found):
            self.s = self.s[:match.start()] + " " * (match.end() - match.start()) + self.s[match.end():]
        return found

    def has(self, pattern: str) -> bool:
        return re.search(pattern, self.s) is not None


def _expand(path: str) -> str:
    """A folder as the sentence named it, made absolute: `~` and bare names are under the home folder, `./` under the current one."""
    text = path.rstrip("/") or "/"
    if text.startswith("~"):
        return str(Path(text).expanduser())
    if text.startswith("./") or text == ".":
        return str(Path.cwd() / text[2:])
    if text.startswith("/"):
        return text
    return str(Path.home() / text)


def _mask_places(sentence: str) -> tuple[str, list[tuple[str, str]]]:
    """(the sentence with every address and folder replaced by a marker, [(kind, text)]). Kinds: url, path."""
    places: list[tuple[str, str]] = []

    def mark(kind: str, text: str) -> str:
        places.append((kind, text))
        return f" \x01{len(places) - 1}\x02 "

    def url(match: re.Match[str]) -> str:
        return mark("url", match.group(0).rstrip(".,;:!?"))

    def quoted(match: re.Match[str]) -> str:
        inner = next(g for g in match.groups() if g is not None).strip()
        if _URL.fullmatch(inner):
            return mark("url", inner)
        if "/" in inner or inner.startswith("~"):
            return mark("path", inner)
        return match.group(0)

    def path(match: re.Match[str]) -> str:
        text = match.group(0).rstrip(".,;:!?")
        tail = match.group(0)[len(text):]
        low = text.lower()
        if low in _NOT_PATHS or re.fullmatch(r"[\d/.\-]+", text):
            return match.group(0)  # a date or a fraction, not a folder
        first = text.split("/", 1)[0]
        if not text.startswith(("~", ".", "/")) and _DOMAIN.fullmatch(first):
            return mark("url", "https://" + text) + tail
        return mark("path", text) + tail

    masked = _URL.sub(url, sentence)
    masked = _QUOTED.sub(quoted, masked)
    masked = _PATH.sub(path, masked)
    return masked, places


def _frequency(t: _Text) -> dict[str, Any]:
    """Reads how often. Returns freq/days/day_of_month/spare plus `problems` (question codes) and `assumed`."""
    out: dict[str, Any] = {"freq": None, "days": (), "day_of_month": 0, "spare": False, "problems": [], "assumed": []}
    spare = t.take(r"\b(?:when(?:ever)?|if|while|once)\b[^.;,]{0,40}?\b(?:allowance|quota)\b(?:[^.;,]{0,25}?\b(?:to spare|spare|left ?over|left|unused|free|extra|remaining)\b)?"
                   r"|\b(?:with|using|on) (?:any |the |my )?(?:spare|left ?over|unused|extra|leftover|remaining) (?:allowance|quota)\b"
                   r"|\b(?:spare|left ?over|unused|leftover) (?:allowance|quota)\b"
                   r"|(?:當|等)?(?:有|還有)?(?:剩餘|剩下|多餘|多出來|空閒|閒置|用不完|沒用完)的?額度(?:的?時候|時)?"
                   r"|額度(?:還)?(?:有剩|有多|用不完|沒用完|有餘)(?:的?時候|時)?")
    out["spare"] = bool(spare)
    if t.take(r"\bevery (?:other|second|third|fourth|\d+|two|three|four|five|six|few|couple)\b|\b(?:bi-?weekly|bi-?monthly|fortnight\w*|hourly|quarterly|yearly|annually)\b"
              r"|\bevery (?:hour|minute|year|quarter)\b|\b(?:twice|thrice|\d+ times|two times|three times)\b"
              r"|每隔|每兩|每二|每三|每四|每\d+\s*(?:天|日|週|周|個月|月|小時|分鐘|年)|每個?小時|每分鐘|每季|每年|隔週|隔周|隔天|隔日|[兩二三]次"):
        out["problems"].append("interval")
        return out
    found: list[str] = []
    if t.take(r"\b(?:every|each|on|all)\s+(?:week ?day|working day|work ?day|business day)s?\b|\bweekdays\b"
              r"|\bmon(?:day)?s?\s*(?:to|through|thru|-|–)\s*fri(?:day)?s?\b"
              rf"|每個?(?:工作天|工作日|上班日|平日)|平日|每?{_WEEK}一\s*(?:到|至|~|-|–)\s*{_WEEK}?五"):
        found.append("weekdays")
    days: set[int] = set()
    explicit = False
    if t.take(rf"\b(?:every|each|on|all)\s+weekends?\b|\bweekends\b|每個?(?:週末|周末)|(?:週末|周末)"):
        days |= {5, 6}
        explicit = True
    month = t.take(r"\b(?:on |at )?the (?:last day|end) of (?:every|each|the) month\b|\b(?:every|each) month(?:'s)? end\b|\bmonth[- ]end\b"
                   r"|\b(?:at|on) (?:the )?end of (?:every|each|the) month\b|每個?月(?:的)?(?:最後一天|月底|底)|月底")
    dom = LAST_DAY if month else 0
    for match in t.take(r"\b(?:on )?(?:the )?(\d{1,2})(?:st|nd|rd|th)(?: day)?(?: of (?:every|each|the) month)?\b|\b(?:on )?day (\d{1,2}) of (?:every|each|the) month\b"
                        r"|\b(?:on )?the (first) (?:day )?of (?:every|each|the) month\b"
                        rf"|每個?月(?:的)?(?:第)?\s*({_ZH_NUM})\s*(?:號|日)|每個?月(初)|月(初)"):
        month = month or [match]
        raw = next(g for g in match.groups() if g is not None)
        number = 1 if raw in ("first", "初") else _zh_number(raw)
        if number is None or not 1 <= number <= 31 or (dom not in (0, number)):
            out["problems"].append("which_day" if number is None or not 1 <= number <= 31 else "conflict")
            return out
        dom = number
    monthly_word = t.take(r"\b(?:every|each) month\b|\bmonthly\b|\bonce (?:a|per) month\b|每個?月")
    if month or monthly_word:
        found.append("monthly")
        if dom == 0:
            out["problems"].append("which_day")
            return out
    for match in t.take(rf"每?個?{_WEEK}的?([一二三四五六日天](?:\s*[、,，和及與跟]?\s*{_WEEK}?[一二三四五六日天])*)"):
        days |= {_ZH_DAYS[ch] for ch in match.group(1) if ch in _ZH_DAYS}
        explicit = explicit or match.group(0).startswith("每")
    names = "|".join(list(_EN_DAYS) + sorted(_EN_ABBR, key=len, reverse=True))
    for match in t.take(rf"\b(?:(every|each|on|weekly on|every week on)\s+)?({names})(s)?\b(?=\W|$)"):
        word = match.group(2)
        if word in _EN_ABBR and not match.group(1) and not days:
            continue  # "sat", "sun", "wed" on their own are ordinary words
        days.add(_EN_DAYS.get(word, _EN_ABBR.get(word, 0)))
        explicit = explicit or bool(match.group(3)) or (match.group(1) or "").split(" ")[0] in ("every", "each", "weekly")
    weekly_word = t.take(rf"\b(?:every|each) week\b|\bweekly\b|\bonce (?:a|per) week\b|每個?{_WEEK}|每週|每周")
    if days or weekly_word:
        found.append("weekly")
        if not days:
            out["problems"].append("which_day")
            return out
        if not explicit and not weekly_word:
            out["assumed"].append("weekly")
    if t.take(r"\bevery ?day\b|\bdaily\b|\beach day\b|\bonce (?:a|per|every) day\b|\bper day\b|\b(?:every|each) (?=morning|evening|night|afternoon)|\bnightly\b|每天|每日|天天|每(?=晚|個?早上|個?晚上|個?下午|個?上午)個?"):
        found.append("daily")
    if len(found) > 1:
        out["problems"].append("conflict")
        return out
    if found:
        out["freq"] = found[0]
        out["days"] = tuple(sorted(days)) if found[0] == "weekly" else ()
        out["day_of_month"] = dom if found[0] == "monthly" else 0
    return out


def _clock(hour: int, minute: int, period: str) -> str | None:
    """HH:MM from an hour, a minute and a word such as pm or 下午; None when they do not make a time of day."""
    period = period.replace(".", "").strip().lower()
    if period in ("am", "pm"):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if period == "pm" else 0)
    elif period in _ZH_PM or period in ("evening", "afternoon", "night", "tonight"):
        if hour < 12:
            hour += 12
    elif period in _ZH_AM or period == "morning":
        if hour == 12:
            hour = 0
    elif period == "中午":
        if hour < 3:
            hour += 12
    elif period in ("半夜", "午夜"):
        if hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _time(t: _Text, plain: str) -> dict[str, Any]:
    """Reads the time of day: {"times": [...], "bad": bool, "assumed": "HH:MM" or None}."""
    times: list[str] = []
    bad = False

    def add(value: str | None) -> None:
        nonlocal bad
        if value is None:
            bad = True
        elif value not in times:
            times.append(value)

    for match in t.take(rf"{_ZH_PERIOD}?\s*({_ZH_NUM})\s*(?:點鐘?|時)\s*(?:(半)|({_ZH_NUM})\s*分?)?"):
        hour = _zh_number(match.group(2))
        minute = 30 if match.group(3) else (_zh_number(match.group(4)) if match.group(4) else 0)
        add(_clock(hour, minute, match.group(1) or "") if hour is not None and minute is not None else None)
    for match in t.take(rf"{_ZH_PERIOD}?\s*(?<![\d.:])(\d{{1,2}})[:.](\d{{2}})(?![\d:.])\s*(a\.?m\.?|p\.?m\.?)?(?![a-z])"
                        r"(?:\s*(?:in the |at )?(morning|afternoon|evening|night))?"):
        add(_clock(int(match.group(2)), int(match.group(3)), match.group(4) or match.group(5) or match.group(1) or ""))
    for match in t.take(r"(?<![\d.:])(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?![a-z])"):
        add(_clock(int(match.group(1)), 0, match.group(2)))
    for match in t.take(r"\b(?:at|by|around) (\d{1,2})(?: o'?clock)?\b(?![:.]\d)(?:\s*(?:in the |at )?(morning|afternoon|evening|night))?|\b(\d{1,2}) o'?clock\b"):
        add(_clock(int(match.group(1) or match.group(3)), 0, match.group(2) or ""))
    for match in t.take(r"\b(noon|midday|midnight)\b|(中午|正午|午夜|半夜)"):
        add("12:00" if (match.group(1) or match.group(2)) in ("noon", "midday", "中午", "正午") else "00:00")
    assumed = None
    if not times and not bad:
        for pattern, value in ((r"\bmorning\b|早上|早晨|上午|清晨", "08:00"), (r"\bafternoon\b|下午", "14:00"),
                               (r"\bevening\b|傍晚", "18:00"), (r"\b(?:night|nightly|tonight)\b|晚上|晚間|每晚", "21:00")):
            if re.search(pattern, plain):
                assumed = value
                break
    return {"times": times, "bad": bad, "assumed": assumed}


def _template(low: str, has_folder: bool, has_url: bool) -> tuple[str | None, bool]:
    """(template or None, whether it was assumed from "summarise" plus a folder)."""
    feeds = bool(re.search(r"\b(?:feeds?|rss|atom|news|newsletters?|headlines|blogs?|subscriptions?|articles?)\b|訂閱|新聞|消息來源|文章|部落格|頭條", low)) or has_url
    changes = bool(re.search(r"\b(?:what(?:'s| is| has| was)? (?:changed|new|been (?:added|changed))|changes|changed|new files?|new or changed|modified|"
                             r"added|edits?)\b|變動|變更|異動|改了|改動|新增|新檔|更新了|有什麼新|有哪些新|新的檔案", low))
    brief = bool(re.search(r"\b(?:brief(?:ing)?|recap|what (?:muse|my helper|the helper|you) did|yesterday'?s? (?:work|jobs|activity|records)|"
                           r"daily (?:summary|digest|report) of (?:muse|my helper|what))\b|簡報|日報|回顧|做了哪些事|做了什麼|昨天的(?:工作|紀錄|記錄)", low))
    if brief and not (has_folder or has_url or feeds):
        return "daily-brief", False
    if feeds and not changes:
        return "summarise-feeds", False
    if changes and not feeds:
        return "summarise-folder-changes", False
    if feeds and changes:
        explicit = re.search(r"\b(?:feeds?|rss|atom|newsletters?|subscriptions?)\b|訂閱", low) or has_url
        return ("summarise-feeds" if explicit else None), False
    if brief:
        return "daily-brief", False
    if has_folder and re.search(r"\b(?:summari[sz]e|summary|digest|sum up)\b|摘要|整理|總結|彙整", low):
        return "summarise-folder-changes", True
    return None, False


def sentence_lang(sentence: str) -> str:
    low = sentence.lower()
    if re.search(r"\bin english\b|用英文|英文(?:寫|摘要|輸出)", low):
        return "en"
    if re.search(r"\bin (?:traditional )?chinese\b|用中文|用繁體|繁體中文", low):
        return "zh-TW"
    return "zh-TW" if sum(1 for ch in sentence if "一" <= ch <= "鿿") >= 2 else "en"


def parse_sentence(sentence: str) -> dict[str, Any]:
    """Rules only: the fields a sentence states, the question codes for what it does not, and what was assumed.

    Returns {"fields": {...}, "problems": [codes], "assumed": [words], "lang"}. A field is present only when a
    rule read it. Nothing here touches the disk.
    """
    raw = " ".join(str(sentence or "").split())
    lang = sentence_lang(raw)
    if not raw:
        return {"fields": {}, "problems": ["empty"], "assumed": [], "lang": lang}
    if len(raw) > MAX_SENTENCE:
        return {"fields": {}, "problems": ["too_long"], "assumed": [], "lang": lang}
    masked, places = _mask_places(raw)
    low = unicodedata.normalize("NFKC", masked).lower()
    sources: list[str] = []
    urls: list[str] = []
    dest = ""
    for match in re.finditer(r"\x01(\d+)\x02", low):
        kind, text = places[int(match.group(1))]
        if kind == "url":
            if text not in urls:
                urls.append(text)
        elif _DEST_BEFORE.search(low[:match.start()].rstrip(" ") + " ") and not dest:
            dest = _expand(text)
        elif _expand(text) not in sources:
            sources.append(_expand(text))
    plain = re.sub(r"\x01\d+\x02", " ", low)
    template, assumed_what = _template(plain, bool(sources), bool(urls))
    # "daily brief" names a chore, not a schedule: read it as the template, and as daily only when no schedule is given.
    t = _Text(plain)
    implied_daily = bool(t.take(r"\bdaily[- ](?:brief(?:ing)?|summary|digest|report|recap)\b|每日(?:摘要|簡報|回顧)|日報"))
    freq = _frequency(t)
    when = _time(t, plain)
    fields: dict[str, Any] = {"lang": lang, "spare_only": freq["spare"]}
    problems: list[str] = list(freq["problems"])
    assumed: list[str] = list(freq["assumed"])
    if freq["freq"]:
        fields.update(freq=freq["freq"], days=freq["days"], day_of_month=freq["day_of_month"])
    elif not problems:
        if freq["spare"] or implied_daily:
            fields.update(freq="daily", days=(), day_of_month=0)
        elif re.search(r"\b(?:tomorrow|today|tonight|once|next (?:week|month|year|time|\w+day)|this (?:week|month|\w+day))\b|明天|今天|今晚|後天|下週|下周|下個月|這一次|一次就好", plain):
            problems.append("one_off")
        else:
            problems.append("when")
    if when["bad"]:
        problems.append("time")
    elif len(when["times"]) > 1:
        problems.append("two_times")
    elif when["times"]:
        fields["time"] = when["times"][0]
    elif when["assumed"]:
        fields["time"] = when["assumed"]
        assumed.append("time")
    elif freq["spare"] and not freq["freq"] and not problems:
        fields["time"] = ""  # "when there is allowance to spare" alone: any time of day, at most once a day
    if template:
        fields["template"] = template
        if assumed_what:
            assumed.append("what")
    else:
        problems.append("what")
    if sources:
        fields["sources"] = tuple(sources)
    if urls:
        fields["urls"] = tuple(urls)
    if dest:
        fields["dest"] = dest
    return {"fields": fields, "problems": list(dict.fromkeys(problems)), "assumed": assumed, "lang": lang}


# ================================================================ a model's proposal (never trusted)

PROPOSAL_KEYS = ("frequency", "days", "day_of_month", "time", "template")
_PROPOSAL_FREQ = {"daily": "daily", "every day": "daily", "weekdays": "weekdays", "weekday": "weekdays", "weekly": "weekly",
                  "monthly": "monthly", "spare": "spare"}


def validate_proposal(proposal: Any) -> dict[str, Any]:
    """The fields of a model's JSON proposal that are well-formed, in the rules' own vocabulary. Everything else is dropped.

    A proposal can say how often, which days, what time and which template. It can never name a folder, an address
    or a destination: those are read from the sentence by the rules, or asked for.
    """
    if not isinstance(proposal, Mapping):
        return {}
    out: dict[str, Any] = {}
    freq = _PROPOSAL_FREQ.get(str(proposal.get("frequency") or "").strip().lower())
    days: list[int] = []
    raw_days = proposal.get("days")
    for item in raw_days if isinstance(raw_days, (list, tuple)) else []:
        if _is_int(item) and 0 <= item <= 6:
            days.append(item)
        elif isinstance(item, str) and item.strip().lower() in _EN_DAYS:
            days.append(_EN_DAYS[item.strip().lower()])
        elif isinstance(item, str) and item.strip().lower() in _EN_ABBR:
            days.append(_EN_ABBR[item.strip().lower()])
        else:
            days = []
            break
    dom = proposal.get("day_of_month")
    if isinstance(dom, str) and dom.strip().lower() in ("last", "-1"):
        dom = LAST_DAY
    dom = dom if _is_int(dom) and (dom == LAST_DAY or 1 <= dom <= 31) else 0
    if freq == "spare":
        out.update(freq="daily", days=(), day_of_month=0, spare_only=True)
    elif freq in ("daily", "weekdays"):
        out.update(freq=freq, days=(), day_of_month=0)
    elif freq == "weekly" and days:
        out.update(freq="weekly", days=tuple(sorted(set(days))), day_of_month=0)
    elif freq == "monthly" and dom:
        out.update(freq="monthly", days=(), day_of_month=dom)
    when = proposal.get("time")
    if isinstance(when, str):
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", when)
        if match and valid_time(f"{int(match.group(1)):02d}:{match.group(2)}"):
            out["time"] = f"{int(match.group(1)):02d}:{match.group(2)}"
    template = str(proposal.get("template") or "").strip().lower()
    if template in TEMPLATES:
        out["template"] = template
    return out


def wants_proposal(parsed: Mapping[str, Any]) -> bool:
    """True when the only things missing are ones a model may propose (how often, what), not ones only the person can say."""
    problems = set(parsed.get("problems") or [])
    return bool(problems) and problems <= {"when", "what"}


# ================================================================ what was understood


def describe_when(spec: Spec, tz_name: str = "", lang: str | None = None) -> str:
    lang = lang or spec.lang
    names = tr("days", lang).split(",")
    clock = spec.time or "00:00"
    if spec.freq == "weekly":
        days = [names[d] for d in spec.days]
        joined = tr("list", lang).join(days[:-1]) + (tr("and", lang) if len(days) > 1 else "") + days[-1]
        text = tr("when_weekly", lang, days=joined, time=clock)
    elif spec.freq == "monthly":
        key = "when_last" if spec.day_of_month == LAST_DAY else ("when_monthly_short" if spec.day_of_month >= 29 else "when_monthly")
        text = tr(key, lang, day=spec.day_of_month, time=clock)
    else:
        text = tr(f"when_{spec.freq}", lang, time=clock)
    if spec.spare_only and not spec.time:
        cut = text.rfind(tr("strip_at", lang) + clock)
        text = tr("when_spare_any", lang, when=text[:cut] if cut > 0 else text)
    elif spec.spare_only:
        text = tr("when_spare", lang, when=text)
    return tr("tz", lang, when=text, tz=tz_name) if tz_name else text


def budget_text(budget: Mapping[str, Any], lang: str = "en") -> str:
    paid = float(budget.get("paid_usd") or 0)
    if paid > 0:
        return tr("budget_paid", lang, requests=int(budget["requests"]), paid=f"{paid:.2f}")
    return tr("budget", lang, requests=int(budget["requests"]))


def default_out_dir(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "skills-out" / SKILL


def default_budget(template: str) -> dict[str, Any]:
    return dict(TEMPLATES[template]["budget"])


def describe_lines(template: str, sources: Sequence[str], dest: str, lang: str, budget: Mapping[str, Any],
                   environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The what / where / budget / permission lines for a template, its folders and its output folder."""
    shown_sources = tr("list", lang).join(sources) if sources else "…"
    shown_dest = dest or tr("where_default", lang, path=str(default_out_dir(environ)))
    key = {"summarise-feeds": "what_feeds", "summarise-folder-changes": "what_changes", "daily-brief": "what_brief"}[template]
    read = tr("perm_brief", lang) if template == "daily-brief" else tr("perm_read", lang, sources=shown_sources)
    return {"what": tr(key, lang, sources=shown_sources), "where": tr("where", lang, dest=shown_dest),
            "budget": budget_text(budget, lang), "permission": read + tr("join", lang) + tr("perm_write", lang, dest=shown_dest)}


def describe(chore: "Chore", environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A stored chore as the same five lines it was confirmed with."""
    spec = chore.spec
    lines = describe_lines(spec.template, spec.sources, spec.dest, spec.lang, chore.budget, environ)
    lines["when"] = describe_when(spec, chore.tz)
    return lines


_LABELS = {
    "en": {"what": "What", "when": "When", "where": "Result", "budget": "Budget", "permission": "Needs", "note": "Note", "question": "Question"},
    "zh-TW": {"what": "做什麼", "when": "什麼時候", "where": "結果", "budget": "預算", "permission": "需要", "note": "備註", "question": "請問"},
}


def understood_text(understood: Mapping[str, Any]) -> str:
    """What was understood, as the lines a person confirms: what, when, where the result goes, the budget, what it needs."""
    lang = understood.get("lang") if understood.get("lang") in LANGS else "en"
    labels = _LABELS[lang]
    colon = "：" if lang == "zh-TW" else ": "
    lines = [f"{labels[key]}{colon}{understood[key]}" for key in ("what", "when", "where", "budget", "permission") if understood.get(key)]
    lines += [f"{labels['note']}{colon}{note}" for note in understood.get("notes") or []]
    lines += [f"{labels['question']}{colon}{q['text']}" for q in understood.get("questions") or []]
    return "\n".join(lines)


def understand(
    sentence: str,
    *,
    folders: Sequence[str | os.PathLike[str]] = (),
    out: str | os.PathLike[str] | None = None,
    proposal: Any = None,
    tz_name: str = "",
    environ: Mapping[str, str] | None = None,
    check_folders: bool = True,
    budget: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """What Muse understood from a sentence, as the lines shown before the one confirmation (ux 4.3).

    `ok` is true only when nothing is left to ask. `folders` and `out` are what the person picked beside the
    sentence ("these 5 feeds"); `proposal` is a model's JSON for what the rules could not read, validated here.
    """
    parsed = parse_sentence(sentence)
    lang = parsed["lang"]
    fields = dict(parsed["fields"])
    problems = list(parsed["problems"])
    assumed = list(parsed["assumed"])
    parsed_by = "rules"
    offered = validate_proposal(proposal) if proposal is not None and wants_proposal(parsed) else {}
    if offered:
        if "when" in problems and offered.get("freq"):
            fields.update({k: offered[k] for k in ("freq", "days", "day_of_month")})
            fields["spare_only"] = bool(fields.get("spare_only") or offered.get("spare_only"))
            if "time" not in fields and "time" in offered:
                fields["time"] = offered["time"]
            problems.remove("when")
            parsed_by = "model"
        if "what" in problems and offered.get("template"):
            fields["template"] = offered["template"]
            problems.remove("what")
            parsed_by = "model"
    sources = list(fields.get("sources") or ())
    for folder in folders:
        path = str(Path(os.fspath(folder)).expanduser())
        if path not in sources:
            sources.append(path)
    dest = str(Path(os.fspath(out)).expanduser()) if out is not None else str(fields.get("dest") or "")
    questions: list[dict[str, str]] = [{"code": code, "text": tr(f"q_{code}", lang)} for code in problems]
    template = fields.get("template")
    if template and TEMPLATES[template]["sources"]:
        if not sources:
            questions.append({"code": "sources", "text": tr("q_sources", lang)})
        elif len(sources) > MAX_SOURCES:
            questions.append({"code": "too_many", "text": tr("q_too_many", lang, n=MAX_SOURCES)})
        elif check_folders:
            for path in sources:
                if not Path(path).is_dir():
                    questions.append({"code": "folder_missing", "text": tr("q_folder_missing", lang, path=path)})
    elif template:
        sources = []  # the daily brief reads no folder
    notes: list[str] = []
    spec: Spec | None = None
    spare = bool(fields.get("spare_only"))
    if "time" not in fields and fields.get("freq") and not any(q["code"] in ("time", "two_times") for q in questions):
        if offered.get("spare_only"):
            fields["time"] = ""
        else:
            fields["time"] = DEFAULT_TIME
            assumed.append("time")
    if not questions:
        data = {"freq": fields.get("freq"), "template": template, "time": fields.get("time"), "days": list(fields.get("days") or ()),
                "day_of_month": fields.get("day_of_month") or 0, "sources": sources, "urls": list(fields.get("urls") or ()),
                "dest": dest, "lang": lang, "spare_only": spare}
        if spec_problems(data):
            questions.append({"code": "when", "text": tr("q_when", lang)})
        else:
            spec = Spec.from_dict(data)
    the_budget = dict(budget) if budget else (default_budget(template) if template else None)
    result: dict[str, Any] = {
        "ok": spec is not None, "sentence": " ".join(str(sentence or "").split())[:MAX_SENTENCE], "lang": lang,
        "spec": spec.to_dict() if spec else None, "tz": tz_name, "parsed_by": parsed_by,
        "what": "", "when": "", "where": "", "budget": "", "permission": "",
        "budget_values": the_budget, "questions": questions, "assumed": list(dict.fromkeys(assumed)), "notes": notes,
    }
    if template:
        result.update(describe_lines(template, sources, dest, lang, the_budget, environ))
        if template == "summarise-feeds":
            notes.append(tr("n_feed_files", lang))
        if template == "summarise-folder-changes":
            notes.append(tr("n_no_old", lang))
    if spec is not None:
        result["when"] = describe_when(spec, tz_name)
    if fields.get("urls"):
        notes.append(tr("n_urls", lang, n=len(fields["urls"])))
    if "time" in result["assumed"] and fields.get("time"):
        notes.append(tr("n_assumed_time", lang, time=fields["time"]))
    if "weekly" in result["assumed"]:
        notes.append(tr("n_assumed_weekly", lang))
    if "what" in result["assumed"]:
        notes.append(tr("n_assumed_what", lang))
    if spare:
        notes.append(tr("n_spare", lang))
    if parsed_by == "model":
        notes.append(tr("n_model", lang))
    return result


# ================================================================ when is it due


def _wall(day: date, clock: str, tz: tzinfo) -> float:
    """The instant a local wall-clock time happens. A time skipped by a clock change runs just after the gap
    (02:30 on a spring-forward day becomes 03:30); a time that happens twice runs the first time."""
    hour, minute = (int(part) for part in (clock or "00:00").split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).timestamp()  # fold=0: the earlier offset


def _matches(spec: Spec, day: date) -> bool:
    if spec.freq == "daily":
        return True
    if spec.freq == "weekdays":
        return day.weekday() < 5
    if spec.freq == "weekly":
        return day.weekday() in spec.days
    last = calendar.monthrange(day.year, day.month)[1]
    wanted = last if spec.day_of_month == LAST_DAY else min(spec.day_of_month, last)  # the 31st of a 30-day month: its last day
    return day.day == wanted


def next_after(spec: Spec, now: float, tz: tzinfo | str | None = None) -> float:
    """The first time `spec` is due strictly after `now` (epoch seconds), in wall-clock time of `tz`."""
    zone = resolve_tz(tz) if (tz is None or isinstance(tz, str)) else tz
    day = datetime.fromtimestamp(now, zone).date()
    for _ in range(400):
        if _matches(spec, day):
            due = _wall(day, spec.time, zone)
            if due > now:
                return due
        day += timedelta(days=1)
    raise ChoreError("this schedule is never due")  # cannot happen for a valid spec


def local_date(now: float, tz: tzinfo | str | None = None) -> str:
    zone = resolve_tz(tz) if (tz is None or isinstance(tz, str)) else tz
    return datetime.fromtimestamp(now, zone).date().isoformat()


# ================================================================ the store

SCHEMA = """
CREATE TABLE IF NOT EXISTS chores (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  sentence TEXT NOT NULL,            -- as the person typed it
  spec TEXT NOT NULL,                -- JSON (Spec.to_dict)
  skill TEXT NOT NULL,
  inputs TEXT NOT NULL,              -- JSON: the skill's inputs for every run
  budget TEXT NOT NULL,              -- JSON: requests, tokens, seconds, paid_usd
  privacy TEXT,                      -- NULL = the skill's default
  tz TEXT NOT NULL,                  -- an IANA zone name, or '' for the system zone
  next_run REAL NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  spare_only INTEGER NOT NULL DEFAULT 0,
  runs INTEGER NOT NULL DEFAULT 0,
  last_run_at REAL,
  last_status TEXT,
  last_summary TEXT                  -- counts and a status in plain words: never content
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  chore_id TEXT NOT NULL,
  due_at REAL NOT NULL,              -- the occurrence this run is for
  created_at REAL NOT NULL,
  trigger TEXT NOT NULL,             -- schedule | manual
  status TEXT NOT NULL,
  not_before REAL NOT NULL,          -- earliest next try (the router said when)
  deadline REAL,                     -- give up at the chore's next occurrence
  attempts INTEGER NOT NULL DEFAULT 0,
  deferrals INTEGER NOT NULL DEFAULT 0,
  started_at REAL, finished_at REAL, runner TEXT,
  summary TEXT,                      -- counts and reasons, never content
  card_id TEXT,
  out_path TEXT,                     -- where the result is (or would be) written
  requests INTEGER, tokens INTEGER, cost_usd REAL
);
CREATE INDEX IF NOT EXISTS runs_open ON runs (status, not_before);
CREATE UNIQUE INDEX IF NOT EXISTS runs_once ON runs (chore_id, due_at) WHERE trigger='schedule';
"""


def chores_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "chores.sqlite"


def pending_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Results waiting for the person's answer: the proposed file's bytes, owner-only, removed once answered."""
    folder = paths.home(environ) / "chores-pending"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        folder.chmod(0o700)
    except OSError:
        pass
    return folder


def iso(ts: float | None) -> str | None:
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass(slots=True)
class Chore:
    id: str
    created_at: float
    sentence: str
    spec: Spec
    skill: str
    inputs: dict[str, Any]
    budget: dict[str, Any]
    privacy: str | None
    tz: str
    next_run: float
    enabled: bool
    spare_only: bool
    runs: int = 0
    last_run_at: float | None = None
    last_status: str | None = None
    last_summary: str | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "created_at": iso(self.created_at), "sentence": self.sentence, "spec": self.spec.to_dict(),
                "skill": self.skill, "inputs": self.inputs, "budget": self.budget, "privacy": self.privacy, "tz": self.tz,
                "next_run": iso(self.next_run) if self.enabled else None, "enabled": self.enabled, "spare_only": self.spare_only,
                "runs": self.runs, "last_run_at": iso(self.last_run_at), "last_status": self.last_status,
                "last_summary": self.last_summary, "when": describe_when(self.spec, self.tz),
                "budget_text": budget_text(self.budget, self.spec.lang)}


@dataclass(slots=True)
class Run:
    id: str
    chore_id: str
    due_at: float
    created_at: float
    trigger: str
    status: str
    not_before: float
    deadline: float | None = None
    attempts: int = 0
    deferrals: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    runner: str | None = field(default=None, repr=False)
    summary: str | None = None
    card_id: str | None = None
    out_path: str | None = None
    requests: int | None = None
    tokens: int | None = None
    cost_usd: float | None = None

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "chore_id": self.chore_id, "due_at": iso(self.due_at), "created_at": iso(self.created_at),
                "trigger": self.trigger, "status": self.status,
                "not_before": iso(self.not_before) if self.status == "queued" else None, "deadline": iso(self.deadline),
                "attempts": self.attempts, "deferrals": self.deferrals, "started_at": iso(self.started_at),
                "finished_at": iso(self.finished_at), "summary": self.summary, "card_id": self.card_id, "out_path": self.out_path,
                "requests": self.requests, "tokens": self.tokens, "cost_usd": self.cost_usd}


def _chore(row: sqlite3.Row) -> Chore:
    return Chore(id=row["id"], created_at=row["created_at"], sentence=row["sentence"], spec=Spec.from_dict(json.loads(row["spec"])),
                 skill=row["skill"], inputs=json.loads(row["inputs"]), budget=json.loads(row["budget"]), privacy=row["privacy"],
                 tz=row["tz"], next_run=row["next_run"], enabled=bool(row["enabled"]), spare_only=bool(row["spare_only"]),
                 runs=row["runs"], last_run_at=row["last_run_at"], last_status=row["last_status"], last_summary=row["last_summary"])


def _run(row: sqlite3.Row) -> Run:
    return Run(**{key: row[key] for key in row.keys() if key in Run.__dataclass_fields__})


def _budget(value: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, kind in (("requests", int), ("tokens", int), ("seconds", float), ("paid_usd", float)):
        raw = value.get(key, 0 if key == "paid_usd" else None)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0 or (key != "paid_usd" and raw <= 0):
            raise ChoreError(f"the budget's {key} must be a number above zero" if key != "paid_usd" else "paid_usd must be zero or more")
        out[key] = kind(raw)
    return out


class ChoreStore:
    """Access to chores.sqlite. Every method is one short transaction, safe across threads and processes."""

    def __init__(self, environ: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time,
                 path: Path | None = None) -> None:
        self.environ = environ
        self.clock = clock
        self.path = Path(path) if path is not None else chores_path(environ)

    @contextmanager
    def _conn(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)  # 0600 before sqlite touches it
            os.close(fd)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA secure_delete=ON")
            conn.executescript(SCHEMA)
            if write:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if write and conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                if write and conn.in_transaction:
                    conn.execute("COMMIT")
        finally:
            conn.close()

    # ---- chores

    def add(self, sentence: str, spec: Spec, *, budget: Mapping[str, Any] | None = None, privacy: str | None = None,
            tz: str = "", inputs: Mapping[str, Any] | None = None, skill: str = SKILL) -> Chore:
        """Store a confirmed chore. Its first run is the next time the schedule comes round, never at once."""
        problems = spec_problems(spec.to_dict())
        if problems:
            raise ChoreError("; ".join(problems))
        now = self.clock()
        chore_id = secrets.token_hex(4)
        the_budget = _budget(budget if budget is not None else default_budget(spec.template))
        the_inputs = dict(inputs) if inputs is not None else {"mode": "run", "template": spec.template, "lang": spec.lang}
        with self._conn(write=True) as conn:
            conn.execute(
                "INSERT INTO chores (id, created_at, sentence, spec, skill, inputs, budget, privacy, tz, next_run, enabled, spare_only)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,1,?)",
                (chore_id, now, " ".join(str(sentence).split())[:MAX_SENTENCE], json.dumps(spec.to_dict(), ensure_ascii=False), skill,
                 json.dumps(the_inputs, ensure_ascii=False), json.dumps(the_budget), privacy, tz, next_after(spec, now, tz),
                 int(spec.spare_only)),
            )
        return self.get(chore_id)

    def replace(self, chore_id: str, sentence: str, spec: Spec, *, budget: Mapping[str, Any] | None = None) -> Chore:
        """Edit a chore as a new sentence: same id and history, new schedule."""
        now = self.clock()
        chore = self.get(chore_id)
        the_budget = _budget(budget if budget is not None else default_budget(spec.template))
        with self._conn(write=True) as conn:
            conn.execute("UPDATE chores SET sentence=?, spec=?, inputs=?, budget=?, next_run=?, spare_only=? WHERE id=?",
                         (" ".join(str(sentence).split())[:MAX_SENTENCE], json.dumps(spec.to_dict(), ensure_ascii=False),
                          json.dumps({"mode": "run", "template": spec.template, "lang": spec.lang}, ensure_ascii=False),
                          json.dumps(the_budget), next_after(spec, now, chore.tz), int(spec.spare_only), chore_id))
        return self.get(chore_id)

    def get(self, chore_id: str) -> Chore:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM chores WHERE id=?", (chore_id,)).fetchone()
        if row is None:
            raise ChoreError(f"no chore {chore_id}")
        return _chore(row)

    def find(self, prefix: str) -> Chore:
        if not re.fullmatch(r"[0-9a-f]{1,8}", prefix or ""):
            raise ChoreError("a chore is named by its id, as `muse chores list` shows it")
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM chores WHERE id LIKE ? ORDER BY id LIMIT 2", (prefix + "%",)).fetchall()
        if not rows:
            raise ChoreError(f"no chore {prefix}")
        if len(rows) > 1:
            raise ChoreError(f"more than one chore starts with {prefix}; give more of the id")
        return _chore(rows[0])

    def list(self) -> list[Chore]:
        with self._conn() as conn:
            return [_chore(row) for row in conn.execute("SELECT * FROM chores ORDER BY created_at, id")]

    def set_enabled(self, chore_id: str, enabled: bool) -> Chore:
        """Pause or resume. Resuming schedules the next occurrence from now: nothing missed while paused runs late."""
        chore = self.get(chore_id)
        with self._conn(write=True) as conn:
            if enabled:
                conn.execute("UPDATE chores SET enabled=1, next_run=? WHERE id=?", (next_after(chore.spec, self.clock(), chore.tz), chore_id))
            else:
                conn.execute("UPDATE chores SET enabled=0 WHERE id=?", (chore_id,))
                conn.execute("UPDATE runs SET status='skipped', summary='the chore was paused', finished_at=? "
                             "WHERE chore_id=? AND status='queued'", (self.clock(), chore_id))
        return self.get(chore_id)

    def remove(self, chore_id: str) -> list[Run]:
        """Delete a chore and its history. Returns the runs it had, so the caller can clear what they left pending."""
        self.get(chore_id)
        with self._conn(write=True) as conn:
            runs = [_run(row) for row in conn.execute("SELECT * FROM runs WHERE chore_id=?", (chore_id,))]
            conn.execute("DELETE FROM runs WHERE chore_id=?", (chore_id,))
            conn.execute("DELETE FROM chores WHERE id=?", (chore_id,))
        return runs

    def due(self, now: float | None = None) -> list[Chore]:
        """Enabled chores whose next_run has come."""
        ts = self.clock() if now is None else now
        with self._conn() as conn:
            return [_chore(row) for row in conn.execute("SELECT * FROM chores WHERE enabled=1 AND next_run<=? ORDER BY next_run, id", (ts,))]

    def mark_ran(self, chore_id: str, status: str, summary: str, at: float | None = None) -> None:
        """Record a run's outcome on the chore: a status and a plain summary of counts. Never content."""
        with self._conn(write=True) as conn:
            conn.execute("UPDATE chores SET runs=runs+1, last_run_at=?, last_status=?, last_summary=? WHERE id=?",
                         (self.clock() if at is None else at, status, " ".join(str(summary).split())[:300], chore_id))

    def note_result(self, chore_id: str, status: str, summary: str) -> None:
        """Update the last result without counting another run (a card was answered later)."""
        with self._conn(write=True) as conn:
            conn.execute("UPDATE chores SET last_status=?, last_summary=? WHERE id=?",
                         (status, " ".join(str(summary).split())[:300], chore_id))

    # ---- runs

    def tick(self, now: float | None = None) -> list[Run]:
        """Queue one run for every due chore and move its next_run past `now`. Returns the runs queued by THIS call.

        One transaction: a second process ticking at the same moment finds next_run already moved and queues
        nothing. A chore that was due several times while the home was off is queued once, not once per miss.
        A run still waiting when its chore's next occurrence comes is skipped first, so runs never pile up.
        """
        ts = self.clock() if now is None else now
        made: list[str] = []
        with self._conn(write=True) as conn:
            conn.execute("UPDATE runs SET status='queued', runner=NULL, attempts=attempts+1, not_before=? "
                         "WHERE status='running' AND started_at<=?", (ts, ts - STALE_RUNNING_S))  # a runner that died
            # A run still waiting when its chore's next occurrence has come is given up, so the new one can go.
            conn.execute("UPDATE runs SET status='skipped', finished_at=?, summary=CASE WHEN deferrals>0 THEN "
                         "'no allowance was free before the next run came round' ELSE 'not started before the next run came round' END "
                         "WHERE status='queued' AND deadline IS NOT NULL AND deadline<=?", (ts, ts))
            for row in conn.execute("SELECT * FROM chores WHERE enabled=1 AND next_run<=? ORDER BY next_run, id", (ts,)).fetchall():
                chore = _chore(row)
                following = next_after(chore.spec, ts, chore.tz)
                waiting = conn.execute("SELECT 1 FROM runs WHERE chore_id=? AND status IN ('queued','running') AND trigger='schedule'",
                                       (chore.id,)).fetchone()
                if waiting is None:
                    run_id = secrets.token_hex(6)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO runs (id, chore_id, due_at, created_at, trigger, status, not_before, deadline)"
                        " VALUES (?,?,?,?, 'schedule', 'queued', ?, ?)", (run_id, chore.id, chore.next_run, ts, ts, following))
                    if cur.rowcount:
                        made.append(run_id)
                conn.execute("UPDATE chores SET next_run=? WHERE id=?", (following, chore.id))
        return [self.get_run(run_id) for run_id in made]

    def add_manual_run(self, chore_id: str, now: float | None = None) -> Run:
        ts = self.clock() if now is None else now
        self.get(chore_id)
        run_id = secrets.token_hex(6)
        with self._conn(write=True) as conn:
            conn.execute("INSERT INTO runs (id, chore_id, due_at, created_at, trigger, status, not_before) VALUES (?,?,?,?, 'manual', 'queued', ?)",
                         (run_id, chore_id, ts, ts, ts))
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> Run:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ChoreError(f"no run {run_id}")
        return _run(row)

    def runs(self, chore_id: str | None = None, statuses: Sequence[str] | None = None, limit: int = 50) -> list[Run]:
        clauses, values = [], []
        if chore_id is not None:
            clauses.append("chore_id=?")
            values.append(chore_id)
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            values.extend(statuses)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as conn:
            return [_run(row) for row in conn.execute(f"SELECT * FROM runs{where} ORDER BY created_at DESC, id LIMIT ?", (*values, limit))]

    def claim(self, now: float, runner: str, run_id: str | None = None) -> Run | None:
        """Take one queued run whose time has come (or the named one), marking it running. None when there is none."""
        with self._conn(write=True) as conn:
            if run_id is None:
                row = conn.execute("SELECT * FROM runs WHERE status='queued' AND not_before<=? ORDER BY not_before, created_at, id LIMIT 1",
                                   (now,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM runs WHERE id=? AND status='queued'", (run_id,)).fetchone()
            if row is None:
                return None
            cur = conn.execute("UPDATE runs SET status='running', runner=?, started_at=? WHERE id=? AND status='queued'",
                               (runner, now, row["id"]))
            if not cur.rowcount:
                return None
        return self.get_run(row["id"])

    def settle(self, run_id: str, *, expect: str | None = None, **changes: Any) -> bool:
        """Change a run's columns; with `expect`, only while it still has that status. True when a row changed."""
        allowed = set(Run.__dataclass_fields__) - {"id", "chore_id", "due_at", "created_at", "trigger"}
        if not changes or set(changes) - allowed:
            raise ChoreError("unknown run column")
        if "status" in changes and changes["status"] not in RUN_STATUSES:
            raise ChoreError("unknown run status")
        if "summary" in changes and changes["summary"] is not None:
            changes["summary"] = " ".join(str(changes["summary"]).split())[:300]
        sets = ", ".join(f"{key}=?" for key in changes)
        sql = f"UPDATE runs SET {sets} WHERE id=?" + (" AND status=?" if expect else "")
        with self._conn(write=True) as conn:
            cur = conn.execute(sql, (*changes.values(), run_id, *([expect] if expect else [])))
            return bool(cur.rowcount)

    def last_ok_run(self, chore_id: str) -> str:
        """The id of the latest run that produced its result (whatever the person then did with it), or ''."""
        with self._conn() as conn:
            row = conn.execute(f"SELECT id FROM runs WHERE chore_id=? AND status IN ({','.join('?' * len(RUN_OK))}) "
                               "ORDER BY started_at DESC, created_at DESC LIMIT 1", (chore_id, *RUN_OK)).fetchone()
        return row["id"] if row else ""

    def typical_time(self, chore_id: str, tz: tzinfo | str | None = None) -> str | None:
        """The usual local start time of a chore's finished runs ("HH:MM", the median), for spare-allowance chores."""
        zone = resolve_tz(tz) if (tz is None or isinstance(tz, str)) else tz
        with self._conn() as conn:
            starts = [row["started_at"] for row in conn.execute(
                "SELECT started_at FROM runs WHERE chore_id=? AND started_at IS NOT NULL AND status IN ('done','partial','waiting_card','declined','expired')"
                " ORDER BY started_at DESC LIMIT 15", (chore_id,))]
        if not starts:
            return None
        minutes = sorted(datetime.fromtimestamp(s, zone).hour * 60 + datetime.fromtimestamp(s, zone).minute for s in starts)
        middle = minutes[len(minutes) // 2]
        return f"{middle // 60:02d}:{middle % 60:02d}"
