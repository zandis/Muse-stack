"""The catalogue as typed data: AI providers, other resources, subscriptions.

Why this module exists: `config/providers.yaml` (v2), `config/resources/*.yaml`
are written by people and a discovery bot, and read by three consumers that must
agree: `muse connect` (what to suggest and which steps to show), the status
snapshot (what the dashboard shows) and `scripts/validate_config.py` (what may
be merged). They all go through here so a fact is stated once.

Rules this module enforces for every entry:
- every fact is dated (`checked_on`) and says how sure it is (`confidence`);
- a number is shown to the user only if the catalogue holds a verified one, and
  otherwise the text says "allowance not confirmed";
- nothing in a catalogue file may look like a key or token;
- strings for people exist in English and Traditional Chinese (zh-TW).

Standard library plus PyYAML only.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import math
import os
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import yaml

from wardmuse.paths import CATALOG_PATH, REPO_ROOT

RESOURCES_DIR = REPO_ROOT / "config" / "resources"
RESOURCE_FILES = {"storage": "storage.yaml", "compute": "compute.yaml", "chain": "chain.yaml", "messaging": "messaging.yaml"}
# Kinds added after the first three. Their file may be absent (an older resources folder); the first three must exist.
OPTIONAL_RESOURCE_KINDS = frozenset({"messaging"})
RESOURCE_KINDS = tuple(RESOURCE_FILES)
SUBSCRIPTIONS_FILE = "subscriptions.yaml"

LANGS = ("en", "zh-TW")
DEFAULT_LANG = "en"

KINDS_OF_ALLOWANCE = ("daily", "monthly", "one_off", "scarce_strong", "abundant_small")
WINDOWS = ("minute", "day", "month", "total")
RESET_RULES = ("utc_day", "pacific_day", "rolling", "calendar_month", "none", "unknown")
READ_METHODS = ("headers", "api_endpoint", "dashboard_only", "none", "unknown")
CONNECTION_METHODS = ("oauth_pkce", "paste_key", "none")
TRI = ("yes", "no", "unknown")
GOOD_FOR = ("everyday", "long_documents", "quick_answers", "embeddings", "reranking")
CONFIDENCE = ("H", "M", "L")
FLAG_VALUES: dict[str, tuple[str, ...]] = {
    "one_account_per_person": ("yes", "no", "unknown"),
    "aggregation_or_reselling": ("prohibited", "allowed", "unknown"),
    "automation_of_console": ("prohibited", "allowed", "unknown"),
}
HOSTED_RE = re.compile(r"^(?:[a-z]{2}|varies|local|unknown)$")
UNIT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
HANDOFF_SCHEMES = {"claude"}

# Used only to put a token allowance and a request allowance on one scale when
# ranking suggestions. It is a rough assumption and is never shown as a fact.
ASSUMED_TOKENS_PER_REQUEST = 2000

SUGGESTIBLE_TIERS = {"free", "trial"}
# An output floor is a few hundred tokens; a larger number is almost certainly a typing mistake.
MIN_OUTPUT_TOKENS_MAX = 8192


# ------------------------------------------------------------------ strings

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "gain_day": "adds roughly {n} {unit} a day",
        "gain_month": "adds roughly {n} {unit} a month",
        "gain_total": "a one-time allowance of about {n} {unit}, which does not refill",
        "gain_unknown": "allowance not confirmed",
        "gain_local": "no daily limit; runs on this computer",
        "meta_steps": "about {minutes} min, {steps} steps",
        "meta_oauth": "about {minutes} min, one tap",
        "meta_unknown_time": "{steps} steps",
        "meta_local": "needs Ollama, LM Studio or another local runtime",
        "friction_card": "needs a card",
        "friction_phone": "needs a phone number",
        "friction_both": "needs a card and a phone number",
        "friction_unknown": "card or phone may be needed (not confirmed)",
        "status_next": "Next helper to add: {name} ({meta})",
        "status_next_plain": "Next helper to add: {name}",
        "per_minute": "a minute",
        "per_day": "a day",
        "per_month": "a month",
        "per_total": "in total",
        "unit_requests": "requests",
        "unit_tokens": "tokens",
        "unit_neurons": "neurons",
        "unit_inputs": "inputs",
        "unit_gb": "GB",
        "unit_gb_month": "GB-month",
        "unit_gpu_minutes": "GPU minutes",
        "unit_class_a_operations": "class A operations",
        "unit_class_b_operations": "class B operations",
        "unit_transactions": "transactions",
        "unit_pushed_messages": "pushed messages",
        "reset_utc_day": "refills at 00:00 UTC",
        "reset_pacific_day": "refills at midnight Pacific time",
        "reset_rolling": "refills gradually, not at a fixed time",
        "reset_calendar_month": "refills at the start of each month",
        "reset_none": "does not refill",
        "reset_unknown": "refill time not confirmed",
        "kind_daily": "daily",
        "kind_monthly": "monthly",
        "kind_one_off": "one-off",
        "kind_scarce_strong": "scarce and strong",
        "kind_abundant_small": "abundant and small",
        "good_everyday": "Everyday",
        "good_long_documents": "Long documents",
        "good_quick_answers": "Quick answers",
        "good_embeddings": "Searching your files",
        "good_reranking": "Sorting search results",
        "method_oauth_pkce": "one tap",
        "method_paste_key": "paste a key",
        "method_none": "not connected by Muse",
        "conf_H": "all facts read on the provider's pages",
        "conf_M": "main facts confirmed, some unknown",
        "conf_L": "little confirmed",
    },
    "zh-TW": {
        "gain_day": "每天多約 {n} {unit}",
        "gain_month": "每月多約 {n} {unit}",
        "gain_total": "一次性額度約 {n} {unit}，用完不會補充",
        "gain_unknown": "額度尚未確認",
        "gain_local": "沒有每日上限，在這台電腦上執行",
        "meta_steps": "約 {minutes} 分鐘，{steps} 個步驟",
        "meta_oauth": "約 {minutes} 分鐘，點一下就好",
        "meta_unknown_time": "{steps} 個步驟",
        "meta_local": "需要先裝 Ollama、LM Studio 或其他本機執行環境",
        "friction_card": "需要信用卡",
        "friction_phone": "需要手機號碼",
        "friction_both": "需要信用卡和手機號碼",
        "friction_unknown": "可能需要信用卡或手機號碼（尚未確認）",
        "status_next": "下一個建議新增的幫手：{name}（{meta}）",
        "status_next_plain": "下一個建議新增的幫手：{name}",
        "per_minute": "每分鐘",
        "per_day": "每天",
        "per_month": "每月",
        "per_total": "總共",
        "unit_requests": "次請求",
        "unit_tokens": "個 token",
        "unit_neurons": "個 neurons",
        "unit_inputs": "筆輸入",
        "unit_gb": "GB",
        "unit_gb_month": "GB-月",
        "unit_gpu_minutes": "GPU 分鐘",
        "unit_class_a_operations": "A 類操作",
        "unit_class_b_operations": "B 類操作",
        "unit_transactions": "筆交易",
        "unit_pushed_messages": "則主動推播訊息",
        "reset_utc_day": "在 UTC 00:00 補滿",
        "reset_pacific_day": "在太平洋時間午夜補滿",
        "reset_rolling": "逐步補回，不是固定時刻",
        "reset_calendar_month": "每月初補滿",
        "reset_none": "不會補充",
        "reset_unknown": "補滿時間尚未確認",
        "kind_daily": "每日",
        "kind_monthly": "每月",
        "kind_one_off": "一次性",
        "kind_scarce_strong": "稀少但強",
        "kind_abundant_small": "量多但小",
        "good_everyday": "日常使用",
        "good_long_documents": "長篇文件",
        "good_quick_answers": "快速回答",
        "good_embeddings": "搜尋你的檔案",
        "good_reranking": "整理搜尋結果",
        "method_oauth_pkce": "點一下就好",
        "method_paste_key": "貼上金鑰",
        "method_none": "Muse 不負責連接",
        "conf_H": "所有事實都已在服務商頁面核對",
        "conf_M": "主要事實已確認，部分未知",
        "conf_L": "幾乎沒有確認",
    },
}

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def normalise_lang(raw: str | None) -> str:
    """Map anything a user or locale might say to a supported language code."""
    text = (raw or "").strip().replace("_", "-").lower()
    if text.startswith("zh"):
        return "zh-TW"
    return "en"


def default_lang(environ: Mapping[str, str] | None = None) -> str:
    """MUSE_LANG, else zh-TW when the system locale is Chinese, else English."""
    env = os.environ if environ is None else environ
    explicit = (envget(env, "MUSE_LANG") or "").strip()
    if explicit:
        return normalise_lang(explicit)
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = (env.get(name) or "").strip()
        if value and value not in ("C", "POSIX"):
            return normalise_lang(value)
    return DEFAULT_LANG


def tr(key: str, lang: str, **fmt: Any) -> str:
    """A localised string; falls back to English, then to the key (never raises)."""
    table = STRINGS.get(lang) or STRINGS[DEFAULT_LANG]
    text = table.get(key) or STRINGS[DEFAULT_LANG].get(key) or key
    try:
        return text.format(**fmt)
    except (KeyError, IndexError, ValueError):
        return text


def pick(value: Any, lang: str) -> str:
    """The `lang` side of a {en, zh-TW} mapping, falling back to English; plain strings pass through."""
    if isinstance(value, Mapping):
        return str(value.get(lang) or value.get(DEFAULT_LANG) or "")
    return "" if value is None else str(value)


def placeholders(text: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(text))


def unit_label(unit: str, lang: str) -> str:
    key = f"unit_{unit}"
    table = STRINGS.get(lang) or STRINGS[DEFAULT_LANG]
    return table.get(key) or unit.replace("_", " ")


def fmt_number(n: int | float) -> str:
    return f"{int(n):,}"


# --------------------------------------------------------------------- types


@dataclass(frozen=True)
class Step:
    id: str
    title: dict[str, str]
    detail: dict[str, str]


@dataclass(frozen=True)
class AllowanceEntry:
    window: str
    limit: int
    unit: str
    scope: str | None = None
    condition: str | None = None
    headline: bool = False

    def per_day_requests(self) -> float | None:
        """Requests a day this entry is worth, or None when that cannot be said honestly."""
        if self.window == "day":
            per_day = float(self.limit)
        elif self.window == "month":
            per_day = self.limit / 30.0
        else:
            return None
        if self.unit == "requests":
            return per_day
        if self.unit == "tokens":
            return per_day / ASSUMED_TOKENS_PER_REQUEST
        return None  # neurons and the like depend on the model


@dataclass(frozen=True)
class Connection:
    method: str
    key_url: str | None = None
    auth_url: str | None = None
    exchange_url: str | None = None
    steps: tuple[Step, ...] = ()
    card_required: str = "unknown"
    phone_required: str = "unknown"
    minutes_estimate: int | None = None
    usable_on_phone: str = "unknown"
    test: dict[str, Any] | None = None
    how: dict[str, str] | None = None


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    kind: str
    tier: str
    env: str | None
    also_env: tuple[str, ...]
    one_off: bool
    kinds_of_allowance: tuple[str, ...]
    allowance: tuple[AllowanceEntry, ...]
    reset: dict[str, Any]
    read_remaining: dict[str, Any]
    connection: Connection | None
    good_for: tuple[str, ...]
    terms_flags: dict[str, str]
    checked_on: str | None
    confidence: str | None
    source_urls: tuple[str, ...]
    policy: dict[str, Any]
    raw: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)
    min_output_tokens: int | None = None  # the provider's default output floor for its chat deployments

    @property
    def local(self) -> bool:
        return self.kind == "local"

    def min_output_for(self, deployment_id: str) -> int | None:
        """The output floor of one of this provider's deployments (see min_output_tokens_for)."""
        for dep in self.raw.get("deployments") or []:
            if isinstance(dep, Mapping) and str(dep.get("id")) == deployment_id:
                return min_output_tokens_for(self.raw, dep)
        return None

    def headline(self) -> AllowanceEntry | None:
        """The entry quoted as "what you gain": flagged, else the first that is not per-minute."""
        for entry in self.allowance:
            if entry.headline:
                return entry
        for entry in self.allowance:
            if entry.window != "minute":
                return entry
        return None


@dataclass(frozen=True)
class Resource:
    id: str
    kind: str
    name: dict[str, str]
    provider: str
    summary: dict[str, str]
    managed: bool
    kinds_of_allowance: tuple[str, ...]
    allowance: tuple[AllowanceEntry, ...]
    reset: dict[str, Any]
    read_remaining: dict[str, Any]
    connection: Connection | None
    terms_flags: dict[str, str]
    checked_on: str | None
    confidence: str | None
    source_urls: tuple[str, ...]
    notes: str | None = None

    def headline(self) -> AllowanceEntry | None:
        for entry in self.allowance:
            if entry.headline:
                return entry
        return self.allowance[0] if self.allowance else None


@dataclass(frozen=True)
class Subscription:
    id: str
    name: str
    handoff_only: bool
    description: dict[str, str]
    open: dict[str, dict[str, Any]]
    checked_on: str | None
    confidence: str | None
    source_urls: tuple[str, ...]
    note: str | None = None


@dataclass(frozen=True)
class Catalog:
    providers: tuple[Provider, ...]
    resources: tuple[Resource, ...]
    subscriptions: tuple[Subscription, ...]

    def provider(self, provider_id: str) -> Provider | None:
        return next((p for p in self.providers if p.id == provider_id), None)

    def ai_providers(self) -> tuple[Provider, ...]:
        return tuple(p for p in self.providers if not p.local)


# ------------------------------------------------------------------- loading


def _read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _s(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    text = str(value).strip()
    return text or None


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v) for v in value if isinstance(v, (str, int)) and str(v).strip())


def _bilingual(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {str(k): str(v) for k, v in value.items() if v is not None}
    return {"en": str(value)} if value else {}


def _allowance(value: Any) -> tuple[AllowanceEntry, ...]:
    out: list[AllowanceEntry] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, Mapping):
            continue
        limit = item.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            continue
        window, unit = str(item.get("window", "")), str(item.get("unit", ""))
        if window not in WINDOWS or not unit:
            continue
        out.append(
            AllowanceEntry(
                window=window,
                limit=limit,
                unit=unit,
                scope=_s(item.get("scope")),
                condition=_s(item.get("condition")),
                headline=item.get("headline") is True,
            )
        )
    return tuple(out)


def _connection(value: Any) -> Connection | None:
    if not isinstance(value, Mapping):
        return None
    steps = []
    for item in value.get("steps") or []:
        if isinstance(item, Mapping) and item.get("id"):
            steps.append(Step(str(item["id"]), _bilingual(item.get("title")), _bilingual(item.get("detail"))))
    minutes = value.get("minutes_estimate")
    test = value.get("test")
    return Connection(
        method=str(value.get("method", "none")),
        key_url=_s(value.get("key_url")),
        auth_url=_s(value.get("auth_url")),
        exchange_url=_s(value.get("exchange_url")),
        steps=tuple(steps),
        card_required=_tri_word(value.get("card_required")),
        phone_required=_tri_word(value.get("phone_required")),
        minutes_estimate=minutes if isinstance(minutes, int) and not isinstance(minutes, bool) and minutes > 0 else None,
        usable_on_phone=_tri_word(value.get("usable_on_phone")),
        test=dict(test) if isinstance(test, Mapping) else None,
        how=_bilingual(value["how"]) if value.get("how") else None,
    )


def _tri_word(value: Any) -> str:
    """YAML turns bare yes/no into booleans; bring them back to words."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    text = str(value).strip().lower() if value is not None else ""
    return text if text in ("yes", "no") else "unknown"


def _flags(value: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, raw in _map(value).items():
        out[str(key)] = _tri_word(raw) if key == "one_account_per_person" else str(raw if raw is not None else "unknown").strip().lower()
    return out


def _min_output(value: Any) -> int | None:
    """A usable min_output_tokens value, else None (validation reports what is wrong)."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MIN_OUTPUT_TOKENS_MAX:
        return value
    return None


def min_output_tokens_for(provider: Mapping[str, Any], deployment: Mapping[str, Any]) -> int | None:
    """The output floor for one deployment: its own `min_output_tokens`, else the provider's.

    Chat deployments only: the floor is about visible answer text. The router
    raises a smaller max_tokens to this number (muse/hook.py), because a
    reasoning model spends output tokens on hidden reasoning before it writes.
    scripts/render_config.py renders the same value as model_info.muse_min_output_tokens.
    """
    if str(deployment.get("mode")) != "chat":
        return None
    if deployment.get("min_output_tokens") is not None:
        return _min_output(deployment.get("min_output_tokens"))
    return _min_output(provider.get("min_output_tokens"))


def validate_min_output_tokens(item: Mapping[str, Any], where: str) -> list[str]:
    """`min_output_tokens` on a provider and on its deployments: a whole number from 1 to MIN_OUTPUT_TOKENS_MAX."""
    problems: list[str] = []
    need = f"must be a whole number from 1 to {MIN_OUTPUT_TOKENS_MAX}"
    if "min_output_tokens" in item and _min_output(item["min_output_tokens"]) is None:
        problems.append(f"{where}.min_output_tokens: {need}")
    deployments = item.get("deployments")
    for dep in deployments if isinstance(deployments, list) else []:
        if not isinstance(dep, Mapping) or "min_output_tokens" not in dep:
            continue
        spot = f"{where}: deployment {dep.get('id', '?')}.min_output_tokens"
        if _min_output(dep["min_output_tokens"]) is None:
            problems.append(f"{spot}: {need}")
        elif str(dep.get("mode")) != "chat":
            problems.append(f"{spot}: only a chat deployment has an output floor")
    return problems


REASONING_PARAMS = ("reasoning_effort",)  # what LiteLLM 1.108.5 passes through for the providers that use it (README)
REASONING_VALUES = ("low", "medium", "high")
REASONING_TASKS = ("light", "everyday", "heavy", "frontier")


def reasoning_for(deployment: Mapping[str, Any]) -> dict[str, str] | None:
    """A chat deployment's `reasoning` setting as {"param": ..., <task>: <value>, ...}; None when it has none or it is bad.

    `reasoning: {param: reasoning_effort, light: low, everyday: low}` asks the provider to think less on short
    tasks, so a small output allowance is not used up by hidden reasoning. The router sets it per call by the
    task tier, only when the client did not set the parameter itself (muse/hook.py). Only on deployments whose
    provider documents the parameter AND whose LiteLLM route passes it through (a dropped parameter does nothing).
    scripts/render_config.py renders the same value as model_info.muse_reasoning.
    """
    raw = deployment.get("reasoning")
    if str(deployment.get("mode")) != "chat" or not isinstance(raw, Mapping) or validate_reasoning_value(raw):
        return None
    return {str(k): str(v) for k, v in raw.items()}


def validate_reasoning_value(raw: Any) -> list[str]:
    if not isinstance(raw, Mapping):
        return ["must be a mapping like {param: reasoning_effort, light: low}"]
    problems: list[str] = []
    if raw.get("param") not in REASONING_PARAMS:
        problems.append(f"param must be one of {', '.join(REASONING_PARAMS)}")
    tasks = [k for k in raw if k != "param"]
    if not tasks:
        problems.append("name at least one task (light, everyday, ...) and its value")
    for key in tasks:
        if key not in REASONING_TASKS:
            problems.append(f"unknown task {key!r} (one of {', '.join(REASONING_TASKS)})")
        elif raw[key] not in REASONING_VALUES:
            problems.append(f"{key} must be one of {', '.join(REASONING_VALUES)}")
    return problems


def validate_reasoning(item: Mapping[str, Any], where: str) -> list[str]:
    """`reasoning` on a provider's deployments (chat only); see reasoning_for."""
    problems: list[str] = []
    if "reasoning" in item:
        problems.append(f"{where}.reasoning: set it on a deployment, not on the provider")
    deployments = item.get("deployments")
    for dep in deployments if isinstance(deployments, list) else []:
        if not isinstance(dep, Mapping) or "reasoning" not in dep:
            continue
        spot = f"{where}: deployment {dep.get('id', '?')}.reasoning"
        problems.extend(f"{spot}: {p}" for p in validate_reasoning_value(dep["reasoning"]))
        if str(dep.get("mode")) != "chat":
            problems.append(f"{spot}: only a chat deployment reasons")
    return problems


def provider_from_dict(item: Mapping[str, Any]) -> Provider:
    """Build a Provider from one entry of providers.yaml. Tolerant: validation is separate."""
    local = item.get("kind", "cloud") == "local"
    return Provider(
        id=str(item["id"]),
        name=str(item.get("name") or item["id"]),
        kind="local" if local else "cloud",
        tier=str(item.get("tier", "unknown")),
        env=None if local else _s(item.get("env")),
        also_env=_strs(item.get("also_env")),
        one_off=item.get("one_off", False) is True,
        kinds_of_allowance=_strs(item.get("kind_of_allowance")),
        allowance=_allowance(item.get("allowance")),
        reset=_map(item.get("reset")),
        read_remaining=_map(item.get("read_remaining")),
        connection=_connection(item.get("connection")),
        good_for=_strs(item.get("good_for")),
        terms_flags=_flags(item.get("terms_flags")),
        checked_on=_s(item.get("checked_on")),
        confidence=_s(item.get("confidence")),
        source_urls=_strs(item.get("source_urls")),
        policy=_map(item.get("policy")),
        raw=dict(item),
        min_output_tokens=_min_output(item.get("min_output_tokens")),
    )


def resource_from_dict(item: Mapping[str, Any], kind: str) -> Resource:
    return Resource(
        id=str(item["id"]),
        kind=kind,
        name=_bilingual(item.get("name")),
        provider=str(item.get("provider", "")),
        summary=_bilingual(item.get("summary")),
        managed=item.get("managed", False) is True,
        kinds_of_allowance=_strs(item.get("kind_of_allowance")),
        allowance=_allowance(item.get("allowance")),
        reset=_map(item.get("reset")),
        read_remaining=_map(item.get("read_remaining")),
        connection=_connection(item.get("connection")),
        terms_flags=_flags(item.get("terms_flags")),
        checked_on=_s(item.get("checked_on")),
        confidence=_s(item.get("confidence")),
        source_urls=_strs(item.get("source_urls")),
        notes=_s(item.get("notes")),
    )


def subscription_from_dict(item: Mapping[str, Any]) -> Subscription:
    return Subscription(
        id=str(item["id"]),
        name=str(item.get("name") or item["id"]),
        handoff_only=item.get("handoff_only") is True,
        description=_bilingual(item.get("description")),
        open={k: _map(v) for k, v in _map(item.get("open")).items()},
        checked_on=_s(item.get("checked_on")),
        confidence=_s(item.get("confidence")),
        source_urls=_strs(item.get("source_urls")),
        note=_s(item.get("note")),
    )


def load_providers(path: Path | None = None) -> tuple[Provider, ...]:
    data = _read_yaml(path or CATALOG_PATH)
    items = data.get("providers") if isinstance(data, Mapping) else None
    return tuple(provider_from_dict(i) for i in items or [] if isinstance(i, Mapping) and i.get("id"))


def load_resources(directory: Path | None = None) -> tuple[Resource, ...]:
    base = directory or RESOURCES_DIR
    out: list[Resource] = []
    for kind, filename in RESOURCE_FILES.items():
        try:
            data = _read_yaml(base / filename)
        except (OSError, yaml.YAMLError):
            continue
        for item in (data.get("resources") if isinstance(data, Mapping) else None) or []:
            if isinstance(item, Mapping) and item.get("id"):
                out.append(resource_from_dict(item, kind))
    return tuple(out)


def load_subscriptions(directory: Path | None = None) -> tuple[Subscription, ...]:
    base = directory or RESOURCES_DIR
    try:
        data = _read_yaml(base / SUBSCRIPTIONS_FILE)
    except (OSError, yaml.YAMLError):
        return ()
    items = data.get("subscriptions") if isinstance(data, Mapping) else None
    return tuple(subscription_from_dict(i) for i in items or [] if isinstance(i, Mapping) and i.get("id"))


def load_catalog(providers_path: Path | None = None, resources_dir: Path | None = None) -> Catalog:
    """Providers, other resources and subscriptions. A missing resources folder just means none."""
    return Catalog(load_providers(providers_path), load_resources(resources_dir), load_subscriptions(resources_dir))


# ---------------------------------------------------------------- validation

_SECRET_PREFIXES = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{16,}|\bgsk_[A-Za-z0-9]{16,}|\bAIza[0-9A-Za-z_-]{20,}|\bjina_[A-Za-z0-9]{16,}"
    r"|\bnvapi-[A-Za-z0-9_-]{16,}|\bcsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----|\bBearer\s+[A-Za-z0-9._~+/=-]{16,})"
)
_TOKENISH = re.compile(r"^[A-Za-z0-9_\-+=]{32,}$")


def _entropy(text: str) -> float:
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / len(text) * math.log2(c / len(text)) for c in counts.values())


def looks_secret(text: str) -> bool:
    """True for a string that resembles an API key or token. URLs and plain words are fine."""
    if _SECRET_PREFIXES.search(text):
        return True
    for word in re.split(r"[\s,;:()\"'/]+", text):
        if (
            _TOKENISH.match(word)
            and not word.startswith(("http", "/"))
            and any(c.isupper() for c in word)
            and any(c.islower() for c in word)
            and any(c.isdigit() for c in word)
            and _entropy(word) >= 4.0
        ):
            return True
    return False


def find_secretlike(obj: Any, path: str = "") -> list[str]:
    """Paths of every string inside `obj` that looks like a key or token."""
    found: list[str] = []
    if isinstance(obj, str):
        if looks_secret(obj):
            found.append(path or "(root)")
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            if isinstance(k, str) and looks_secret(k):
                found.append(f"{path}.{k} (key)")
            found.extend(find_secretlike(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            found.extend(find_secretlike(v, f"{path}[{i}]"))
    return found


def _https(url: Any) -> bool:
    return isinstance(url, str) and url.startswith("https://") and len(url) > len("https://") and " " not in url


def _is_date(value: Any, today: date) -> str | None:
    """None when fine, else the problem."""
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        parsed: date | None = value
    elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\d", value):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return f"{value!r} is not a real date"
    else:
        return "must be a date written YYYY-MM-DD"
    assert parsed is not None
    if parsed > today:
        return f"{parsed.isoformat()} is in the future"
    return None


def _bilingual_problems(value: Any, where: str, *, required: bool = True) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, Mapping):
        return [f"{where}: needs both languages ({', '.join(LANGS)})"]
    problems = []
    for lang in LANGS:
        text = value.get(lang)
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{where}: missing {lang} text")
    return problems


def validate_allowance(value: Any, where: str) -> list[str]:
    problems: list[str] = []
    if value is None:
        return problems
    if not isinstance(value, list):
        return [f"{where}: must be a list"]
    headlines = 0
    for i, item in enumerate(value):
        at = f"{where}[{i}]"
        if not isinstance(item, Mapping):
            problems.append(f"{at}: must be a mapping")
            continue
        extra = set(item) - {"window", "limit", "unit", "scope", "condition", "headline"}
        if extra:
            problems.append(f"{at}: unknown keys {sorted(extra)}")
        if item.get("window") not in WINDOWS:
            problems.append(f"{at}: window must be one of {', '.join(WINDOWS)}")
        limit = item.get("limit")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            problems.append(f"{at}: limit must be a positive whole number")
        if not isinstance(item.get("unit"), str) or not UNIT_RE.match(item.get("unit") or ""):
            problems.append(f"{at}: unit must be lower-case words joined by _")
        for key in ("scope", "condition"):
            if key in item and not isinstance(item[key], str):
                problems.append(f"{at}: {key} must be text")
        if "headline" in item and not isinstance(item["headline"], bool):
            problems.append(f"{at}: headline must be true or false")
        headlines += item.get("headline") is True
    if headlines > 1:
        problems.append(f"{where}: more than one headline entry")
    return problems


def validate_reset(value: Any, where: str, allowance: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return [f"{where}: must be a mapping"]
    problems = []
    rule = value.get("rule")
    if rule not in RESET_RULES:
        problems.append(f"{where}: rule must be one of {', '.join(RESET_RULES)}")
    if "tz" in value and not isinstance(value["tz"], str):
        problems.append(f"{where}: tz must be text")
    note = value.get("note")
    if note is not None and not isinstance(note, (str, Mapping)):
        problems.append(f"{where}: note must be text or {{en, zh-TW}}")
    if isinstance(note, Mapping):
        problems.extend(_bilingual_problems(note, f"{where}.note"))
    windows = {i.get("window") for i in allowance or [] if isinstance(i, Mapping)}
    if "total" in windows and rule not in ("none", "unknown", None):
        problems.append(f"{where}: an allowance with window total never refills, so rule must be none or unknown")
    if windows & {"day", "month"} and rule == "none":
        problems.append(f"{where}: a daily or monthly allowance cannot have reset rule none")
    return problems


def validate_read_remaining(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return [f"{where}: must be a mapping"]
    problems = []
    method = value.get("method")
    if method not in READ_METHODS:
        problems.append(f"{where}: method must be one of {', '.join(READ_METHODS)}")
    if method == "api_endpoint" and not _https(value.get("endpoint")):
        problems.append(f"{where}: api_endpoint needs an https endpoint")
    fields = value.get("fields")
    if fields is not None and not (isinstance(fields, list) and all(isinstance(f, str) for f in fields)):
        problems.append(f"{where}: fields must be a list of names")
    return problems


def validate_connection(value: Any, where: str, *, steps_required: bool, minutes_required: bool = False) -> list[str]:
    if not isinstance(value, Mapping):
        return [f"{where}: missing"]
    problems = []
    method = value.get("method")
    if method not in CONNECTION_METHODS:
        problems.append(f"{where}: method must be one of {', '.join(CONNECTION_METHODS)}")
    if method == "oauth_pkce":
        for key in ("auth_url", "exchange_url"):
            if not _https(value.get(key)):
                problems.append(f"{where}: oauth_pkce needs {key} on https")
    elif value.get("auth_url") or value.get("exchange_url"):
        problems.append(f"{where}: auth_url and exchange_url belong to oauth_pkce only")
    if method in ("oauth_pkce", "paste_key") and not _https(value.get("key_url")):
        problems.append(f"{where}: needs key_url on https")
    if value.get("key_url") is not None and not _https(value["key_url"]):
        problems.append(f"{where}: key_url must be https")
    for key in ("card_required", "phone_required", "usable_on_phone"):
        raw = value.get(key, "unknown")
        word = "yes" if raw is True else "no" if raw is False else raw
        if word not in TRI:
            problems.append(f"{where}: {key} must be yes, no or unknown")
    minutes = value.get("minutes_estimate")
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 120):
        problems.append(f"{where}: minutes_estimate must be a whole number from 1 to 120")
    if minutes is None and minutes_required:
        problems.append(f"{where}: minutes_estimate is needed for a helper Muse suggests")
    steps = value.get("steps")
    if steps is None:
        steps = []
    if not isinstance(steps, list):
        problems.append(f"{where}: steps must be a list")
        steps = []
    if steps_required and method in ("oauth_pkce", "paste_key") and not steps:
        problems.append(f"{where}: needs at least one step")
    seen: set[str] = set()
    for i, step in enumerate(steps):
        at = f"{where}.steps[{i}]"
        if not isinstance(step, Mapping):
            problems.append(f"{at}: must be a mapping")
            continue
        sid = step.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", sid or ""):
            problems.append(f"{at}: id must be lower-case words joined by -")
        elif sid in seen:
            problems.append(f"{at}: duplicate id {sid}")
        else:
            seen.add(sid)
        problems.extend(_bilingual_problems(step.get("title"), f"{at}.title"))
        problems.extend(_bilingual_problems(step.get("detail"), f"{at}.detail"))
    test = value.get("test")
    if test is not None:
        if not isinstance(test, Mapping) or not _https(test.get("url")):
            problems.append(f"{where}.test: needs an https url")
        else:
            auth = test.get("auth")
            ok = auth == "bearer" or (isinstance(auth, Mapping) and isinstance(auth.get("header"), str) and re.fullmatch(r"[A-Za-z0-9-]+", auth["header"]))
            if not ok:
                problems.append(f"{where}.test: auth must be bearer or {{header: NAME}}")
    if value.get("how") is not None:
        problems.extend(_bilingual_problems(value.get("how"), f"{where}.how"))
    return problems


def validate_terms_flags(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return [f"{where}: must be a mapping"]
    problems = []
    for key, raw in value.items():
        if key == "hosted_in":
            if not isinstance(raw, str) or not HOSTED_RE.match(raw):
                problems.append(f"{where}.hosted_in: use a two-letter country code, varies, local or unknown")
        elif key in FLAG_VALUES:
            word = "yes" if raw is True else "no" if raw is False else raw
            if word not in FLAG_VALUES[key]:
                problems.append(f"{where}.{key}: must be one of {', '.join(FLAG_VALUES[key])}")
        else:
            problems.append(f"{where}: unknown flag {key}")
    return problems


def validate_provenance(item: Mapping[str, Any], where: str, today: date, *, required: bool) -> list[str]:
    problems = []
    checked = item.get("checked_on")
    if checked is None:
        if required:
            problems.append(f"{where}.checked_on: every entry is dated")
    else:
        issue = _is_date(checked, today)
        if issue:
            problems.append(f"{where}.checked_on: {issue}")
    conf = item.get("confidence")
    if conf is None:
        if required:
            problems.append(f"{where}.confidence: needs H, M or L")
    elif conf not in CONFIDENCE:
        problems.append(f"{where}.confidence: must be H, M or L")
    urls = item.get("source_urls")
    if urls is None:
        urls = []
    if not isinstance(urls, list) or not all(_https(u) for u in urls):
        problems.append(f"{where}.source_urls: must be a list of https links")
        urls = []
    if conf in ("H", "M") and not urls:
        problems.append(f"{where}.source_urls: confidence {conf} needs the pages the facts were read on")
    return problems


def _tri_norm(value: Any) -> str:
    return _tri_word(value)


def _deployment_modes(provider: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    modes: set[str] = set()
    aliases: set[str] = set()
    for dep in provider.get("deployments") or []:
        if isinstance(dep, Mapping):
            modes.add(str(dep.get("mode")))
            aliases.update(str(a) for a in dep.get("aliases") or [])
    return modes, aliases


def validate_provider(item: Mapping[str, Any], today: date) -> list[str]:
    pid = str(item.get("id", "?"))
    where = f"provider {pid}"
    problems: list[str] = []
    local = item.get("kind", "cloud") == "local"
    if not isinstance(item.get("name"), str) or not item["name"].strip():
        problems.append(f"{where}: name is needed")
    problems.extend(validate_min_output_tokens(item, where))
    problems.extend(validate_reasoning(item, where))
    if local:
        problems.extend(validate_terms_flags(item.get("terms_flags"), f"{where}.terms_flags"))
        return problems
    if "one_off" in item and not isinstance(item["one_off"], bool):
        problems.append(f"{where}.one_off: must be true or false")
    also = item.get("also_env")
    if also is not None and not (isinstance(also, list) and all(isinstance(n, str) and NAME_RE.match(n) for n in also)):
        problems.append(f"{where}.also_env: a list of variable names")
    kinds = item.get("kind_of_allowance")
    if kinds is not None:
        if not isinstance(kinds, list) or any(k not in KINDS_OF_ALLOWANCE for k in kinds):
            problems.append(f"{where}.kind_of_allowance: use {', '.join(KINDS_OF_ALLOWANCE)}")
        elif ("one_off" in kinds) != (item.get("one_off") is True):
            problems.append(f"{where}: one_off: true and kind_of_allowance one_off must go together")
    problems.extend(validate_allowance(item.get("allowance"), f"{where}.allowance"))
    problems.extend(validate_reset(item.get("reset"), f"{where}.reset", item.get("allowance")))
    problems.extend(validate_read_remaining(item.get("read_remaining"), f"{where}.read_remaining"))
    suggestible = str(item.get("tier", "")) in SUGGESTIBLE_TIERS
    problems.extend(validate_connection(item.get("connection"), f"{where}.connection", steps_required=True, minutes_required=suggestible))
    conn = item.get("connection")
    if isinstance(conn, Mapping) and conn.get("key_url") != item.get("key_url"):
        problems.append(f"{where}: connection.key_url must equal key_url")
    good = item.get("good_for")
    modes, aliases = _deployment_modes(item)
    if good is not None:
        if not isinstance(good, list) or any(g not in GOOD_FOR for g in good):
            problems.append(f"{where}.good_for: use {', '.join(GOOD_FOR)}")
        else:
            if ("embeddings" in good) != ("embedding" in modes):
                problems.append(f"{where}.good_for: embeddings must match whether there is an embedding deployment")
            if ("reranking" in good) != ("rerank" in modes):
                problems.append(f"{where}.good_for: reranking must match whether there is a rerank deployment")
            if "long_documents" in good and "long-context" not in aliases:
                problems.append(f"{where}.good_for: long_documents needs a deployment in long-context")
            if "quick_answers" in good and "fast" not in aliases:
                problems.append(f"{where}.good_for: quick_answers needs a deployment in fast")
            if "everyday" in good and "chat" not in modes:
                problems.append(f"{where}.good_for: everyday needs a chat deployment")
    problems.extend(validate_terms_flags(item.get("terms_flags"), f"{where}.terms_flags"))
    problems.extend(validate_provenance(item, where, today, required=True))
    # A quota the rationing code reads must not disagree with the allowance shown to people.
    quota = item.get("quota")
    if isinstance(quota, Mapping) and isinstance(item.get("allowance"), list):
        for key, limit in quota.items():
            metric, _, window = str(key).partition("_per_")
            plain = [
                a for a in item["allowance"]
                if isinstance(a, Mapping) and a.get("window") == window and a.get("unit") == metric and not a.get("condition")
            ]
            if plain and not any(a.get("limit") == limit for a in plain):
                problems.append(f"{where}: quota {key}={limit} disagrees with allowance")
    # Mainland-China hosting is labelled and must not hide a training claim.
    flags = item.get("terms_flags") if isinstance(item.get("terms_flags"), Mapping) else {}
    region = str((item.get("policy") or {}).get("region", "")).strip().lower()
    hosted = str(flags.get("hosted_in", "")).strip().lower()
    if region == "cn" and hosted != "cn":
        problems.append(f"{where}: policy.region is cn, so terms_flags.hosted_in must be cn")
    if hosted == "cn":
        if region != "cn":
            problems.append(f"{where}: hosted_in is cn, so policy.region must be cn")
        trains = _tri_norm((item.get("policy") or {}).get("trains_on_inputs"))
        sources = item.get("source_urls") if isinstance(item.get("source_urls"), list) else []
        if trains == "no" and not sources:
            problems.append(f"{where}: a China-hosted provider may only say trains_on_inputs: no with a source URL")
    return problems


def validate_resource(item: Mapping[str, Any], kind: str, today: date) -> list[str]:
    rid = str(item.get("id", "?"))
    where = f"resource {rid}"
    problems: list[str] = []
    if not isinstance(item.get("id"), str) or not re.fullmatch(r"[a-z][a-z0-9-]*", item.get("id") or ""):
        problems.append(f"{where}: id must be lower-case words joined by -")
    problems.extend(_bilingual_problems(item.get("name"), f"{where}.name"))
    problems.extend(_bilingual_problems(item.get("summary"), f"{where}.summary"))
    if not isinstance(item.get("provider"), str) or not item["provider"].strip():
        problems.append(f"{where}: provider is needed")
    if item.get("managed") is not False:
        problems.append(f"{where}: managed must be false; only AI is rationed automatically")
    kinds = item.get("kind_of_allowance")
    if kinds is not None and (not isinstance(kinds, list) or any(k not in KINDS_OF_ALLOWANCE for k in kinds)):
        problems.append(f"{where}.kind_of_allowance: use {', '.join(KINDS_OF_ALLOWANCE)}")
    if not item.get("allowance"):
        problems.append(f"{where}: needs at least one allowance (an entry that cannot be confirmed is not added)")
    problems.extend(validate_allowance(item.get("allowance"), f"{where}.allowance"))
    problems.extend(validate_reset(item.get("reset"), f"{where}.reset", item.get("allowance")))
    problems.extend(validate_read_remaining(item.get("read_remaining"), f"{where}.read_remaining"))
    problems.extend(validate_connection(item.get("connection"), f"{where}.connection", steps_required=False))
    problems.extend(validate_terms_flags(item.get("terms_flags"), f"{where}.terms_flags"))
    problems.extend(validate_provenance(item, where, today, required=True))
    if item.get("notes") is not None and not isinstance(item["notes"], str):
        problems.append(f"{where}.notes: must be text")
    return problems


# Hosts of consumer chat subscriptions. They are hand-off only (subscriptions.yaml):
# a provider entry must never route API traffic to them.
CONSUMER_HOSTS = (
    "chatgpt.com", "chat.openai.com", "claude.ai", "gemini.google.com", "copilot.microsoft.com",
    "chat.deepseek.com", "chat.mistral.ai", "grok.com", "perplexity.ai", "www.perplexity.ai",
)
# LiteLLM route prefixes that name a wire format, not one service; several providers may share them.
GENERIC_ROUTES = frozenset({"openai", "hosted_vllm", "custom_openai", "openai_like"})
MANY_KEY_FIELDS = ("keys", "api_keys", "envs", "key_pool", "accounts", "rotate", "rotation")


def _host(url: Any) -> str:
    text = str(url or "").strip().lower()
    if "://" not in text:
        return ""
    return text.split("://", 1)[1].split("/", 1)[0].split("@")[-1].split(":", 1)[0]


def validate_account_rules(items: list[Any]) -> list[str]:
    """One account per provider, official API hosts only (docs/reuse-plan.md, standing rules).

    A second entry for a service the catalogue already has, or a list of keys on
    one entry, is how a free tier gets multiplied; both are refused here. So is
    any entry that points at a consumer chat subscription's website.
    """
    problems: list[str] = []
    env_owner: dict[str, str] = {}
    host_owner: dict[str, str] = {}
    route_owner: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        pid = str(item.get("id"))
        where = f"provider {pid}"
        for field_name in MANY_KEY_FIELDS:
            if field_name in item:
                problems.append(f"{where}: no {field_name} field; one account and one key per provider")
        env = item.get("env")
        if env is not None and not isinstance(env, str):
            problems.append(f"{where}.env: one variable name, not a list; one key per provider")
            env = None
        if item.get("kind") == "local":
            continue
        if env:
            if env in env_owner:
                problems.append(f"{where}: shares {env} with {env_owner[env]}; one entry per account")
            env_owner.setdefault(env, pid)
        host = _host(item.get("api_base"))
        urls = [item.get("api_base")]
        routes: set[str] = set()
        for dep in item.get("deployments") or []:
            if isinstance(dep, Mapping):
                urls.append(dep.get("api_base"))
                model = str(dep.get("model") or "")
                if "/" in model:
                    routes.add(model.split("/", 1)[0])
        for url in urls:
            bad = _host(url)
            if bad and any(bad == c or bad.endswith("." + c) for c in CONSUMER_HOSTS):
                problems.append(f"{where}: {bad} is a consumer subscription site; those are hand-off only, never an API route")
        if host:
            if host in host_owner:
                problems.append(f"{where}: same API host as {host_owner[host]} ({host}); one account per provider")
            host_owner.setdefault(host, pid)
        else:
            for route in sorted(routes - GENERIC_ROUTES):
                family = route.split("_", 1)[0]  # cohere and cohere_chat are one service
                if family in route_owner and route_owner[family] != pid:
                    problems.append(f"{where}: same service as {route_owner[family]} ({route}); one account per provider")
                route_owner.setdefault(family, pid)
    return problems


def validate_subscription(item: Mapping[str, Any], today: date) -> list[str]:
    sid = str(item.get("id", "?"))
    where = f"subscription {sid}"
    problems: list[str] = []
    if item.get("handoff_only") is not True:
        problems.append(f"{where}: handoff_only must be true; subscriptions are never routed through")
    forbidden = {"token", "api_key", "key", "login", "password", "secret", "quota", "oauth", "cookie"}
    for key in item:
        if str(key).lower() in forbidden:
            problems.append(f"{where}: no {key} field; Muse never signs in to or reads a subscription")
    problems.extend(_bilingual_problems(item.get("description"), f"{where}.description"))
    opened = item.get("open")
    if not isinstance(opened, Mapping) or not opened:
        problems.append(f"{where}.open: needs a web or app link")
        opened = {}
    for kind, spec in opened.items():
        at = f"{where}.open.{kind}"
        if kind not in ("web", "app") or not isinstance(spec, Mapping):
            problems.append(f"{at}: use web or app, each a mapping")
            continue
        url = spec.get("url")
        if kind == "web" and not _https(url):
            problems.append(f"{at}.url: must be https")
        if kind == "app":
            scheme = str(url).split(":", 1)[0] if isinstance(url, str) and ":" in url else ""
            if scheme not in HANDOFF_SCHEMES:
                problems.append(f"{at}.url: scheme must be one of {', '.join(sorted(HANDOFF_SCHEMES))}")
        prefill = spec.get("prefill")
        if prefill not in ("documented", "unknown"):
            problems.append(f"{at}.prefill: documented or unknown")
        template = spec.get("prompt_template")
        if prefill == "documented":
            if not isinstance(template, str) or "{prompt}" not in template:
                problems.append(f"{at}: documented prefill needs a prompt_template containing {{prompt}}")
            if not item.get("source_urls"):
                problems.append(f"{at}: documented prefill needs a source URL")
        elif template is not None:
            problems.append(f"{at}: prompt_template only with documented prefill")
        if isinstance(template, str):
            if not template.startswith(str(url).split("?", 1)[0]):
                problems.append(f"{at}: prompt_template must start with the same link as url")
    problems.extend(validate_provenance(item, where, today, required=True))
    return problems


def validate_catalog(
    providers_path: Path | None = None,
    resources_dir: Path | None = None,
    today: date | None = None,
) -> list[str]:
    """Every problem found in the AI catalogue, resource files and subscriptions. Empty means fine."""
    today = today or date.today()
    problems: list[str] = []
    ppath = providers_path or CATALOG_PATH
    try:
        data = _read_yaml(ppath)
    except (OSError, yaml.YAMLError) as exc:
        return [f"{ppath}: cannot be read ({type(exc).__name__})"]
    items = data.get("providers") if isinstance(data, Mapping) else None
    if not isinstance(items, list):
        return [f"{ppath}: needs a providers list"]
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            problems.append("providers: every entry must be a mapping")
            continue
        pid = str(item.get("id"))
        if pid in seen:
            problems.append(f"provider {pid}: duplicate id")
        seen.add(pid)
        problems.extend(validate_provider(item, today))
    problems.extend(f"{ppath.name}: {p} looks like a key or token" for p in find_secretlike(data))
    problems.extend(validate_account_rules(items))

    base = resources_dir or RESOURCES_DIR
    if base.is_dir() or resources_dir is not None:
        ids: set[str] = set()
        for kind, filename in RESOURCE_FILES.items():
            path = base / filename
            if kind in OPTIONAL_RESOURCE_KINDS and not path.exists():
                continue
            try:
                doc = _read_yaml(path)
            except (OSError, yaml.YAMLError) as exc:
                problems.append(f"{filename}: cannot be read ({type(exc).__name__})")
                continue
            if not isinstance(doc, Mapping) or doc.get("kind") != kind or not isinstance(doc.get("resources"), list):
                problems.append(f"{filename}: needs kind: {kind} and a resources list")
                continue
            for item in doc["resources"]:
                if not isinstance(item, Mapping):
                    problems.append(f"{filename}: every entry must be a mapping")
                    continue
                if str(item.get("id")) in ids:
                    problems.append(f"resource {item.get('id')}: duplicate id")
                ids.add(str(item.get("id")))
                problems.extend(validate_resource(item, kind, today))
            problems.extend(f"{filename}: {p} looks like a key or token" for p in find_secretlike(doc))
        path = base / SUBSCRIPTIONS_FILE
        try:
            doc = _read_yaml(path)
        except (OSError, yaml.YAMLError) as exc:
            problems.append(f"{SUBSCRIPTIONS_FILE}: cannot be read ({type(exc).__name__})")
            doc = None
        if doc is not None:
            if not isinstance(doc, Mapping) or not isinstance(doc.get("subscriptions"), list):
                problems.append(f"{SUBSCRIPTIONS_FILE}: needs a subscriptions list")
            else:
                sids: set[str] = set()
                for item in doc["subscriptions"]:
                    if not isinstance(item, Mapping):
                        problems.append(f"{SUBSCRIPTIONS_FILE}: every entry must be a mapping")
                        continue
                    if str(item.get("id")) in sids:
                        problems.append(f"subscription {item.get('id')}: duplicate id")
                    sids.add(str(item.get("id")))
                    problems.extend(validate_subscription(item, today))
                problems.extend(f"{SUBSCRIPTIONS_FILE}: {p} looks like a key or token" for p in find_secretlike(doc))
    return problems


# ----------------------------------------------------------------- phrasing


def allowance_text(entry: AllowanceEntry, lang: str) -> str:
    """"1,000,000 tokens a day": a verified entry as people read it."""
    per = tr(f"per_{entry.window}", lang)
    unit = unit_label(entry.unit, lang)
    n = fmt_number(entry.limit)
    return f"{n} {unit} {per}" if lang == "en" else f"{per} {n} {unit}"


def gain_text(entry: AllowanceEntry | None, lang: str, *, local: bool = False) -> str:
    """What connecting adds. Only a verified catalogue number is ever quoted."""
    if local:
        return tr("gain_local", lang)
    if entry is None or entry.window == "minute":
        return tr("gain_unknown", lang)
    key = {"day": "gain_day", "month": "gain_month", "total": "gain_total"}[entry.window]
    return tr(key, lang, n=fmt_number(entry.limit), unit=unit_label(entry.unit, lang))


def reset_text(reset: Mapping[str, Any], lang: str) -> str:
    note = reset.get("note")
    if isinstance(note, Mapping) and pick(note, lang):
        return pick(note, lang)
    return tr(f"reset_{reset.get('rule') or 'unknown'}", lang)


def friction_text(card: str, phone: str, lang: str) -> str | None:
    """Plain words for card and phone needs. None when neither is a concern."""
    if card == "yes" and phone == "yes":
        return tr("friction_both", lang)
    if card == "yes":
        return tr("friction_card", lang)
    if phone == "yes":
        return tr("friction_phone", lang)
    if card == "unknown" or phone == "unknown":
        return tr("friction_unknown", lang)
    return None


# -------------------------------------------------------------- suggestions


@dataclass(frozen=True)
class Suggestion:
    """One not-yet-connected helper, ranked. `gain` is None unless the catalogue has a verified number."""

    provider_id: str
    name: str
    method: str
    minutes: int | None
    steps: int
    gain: AllowanceEntry | None
    card: str
    phone: str
    rank: int = 0
    local: bool = False

    def gain_text(self, lang: str) -> str:
        return gain_text(self.gain, lang, local=self.local)

    def meta_text(self, lang: str) -> str:
        if self.local:
            return tr("meta_local", lang)
        if self.minutes is None:
            return tr("meta_unknown_time", lang, steps=self.steps)
        if self.method == "oauth_pkce":
            return tr("meta_oauth", lang, minutes=self.minutes)
        return tr("meta_steps", lang, minutes=self.minutes, steps=self.steps)

    def friction_text(self, lang: str) -> str | None:
        return None if self.local else friction_text(self.card, self.phone, lang)

    def status_line(self, lang: str) -> str:
        return tr("status_next", lang, name=self.name, meta=self.meta_text(lang))

    def to_dict(self) -> dict[str, Any]:
        """JSON for the snapshot: ids and numbers, plus every string in both languages."""
        return {
            "id": self.provider_id,
            "name": self.name,
            "rank": self.rank,
            "method": self.method,
            "minutes_estimate": self.minutes,
            "steps": self.steps,
            "card_required": self.card,
            "phone_required": self.phone,
            "gain": None
            if self.gain is None
            else {"window": self.gain.window, "limit": self.gain.limit, "unit": self.gain.unit},
            "text": {
                lang: {
                    "gain": self.gain_text(lang),
                    "meta": self.meta_text(lang),
                    "friction": self.friction_text(lang),
                }
                for lang in LANGS
            },
        }


def _friction_tier(card: str, phone: str) -> int:
    if card == "yes" or phone == "yes":
        return 2
    if card == "unknown" or phone == "unknown":
        return 1
    return 0


def suggest_next(
    connected: set[str],
    environ: Mapping[str, str] | None = None,
    *,
    providers: tuple[Provider, ...] | None = None,
) -> list[Suggestion]:
    """Free and trial helpers not yet connected, best next step first.

    Order: one-tap before paste-a-key; then no card or phone before maybe before
    needs one; then the most allowance gained per minute of setup (only where the
    catalogue holds a verified number; the rest follow, shortest setup first).
    Providers named in MUSE_DISABLE_PROVIDERS, or whose key is already set in
    `environ`, are not suggested. A local-model suggestion closes the list when
    no local runtime is connected.
    """
    env = os.environ if environ is None else environ
    items = providers if providers is not None else load_providers()
    disabled = {p.strip().lower() for p in (envget(env, "MUSE_DISABLE_PROVIDERS") or "").split(",") if p.strip()}
    have = set(connected)
    for p in items:
        names = [n for n in (p.env, *p.also_env) if n]
        if not p.local and names and all((env.get(n) or "").strip() for n in names):
            have.add(p.id)
    scored: list[tuple[tuple[Any, ...], Suggestion]] = []
    for p in items:
        if p.local or p.id in have or p.id.lower() in disabled or p.tier not in SUGGESTIBLE_TIERS or p.connection is None:
            continue
        if p.connection.method == "none":
            continue
        entry = p.headline()
        gain = entry if entry is not None and entry.window != "minute" else None
        minutes = p.connection.minutes_estimate
        per_day = None
        if gain is not None and minutes:
            requests = gain.per_day_requests()
            per_day = None if requests is None else requests / minutes
        card, phone = p.connection.card_required, p.connection.phone_required
        key = (
            0 if p.connection.method == "oauth_pkce" else 1,
            _friction_tier(card, phone),
            0 if gain is not None else 1,
            0 if per_day is not None else 1,
            -(per_day or 0.0),
            minutes if minutes is not None else 10**6,
            p.id,
        )
        scored.append(
            (
                key,
                Suggestion(
                    provider_id=p.id,
                    name=p.name,
                    method=p.connection.method,
                    minutes=minutes,
                    steps=len(p.connection.steps),
                    gain=gain,
                    card=card,
                    phone=phone,
                ),
            )
        )
    ordered = [s for _, s in sorted(scored, key=lambda pair: pair[0])]
    def _base_set(name: str) -> bool:
        if name.startswith("MUSE_") or name.startswith("WARDMUSE_"):
            return bool(str(envget(env, name, "") or "").strip())
        return bool(str((env.get(name) or "")).strip())

    local_connected = any(p.local and p.id in have for p in items) or any(
        _base_set(n) for n in ("OLLAMA_API_BASE", "LM_STUDIO_API_BASE", "MUSE_LOCAL_OPENAI_BASE")
    )
    if not local_connected:
        ordered.append(Suggestion("local", "AI on this computer", "local", None, 0, None, "no", "no", local=True))
    return [replace(s, rank=i) for i, s in enumerate(ordered, start=1)]


# ------------------------------------------------------------ snapshot pieces


def provider_meta(provider: Provider) -> dict[str, Any]:
    """The catalogue facts the snapshot adds to a provider (no secrets, no prompts)."""
    conn = provider.connection
    return {
        "name": provider.name,
        "connection_method": conn.method if conn else "none",
        "minutes_estimate": conn.minutes_estimate if conn else None,
        "good_for": list(provider.good_for),
        "kind_of_allowance": list(provider.kinds_of_allowance),
        "checked_on": provider.checked_on,
        "confidence": provider.confidence,
    }


def _allowance_dict(entry: AllowanceEntry) -> dict[str, Any]:
    return {
        "window": entry.window,
        "limit": entry.limit,
        "unit": entry.unit,
        "scope": entry.scope,
        "condition": entry.condition,
        "headline": entry.headline,
        "text": {lang: allowance_text(entry, lang) for lang in LANGS},
    }


def resource_to_dict(resource: Resource) -> dict[str, Any]:
    """A non-AI allowance as the snapshot and `muse resources --json` show it. Read-only data."""
    head = resource.headline()
    conn = resource.connection
    return {
        "id": resource.id,
        "kind": resource.kind,
        "name": {lang: pick(resource.name, lang) for lang in LANGS},
        "provider": resource.provider,
        "summary": {lang: pick(resource.summary, lang) for lang in LANGS},
        "managed": False,
        "kind_of_allowance": list(resource.kinds_of_allowance),
        "allowance": [_allowance_dict(a) for a in resource.allowance],
        "headline": None if head is None else _allowance_dict(head),
        "reset": {
            "rule": resource.reset.get("rule", "unknown"),
            "text": {lang: reset_text(resource.reset, lang) for lang in LANGS},
        },
        "how": {lang: pick(conn.how, lang) for lang in LANGS} if conn and conn.how else None,
        "key_url": conn.key_url if conn else None,
        "checked_on": resource.checked_on,
        "confidence": resource.confidence,
        "source_urls": list(resource.source_urls),
    }


def subscription_to_dict(sub: Subscription) -> dict[str, Any]:
    """A hand-off-only subscription. Only links; never a token, login or quota."""
    web = sub.open.get("web") or {}
    app = sub.open.get("app") or {}
    return {
        "id": sub.id,
        "name": sub.name,
        "handoff_only": True,
        "description": {lang: pick(sub.description, lang) for lang in LANGS},
        "web_url": web.get("url"),
        "app_url": app.get("url"),
        "prefill": {
            "web": web.get("prefill", "unknown"),
            "app": app.get("prefill", "unknown") if app else None,
        },
        "checked_on": sub.checked_on,
        "confidence": sub.confidence,
    }


def handoff_link(sub: Subscription, prompt: str, kind: str = "app") -> str | None:
    """The link that opens `sub` with `prompt` prefilled, where the provider documents one; else the plain link."""
    from urllib.parse import quote

    spec = sub.open.get(kind) or {}
    template = spec.get("prompt_template")
    if spec.get("prefill") == "documented" and isinstance(template, str):
        limit = spec.get("max_prompt_chars")
        text = prompt[: int(limit)] if isinstance(limit, int) else prompt
        return template.replace("{prompt}", quote(text, safe=""))
    url = spec.get("url")
    return str(url) if url else None


# ------------------------------------------------------- the public catalogue
#
# The shape published as catalogue/catalogue.v1.json (scripts/publish_catalogue.py,
# docs/catalogue.md). Only public facts: nothing here reads the environment, a key
# store or the ledger, and the key-check request (`connection.test`), API base
# addresses, routing weights and aliases are left out on purpose.

PUBLIC_SCHEMA_VERSION = "1.1"
LIMIT_STATUS = ("verified", "estimated", "unknown")


def _public_limits(allowance: tuple[AllowanceEntry, ...], quota: Any = None) -> list[dict[str, Any]]:
    """Allowance entries are verified by the catalogue's rule. A `quota:` number with no matching entry is an estimate."""
    out: list[dict[str, Any]] = [
        {"window": a.window, "limit": a.limit, "unit": a.unit, "scope": a.scope, "condition": a.condition,
         "headline": a.headline, "status": "verified"}
        for a in allowance
    ]
    have = {(a.window, a.unit) for a in allowance}
    for key, limit in sorted(_map(quota).items()):
        unit, _, window = str(key).partition("_per_")
        if window in WINDOWS and (window, unit) not in have and isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            out.append({"window": window, "limit": limit, "unit": unit, "scope": None, "condition": None,
                        "headline": False, "status": "estimated"})
    return out


def _public_allowance(allowance: tuple[AllowanceEntry, ...], kinds: tuple[str, ...], reset: Mapping[str, Any],
                      quota: Any = None, *, one_off: bool = False) -> dict[str, Any]:
    limits = _public_limits(allowance, quota)
    note = reset.get("note")
    return {
        "status": "verified" if any(item["status"] == "verified" for item in limits) else "estimated" if limits else "unknown",
        "kinds": list(kinds),
        "one_off": one_off,
        "limits": limits,
        "reset": {
            "rule": str(reset.get("rule") or "unknown"),
            "tz": _s(reset.get("tz")),
            "note": {lang: pick(note, lang) for lang in LANGS} if isinstance(note, Mapping) else _s(note),
        },
    }


def _public_read_remaining(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = value.get("fields")
    return {
        "method": str(value.get("method") or "unknown"),
        "endpoint": _s(value.get("endpoint")),
        "fields": [str(f) for f in fields] if isinstance(fields, list) else [],
        "note": _s(value.get("note")),
    }


def _public_connection(conn: Connection | None, env_names: list[str]) -> dict[str, Any]:
    if conn is None:
        return {"method": "none", "key_url": None, "auth_url": None, "env_names": env_names, "steps": [], "how": None,
                "minutes_estimate": None, "card_required": "unknown", "phone_required": "unknown", "usable_on_phone": "unknown"}
    return {
        "method": conn.method,
        "key_url": conn.key_url,
        "auth_url": conn.auth_url,
        "env_names": env_names,
        "steps": [
            {"id": s.id, "title": {lang: pick(s.title, lang) for lang in LANGS}, "detail": {lang: pick(s.detail, lang) for lang in LANGS}}
            for s in conn.steps
        ],
        "how": {lang: pick(conn.how, lang) for lang in LANGS} if conn.how else None,
        "minutes_estimate": None if conn.minutes_estimate is None else {"minutes": conn.minutes_estimate, "basis": "estimate"},
        "card_required": conn.card_required,
        "phone_required": conn.phone_required,
        "usable_on_phone": conn.usable_on_phone,
    }


def _clinical_word(value: Any) -> str:
    text = str(value).strip().lower() if value is not None else ""
    return text if text in {"forbidden", "allowed", "unknown"} else "unknown"


def _public_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Privacy facts. clinical_use is omitted when unknown so an unset field stays absent."""
    out: dict[str, Any] = {
        "trains_on_inputs": _tri_word(policy.get("trains_on_inputs")),
        "human_review": _tri_word(policy.get("human_review")),
        "region": str(policy.get("region") or "unknown"),
        "commercial_use": str(policy.get("commercial_use") or "unknown"),
        "terms_url": _s(policy.get("terms_url")),
        "note": _s(policy.get("note")),
    }
    clinical = _clinical_word(policy.get("clinical_use"))
    if clinical != "unknown":
        out["clinical_use"] = clinical
    return out


def _public_terms(flags: Mapping[str, str]) -> dict[str, str]:
    return {
        "one_account_per_person": flags.get("one_account_per_person", "unknown"),
        "aggregation_or_reselling": flags.get("aggregation_or_reselling", "unknown"),
        "automation_of_console": flags.get("automation_of_console", "unknown"),
        "hosted_in": flags.get("hosted_in", "unknown"),
    }


def _public_price(value: Any) -> dict[str, float] | None:
    if not isinstance(value, Mapping):
        return None
    out = {k: float(v) for k, v in sorted(value.items()) if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return {**out, "currency": "USD"} if out else None  # type: ignore[dict-item]


def public_ai_service(provider: Provider) -> dict[str, Any]:
    """One AI service as the published catalogue shows it."""
    raw = provider.raw
    policy = provider.policy
    names = [n for n in (provider.env, *provider.also_env) if n]
    if provider.local and raw.get("base_env"):
        names = [str(raw["base_env"])]
    models = []
    for dep in raw.get("deployments") or []:
        if not isinstance(dep, Mapping) or not dep.get("model"):
            continue
        context = dep.get("context_tokens")
        models.append({
            "model": str(dep["model"]),
            "mode": str(dep.get("mode") or "unknown"),
            "context_tokens": context if isinstance(context, int) and not isinstance(context, bool) else None,
            "price": _public_price(dep.get("price")),
            "verified_by": _s(dep.get("verified")),
            "notes": _s(dep.get("notes")),
        })
    return {
        "id": provider.id,
        "name": provider.name,
        "kind": "local" if provider.local else "cloud",
        "tier": provider.tier,
        "good_for": list(provider.good_for),
        "allowance": _public_allowance(provider.allowance, provider.kinds_of_allowance, provider.reset, raw.get("quota"), one_off=provider.one_off),
        "read_remaining": _public_read_remaining(provider.read_remaining),
        "connection": _public_connection(provider.connection, names),
        "policy": _public_policy(policy),
        "terms": _public_terms(provider.terms_flags),
        "models": sorted(models, key=lambda m: (m["mode"], m["model"])),
        "checked_on": provider.checked_on,
        "confidence": provider.confidence,
        "source_urls": list(provider.source_urls),
        "notes": _s(raw.get("notes")),
    }


def public_other_allowance(resource: Resource) -> dict[str, Any]:
    """Storage, compute, chain or messaging: an allowance Muse lists but does not manage."""
    return {
        "id": resource.id,
        "name": {lang: pick(resource.name, lang) for lang in LANGS},
        "kind": resource.kind,
        "provider": resource.provider,
        "good_for": {lang: pick(resource.summary, lang) for lang in LANGS},
        "tier": "free",
        "allowance": _public_allowance(resource.allowance, resource.kinds_of_allowance, resource.reset),
        "read_remaining": _public_read_remaining(resource.read_remaining),
        "connection": _public_connection(resource.connection, []),
        "terms": _public_terms(resource.terms_flags),
        "checked_on": resource.checked_on,
        "confidence": resource.confidence,
        "source_urls": list(resource.source_urls),
        "notes": resource.notes,
    }


def public_subscription(sub: Subscription) -> dict[str, Any]:
    """A consumer subscription: hand-off only, so only where it opens."""
    opened = {}
    for kind in ("web", "app"):
        spec = sub.open.get(kind)
        if spec:
            opened[kind] = {"url": _s(spec.get("url")), "prefill": str(spec.get("prefill") or "unknown")}
    return {
        "id": sub.id,
        "name": sub.name,
        "kind": "subscription",
        "handoff_only": True,
        "good_for": {lang: pick(sub.description, lang) for lang in LANGS},
        "open": opened,
        "checked_on": sub.checked_on,
        "confidence": sub.confidence,
        "source_urls": list(sub.source_urls),
        "notes": sub.note,
    }


def newest_checked_on(cat: Catalog) -> str | None:
    dates = [d for d in (*(p.checked_on for p in cat.providers), *(r.checked_on for r in cat.resources),
                         *(s.checked_on for s in cat.subscriptions)) if d and re.fullmatch(r"\d{4}-\d\d-\d\d", d)]
    return max(dates) if dates else None


def public_entries(cat: Catalog) -> dict[str, list[dict[str, Any]]]:
    """The three published lists, each in a stable order."""
    order = {kind: i for i, kind in enumerate(RESOURCE_KINDS)}
    return {
        "ai_services": [public_ai_service(p) for p in sorted(cat.providers, key=lambda p: p.id)],
        "other_allowances": [public_other_allowance(r) for r in sorted(cat.resources, key=lambda r: (order.get(r.kind, 99), r.id))],
        "subscriptions": [public_subscription(s) for s in sorted(cat.subscriptions, key=lambda s: s.id)],
    }
