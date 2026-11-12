"""HealthBot: the user's delegate over their own health records.

Pipeline for one turn (`chat`):
1. Screen the user's own words (safety.screen_user_text). Urgent -> emergency
   text first, nothing else happens. Medical advice -> decline, offer to add
   the question to the visit question list.
2. Intent from cheap deterministic rules; the model is used only to answer
   questions about records, explain terms, or (as a last resort) to turn a
   write request into the JSON action below.
3. Mandate check before every read, model call and write; receipt after each
   (content hashes and counts only, never text).
4. Retrieved and pasted text goes into the prompt as delimited DATA, capped to
   a token budget.
5. Model egress only at the tenant's privacy class or stricter, never weaker,
   never retried at another class.
6. The answer is screened (safety.screen_model_text) and its citations checked
   against the records actually in context.

Why an unsafe answer is replaced, not edited: deleting one sentence of a
medical answer can flip what the rest means ("...do not." becomes "..."),
leave numbers that only made sense with the removed claim, or keep the
reassurance around a removed instruction. A fixed fallback that points to the
cited records and the clinician is always safe to show.

JSON action protocol (for writes; tolerant to fences and prose around it):
  {"action": "add_item" | "update_item" | "delete_item",
   "kind": "reminder" | "medication" | "appointment" | "question_list",
   "item_id": "<id>"            (update/delete only),
   "data": {...}}               (fields per ITEM_SCHEMA; unknown keys rejected)
Every action becomes a ProposedChange. Nothing is written until confirm().
Record text and model output never choose the mandate, grantee, privacy class
or any path: those come from the tenant, the constant GRANTEE and the code.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

from muse.health import prompts
from muse.health.contracts import (
    Action,
    BotReply,
    Decision,
    Item,
    LLM,
    LLMRefused,
    LLMResult,
    LLMUnavailable,
    MandateBook,
    ProposedChange,
    ReceiptLog,
    Record,
    Tenant,
    Vault,
    VaultError,
    VaultLocked,
)
from muse.health.safety import (
    SafetyVerdict,
    detect_locale,
    emergency_message,
    looks_like_injection,
    screen_model_text,
    screen_user_text,
    wrap_untrusted,
)

GRANTEE = "bot"
PRIVACY_ORDER = ("public", "standard", "private", "local")
SCOPE_FOR_CLASS = {"local": "llm.local", "private": "llm.private", "standard": "llm.cloud", "public": "llm.cloud"}
EGRESS_FOR_CLASS = {"local": "local", "private": "private", "standard": "cloud", "public": "cloud"}

# field -> (type, max length, required). Types: str, time (HH:MM), date
# (YYYY-MM-DD), repeat (enum), list (list of str).
ITEM_SCHEMA: dict[str, dict[str, tuple[str, int, bool]]] = {
    "reminder": {"text": ("str", 200, True), "time": ("time", 5, False), "date": ("date", 10, False), "repeat": ("repeat", 6, False)},
    "medication": {"name": ("str", 80, True), "dose": ("str", 40, False), "schedule": ("str", 80, False), "note": ("str", 300, False)},
    "appointment": {"title": ("str", 120, True), "date": ("date", 10, True), "time": ("time", 5, False), "place": ("str", 120, False), "note": ("str", 300, False)},
    "question_list": {"title": ("str", 120, False), "questions": ("list", 300, True)},
}
OPS = ("add_item", "update_item", "delete_item")


class ActionError(ValueError):
    """A proposed action failed validation. Message is safe to show."""


def _tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # no tz database: Taiwan has had no DST since 1979
        return timezone(timedelta(hours=8), "Asia/Taipei")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cjk_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))


def estimate_tokens(text: str) -> int:
    """Conservative: one token per CJK character, one per four other characters."""
    cjk = _cjk_count(text)
    return cjk + (len(text) - cjk + 3) // 4


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

T: dict[str, tuple[str, str]] = {
    "locked": ("你的紀錄目前是鎖住的，請先解鎖。", "Your records are locked. Please unlock them first."),
    "denied": ("我沒有做這件事的授權：{reason}", "I don't have permission to do that: {reason}"),
    "no_model_hospital": ("本院院內模型目前無法使用，所以我不會把你的紀錄送到其他地方。",
                          "The hospital's on-site model is not available right now, so I will not send your records anywhere else."),
    "no_model": ("目前沒有符合你隱私設定（{cls}）的模型可用，所以我不會把你的紀錄送到其他地方。",
                 "No model allowed by your privacy setting ({cls}) is available right now, so I will not send your records anywhere else."),
    "still_can": ("不用模型我還是可以：列出提醒、用藥和預約，看接下來的行程，看紀錄時間軸，打開某一筆紀錄，起草病歷申請信，準備看診問題。",
                  "Without a model I can still: list reminders, medications and appointments, show what's coming up, show your records timeline, open a record, draft a records-request letter, and prepare questions for a visit."),
    "found_records": ("可能相關的紀錄：", "Records that may be relevant:"),
    "nothing_found": ("你的紀錄裡找不到和這個問題相關的內容。", "I couldn't find anything about that in your records."),
    "declined": ("這個問題需要醫師根據你的整體狀況來判斷，我不能回答（我不是醫師，不做診斷，也不建議調整藥物或治療）。",
                 "That needs your clinician, who knows your whole situation; I can't answer it (I'm not a doctor, and I don't diagnose or advise on medicines or treatment)."),
    "declined_offer": ("我可以幫你說明報告上的名詞和參考範圍，或把這個問題加到看診問題清單，下次問醫師。",
                       "I can explain terms and reference ranges in your reports, or add this question to your list for your next visit."),
    "withheld": ("模型的回答可能包含治療建議、診斷，或紀錄裡沒有的數字或聯絡資訊，所以我不顯示它。",
                 "The model's answer may have contained treatment advice, a diagnosis, or numbers or contact details that are not in your records, so I'm not showing it."),
    "withheld_sources": ("你可以直接看原始紀錄，並請醫師說明：", "You can read the original records and ask your clinician about them:"),
    "bad_citations": ("（我移除了指向我沒有讀到的紀錄的引用。）", "(I removed references to records I did not read.)"),
    "no_citation": ("（這個回答沒有引用你的紀錄，請當作一般說明。）", "(This answer doesn't cite your records; treat it as general information.)"),
    "unverified": ("無法確認模型的回答是在你的隱私設定下產生的，所以我不顯示它。", "I couldn't confirm the answer came from a model allowed by your privacy setting, so I'm not showing it."),
    "proposed": ("請確認：{summary}（回覆確認後才會寫入）", "Please confirm: {summary} (nothing is saved until you confirm)"),
    "confirmed": ("已儲存：{summary}", "Saved: {summary}"),
    "rejected": ("已取消：{summary}", "Cancelled: {summary}"),
    "expired": ("這個變更已過期，請重新告訴我。", "That change has expired; please ask again."),
    "unknown_change": ("找不到這個待確認的變更。", "I can't find that pending change."),
    "receipt_failed": ("出了點問題，這一步沒有完成，也沒有顯示任何內容。", "Something went wrong; this step was not completed and nothing is shown."),
    "write_failed": ("儲存失敗，沒有任何變更。", "Saving failed; nothing was changed."),
    "need_details": ("我需要更多資訊：{what}", "I need a bit more detail: {what}"),
    "which_item": ("有好幾個符合，請告訴我是哪一個：", "Several items match; which one did you mean?"),
    "no_item": ("找不到符合的項目。", "I couldn't find a matching item."),
    "empty_list": ("目前沒有{kind}。", "You have no {kind}."),
    "nothing_upcoming": ("接下來 60 天沒有預約或提醒。", "Nothing scheduled in the next 60 days."),
    "upcoming": ("接下來：", "Coming up:"),
    "timeline_empty": ("目前沒有紀錄。", "You have no records yet."),
    "timeline": ("你的紀錄（依日期）：", "Your records by date:"),
    "record_missing": ("找不到這筆紀錄。", "I can't find that record."),
    "letter_need": ("請告訴我醫院名稱和日期範圍，例如：「起草病歷申請信給台大醫院，2024-01-01 到 2024-12-31」。",
                    "Tell me the hospital and the date range, e.g. \"draft a records request letter to NTUH from 2024-01-01 to 2024-12-31\"."),
    "questions_header": ("看診時可以問的問題（請自行增刪）：", "Questions for your visit (edit as you like):"),
    "questions_save": ("儲存這份問題清單", "save this question list"),
    "add_question": ("把這個問題加到看診問題清單", "add this question to your visit question list"),
    "truncated": ("（紀錄太多，我只讀了其中 {kept}/{total} 筆最相關的。）", "(Too many records: I read only the {kept} most relevant of {total}.)"),
    "injection_note": ("（有紀錄包含像指令的文字，我把它當作資料，沒有照做。）", "(A record contained instruction-like text; I treated it as data and did not follow it.)"),
}

_SPELL_OUT = {"zh-TW": "請把內容、日期和時間寫清楚一點", "en": "please spell out what, when and the time"}

KIND_NAMES = {
    "reminder": ("提醒", "reminders"), "medication": ("用藥", "medications"),
    "appointment": ("預約", "appointments"), "question_list": ("問題清單", "question lists"),
}

_STOP_EN = set("""what did does do is are was were my me i the a an of for in on to and or please can could you tell show say says said
mean means meaning explain explanation about when how it this that these those last latest recent most report reports result results
lab labs test tests record records value values level levels any have has get from with be been there their""".split())
_STOP_ZH = ("請問", "請", "幫我", "我的", "上次", "上一次", "最近", "一次", "說什麼", "寫什麼", "說了什麼", "是什麼意思", "什麼意思", "是什麼",
            "什麼", "解釋", "說明", "一下", "報告", "結果", "紀錄", "記錄", "檢驗", "數值", "怎麼", "嗎", "呢", "的", "了", "我", "有", "在", "看")

_KIND_HINTS = (
    (re.compile(r"(?i)\blab|blood test|檢驗|抽血|血液"), "lab"),
    (re.compile(r"(?i)imaging|x-?ray|ct\b|mri|ultrasound|影像|超音波|斷層|x光"), "imaging_report"),
    (re.compile(r"(?i)patholog|biopsy|病理|切片"), "pathology_report"),
    (re.compile(r"(?i)\bvisit|門診紀錄|就診"), "visit"),
)


@dataclass
class _Pending:
    change: ProposedChange
    expires: datetime
    locale: str


@dataclass(frozen=True)
class _Plan:
    privacy: str
    scope: str
    egress: str
    mandate_id: str | None


class _Turn:
    """Receipts produced in one turn."""

    def __init__(self) -> None:
        self.seqs: list[int] = []


# --------------------------------------------------------------------------
# Parsing helpers (deterministic, both languages)
# --------------------------------------------------------------------------

_DOSE = re.compile(r"(?i)\d+(?:\.\d+)?\s*(?:mg|mcg|µg|ug|g\b|ml|mL|units?\b|iu\b|毫克|微克|毫升|顆|錠|粒|單位|片|包|cc)")
_WEEKDAYS_EN = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAYS_ZH = "一二三四五六日"


def parse_time(text: str) -> str | None:
    m = re.search(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if re.search(r"(?i)^\s*pm", text[m.end():]) and hour < 12:
            hour += 12
        if re.search(r"(下午|晚上)\s*$", text[: m.start()]) and hour < 12:
            hour += 12
        return f"{hour:02d}:{minute:02d}"
    m = re.search(r"(?i)\b(1[0-2]|0?[1-9])\s*(am|pm|a\.m\.|p\.m\.)", text)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(2).lower().startswith("p") else 0)
        return f"{hour:02d}:00"
    m = re.search(r"(早上|上午|中午|下午|晚上|凌晨|清晨)?\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[點点時](半|(\d{1,2})\s*分)?", text)
    if m:
        raw = m.group(2)
        hour = int(raw) if raw.isdigit() else _zh_num(raw)
        if hour is None or hour > 23:
            return None
        if m.group(1) in ("下午", "晚上") and hour < 12:
            hour += 12
        if m.group(1) == "中午" and hour < 6:
            hour += 12
        minute = 30 if m.group(3) == "半" else int(m.group(4) or 0)
        return f"{hour:02d}:{minute:02d}"
    m = re.search(r"(?i)\bat\s+(1[0-2]|[1-9])\b(?![:\d/])", text)
    if m:
        return f"{int(m.group(1)):02d}:00"
    return None


def _zh_num(s: str) -> int | None:
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + digits.get(s[1:], 0)
    if "十" in s:
        a, _, b = s.partition("十")
        return digits.get(a, 0) * 10 + (digits.get(b, 0) if b else 0)
    return digits.get(s)


def _valid_date(y: int, m: int, d: int) -> date | None:
    if y < 1900:
        y += 1911  # ROC (民國) year
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_dates(text: str, today: date) -> list[date]:
    """Dates in reading order: ISO, Y/M/D, ROC years, M/D, 月日, relative words, weekdays."""
    found: list[tuple[int, date]] = []
    taken: list[tuple[int, int]] = []

    def add(pos: int, end: int, value: date | None) -> None:
        if value is not None and not any(a <= pos < b for a, b in taken):
            found.append((pos, value))
            taken.append((pos, end))

    for m in re.finditer(r"(?<!\d)(\d{2,4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)", text):
        add(m.start(), m.end(), _valid_date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in re.finditer(r"(?:(?:民國)?(\d{2,4})\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日號号]?", text):
        y = int(m.group(1)) if m.group(1) else today.year
        value = _valid_date(y, int(m.group(2)), int(m.group(3)))
        if value and not m.group(1) and value < today:
            value = _valid_date(y + 1, int(m.group(2)), int(m.group(3)))
        add(m.start(), m.end(), value)
    for m in re.finditer(r"(?<![\d:/.-])(\d{1,2})/(\d{1,2})(?![\d:/])", text):
        value = _valid_date(today.year, int(m.group(1)), int(m.group(2)))
        if value and value < today:
            value = _valid_date(today.year + 1, int(m.group(1)), int(m.group(2)))
        add(m.start(), m.end(), value)
    rel = {"today": 0, "今天": 0, "tomorrow": 1, "明天": 1, "後天": 2, "day after tomorrow": 2}
    for word, days in rel.items():
        for m in re.finditer(re.escape(word), text, re.IGNORECASE):
            add(m.start(), m.end(), today + timedelta(days=days))
    for m in re.finditer(r"(?i)\b(next\s+)?(" + "|".join(_WEEKDAYS_EN) + r")\b", text):
        wd = _WEEKDAYS_EN.index(m.group(2).lower())
        delta = (wd - today.weekday()) % 7 or 7
        if m.group(1) and delta < 7:
            delta += 7 if (wd - today.weekday()) % 7 == 0 else 0
        add(m.start(), m.end(), today + timedelta(days=delta))
    for m in re.finditer(r"(下)?(?:週|周|星期|禮拜)([一二三四五六日天])", text):
        wd = _WEEKDAYS_ZH.index("日" if m.group(2) == "天" else m.group(2))
        if m.group(1):
            monday_next = today + timedelta(days=7 - today.weekday())
            value = monday_next + timedelta(days=wd)
        else:
            value = today + timedelta(days=(wd - today.weekday()) % 7 or 7)
        add(m.start(), m.end(), value)
    return [d for _, d in sorted(found, key=lambda t: t[0])]


def extract_json_action(text: str) -> dict[str, Any] | None:
    """First JSON object in a model answer, tolerating code fences and prose around it."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, min(len(text), start + 4000)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                    except ValueError:
                        break
                    return obj if isinstance(obj, dict) else None
        start = text.find("{", start + 1)
    return None


def validate_action(obj: Mapping[str, Any], *, user_text: str = "", from_model: bool = False,
                    existing: Mapping[str, Item] | None = None) -> dict[str, Any]:
    """Strict schema check. Returns {"op", "kind", "item_id", "data"}; raises ActionError.

    From the model, every number in a free-text field must appear in the
    user's own message: the model may not choose a dose, a time or a count.
    """
    if not isinstance(obj, Mapping):
        raise ActionError("not an object")
    extra = set(obj) - {"action", "kind", "item_id", "data"}
    if extra:
        raise ActionError("unknown fields")
    op = obj.get("action")
    if op not in OPS:
        raise ActionError("unsupported action")
    kind = obj.get("kind")
    if kind not in ITEM_SCHEMA:
        raise ActionError("unsupported kind")
    item_id = obj.get("item_id")
    if op in ("update_item", "delete_item"):
        if not isinstance(item_id, str) or existing is None or item_id not in existing or existing[item_id].kind != kind:
            raise ActionError("unknown item")
    elif item_id is not None:
        raise ActionError("item_id is only for update or delete")
    data = obj.get("data", {}) if op != "delete_item" else {}
    if not isinstance(data, Mapping):
        raise ActionError("data must be an object")
    schema = ITEM_SCHEMA[kind]
    if set(data) - set(schema):
        raise ActionError("unknown data fields")
    clean: dict[str, Any] = {}
    for name, (ftype, limit, required) in schema.items():
        value = data.get(name)
        if value is None or value == "" or value == []:
            if required and op == "add_item":
                raise ActionError(f"missing {name}")
            continue
        if ftype == "list":
            if not isinstance(value, list) or not value or len(value) > 20 or not all(isinstance(v, str) and 0 < len(v.strip()) <= limit for v in value):
                raise ActionError(f"bad {name}")
            clean[name] = [v.strip() for v in value]
            continue
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise ActionError(f"bad {name}")
        value = value.strip()
        if ftype == "time" and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ActionError(f"bad {name}")
        if ftype == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                raise ActionError(f"bad {name}") from None
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ActionError(f"bad {name}")
        if ftype == "repeat" and value not in ("daily", "weekly", "none"):
            raise ActionError(f"bad {name}")
        if "\x00" in value or re.search(r"[\x01-\x08\x0b\x0c\x0e-\x1f]", value):
            raise ActionError(f"bad {name}")
        clean[name] = value
    if op == "update_item" and not clean:
        raise ActionError("nothing to change")
    if from_model:
        user_numbers = set(re.findall(r"\d+(?:\.\d+)?", user_text))
        for name, value in clean.items():
            if ITEM_SCHEMA[kind][name][0] in ("time", "date"):
                continue
            for v in value if isinstance(value, list) else [value]:
                if any(n not in user_numbers for n in re.findall(r"\d+(?:\.\d+)?", v)):
                    raise ActionError("the model added a number the user did not say")
        if kind == "medication" and "name" in clean and clean["name"].lower() not in user_text.lower():
            raise ActionError("the model named a medicine the user did not say")
    return {"op": op, "kind": kind, "item_id": item_id, "data": clean}


# --------------------------------------------------------------------------
# Intent rules
# --------------------------------------------------------------------------

def _r(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE)


_INTENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("show_record", _r(r"\b(?:show|open|display|view)\s+(?:me\s+)?(?:the\s+)?record\s+\[?(?:R:)?([A-Za-z0-9_\-]{3,64})\]?|(?:顯示|打開|開啟|查看|看)(?:這筆|那筆)?(?:紀錄|記錄)\s*\[?(?:R:)?([A-Za-z0-9_\-]{3,64})\]?")),
    ("letter", _r(r"\b(?:draft|write|prepare)\b.{0,40}\b(?:request|letter)\b|\brequest\s+(?:a\s+copy\s+of\s+)?my\s+(?:medical\s+)?records\b|病歷.{0,6}(?:申請|調閱|複製本|影本)|(?:申請|調閱).{0,6}病歷")),
    ("questions", _r(r"\bprepare\b.{0,30}\bquestions?\b|\bquestions?\s+for\s+(?:my\s+)?(?:next\s+)?(?:visit|appointment|doctor|clinician)\b|\bquestion\s+list\b|準備.{0,10}問題|看診.{0,4}(?:要|想)?問(?:的)?問題|問題清單|要問(?:醫師|醫生)的")),
    ("add_question", _r(r"^\s*(?:add|note|save)\s+(?:a\s+)?(?:question|concern)\s*[:：]|^\s*(?:記下|新增|加入)(?:一個)?(?:問題|疑問)\s*[:：]?")),
    ("upcoming", _r(r"\bcoming\s+up\b|\bupcoming\b|\bwhat'?s\s+next\b|\bnext\s+appointment\b|\bmy\s+schedule\b|接下來|近期|下次回診|最近的(?:預約|提醒|行程)|有什麼行程|快到了")),
    ("timeline", _r(r"\btimeline\b|\b(?:list|show)\s+(?:all\s+)?(?:of\s+)?my\s+records\b|\ball\s+my\s+records\b|時間軸|時間線|所有(?:的)?紀錄|列出(?:我的)?紀錄|紀錄清單")),
    ("list_items", _r(r"\b(?:list|show|what\s+are|see)\b.{0,20}\b(reminders?|medications?|meds|medicines|appointments?)\b|(?:列出|顯示|查看|看)(?:我的)?(?:所有)?(提醒|用藥|藥物|藥|預約|回診)|(提醒|用藥|藥物|預約|回診)(?:清單|列表|有哪些)|有哪些(提醒|藥|預約|回診)")),
    ("add_item", _r(r"\bremind\s+me\b|\badd\b.{0,30}\b(?:reminder|medication|med|medicine|appointment)\b|\bset\s+(?:a\s+|an\s+)?(?:reminder|alarm)\b|\bi\s+(?:have|got)\s+an?\s+(?:appointment|visit|check-?up)\b|\bnew\s+(?:reminder|appointment|medication)\b|\bi(?:'m|\s+am)\s+(?:now\s+)?taking\b|^\s*i\s+take\b|提醒我|新增|加入|加一個|設(?:定)?(?:一個)?提醒|我有.{0,12}(?:回診|門診|看診|預約)|預約了|我(?:現在)?(?:有)?在吃")),
    ("delete_item", _r(r"\b(?:delete|remove|cancel)\b|(?:刪除|移除|刪掉|取消)")),
    ("update_item", _r(r"\b(?:change|update|move|reschedule|edit)\b|(?:改成|改為|更改|修改|改到|延到|提前到|換成)")),
    ("explain", _r(r"\bwhat\s+(?:does|do|is|are)\b.{0,60}\bmean\b|\bmeaning\s+of\b|\bexplain\b|\bwhat\s+is\s+(?:an?\s+)?[A-Za-z0-9\-]+\s*\??$|什麼意思|是什麼|解釋|說明|看不懂")),
)

_KIND_WORDS = (
    ("appointment", _r(r"appointment|visit|check-?up|回診|門診|看診|預約|掛號")),
    ("medication", _r(r"\bmedications?\b|\bmeds?\b|\bmedicines?\b|\bi\s+take\b|\btaking\b|用藥|藥物|在吃|藥")),
    ("reminder", _r(r"remind|reminder|alarm|提醒")),
    ("question_list", _r(r"question|問題")),
)


def _kind_from(text: str, default: str | None = None) -> str | None:
    if re.search(r"(?i)remind\s+me|提醒我", text):
        return "reminder"
    for kind, rx in _KIND_WORDS:
        if rx.search(text):
            return kind
    return default


def classify(text: str) -> tuple[str, re.Match[str] | None]:
    for name, rx in _INTENTS:
        m = rx.search(text)
        if m:
            if name in ("delete_item", "update_item") and not _kind_from(text):
                # No kind word: it may still name an item ("change metformin to 1000 mg").
                return f"maybe_{name}", m
            return name, m
    if re.search(r"(?i)\b(?:what\s+did|my\s+(?:last|latest|recent))\b|報告|檢驗|紀錄|記錄|上次|結果", text):
        return "ask", None
    return "ask", None


# --------------------------------------------------------------------------
# The bot
# --------------------------------------------------------------------------

class HealthBot:
    """See module docstring."""

    def __init__(
        self,
        vault: Vault,
        mandates: MandateBook,
        receipts: ReceiptLog,
        llm: LLM,
        tenant: Tenant,
        clock: Callable[[], datetime] | None = None,
        *,
        timezone_name: str = "Asia/Taipei",
        context_budget_tokens: int = 6000,
        per_record_chars: int = 4000,
        max_records: int = 8,
        alias: str = "smart",
        max_tokens: int = 800,
        change_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        self.vault = vault
        self.mandates = mandates
        self.receipts = receipts
        self.llm = llm
        self.tenant = tenant
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tz = _tz(timezone_name)
        self.context_budget_tokens = context_budget_tokens
        self.per_record_chars = per_record_chars
        self.max_records = max_records
        self.alias = alias
        self.max_tokens = max_tokens
        self.change_ttl = change_ttl
        self._pending: dict[str, _Pending] = {}
        self._turn = _Turn()

    # ------------------------------------------------------------ plumbing

    def _now(self) -> datetime:
        now = self.clock()
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)

    def _today(self) -> date:
        return self._now().astimezone(self.tz).date()

    def _t(self, key: str, loc: str, **kw: Any) -> str:
        zh, en = T[key]
        return (zh if loc == "zh-TW" else en).format(**kw)

    def _kind_name(self, kind: str, loc: str) -> str:
        zh, en = KIND_NAMES.get(kind, (kind, kind))
        return zh if loc == "zh-TW" else en

    def _receipt(self, action: str, *, mandate_id: str | None = None, record_refs: Sequence[str] = (), egress: str = "none",
                 model: str | None = None, privacy: str | None = None, detail: str = "") -> None:
        receipt = self.receipts.append("bot", action, mandate_id=mandate_id, record_refs=tuple(record_refs), egress=egress,
                                       model=model, privacy=privacy, detail=detail[:120])
        self._turn.seqs.append(receipt.seq)

    def _check(self, scope: str, kinds: Sequence[str] = (), egress: str = "none") -> Decision:
        return self.mandates.check(GRANTEE, Action(scope, tuple(sorted(set(kinds))), egress))

    def _deny(self, scope: str, decision: Decision, loc: str, **extra: Any) -> BotReply:
        self._receipt(scope, detail=f"denied: {scope} not allowed by any mandate")
        return self._reply(self._t("denied", loc, reason=decision.reason), **extra)

    def _reply(self, text: str, **kw: Any) -> BotReply:
        return BotReply(text=text, receipts=tuple(self._turn.seqs), **kw)

    def _locale(self, text: str) -> str:
        default = "zh-TW" if self.tenant.locale.lower().startswith("zh") else "en"
        return detect_locale(text, default)

    # ------------------------------------------------------------ chat

    def chat(self, user_text: str, history: Sequence[Mapping[str, str]] = (), *, pasted: str | None = None) -> BotReply:
        """One turn. `pasted` is report text the user attached: data, never screened as their own words."""
        self._turn = _Turn()
        self._expire()
        text = (user_text or "").strip()[:4000]
        loc = self._locale(text)
        verdict = screen_user_text(text, loc)
        if verdict.level == "urgent":
            # Nothing else this turn: no read, no model call, so no receipt that
            # would record that a crisis happened.
            return self._reply(emergency_message(self.tenant.emergency_text, verdict, loc), safety="urgent")
        intent, match = classify(text)
        # Storing the user's own words is logistics, even if they contain an
        # advice question ("remind me to ask whether to stop X"). Changing or
        # deleting is not exempt: "should I change my medication?" is declined.
        write_intents = ("add_item", "add_question", "questions")
        if verdict.level == "declined_medical_advice" and intent not in write_intents:
            return self._declined(text, loc)
        try:
            if intent == "show_record" and match is not None:
                return self._show_record(match.group(1) or match.group(2), loc)
            if intent == "timeline":
                return self._timeline(loc)
            if intent == "upcoming":
                return self._upcoming(loc)
            if intent == "list_items":
                return self._list_items(text, loc)
            if intent == "letter":
                return self._letter(text, loc)
            if intent == "questions":
                return self._questions(text, loc)
            if intent == "add_question":
                body = re.split(r"[:：]", text, maxsplit=1)[-1].strip() or text
                return self._propose({"action": "add_item", "kind": "question_list", "data": {"questions": [body[:300]]}}, loc, text)
            if intent == "add_item":
                return self._add_item(text, loc)
            if intent in ("update_item", "delete_item"):
                return self._change_item(intent, text, loc)
            if intent.startswith("maybe_"):
                reply = self._change_item(intent[len("maybe_"):], text, loc, fallthrough=True)
                if reply is not None:
                    return reply
            return self._ask(text, loc, "explain" if intent == "explain" else "answer", history, pasted)
        except VaultLocked:
            return self._reply(self._t("locked", loc))
        except VaultError:
            # Typically a receipt that could not be written. An action we cannot
            # account for is not shown; the message never echoes the cause.
            return self._reply(self._t("receipt_failed", loc))

    # ------------------------------------------------------------ declined / urgent

    def _declined(self, text: str, loc: str) -> BotReply:
        reply_text = f"{self._t('declined', loc)}\n{self._t('declined_offer', loc)}"
        change = self._make_change("add_item", "question_list", None, {"questions": [text[:300]]}, self._t("add_question", loc), loc)
        return self._reply(f"{reply_text}\n{self._t('proposed', loc, summary=change.summary)}",
                           safety="declined_medical_advice", proposed=(change,))

    # ------------------------------------------------------------ reads without a model

    def _readable_kinds(self, kinds: Sequence[str]) -> set[str]:
        return {k for k in set(kinds) if self._check("vault.read", (k,)).allowed}

    def _show_record(self, record_id: str, loc: str) -> BotReply:
        decision = self._check("vault.read")
        if not decision.allowed:
            return self._deny("vault.read", decision, loc)
        try:
            record, text = self.vault.get(record_id)
        except (KeyError, LookupError, VaultError) as exc:
            if isinstance(exc, VaultLocked):
                raise
            return self._reply(self._t("record_missing", loc))
        kind_ok = self._check("vault.read", (record.kind,))
        if not kind_ok.allowed:
            return self._deny("vault.read", kind_ok, loc)
        self._receipt("vault.read", mandate_id=kind_ok.mandate_id, record_refs=(record.content_sha256,), detail="show 1 record")
        header = f"{record.title}（{record.occurred_on or '—'}，{record.source or '—'}）[R:{record.id}]" if loc == "zh-TW" else \
            f"{record.title} ({record.occurred_on or '-'}, {record.source or '-'}) [R:{record.id}]"
        return self._reply(f"{header}\n\n{text}", cited_records=(record.id,))

    def _timeline(self, loc: str) -> BotReply:
        decision = self._check("vault.read")
        if not decision.allowed:
            return self._deny("vault.read", decision, loc)
        records = self.vault.list(limit=200)
        allowed = self._readable_kinds([r.kind for r in records])
        records = [r for r in records if r.kind in allowed]
        self._receipt("vault.read", mandate_id=decision.mandate_id, record_refs=[r.content_sha256 for r in records],
                      detail=f"timeline: {len(records)} records, titles only")
        if not records:
            return self._reply(self._t("timeline_empty", loc))
        records.sort(key=lambda r: (r.occurred_on or r.added_at[:10]), reverse=True)
        lines = [self._t("timeline", loc)]
        lines += [f"- {r.occurred_on or '----------'}  {r.title}  [R:{r.id}]" for r in records]
        return self._reply("\n".join(lines), cited_records=tuple(r.id for r in records))

    def _items(self, loc: str, kinds: Sequence[str] | None = None) -> tuple[list[Item] | None, BotReply | None]:
        decision = self._check("vault.read")
        if not decision.allowed:
            return None, self._deny("vault.read", decision, loc)
        items = [i for i in self.vault.list_items() if kinds is None or i.kind in kinds]
        self._receipt("vault.read", mandate_id=decision.mandate_id, detail=f"items: {len(items)}")
        return items, None

    def _fmt_item(self, item: Item) -> str:
        d = item.data
        if item.kind == "medication":
            parts = [d.get("name", ""), d.get("dose", ""), d.get("schedule", "")]
        elif item.kind == "appointment":
            parts = [d.get("date", ""), d.get("time", ""), d.get("title", ""), d.get("place", "")]
        elif item.kind == "reminder":
            parts = [d.get("date", ""), d.get("time", ""), d.get("text", ""), "" if d.get("repeat") in (None, "none") else f"({d['repeat']})"]
        else:
            parts = [d.get("title", "")] + [f"\n    · {q}" for q in d.get("questions", [])]
        return " ".join(str(p) for p in parts if p) + f"  [{item.id}]"

    def _list_items(self, text: str, loc: str) -> BotReply:
        kind = _kind_from(text, "reminder") or "reminder"
        items, denied = self._items(loc, (kind,))
        if denied:
            return denied
        assert items is not None
        if not items:
            return self._reply(self._t("empty_list", loc, kind=self._kind_name(kind, loc)))
        return self._reply("\n".join(f"- {self._fmt_item(i)}" for i in items))

    def _upcoming(self, loc: str) -> BotReply:
        items, denied = self._items(loc, ("appointment", "reminder"))
        if denied:
            return denied
        assert items is not None
        now = self._now().astimezone(self.tz)
        today, horizon = now.date(), now.date() + timedelta(days=60)
        events: list[tuple[datetime, Item]] = []
        for item in items:
            when = self._next_time(item, now)
            if when is not None and today <= when.date() <= horizon:
                events.append((when, item))
        if not events:
            return self._reply(self._t("nothing_upcoming", loc))
        events.sort(key=lambda e: e[0])
        lines = [self._t("upcoming", loc)]
        for when, item in events[:8]:
            label = item.data.get("title") or item.data.get("text") or ""
            lines.append(f"- {when:%Y-%m-%d %H:%M}  {label}  [{item.id}]")
        return self._reply("\n".join(lines))

    def _next_time(self, item: Item, now: datetime) -> datetime | None:
        d = item.data
        try:
            hhmm = time.fromisoformat(d["time"]) if d.get("time") else None
        except ValueError:
            hhmm = None
        if d.get("date"):
            try:
                day = date.fromisoformat(d["date"])
            except ValueError:
                return None
            when = datetime.combine(day, hhmm or time(0, 0), self.tz)
            return when if when >= now or (hhmm is None and day == now.date()) else None
        if item.kind == "reminder" and hhmm is not None:
            when = datetime.combine(now.date(), hhmm, self.tz)
            if when < now:
                when += timedelta(days=7 if d.get("repeat") == "weekly" else 1)
            return when
        return None

    # ------------------------------------------------------------ drafts

    def _letter(self, text: str, loc: str) -> BotReply:
        decision = self._check("draft.letter")
        if not decision.allowed:
            return self._deny("draft.letter", decision, loc)
        hospital = None
        m = re.search(r"(?:向|給|寄給|跟|到)\s*([^\s，,。]{2,30}?(?:醫院|診所|醫學中心|分院|院區))", text) or \
            re.search(r"([^\s，,。給向]{2,30}?(?:醫院|診所|醫學中心|分院|院區))", text) or \
            re.search(r"(?i)\bto\s+(?:the\s+)?(.+?)(?=\s+(?:for|from|covering|between)\b|[,.;]|$)", text)
        if m:
            hospital = m.group(1).strip()
        dates = parse_dates(text, self._today())
        all_range = re.search(r"(?i)\ball\b|全部|所有", text)
        if not hospital or (len(dates) < 2 and not all_range):
            return self._reply(self._t("letter_need", loc))
        if len(dates) >= 2:
            start, end = sorted(dates[:2])
            span_zh, span_en = f"{start.isoformat()} 至 {end.isoformat()}", f"from {start.isoformat()} to {end.isoformat()}"
        else:
            span_zh, span_en = "所有就醫期間", "for all dates of care"
        today = self._today().isoformat()
        if loc == "zh-TW":
            body = (
                "【草稿｜請自行確認、簽名後寄出或送交，本助理不會代為寄送】\n"
                "主旨：申請病歷複製本\n\n"
                f"{hospital} 病歷管理單位 您好：\n\n"
                "本人（姓名：＿＿＿＿，出生日期：＿＿＿＿，病歷號碼：＿＿＿＿）"
                f"依醫療法第71條，申請提供本人{span_zh}在貴院就醫之病歷複製本；必要時並請提供中文病歷摘要。所需費用由本人負擔。\n\n"
                "申請項目：□門診紀錄 □住院紀錄（含出院病摘） □檢驗報告 □影像報告 □其他：＿＿＿＿\n"
                "取件方式：□親自領取 □郵寄至：＿＿＿＿\n"
                "聯絡電話：＿＿＿＿\n\n"
                "申請人：＿＿＿＿（簽名）\n"
                f"日期：{today}\n\n"
                "附註：申請時通常需出示身分證明文件，委託他人申請可能需要委託書；實際流程與費用請依醫院規定。"
            )
        else:
            body = (
                "[DRAFT - review, sign and send it yourself; the assistant does not send anything]\n"
                "Subject: Request for a copy of my medical records\n\n"
                f"To the Medical Records Office, {hospital}:\n\n"
                "I, ________ (date of birth ________, medical record number ________), request a copy of my medical records "
                f"{span_en}, under Article 71 of Taiwan's Medical Care Act, and a Chinese-language summary where needed. "
                "I will pay the applicable fees.\n\n"
                "Records requested: [ ] outpatient [ ] inpatient / discharge summary [ ] lab reports [ ] imaging reports [ ] other: ____\n"
                "Delivery: [ ] collect in person [ ] post to: ________\n"
                "Phone: ________\n\n"
                "Signature: ________\n"
                f"Date: {today}\n\n"
                "Note: hospitals usually ask for photo ID, and a letter of authorisation if someone applies for you. Fees and steps vary by hospital."
            )
        self._receipt("draft.letter", mandate_id=decision.mandate_id, detail="records request letter drafted (template, no model)")
        return self._reply(body)

    def _questions(self, text: str, loc: str) -> BotReply:
        decision = self._check("draft.letter")
        if not decision.allowed:
            return self._deny("draft.letter", decision, loc)
        concerns: list[str] = []
        tail = re.split(r"[:：]|\babout\b|關於", text, maxsplit=1)
        if len(tail) == 2:
            concerns = [c.strip(" -·•。.") for c in re.split(r"[;；,，、\n]|\band\b|還有|以及", tail[1]) if len(c.strip(" -·•。.")) >= 2]
        saved: list[str] = []
        if self._check("vault.read").allowed:
            items, _ = self._items(loc, ("question_list",))
            for item in items or []:
                saved.extend(q for q in item.data.get("questions", []) if isinstance(q, str))
        if loc == "zh-TW":
            generic = ["我最近的檢查結果代表什麼？需要追蹤或複查嗎？", "我目前用的藥有沒有需要注意的地方？", "有哪些狀況出現時，我應該提早回診或去急診？"]
            concerns_q = [c if c.endswith(("？", "?")) else f"關於「{c}」，我該知道什麼？" for c in concerns]
        else:
            generic = ["What do my latest results mean, and do they need follow-up?", "Is there anything I should know about my current medicines?",
                       "Which symptoms should make me come back early or go to the emergency department?"]
            concerns_q = [c if c.endswith("?") else f"About \"{c}\": what should I know?" for c in concerns]
        questions = list(dict.fromkeys(concerns_q + saved + generic))[:20]
        lines = [self._t("questions_header", loc)] + [f"{i}. {q}" for i, q in enumerate(questions, 1)]
        self._receipt("draft.letter", mandate_id=decision.mandate_id, detail=f"question list drafted: {len(questions)} questions (no model)")
        title = "看診問題" if loc == "zh-TW" else "Questions for my visit"
        change = self._make_change("add_item", "question_list", None, {"title": title, "questions": [q[:300] for q in questions]},
                                   self._t("questions_save", loc), loc)
        lines.append(self._t("proposed", loc, summary=change.summary))
        return self._reply("\n".join(lines), proposed=(change,))

    # ------------------------------------------------------------ writes

    def _make_change(self, op: str, kind: str, item_id: str | None, data: dict[str, Any], summary: str, loc: str) -> ProposedChange:
        cid = secrets.token_hex(4)
        change = ProposedChange(cid, op, summary, {"op": op, "kind": kind, "item_id": item_id, "data": data})
        self._pending[cid] = _Pending(change, self._now() + self.change_ttl, loc)
        return change

    def _summary(self, op: str, kind: str, data: Mapping[str, Any], loc: str, item: Item | None = None) -> str:
        verb = {"add_item": ("新增", "add"), "update_item": ("修改", "change"), "delete_item": ("刪除", "delete")}[op][0 if loc == "zh-TW" else 1]
        shown = item.data if (item is not None and op == "delete_item") else data
        fields = "，".join if loc == "zh-TW" else ", ".join
        detail = fields(f"{k}: {v if not isinstance(v, list) else len(v)}" for k, v in shown.items())
        target = f" [{item.id}]" if item is not None else ""
        return f"{verb} {self._kind_name(kind, loc)}{target}（{detail}）" if loc == "zh-TW" else f"{verb} {self._kind_name(kind, loc)}{target} ({detail})"

    def _propose(self, obj: Mapping[str, Any], loc: str, user_text: str, *, from_model: bool = False,
                 existing: Mapping[str, Item] | None = None) -> BotReply:
        try:
            action = validate_action(obj, user_text=user_text, from_model=from_model, existing=existing)
        except ActionError as exc:
            return self._reply(self._t("need_details", loc, what=str(exc)))
        item = existing.get(action["item_id"]) if existing and action["item_id"] else None
        summary = self._summary(action["op"], action["kind"], action["data"], loc, item)
        change = self._make_change(action["op"], action["kind"], action["item_id"], action["data"], summary, loc)
        return self._reply(self._t("proposed", loc, summary=summary), proposed=(change,))

    def _add_item(self, text: str, loc: str) -> BotReply:
        kind = _kind_from(text, "reminder") or "reminder"
        today = self._today()
        when = parse_time(text)
        dates = parse_dates(text, today)
        data: dict[str, Any] = {}
        if kind == "reminder":
            body = re.sub(r"(?i)^.*?\bremind\s+me\s+(?:to\s+|about\s+)?|^.*?提醒我(?:要)?|^.*?\bset\s+(?:a\s+)?reminder\s+(?:to\s+)?", "", text, count=1).strip(" ,，。.")
            data = {"text": body or text}
            if when:
                data["time"] = when
            if dates:
                data["date"] = dates[0].isoformat()
            if re.search(r"(?i)every\s*day|daily|每天|每日", text):
                data["repeat"] = "daily"
            elif re.search(r"(?i)every\s+week|weekly|每週|每周|每星期", text):
                data["repeat"] = "weekly"
        elif kind == "medication":
            dose = _DOSE.search(text)
            m = re.search(r"(?i)(?:medication|medicine|\bmed\b|taking|\btake\b)\s*[:：]?\s*([A-Za-z][A-Za-z\-]+(?:\s+[A-Za-z][A-Za-z\-]+)?)", text) or \
                re.search(r"(?:在吃|用藥|藥物|服用|新增藥)\s*[:：]?\s*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z\-]{1,20}?)(?=\s|\d|，|,|$)", text)
            name = m.group(1).strip() if m else ""
            name = re.sub(r"(?i)\s+(?:at|every|daily|in|on)$", "", name)
            if name:
                data["name"] = name
            if dose:
                data["dose"] = dose.group(0).strip()
            sched = re.search(r"(?i)(?:\bat\b|\bevery\b|\bdaily\b|\btwice\b|每天|每日|早上|晚上|睡前|飯後|飯前|一天).*$", text[dose.end():] if dose else text)
            if sched:
                data["schedule"] = sched.group(0).strip(" ,，。.")
            data["note"] = text[:300]
        elif kind == "appointment":
            if not dates:
                return self._reply(self._t("need_details", loc, what="日期（例如 2026-12-03 或 12月3日）" if loc == "zh-TW" else "the date (e.g. 2026-12-03)"))
            data["date"] = dates[0].isoformat()
            if when:
                data["time"] = when
            place = re.search(r"(?:在|到)\s*([^\s，,。]{2,30}?(?:醫院|診所|醫學中心|分院|院區))", text) or \
                re.search(r"(?i)\bat\s+((?:[A-Z][\w'.-]*\s*){1,6}(?:Hospital|Clinic|Center|Centre))", text)
            if place:
                data["place"] = place.group(1).strip()
            data["title"] = re.sub(r"(?i)^\s*(?:add\s+(?:an?\s+)?appointment\s*[:：]?|新增(?:預約|回診)\s*[:：]?)", "", text).strip()[:120] or text[:120]
        else:
            data = {"questions": [text[:300]]}
        obj = {"action": "add_item", "kind": kind, "data": data}
        try:
            validate_action(obj)
        except ActionError:
            return self._model_action(text, loc)  # deterministic parse failed: let the model try, then validate strictly
        return self._propose(obj, loc, text)

    def _change_item(self, op: str, text: str, loc: str, fallthrough: bool = False) -> BotReply | None:
        if fallthrough and not self._check("vault.read").allowed:
            return None
        items, denied = self._items(loc)
        if denied:
            return denied
        assert items is not None
        kind = _kind_from(text)
        by_id = {i.id: i for i in items}
        target = [by_id[t] for t in re.findall(r"[A-Za-z0-9_\-]{3,64}", text) if t in by_id]
        if not target:
            lowered = text.lower()
            for item in items:
                if kind and item.kind != kind and not (kind == "reminder" and item.kind == "medication"):
                    continue
                key = str(item.data.get("name") or item.data.get("title") or item.data.get("text") or "").lower()
                words = [w for w in re.findall(r"[a-z][a-z\-]{3,}|[\u4e00-\u9fff]{2,}", key) if w not in _STOP_EN]
                if key and (key in lowered or any(w in lowered for w in words)):
                    target.append(item)
        if not target:
            return None if fallthrough else self._reply(self._t("no_item", loc))
        if len(target) > 1:
            return self._reply("\n".join([self._t("which_item", loc)] + [f"- {self._fmt_item(i)}" for i in target[:10]]))
        item = target[0]
        existing = {item.id: item}
        if op == "delete_item":
            return self._propose({"action": "delete_item", "kind": item.kind, "item_id": item.id}, loc, text, existing=existing)
        tail = re.split(r"(?i)\bto\b|改成|改為|改到|延到|提前到|換成|為", text)[-1]
        data: dict[str, Any] = {}
        when = parse_time(tail)
        dates = parse_dates(tail, self._today())
        if item.kind == "medication":
            dose = _DOSE.search(tail)
            if dose:
                data["dose"] = dose.group(0).strip()
            if when:
                data["schedule"] = when
        else:
            if when:
                data["time"] = when
            if dates:
                data["date"] = dates[0].isoformat()
        if not data:
            return self._reply(self._t("need_details", loc, what="新的時間、日期或劑量" if loc == "zh-TW" else "the new time, date or dose"))
        return self._propose({"action": "update_item", "kind": item.kind, "item_id": item.id, "data": data}, loc, text, existing=existing)

    def _model_action(self, text: str, loc: str) -> BotReply:
        """Last resort for a write we could not parse. Only the user's own words are sent."""
        plan, denial = self._egress_plan(())
        if plan is None:
            return self._reply(self._t("need_details", loc, what=_SPELL_OUT[loc]))
        messages = prompts.build_messages(loc, "action", text, [], assistant_name=self.tenant.display_name)
        try:
            result = self.llm.complete(messages, privacy=plan.privacy, alias=self.alias, max_tokens=300)
        except (LLMRefused, LLMUnavailable) as exc:
            self._receipt_failure(exc, plan, (), "action parse")
            return self._no_model(loc, plan.privacy, [])
        if not self._route_ok(result, plan):
            self._receipt("llm.complete", mandate_id=plan.mandate_id, egress="cloud", privacy=result.privacy, model=self._model_name(result),
                          detail="refused: unverified route, answer discarded (action parse)")
            return self._reply(self._t("unverified", loc), safety="no_model")
        self._receipt("llm.complete", mandate_id=plan.mandate_id, egress=plan.egress, model=self._model_name(result), privacy=result.privacy,
                      detail=f"action parse: 0 records, {len(text):,} chars")
        obj = extract_json_action(result.text)
        if not obj or obj.get("action") == "none":
            return self._reply(self._t("need_details", loc, what=_SPELL_OUT[loc]), route=self._route(result))
        reply = self._propose(obj, loc, text, from_model=True)
        return BotReply(reply.text, reply.cited_records, reply.proposed, reply.safety, reply.receipts, self._route(result))

    def confirm(self, change_id: str) -> BotReply:
        """The user said yes. Re-check the mandate now (it may have been revoked), then write."""
        self._turn = _Turn()
        pending = self._pending.pop(change_id, None)
        loc = pending.locale if pending else ("zh-TW" if self.tenant.locale.startswith("zh") else "en")
        if pending is None:
            return self._reply(self._t("unknown_change", loc))
        if self._now() > pending.expires:
            return self._reply(self._t("expired", loc))
        payload = pending.change.payload
        op, kind, item_id, data = payload["op"], payload["kind"], payload["item_id"], payload["data"]
        decision = self._check("items.write")
        if not decision.allowed:
            return self._deny("items.write", decision, loc)
        try:
            if op == "add_item":
                item = self.vault.add_item(kind, dict(data))
                item_id = item.id
            elif op == "update_item":
                self.vault.update_item(item_id, dict(data))
            elif op == "delete_item":
                self.vault.delete_item(item_id)
            else:  # never produced by this module
                return self._reply(self._t("write_failed", loc))
        except VaultLocked:
            return self._reply(self._t("locked", loc))
        except (VaultError, KeyError, LookupError, ValueError) as exc:
            self._receipt("items.write", mandate_id=decision.mandate_id, detail=f"failed: {op} {kind} ({type(exc).__name__})")
            return self._reply(self._t("write_failed", loc))
        self._receipt("items.write", mandate_id=decision.mandate_id, detail=f"{op} {kind} {item_id}, confirmed by user")
        return self._reply(self._t("confirmed", loc, summary=pending.change.summary))

    def reject(self, change_id: str) -> BotReply:
        self._turn = _Turn()
        pending = self._pending.pop(change_id, None)
        loc = pending.locale if pending else ("zh-TW" if self.tenant.locale.startswith("zh") else "en")
        if pending is None:
            return self._reply(self._t("unknown_change", loc))
        return self._reply(self._t("rejected", loc, summary=pending.change.summary))

    def pending(self) -> list[ProposedChange]:
        self._expire()
        return [p.change for p in self._pending.values()]

    def _expire(self) -> None:
        now = self._now()
        for cid in [cid for cid, p in self._pending.items() if now > p.expires]:
            del self._pending[cid]

    # ------------------------------------------------------------ model path

    def _egress_plan(self, kinds: Sequence[str]) -> tuple[_Plan | None, Decision | None]:
        """Tenant default class or stricter, never weaker; cloud only with the feature AND a mandate."""
        default = self.tenant.default_privacy if self.tenant.default_privacy in PRIVACY_ORDER else "local"
        last: Decision | None = None
        for cls in PRIVACY_ORDER[PRIVACY_ORDER.index(default):]:
            if cls not in self.tenant.allowed_privacy:
                continue
            scope = SCOPE_FOR_CLASS[cls]
            if scope == "llm.cloud" and not self.tenant.features.get("cloud_models", False):
                continue
            decision = self._check(scope, kinds, EGRESS_FOR_CLASS[cls])
            if decision.allowed:
                return _Plan(cls, scope, EGRESS_FOR_CLASS[cls], decision.mandate_id), decision
            last = decision
        return None, last

    def _retrieve(self, text: str) -> list[tuple[Record, str]]:
        """Keyword searches (robust to vaults that match whole strings), merged by hits then rank."""
        latin = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9\-.]*[A-Za-z0-9]|[A-Za-z]", text) if len(w) >= 2 and w.lower() not in _STOP_EN]
        cjk_text = " ".join(re.findall(r"[\u4e00-\u9fff]+", text))
        for stop in sorted(_STOP_ZH, key=len, reverse=True):
            cjk_text = cjk_text.replace(stop, " ")
        cjk = [w for w in cjk_text.split() if len(w) >= 2]
        queries = list(dict.fromkeys(latin + cjk))[:8]
        scores: dict[str, list[Any]] = {}
        for q in queries:
            for rank, (record, snippet) in enumerate(self.vault.search(q, limit=self.max_records)):
                entry = scores.setdefault(record.id, [0, 0.0, record, snippet])
                entry[0] += 1
                entry[1] += 1.0 / (rank + 1)
        hits = sorted(scores.values(), key=lambda e: (-e[0], -e[1], e[2].occurred_on or ""))
        if not hits:
            for rx, kind in _KIND_HINTS:
                if rx.search(text):
                    recent = sorted(self.vault.list(kind=kind, limit=50), key=lambda r: r.occurred_on or "", reverse=True)[:3]
                    return [(r, "") for r in recent]
            if re.search(r"(?i)\breport\b|報告", text):
                recent = sorted(self.vault.list(limit=50), key=lambda r: r.occurred_on or "", reverse=True)[:3]
                return [(r, "") for r in recent]
        return [(e[2], e[3]) for e in hits[: self.max_records]]

    def _ask(self, text: str, loc: str, task: str, history: Sequence[Mapping[str, str]], pasted: str | None) -> BotReply:
        decision = self._check("vault.read")
        if not decision.allowed:
            return self._deny("vault.read", decision, loc)
        found = self._retrieve(text)
        allowed_kinds = self._readable_kinds([r.kind for r, _ in found])
        found = [(r, s) for r, s in found if r.kind in allowed_kinds]

        # Assemble DATA blocks within the budget, most relevant first.
        blocks: list[str] = []
        included: list[tuple[Record, str]] = []
        sources: list[str] = [text]
        refs: list[str] = []
        used = 0
        injection = False
        if pasted:
            pasted = pasted[: self.per_record_chars * 2]
            block = wrap_untrusted(pasted, "pasted by the user")
            used += estimate_tokens(block)
            blocks.append(block)
            sources.append(pasted)
            refs.append(_sha(pasted))
            injection |= looks_like_injection(pasted)
        read_refs: list[str] = []
        for record, snippet in found:
            try:
                _, full = self.vault.get(record.id)
            except VaultLocked:
                raise
            except (KeyError, LookupError, VaultError):
                full = snippet
            read_refs.append(record.content_sha256)
            body = full if len(full) <= self.per_record_chars else full[: self.per_record_chars] + "\n[...]"
            label = f"R:{record.id} | {record.kind} | {record.occurred_on or '-'} | {record.title}"
            block = wrap_untrusted(body, label)
            cost = estimate_tokens(block)
            if used + cost > self.context_budget_tokens:
                continue
            used += cost
            blocks.append(block)
            included.append((record, body))
            sources.extend([body, label])
            refs.append(record.content_sha256)
            injection |= looks_like_injection(body)
        if read_refs:
            self._receipt("vault.read", mandate_id=decision.mandate_id, record_refs=read_refs, detail=f"search: {len(read_refs)} records")
        truncated = len(included) < len(found)
        kinds = sorted({r.kind for r, _ in included})

        if not included and not pasted and task == "answer":
            return self._reply(self._t("nothing_found", loc))

        plan, denial = self._egress_plan(kinds)
        if plan is None:
            scope = SCOPE_FOR_CLASS.get(self.tenant.default_privacy, "llm.local")
            self._receipt(scope, record_refs=(), detail=f"denied: no model mandate for class {self.tenant.default_privacy}")
            reason = denial.reason if denial else ("租戶設定不允許" if loc == "zh-TW" else "the issuer's settings do not allow it")
            return self._reply(self._t("denied", loc, reason=reason) + "\n" + self._listing(included, loc),
                               cited_records=tuple(r.id for r, _ in included))

        notes: list[str] = []
        if truncated:
            notes.append(self._t("truncated", loc, kept=len(included), total=len(found)))
        prior = [{"role": "user", "content": str(t.get("content", ""))[:500]} for t in list(history)[-6:] if t.get("role") == "user"][-3:]
        messages = prompts.build_messages(loc, task, text, blocks, assistant_name=self.tenant.display_name, history=prior,
                                          note=notes[0].strip("（）()") if notes else "")
        chars = sum(len(b) for b in blocks)
        try:
            result = self.llm.complete(messages, privacy=plan.privacy, alias=self.alias, max_tokens=self.max_tokens)
        except (LLMRefused, LLMUnavailable) as exc:
            self._receipt_failure(exc, plan, refs, f"{len(blocks)} records, {chars:,} chars")
            return self._no_model(loc, plan.privacy, included)
        if not self._route_ok(result, plan):
            self._receipt("llm.complete", mandate_id=plan.mandate_id, record_refs=refs, egress="cloud", model=self._model_name(result),
                          privacy=result.privacy, detail="refused: route not verified, answer discarded")
            return self._reply(self._t("unverified", loc) + "\n" + self._t("still_can", loc), safety="no_model")

        answer = result.text or ""
        allowed_contacts = [self.tenant.contact, *re.findall(r"\d{3,}", self.tenant.emergency_text)]
        flags = screen_model_text(answer, loc, sources=sources, allowed_contacts=allowed_contacts)
        detail = f"{len(blocks)} records, {chars:,} chars"
        if injection:
            detail += "; instruction-like text in data"
        if flags:
            detail += f"; answer withheld ({', '.join(flags)})"
        self._receipt("llm.complete", mandate_id=plan.mandate_id, record_refs=refs, egress=plan.egress, model=self._model_name(result),
                      privacy=result.privacy, detail=detail)
        route = self._route(result)
        if flags:
            text_out = "\n".join([self._t("withheld", loc), self._t("withheld_sources", loc), self._listing(included, loc, header=False)])
            safety = "declined_medical_advice" if {"directive", "diagnosis"} & set(flags) else None
            return self._reply(text_out.strip(), cited_records=tuple(r.id for r, _ in included), safety=safety, route=route)

        valid_ids = {r.id for r, _ in included}
        cited: list[str] = []
        bad = False

        def fix(m: re.Match[str]) -> str:
            nonlocal bad
            rid = m.group(1)
            if rid in valid_ids:
                if rid not in cited:
                    cited.append(rid)
                return m.group(0)
            bad = True
            return ""

        answer = re.sub(r"\[R:([A-Za-z0-9_\-]{1,64})\]", fix, answer).strip()
        if bad:
            notes.append(self._t("bad_citations", loc))
        if included and not cited:
            notes.append(self._t("no_citation", loc))
        if injection:
            notes.append(self._t("injection_note", loc))
        return self._reply("\n\n".join([answer] + notes) if notes else answer, cited_records=tuple(cited), route=route)

    # ------------------------------------------------------------ model helpers

    @staticmethod
    def _model_name(result: LLMResult) -> str | None:
        if not result.provider and not result.deployment:
            return None
        return f"{result.provider or '?'}/{result.deployment or '?'}"

    def _route(self, result: LLMResult) -> str | None:
        name = self._model_name(result)
        return f"{name} ({result.privacy})" if name else None

    @staticmethod
    def _route_ok(result: LLMResult, plan: _Plan) -> bool:
        """Belt and braces on top of GatewayLLM: the class actually enforced must be reported and not weaker."""
        if not result.privacy or result.privacy not in PRIVACY_ORDER:
            return False
        return PRIVACY_ORDER.index(result.privacy) >= PRIVACY_ORDER.index(plan.privacy)

    def _receipt_failure(self, exc: Exception, plan: _Plan, refs: Sequence[str], what: str) -> None:
        if getattr(exc, "content_sent", False):
            # The gateway answered without proving the class: content may have
            # reached any provider. Record the worst case.
            egress, detail = "cloud", f"refused: route not verified, answer discarded ({what})"
        elif isinstance(exc, LLMRefused):
            egress, detail = "none", f"refused: no model allowed for class {plan.privacy} ({what})"
        else:
            name = type(exc).__name__
            egress = "none" if name == "RequestTooLarge" else plan.egress
            detail = f"unavailable: {name} ({what})"
        self._receipt("llm.complete", mandate_id=plan.mandate_id, record_refs=refs, egress=egress, privacy=plan.privacy, detail=detail)

    def _listing(self, included: Sequence[tuple[Record, str]], loc: str, header: bool = True) -> str:
        if not included:
            return ""
        lines = [self._t("found_records", loc)] if header else []
        lines += [f"- {r.occurred_on or '----------'}  {r.title}  [R:{r.id}]" for r, _ in included]
        return "\n".join(lines)

    def _no_model(self, loc: str, privacy: str, included: Sequence[tuple[Record, str]]) -> BotReply:
        if privacy == "local" and self.tenant.issuer == "hospital":
            first = self._t("no_model_hospital", loc)
        else:
            first = self._t("no_model", loc, cls=privacy)
        parts = [first, self._t("still_can", loc)]
        listing = self._listing(included, loc)
        if listing:
            parts.append(listing)
        return self._reply("\n".join(parts), safety="no_model", cited_records=tuple(r.id for r, _ in included))
