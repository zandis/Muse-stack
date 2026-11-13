"""Set up my services: what is connected, the ONE service to add next, and the steps to add it.

Entry: `run(ctx)`. Check: `check(output, ctx)`.

Rules and catalogue data do the work:

  state     which services have a key on this home (`muse.connect.connected_ids`:
            it asks whether a key is present and never returns one)
  rank      `muse.catalog.suggest_next`: one tap first, then no card or phone,
            then the most allowance gained per minute of setup
  goal      an optional free-text goal is matched against a small word table
            (English and Traditional Chinese); services that fit move forward,
            in a stable, repeatable order
  phrase    ONLY when a goal matches no word: one `light` model call may phrase
            a one-line reason. The order never depends on it, and the line is
            dropped unless it is short, holds no number that is not in the
            catalogue entry, and nothing key-shaped
  check     every recommended service exists, is not connected, is free or a
            trial (unless paid or local was asked for), every number shown is in
            its catalogue entry, the steps are there, nothing looks like a key

It never opens a browser, signs up, or stores a key. It returns structured
steps and the command to run.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from muse import catalog, connect

MAX_ALTERNATIVES = 2
MAX_GOAL_CHARS = 300
PHRASE_MAX_CHARS = 240
PHRASE_MAX_TOKENS = 120
LOCAL_ID = "local"
LOCAL_ENV = ("OLLAMA_API_BASE", "LM_STUDIO_API_BASE", "MUSE_LOCAL_OPENAI_BASE")
INCLUDE = ("paid", "local")

# Goal words. English entries match whole words; Chinese entries match anywhere. Order here is the order tags are reported in.
GOAL_WORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "long_documents": (
        ("long", "long document", "long documents", "long context", "book", "books", "big document", "big documents",
         "large document", "large documents", "pdf", "pdfs", "report", "reports"),
        ("長篇", "長文", "長文件", "大文件", "整本", "長報告", "很長"),
    ),
    "quick_answers": (
        ("fast", "faster", "quick", "quicker", "quickly", "speed", "instant", "low latency"),
        ("快", "速度", "即時", "馬上"),
    ),
    "embeddings": (
        ("search", "searching", "embedding", "embeddings", "find in my files"),
        ("搜尋", "找檔案", "找資料", "檢索"),
    ),
    "reranking": (("rerank", "reranking", "re-rank", "sort results"), ("重新排序", "排序")),
    "everyday": (("everyday", "every day", "daily use", "chat", "chatting", "general"), ("日常", "聊天", "一般", "平常")),
    "private": (
        ("private", "privacy", "confidential", "sensitive", "no training", "not train", "offline", "on device",
         "on my computer", "local"),
        ("隱私", "私密", "機密", "敏感", "不要訓練", "不訓練", "不外流", "離線", "本機", "我的電腦"),
    ),
    "no_card": (
        ("no card", "without a card", "without card", "no credit card", "no phone", "without a phone"),
        ("不用信用卡", "免信用卡", "不要信用卡", "沒有信用卡", "不用手機", "免手機"),
    ),
    "more": (("more", "most", "allowance", "quota"), ("更多", "最多", "額度")),
}

T: dict[str, dict[str, str]] = {
    "en": {
        "connected": "Connected now ({n}): {names}.",
        "connected_none": "Nothing is connected yet.",
        "local_name": "AI on this computer",
        "next": "Add next: {name}",
        "none_left": "Every free service in Muse's catalogue is already connected. Nothing to add right now.",
        "none_left_hint": "Paid services and AI on this computer are left out unless you ask: run again with include=paid or include=local.",
        "others": "Other options:",
        "why": "Why: {text}",
        "goal_fit": "Good for what you asked: {labels}.",
        "goal_private_local": "Nothing you send leaves this computer.",
        "goal_private_ok": "Its terms say it does not learn from what you send, and no person reads it.",
        "goal_private_part": "Its terms say it does not learn from what you send; whether a person may read it is not confirmed.",
        "goal_no_card": "No card or phone number is needed.",
        "privacy": "Privacy: {badge} ({detail})",
        "badge_on_device": "On device",
        "badge_private": "Private",
        "badge_standard": "Standard",
        "detail_on_device": "runs on this computer; nothing is sent out",
        "detail_private": "does not learn from what you send; no person reads it",
        "detail_trains": "may learn from what you send",
        "detail_no_train": "does not learn from what you send; human review not confirmed",
        "detail_unknown": "whether it learns from what you send is not confirmed",
        "card_yes": "Card: needed.",
        "card_no": "Card: not needed.",
        "card_unknown": "Card: not confirmed.",
        "phone_yes": "Phone number: needed.",
        "phone_no": "Phone number: not needed.",
        "phone_unknown": "Phone number: not confirmed.",
        "time_unknown": "time not confirmed",
        "checked": "Checked {date} ({conf}).",
        "steps": "Steps:",
        "steps_fallback": "These steps are shown in English: the catalogue has no Traditional Chinese guide for this service yet.",
        "then": "Then run: {cmd}",
        "caveats": "Keep in mind:",
        "cav_trains": "Its free tier may learn from what you send.",
        "cav_trains_unknown": "Whether it learns from what you send is not confirmed.",
        "cav_review": "People at the service may read what you send.",
        "cav_commercial": "Its free use is not for commercial work.",
        "cav_oneoff": "This credit does not refill.",
        "cav_cn": "It is hosted in mainland China.",
        "cav_one_account": "One account per person: extra accounts or keys add no allowance and are not allowed.",
        "cav_paid": "This service charges for use. Muse never pays unless you allow it.",
        "cav_little": "Little about this service has been confirmed.",
        "cav_gain": "Its allowance is not confirmed, so Muse quotes no number for it.",
        "cav_note": "From the catalogue: {note}",
        "footer": "Muse guides; it never signs up for you, never types into a service's website, and never shows a key.",
        "local_step1_title": "Install a local runtime",
        "local_step1_detail": "Install Ollama or LM Studio from its own website and download one model.",
        "local_step2_title": "Let Muse find it",
        "local_step2_detail": "Run `muse connect local`. Muse looks for the runtime on this computer and adds its models.",
        "cav_local": "It is slower than a hosted service and needs free memory on this computer.",
        "note_goal_unmatched": "Muse does not rank by that goal yet, so the order is the usual one: most allowance gained per minute of setup.",
        "note_goal_flat": "The catalogue does not tell these services apart for that goal, so the order is the usual one.",
        "note_fallback": "Some steps are shown in English: the catalogue has no Traditional Chinese guide for them yet.",
    },
    "zh-TW": {
        "connected": "目前已連接（{n} 個）：{names}。",
        "connected_none": "目前還沒有連接任何服務。",
        "local_name": "這台電腦上的 AI",
        "next": "建議下一個新增：{name}",
        "none_left": "Muse 目錄裡的免費服務都已經連接了，目前沒有可以新增的。",
        "none_left_hint": "付費服務和「這台電腦上的 AI」預設不會建議；需要的話請加上 include=paid 或 include=local 再執行一次。",
        "others": "其他選擇：",
        "why": "原因：{text}",
        "goal_fit": "適合你提到的用途：{labels}。",
        "goal_private_local": "你送出的內容不會離開這台電腦。",
        "goal_private_ok": "服務條款表示不會用你送出的內容來訓練，也不會有人查看。",
        "goal_private_part": "服務條款表示不會用你送出的內容來訓練；是否會有人查看，尚未確認。",
        "goal_no_card": "不需要信用卡或手機號碼。",
        "privacy": "隱私：{badge}（{detail}）",
        "badge_on_device": "在本機",
        "badge_private": "私密",
        "badge_standard": "標準",
        "detail_on_device": "在這台電腦上執行，不會送出任何內容",
        "detail_private": "不會用你送出的內容來訓練，也不會有人查看",
        "detail_trains": "可能會用你送出的內容來訓練",
        "detail_no_train": "不會用你送出的內容來訓練；是否有人查看尚未確認",
        "detail_unknown": "是否會用你送出的內容來訓練，尚未確認",
        "card_yes": "信用卡：需要。",
        "card_no": "信用卡：不需要。",
        "card_unknown": "信用卡：尚未確認。",
        "phone_yes": "手機號碼：需要。",
        "phone_no": "手機號碼：不需要。",
        "phone_unknown": "手機號碼：尚未確認。",
        "time_unknown": "所需時間尚未確認",
        "checked": "核對日期：{date}（{conf}）。",
        "steps": "步驟：",
        "steps_fallback": "以下步驟以英文顯示：目錄裡還沒有這個服務的繁體中文說明。",
        "then": "然後執行：{cmd}",
        "caveats": "請留意：",
        "cav_trains": "免費方案可能會用你送出的內容來訓練。",
        "cav_trains_unknown": "是否會用你送出的內容來訓練，尚未確認。",
        "cav_review": "服務商的人員可能會看到你送出的內容。",
        "cav_commercial": "免費方案不可用於商業用途。",
        "cav_oneoff": "這筆額度用完不會補充。",
        "cav_cn": "主機位於中國大陸。",
        "cav_one_account": "每人限一個帳號：多開帳號或金鑰不會增加額度，而且不被允許。",
        "cav_paid": "這個服務要收費。除非你同意，Muse 不會付費。",
        "cav_little": "這個服務的資訊大多尚未確認。",
        "cav_gain": "它的額度尚未確認，所以 Muse 不提供數字。",
        "cav_note": "目錄備註（英文）：{note}",
        "footer": "Muse 只負責引導：不會替你註冊，不會在服務商的網站上輸入任何東西，也不會顯示金鑰。",
        "local_step1_title": "安裝本機執行環境",
        "local_step1_detail": "到 Ollama 或 LM Studio 的官方網站安裝，並下載一個模型。",
        "local_step2_title": "讓 Muse 找到它",
        "local_step2_detail": "執行 `muse connect local`。Muse 會在這台電腦上尋找執行環境，並加入它的模型。",
        "cav_local": "速度比雲端服務慢，而且需要這台電腦有足夠的記憶體。",
        "note_goal_unmatched": "Muse 還不會依這個目標排序，所以順序照舊：每分鐘設定時間能換到最多額度的排在前面。",
        "note_goal_flat": "目錄裡的資料無法依這個目標區分這些服務，所以順序照舊。",
        "note_fallback": "部分步驟以英文顯示：目錄裡還沒有它們的繁體中文說明。",
    },
}


def t(key: str, lang: str, **fmt: Any) -> str:
    text = (T.get(lang) or T["en"]).get(key) or T["en"][key]
    return text.format(**fmt) if fmt else text


# ---------------------------------------------------------------- goal


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def match_goal(goal: str) -> list[str]:
    """The tags a free-text goal matches, in table order. Empty when no word matches."""
    low = " ".join(str(goal).lower().split())
    tags: list[str] = []
    for tag, (english, chinese) in GOAL_WORDS.items():
        if any(re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", low) for word in english) or any(w in low for w in chinese):
            tags.append(tag)
    return tags


def _tri(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    text = str(value).strip().lower() if value is not None else ""
    return text if text in ("yes", "no") else "unknown"


def badge(provider: Any) -> tuple[str, str]:
    """(badge, detail key). `private` only when the catalogue says no training AND no human review."""
    if provider is None or provider.local:
        return "on_device", "detail_on_device"
    trains, review = _tri(provider.policy.get("trains_on_inputs")), _tri(provider.policy.get("human_review"))
    if trains == "no" and review == "no":
        return "private", "detail_private"
    if trains == "yes":
        return "standard", "detail_trains"
    if trains == "no":
        return "standard", "detail_no_train"
    return "standard", "detail_unknown"


def _score(provider: Any, suggestion: Any, tags: list[str]) -> int:
    score = 0
    for tag in tags:
        if tag in catalog.GOOD_FOR:
            score += 1 if provider is not None and tag in provider.good_for else 0
        elif tag == "private":
            if suggestion.local:
                score += 3
            else:
                name, detail = badge(provider)
                score += 2 if name == "private" else (1 if detail == "detail_no_train" else 0)
        elif tag == "no_card":
            if suggestion.card == "no" and suggestion.phone == "no":
                score += 2
            elif "yes" not in (suggestion.card, suggestion.phone):
                score += 1
    return score


# ---------------------------------------------------------------- state and ranking


def _state(cat: Any, env: Mapping[str, str]) -> tuple[set[str], bool]:
    """(ids of connected services, whether a local runtime is connected). Asks only whether keys are present."""
    connected = set(connect.connected_ids(env, cat.providers))
    for p in cat.providers:  # a key set in the process environment counts too (as `suggest_next` reads it)
        names = [n for n in (p.env, *p.also_env) if n]
        if not p.local and names and all((env.get(n) or "").strip() for n in names):
            connected.add(p.id)
    return connected, any((env.get(n) or "").strip() for n in LOCAL_ENV)


def _paid_suggestions(cat: Any, connected: set[str], env: Mapping[str, str]) -> list[Any]:
    disabled = {p.strip().lower() for p in (env.get("MUSE_DISABLE_PROVIDERS") or "").split(",") if p.strip()}
    out = []
    for p in cat.providers:
        if (p.local or p.id in connected or p.id.lower() in disabled or p.tier in catalog.SUGGESTIBLE_TIERS
                or p.connection is None or p.connection.method == "none"):
            continue
        conn = p.connection
        out.append(catalog.Suggestion(provider_id=p.id, name=p.name, method=conn.method, minutes=conn.minutes_estimate,
                                      steps=len(conn.steps), gain=None, card=conn.card_required, phone=conn.phone_required))
    return sorted(out, key=lambda s: (s.minutes if s.minutes is not None else 10**6, s.provider_id))


def rank(cat: Any, connected: set[str], env: Mapping[str, str], tags: list[str], include: set[str]) -> tuple[list[Any], bool]:
    """(suggestions best first, whether the goal changed the usual order). Deterministic for the same inputs."""
    base = catalog.suggest_next(connected, env, providers=cat.providers)
    local_asked = "local" in include or "private" in tags
    pool = [s for s in base if not s.local]
    if "paid" in include:
        pool += _paid_suggestions(cat, connected, env)
    pool += [s for s in base if s.local and local_asked]
    scores = [_score(cat.provider(s.provider_id), s, tags) for s in pool]
    order = sorted(range(len(pool)), key=lambda i: (-scores[i], i))
    return [pool[i] for i in order], order != list(range(len(pool)))


# ---------------------------------------------------------------- one entry


def _steps(provider: Any, lang: str) -> tuple[list[dict[str, Any]], str]:
    """(steps, the language they are in). A missing translation makes the whole guide English, never a mix."""
    if provider is None:  # AI on this computer: Muse's own two steps
        return [{"n": n, "id": f"local-{n}", "title": t(f"local_step{n}_title", lang), "detail": t(f"local_step{n}_detail", lang)}
                for n in (1, 2)], lang
    steps = provider.connection.steps if provider.connection else ()
    use = lang if all(s.title.get(lang) and s.detail.get(lang) for s in steps) else catalog.DEFAULT_LANG
    return [{"n": n, "id": s.id, "title": str(s.title.get(use) or ""), "detail": str(s.detail.get(use) or "")}
            for n, s in enumerate(steps, start=1)], use


def _caveats(provider: Any, suggestion: Any, lang: str) -> list[str]:
    if provider is None:
        return [t("cav_local", lang)]
    out: list[str] = []
    policy = provider.policy
    trains, review = _tri(policy.get("trains_on_inputs")), _tri(policy.get("human_review"))
    if trains == "yes":
        out.append(t("cav_trains", lang))
    elif trains == "unknown":
        out.append(t("cav_trains_unknown", lang))
    if review == "yes":
        out.append(t("cav_review", lang))
    if str(policy.get("commercial_use") or "").strip().lower() == "prohibited":
        out.append(t("cav_commercial", lang))
    if provider.one_off or "one_off" in provider.kinds_of_allowance:
        out.append(t("cav_oneoff", lang))
    if "cn" in (str(policy.get("region") or "").lower(), provider.terms_flags.get("hosted_in", "")):
        out.append(t("cav_cn", lang))
    if provider.terms_flags.get("one_account_per_person") == "yes":
        out.append(t("cav_one_account", lang))
    if provider.tier not in catalog.SUGGESTIBLE_TIERS:
        out.append(t("cav_paid", lang))
    if suggestion.gain is None:
        out.append(t("cav_gain", lang))
    if provider.confidence == "L":
        out.append(t("cav_little", lang))
    note = " ".join(str(policy.get("note") or "").split())
    if note and note.lower() != "not checked.":
        out.append(t("cav_note", lang, note=note))
    return out


def _why(provider: Any, suggestion: Any, tags: list[str], lang: str) -> str:
    parts = [_sentence(suggestion.gain_text(lang), lang), _sentence(_meta(suggestion, lang), lang)]
    if provider is not None:
        fits = [catalog.tr(f"good_{tag}", lang) for tag in tags if tag in catalog.GOOD_FOR and tag in provider.good_for]
        if fits:
            parts.append(t("goal_fit", lang, labels=("、" if lang == "zh-TW" else ", ").join(fits)))
    if "private" in tags:
        name, detail = badge(provider)
        key = {"on_device": "goal_private_local", "private": "goal_private_ok"}.get(name) or (
            "goal_private_part" if detail == "detail_no_train" else "")
        if key:
            parts.append(t(key, lang))
    if "no_card" in tags and suggestion.card == "no" and suggestion.phone == "no":
        parts.append(t("goal_no_card", lang))
    return ("" if lang == "zh-TW" else " ").join(p for p in parts if p)


def _sentence(text: str, lang: str) -> str:
    text = text.strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".。!！?？" else text + ("。" if lang == "zh-TW" else ".")


def _meta(suggestion: Any, lang: str) -> str:
    if suggestion.local:
        return catalog.tr("meta_local", lang)
    return suggestion.meta_text(lang) if suggestion.minutes is not None else f"{suggestion.meta_text(lang)}, {t('time_unknown', lang)}"


def entry(cat: Any, suggestion: Any, tags: list[str], lang: str) -> dict[str, Any]:
    """Everything shown about one suggested service. Numbers come only from its catalogue entry."""
    provider = None if suggestion.local else cat.provider(suggestion.provider_id)
    steps, steps_lang = _steps(provider, lang)
    name, detail = badge(provider)
    gain = suggestion.gain
    return {
        "id": suggestion.provider_id,
        "name": t("local_name", lang) if suggestion.local else suggestion.name,
        "local": bool(suggestion.local),
        "tier": "local" if provider is None else provider.tier,
        "method": suggestion.method,
        "why": _why(provider, suggestion, tags, lang),
        "why_goal": "",
        "gain": None if gain is None else {"window": gain.window, "limit": gain.limit, "unit": gain.unit},
        "gain_text": suggestion.gain_text(lang),
        "minutes": suggestion.minutes,
        "card": suggestion.card,
        "phone": suggestion.phone,
        "badge": name,
        "badge_text": t(f"badge_{name}", lang),
        "badge_detail": t(detail, lang),
        "good_for": [] if provider is None else list(provider.good_for),
        "checked_on": None if provider is None else provider.checked_on,
        "confidence": None if provider is None else provider.confidence,
        "steps": steps,
        "steps_lang": steps_lang,
        "connect": f"muse connect {suggestion.provider_id}",
        "caveats": _caveats(provider, suggestion, lang),
    }


def entry_text(item: Mapping[str, Any], lang: str, *, full: bool = True) -> str:
    """One service as plain lines. `full` adds the steps and the caveats."""
    lines = [str(item["name"]), "  " + t("why", lang, text=item["why"])]
    if item.get("why_goal"):
        lines.append("  " + str(item["why_goal"]))
    lines.append("  " + t("privacy", lang, badge=item["badge_text"], detail=item["badge_detail"]))
    lines.append("  " + t(f"card_{item['card']}", lang) + ("" if lang == "zh-TW" else " ") + t(f"phone_{item['phone']}", lang))
    if item.get("checked_on") and item.get("confidence"):
        lines.append("  " + t("checked", lang, date=item["checked_on"], conf=catalog.tr(f"conf_{item['confidence']}", lang)))
    if full:
        lines.append("  " + t("steps", lang))
        if item["steps_lang"] != lang:
            lines.append("    " + t("steps_fallback", lang))
        for step in item["steps"]:
            lines.append(f"    {step['n']}. {step['title']}")
            if step["detail"]:
                lines.append(f"       {step['detail']}")
    lines.append("  " + t("then", lang, cmd=item["connect"]))
    if full and item["caveats"]:
        lines.append("  " + t("caveats", lang))
        lines.extend(f"    - {c}" for c in item["caveats"])
    return "\n".join(lines)


# ---------------------------------------------------------------- numbers and secrets

_DIGITS = re.compile(r"\d+")


def digit_runs(text: str) -> set[str]:
    """Every run of digits in `text`, with thousands separators removed first (`1,000,000` is one number)."""
    return set(_DIGITS.findall(re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)))


def allowed_numbers(provider: Any, steps: int) -> set[str]:
    """What an entry's text may contain: the numbers of its catalogue entry, the count of its steps, the step numbers."""
    own = digit_runs(json.dumps(provider.raw, ensure_ascii=False, default=str)) if provider is not None else set()
    return own | {str(n) for n in range(1, steps + 1)}


def _clean_phrase(text: str, allowed: set[str]) -> str:
    """The model's one line, or "" when it cannot be shown (too long, a number of its own, anything key-shaped)."""
    line = " ".join(str(text).split()).strip("\"'「」“” ")
    if not line or len(line) > PHRASE_MAX_CHARS or digit_runs(line) - allowed or catalog.find_secretlike(line):
        return ""
    if re.search(r"https?://|www\.", line, re.I):
        return ""
    return line


def _phrase(ctx: Any, goal: str, item: Mapping[str, Any], provider: Any, lang: str) -> str:
    """ONE light call to say why the first suggestion fits a goal the word table does not know. Optional in every way."""
    if str(ctx.inputs.get("phrase", "auto")).strip().lower() == "off" or ctx.llm.remaining()["requests"] < 1:
        ctx.event("phrase_skipped", reason="off-or-budget")
        return ""
    labels = ", ".join(catalog.tr(f"good_{tag}", "en") for tag in item["good_for"]) or "not stated"
    language = "Traditional Chinese (Taiwan)" if lang == "zh-TW" else "English"
    prompt = (
        "A person wants to add an AI service for this goal:\n"
        f"{goal}\n\n"
        f"The service suggested first is {item['name']}. Facts about it:\n"
        f"- good for: {labels}\n- allowance: {item['gain_text']}\n- privacy: {item['badge_detail']}\n\n"
        f"In one short sentence in {language}, say how this service relates to the goal, using only these facts. "
        "If the facts do not show that it fits, say that plainly. Do not write any number, link or name not given above."
    )
    try:
        reply = ctx.llm.ask([{"role": "user", "content": prompt}], task="light", max_tokens=PHRASE_MAX_TOKENS)
    except ctx.llm.Error as exc:  # deferred, refused, over budget, router down: the answer does not need this line
        ctx.event("phrase_skipped", reason=type(exc).__name__)
        return ""
    line = _clean_phrase(reply.text, allowed_numbers(provider, len(item["steps"])))
    ctx.event("phrase", kept=bool(line))
    return line


# ---------------------------------------------------------------- run


def _lang(raw: Any, goal: str, env: Mapping[str, str]) -> str:
    if str(raw or "").strip():
        return catalog.normalise_lang(str(raw))
    if goal:
        return "zh-TW" if _has_cjk(goal) else "en"
    return catalog.default_lang(env)


def _include(raw: Any) -> set[str] | None:
    words = {w for w in re.split(r"[\s,;+]+", str(raw or "").strip().lower()) if w}
    return words if words <= set(INCLUDE) else None


def _connected_view(cat: Any, connected: set[str], local_connected: bool, lang: str) -> list[dict[str, Any]]:
    out = []
    for p in cat.providers:
        if p.id in connected and not p.local:
            name, detail = badge(p)
            out.append({"id": p.id, "name": p.name, "badge": name, "badge_text": t(f"badge_{name}", lang),
                        "badge_detail": t(detail, lang), "good_for": list(p.good_for)})
    if local_connected:
        out.append({"id": LOCAL_ID, "name": t("local_name", lang), "badge": "on_device", "badge_text": t("badge_on_device", lang),
                    "badge_detail": t("detail_on_device", lang), "good_for": []})
    return out


def connected_text(view: list[Mapping[str, Any]], lang: str) -> str:
    if not view:
        return t("connected_none", lang)
    sep = "、" if lang == "zh-TW" else ", "
    wrap = "{name}（{badge}）" if lang == "zh-TW" else "{name} ({badge})"
    return t("connected", lang, n=len(view), names=sep.join(wrap.format(name=v["name"], badge=v["badge_text"]) for v in view))


def render(output: Mapping[str, Any]) -> str:
    lang = output["lang"]
    parts = [output["connected_text"]]
    if output["next"] is None:
        parts.append(t("none_left", lang) + "\n" + t("none_left_hint", lang))
    else:
        parts.append(t("next", lang, name=entry_text(output["next"], lang)))
        if output["alternatives"]:
            parts.append(t("others", lang) + "\n" + "\n".join(entry_text(a, lang, full=False) for a in output["alternatives"]))
    parts.append(t("footer", lang))
    return "\n\n".join(parts)


def run(ctx: Any) -> dict[str, Any]:
    env = os.environ
    goal = " ".join(str(ctx.inputs.get("goal", "") or "").split())
    if len(goal) > MAX_GOAL_CHARS:
        ctx.stop(f"The goal is too long (over {MAX_GOAL_CHARS} characters).")
    if catalog.find_secretlike(goal):
        ctx.stop("The goal looks like a key or token. Muse never takes a key here; use `muse connect`.")
    include = _include(ctx.inputs.get("include"))
    if include is None:
        ctx.stop("include takes paid, local or both.")
    lang = _lang(ctx.inputs.get("lang"), goal, env)
    cat = catalog.load_catalog()
    connected, local_connected = _state(cat, env)
    tags = match_goal(goal)
    ranked, moved = rank(cat, connected, env, tags, include)
    view = _connected_view(cat, connected, local_connected, lang)
    items = [entry(cat, s, tags, lang) for s in ranked[:1 + MAX_ALTERNATIVES]]
    ctx.event("ranked", connected=len(view), candidates=len(ranked), goal=bool(goal), tags=len(tags), moved=moved)

    notes: list[str] = []
    if goal and not tags:
        notes.append(t("note_goal_unmatched", lang))
        if items:
            first = None if items[0]["local"] else cat.provider(items[0]["id"])
            items[0]["why_goal"] = _phrase(ctx, goal, items[0], first, lang)
    elif tags and not moved and len(ranked) > 1 and tags != ["more"]:
        notes.append(t("note_goal_flat", lang))
    fallback = [item["id"] for item in items[:1] if item["steps_lang"] != lang]
    if fallback:
        notes.append(t("note_fallback", lang))
    for note in notes:
        ctx.note(note)

    output: dict[str, Any] = {
        "lang": lang, "goal": goal, "goal_tags": tags, "include": sorted(include),
        "connected": view, "connected_text": connected_text(view, lang),
        "next": items[0] if items else None, "alternatives": items[1:],
        "caveats": list(items[0]["caveats"]) if items else [],
        "steps_fallback": fallback, "notes": notes,
    }
    output["text"] = render(output)
    return output


# ---------------------------------------------------------------- check


def _check_entry(item: Any, cat: Any, connected: set[str], include: set[str], tags: list[str], lang: str, *, first: bool) -> list[str]:
    if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
        return ["a suggestion has no service id"]
    sid = item["id"]
    steps = item.get("steps") if isinstance(item.get("steps"), list) else []
    reasons: list[str] = []
    if sid == LOCAL_ID:
        provider = None
        if "local" not in include and "private" not in tags:
            reasons.append("AI on this computer was suggested without being asked for")
    else:
        provider = cat.provider(sid)
        if provider is None or provider.local:
            return [f"suggested service '{sid[:40]}' is not in the catalogue"]
        if sid in connected:
            reasons.append(f"{sid} is already connected")
        if provider.tier not in catalog.SUGGESTIBLE_TIERS and "paid" not in include:
            reasons.append(f"{sid} is a paid service and paid services were not asked for")
        head = provider.headline()
        gain = item.get("gain")
        if gain is not None:
            if head is None or head.window == "minute" or gain != {"window": head.window, "limit": head.limit, "unit": head.unit}:
                reasons.append(f"{sid}: the allowance shown is not the catalogue's")
            elif item.get("gain_text") != catalog.gain_text(head, lang):
                reasons.append(f"{sid}: the allowance sentence is not the catalogue's")
        elif item.get("gain_text") != catalog.tr("gain_unknown", lang):
            reasons.append(f"{sid}: no allowance is confirmed, but the text does not say so")
        if item.get("minutes") != (provider.connection.minutes_estimate if provider.connection else None):
            reasons.append(f"{sid}: the minutes shown are not the catalogue's")
        if provider.connection and (item.get("card"), item.get("phone")) != (provider.connection.card_required,
                                                                              provider.connection.phone_required):
            reasons.append(f"{sid}: the card or phone need shown is not the catalogue's")
    stray = digit_runs(entry_text(item, lang)) - allowed_numbers(provider, len(steps)) if _shaped(item) else {"?"}
    if stray:
        reasons.append(f"{sid}: {len(stray)} number(s) shown are not in its catalogue entry")
    if first:
        if not steps or not all(isinstance(s, Mapping) and str(s.get("title") or "").strip() for s in steps):
            reasons.append(f"{sid}: the steps are missing")
        elif provider is not None and [s.get("id") for s in steps] != [s.id for s in provider.connection.steps]:
            reasons.append(f"{sid}: the steps are not the catalogue's")
        if item.get("steps_lang") not in (lang, catalog.DEFAULT_LANG):
            reasons.append(f"{sid}: the steps are in neither the requested language nor English")
    if item.get("connect") != f"muse connect {sid}":
        reasons.append(f"{sid}: the command to run is wrong")
    return reasons


def _shaped(item: Mapping[str, Any]) -> bool:
    need = ("name", "why", "badge_text", "badge_detail", "card", "phone", "steps", "steps_lang", "connect", "caveats")
    return all(k in item for k in need) and item["card"] in catalog.TRI and item["phone"] in catalog.TRI


def check(output: Any, ctx: Any = None) -> list[str]:
    """Reasons the suggestion may not be delivered as checked; empty when it passes. Reasons carry ids and counts only."""
    if not isinstance(output, Mapping) or "next" not in output:
        return ["the skill returned no suggestion"]
    lang = output.get("lang")
    if lang not in catalog.LANGS:
        return ["the language is not one Muse writes"]
    cat = catalog.load_catalog()
    connected, local_connected = _state(cat, os.environ)
    include = set(output.get("include") or [])
    tags = [tag for tag in output.get("goal_tags") or [] if tag in GOAL_WORDS]
    reasons: list[str] = []
    items = ([output["next"]] if output["next"] is not None else []) + list(output.get("alternatives") or [])
    if output["next"] is None and output.get("alternatives"):
        reasons.append("alternatives are given without a first suggestion")
    if len(output.get("alternatives") or []) > MAX_ALTERNATIVES:
        reasons.append(f"more than {MAX_ALTERNATIVES} alternatives are given")
    ids = [item.get("id") for item in items if isinstance(item, Mapping)]
    if len(set(ids)) != len(ids):
        reasons.append("the same service is suggested twice")
    for index, item in enumerate(items):
        reasons.extend(_check_entry(item, cat, connected, include, tags, lang, first=index == 0))
    first = output["next"]
    if isinstance(first, Mapping) and first.get("steps_lang") != lang and first.get("id") not in (output.get("steps_fallback") or []):
        reasons.append("the steps fell back to English without saying so")
    shown = {v.get("id") for v in output.get("connected") or [] if isinstance(v, Mapping)}
    if shown != connected | ({LOCAL_ID} if local_connected else set()):
        reasons.append("the list of connected services does not match this home")
    header = str(output.get("connected_text") or "")
    names = " ".join(str(v.get("name", "")) for v in output.get("connected") or [] if isinstance(v, Mapping))
    if digit_runs(header) - digit_runs(names) - {str(len(shown))}:
        reasons.append("the connected line shows a number that is not a count of services")
    if all(_shaped(item) for item in items if isinstance(item, Mapping)) and output.get("text") != render(output):
        reasons.append("the text shown differs from the checked suggestion")
    if catalog.find_secretlike(dict(output)):
        reasons.append("something shaped like a key appears in the answer")
    return reasons
