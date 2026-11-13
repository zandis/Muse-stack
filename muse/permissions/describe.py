"""The sentence a person reads (interface document, section 9).

Twin of core/src/permissions/describe.rs: the same words, byte for byte, in English
and Traditional Chinese. It is computed from the token alone; for a narrowed token it
describes what every level allows together.
"""

from __future__ import annotations

from typing import NamedTuple

from muse.permissions.model import PermissionError, Resource
from muse.permissions.token import Mandate, rfc3339

LANGS = ("en", "zh-TW")


class Description(NamedTuple):
    sentence: str
    lines: tuple[str, ...]


_ACTIONS = {
    "files.read": ("read files", "讀取檔案"),
    "files.write": ("save and change files", "儲存與修改檔案"),
    "mail.read": ("read mail", "讀取郵件"),
    "mail.draft": ("draft mail", "草擬郵件"),
    "mail.send": ("send mail", "寄出郵件"),
    "calendar.read": ("read calendar events", "讀取行事曆"),
    "calendar.write": ("add and change calendar events", "新增與修改行事曆"),
    "llm.call": ("ask AI services", "使用 AI 服務"),
    "llm.paid": ("use paid AI services", "使用付費 AI 服務"),
    "pay.spend": ("spend money", "付款"),
    "health.read": ("read health records", "讀取健康紀錄"),
    "records.read": ("read records", "讀取紀錄"),
    "home.admin": ("change the settings of your home", "變更家用主機的設定"),
}

# kind -> ((one, any) in English, (one, any) in Chinese); `{v}` is the value
_RESOURCES = {
    "folder": (("the folder {v}", "any folder"), ("資料夾 {v}", "任何資料夾")),
    "host": (("the service host {v}", "any service host"), ("服務主機 {v}", "任何服務主機")),
    "calendar": (("the calendar {v}", "any calendar"), ("行事曆 {v}", "任何行事曆")),
    "record_kind": (("records of the kind {v}", "records of any kind"), ("「{v}」類紀錄", "任何類別的紀錄")),
    "provider": (("the provider {v}", "any provider"), ("服務商 {v}", "任何服務商")),
    "home": (("the home {v}", "any home"), ("家用主機 {v}", "任何家用主機")),
}

_HOLDERS = {
    "helper": ('Your helper "{id}"', "你的助手「{id}」"),
    "device": ('Your device "{id}"', "你的裝置「{id}」"),
    "app": ('The app "{id}"', "應用程式「{id}」"),
    "clinician": ('The clinician "{id}"', "醫師「{id}」"),
}

_PRIVACY = {
    "local": ("Nothing may leave your home: local models only", "資料不得離開你的家用主機：只用本機模型"),
    "private": ("May send data only to services rated private", "資料只能送往「私密」等級的服務"),
    "standard": ("May send data to services rated standard or private", "資料可送往「標準」或「私密」等級的服務"),
    "public": ("May send data to any service", "資料可送往任何服務"),
}

_SYMBOLS = {"TWD": "NT$", "USD": "US$", "EUR": "€", "JPY": "JP¥"}


def _action_phrase(action: str, en: bool) -> str:
    if action in _ACTIONS:
        return _ACTIONS[action][0 if en else 1]
    return f'do "{action}"' if en else f"執行「{action}」"


def _resource_phrase(r: Resource, en: bool) -> str:
    one, any_ = _RESOURCES[r.kind][0 if en else 1]
    return any_ if r.value == "*" else one.replace("{v}", r.value)


def _holder_phrase(holder: str, en: bool) -> str:
    kind, _, ident = holder.partition(":")
    return _HOLDERS.get(kind, _HOLDERS["clinician"])[0 if en else 1].replace("{id}", ident)


def _join(items: list[str], en: bool) -> str:
    """`a`, `a and b`, `a, b and c`; in Chinese `a、b、c`."""
    if not en:
        return "、".join(items)
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def format_amount(kind: str, unit: str, decimals: int, amount: int, lang: str) -> str:
    """An amount in minor units as people write it: `NT$20`, `NT$20.50`, `1.50 USDC`."""
    en = lang == "en"
    if kind != "spend":
        if kind == "requests":
            return f"{amount} requests" if en else f"{amount} 次請求"
        return f"{amount} pushed messages" if en else f"{amount} 則推送訊息"
    digits = str(amount).rjust(decimals + 1, "0")
    whole, frac = digits[: len(digits) - decimals], digits[len(digits) - decimals:]
    frac = frac.rstrip("0")
    number = whole if not frac else f"{whole}.{frac.ljust(min(2, decimals), '0')}"
    symbol = _SYMBOLS.get(unit)
    return f"{symbol}{number}" if symbol else f"{number} {unit}"


def format_time(t: int) -> str:
    """`2026-11-30 00:00:00 UTC`."""
    text = rfc3339(t)
    return f"{text[:10]} {text[11:19]} UTC"


def describe(mandate: Mandate, lang: str = "en") -> Description:
    """The sentence and its lines, in `lang` (`en` or `zh-TW`)."""
    if lang not in LANGS:
        raise PermissionError("invalid_language", "lang is en or zh-TW")
    m, en = mandate, lang == "en"
    holder = _holder_phrase(m.draft.holder, en)
    effective = m.actions()
    actions = [_action_phrase(a, en) for a in effective]
    resources = [_resource_phrase(r, en) for r in m.resources()]
    if not actions or not resources:
        sentence = f"{holder} may do nothing: this narrowed copy allows no action." if en else f"{holder}不能做任何事：這份縮小後的許可不允許任何動作。"
    elif en:
        sentence = f"{holder} may {_join(actions, en)}, limited to {_join(resources, en)}."
    else:
        sentence = f"{holder}可以{_join(actions, en)}，範圍限於{_join(resources, en)}。"

    lines: list[str] = []
    limits = m.limits()
    for l in limits:
        amount = format_amount(l.kind, l.unit, l.decimals, l.amount, lang)
        if en:
            lines.append(f"Up to {amount} a day" if l.period == "day" else f"Up to {amount} a month")
        else:
            lines.append(f"每日最多 {amount}" if l.period == "day" else f"每月最多 {amount}")
    if limits:
        zone = m.draft.period_zone
        lines.append(f"Days and months are counted in {zone} time" if en else f"每日與每月以 {zone} 時間計算")
    for c in m.caps():
        amount = format_amount(c.kind, c.unit, m.decimals(c.kind, c.unit), c.amount, lang)
        lines.append(f"No more than {amount} at a time" if en else f"每次最多 {amount}")
    start, end = format_time(m.not_before()), format_time(m.not_after())
    lines.append(f"From {start} until {end}" if en else f"自 {start} 起，至 {end} 止")

    rules = m.confirm_rules()
    if any(r.rule == "each_write" for r in rules):
        lines.append("Asks you before every change or payment" if en else "每次變更或付款前都會先問你")
    else:
        # One line per unit, with the lowest threshold in the chain.
        over: dict[str, tuple[int, int]] = {}
        for r in rules:
            if r.rule == "over_amount":
                over[r.unit] = (over[r.unit][0], min(over[r.unit][1], r.amount)) if r.unit in over else (r.decimals, r.amount)
        for unit in sorted(over):
            money = format_amount("spend", unit, over[unit][0], over[unit][1], lang)
            lines.append(f"Asks you before any payment over {money}, and before every other change" if en else f"超過 {money} 的付款，以及其他每次變更，都會先問你")
        if not over:
            lines.append("Does not ask before acting" if en else "行動前不會先問你")
    routine = [_action_phrase(a, en) for a in m.draft.routine if a in effective]
    if routine and not (len(rules) == 1 and rules[0].rule == "never"):
        lines.append(f"Without asking each time: {_join(routine, en)}" if en else f"不必每次詢問：{_join(routine, en)}")
    lines.append(_PRIVACY[m.privacy()][0 if en else 1])
    text, code = m.draft.purpose_text, m.draft.purpose_code
    if text or code:
        if en:
            lines.append(f"Purpose: {text} ({code})" if code else f"Purpose: {text}")
        else:
            lines.append(f"用途：{text}（{code}）" if code else f"用途：{text}")
    for ident in m.jobs():
        lines.append(f"Only for the job {ident}" if en else f"只限工作 {ident}")
    for ident in m.tools():
        lines.append(f"Only for one tool call ({ident})" if en else f"只限一次工具呼叫（{ident}）")
    for ident in m.cards():
        lines.append(f"Only for one approval ({ident})" if en else f"只限一次核准（{ident}）")
    return Description(sentence, tuple(lines))
