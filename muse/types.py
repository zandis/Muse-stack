"""Shared shapes. The snapshot is the one contract between the ledger, the CLI
and the dashboard: `muse.status.build_snapshot()` returns it, `muse status
--json` prints it, and the dashboard's /api/status serves it.

Rules for everything in a snapshot:
- JSON-serialisable. Times are ISO-8601 UTC strings ending in "Z", or None.
- Never a key value, never prompt or response text.
"""

from __future__ import annotations

from typing import Literal, TypedDict

PrivacyClass = Literal["public", "standard", "private", "local"]
ProviderState = Literal["ok", "cooling", "exhausted", "no_key", "disabled", "unknown"]
Window = Literal["minute", "day", "month"]


class Policy(TypedDict):
    trains_on_inputs: str  # yes | no | unknown
    human_review: str  # yes | no | unknown
    region: str
    commercial_use: str
    terms_url: str
    note: str


class WindowUsage(TypedDict):
    requests: int
    tokens: int


class Limit(TypedDict):
    window: Window
    metric: Literal["requests", "tokens"]
    limit: int | None
    remaining: int | None
    reset_at: str | None
    # header: the provider said so on its last response. catalog: declared in
    # providers.yaml. estimate: limit minus what the ledger counted.
    source: Literal["header", "catalog", "estimate"]


class DeploymentStatus(TypedDict):
    id: str
    model: str
    mode: str
    aliases: list[str]
    state: ProviderState
    blocked_until: str | None
    last_status: int | None
    last_used_at: str | None
    success_rate_24h: float | None  # None when there were no calls


class ProviderStatus(TypedDict):
    id: str
    # Catalogue v2 facts (config/providers.yaml). All dated by checked_on; None / [] = not confirmed.
    name: str
    connection_method: str  # oauth_pkce | paste_key | none
    minutes_estimate: int | None  # Muse's own estimate of the sign-up time, not measured
    good_for: list[str]
    kind_of_allowance: list[str]
    checked_on: str | None  # YYYY-MM-DD
    confidence: str | None  # H | M | L
    kind: Literal["cloud", "local"]
    tier: str
    key_env: str | None  # env var name; None for local providers
    configured: bool
    enabled: bool
    policy: Policy
    state: ProviderState
    blocked_until: str | None
    block_reason: str | None
    usage: dict[str, WindowUsage]  # keys: minute, day, month
    limits: list[Limit]
    deployments: list[DeploymentStatus]


class AliasStatus(TypedDict):
    name: str
    mode: str
    available: int  # deployments that could take a request now
    total: int
    next: str | None  # deployment id most likely to serve the next call


class RecentCall(TypedDict):
    ts: str
    alias: str | None
    deployment: str
    provider: str
    status: int | None
    ok: bool
    latency_ms: int | None
    tokens_in: int | None
    tokens_out: int | None
    privacy: str


class Proxy(TypedDict):
    url: str
    reachable: bool | None  # None = not checked


class Totals(TypedDict):
    requests_today: int
    tokens_today: int


class AppToday(TypedDict):
    requests: int
    failures: int
    tokens: int
    cost_usd: float


class AppStatus(TypedDict):
    name: str
    builtin: bool  # owner and anonymous are built in and not stored
    priority: str  # interactive | normal | background
    daily_requests: int | None
    daily_tokens: int | None
    allow_paid: bool
    default_privacy: str | None
    note: str | None
    created_at: str | None
    last_used_at: str | None
    today: AppToday
    # Calls turned away today, keyed "defer:yield", "refuse:app-requests", ...
    deferrals: dict[str, int]


class Paid(TypedDict):
    cap_usd: float
    spent_usd: float
    held_usd: float  # reserved by calls in flight
    calls: int
    unknown_cost_calls: int  # paid calls whose cost could not be computed; not in spent_usd
    resets_at: str  # next local midnight


class QueueStatus(TypedDict):
    counts: dict[str, int]  # queued, running, deferred, done, failed, cancelled
    waiting: int  # queued + deferred
    next_due_at: str | None
    oldest_queued_at: str | None
    hold_until: str | None  # the queue app hit a cap; nothing runs before this


class SuggestionGain(TypedDict):
    window: str  # day | month | total
    limit: int
    unit: str


class SuggestionText(TypedDict):
    gain: str  # "adds roughly 50 requests a day", or "allowance not confirmed"
    meta: str  # "about 4 min, 3 steps"
    friction: str | None  # card or phone, in plain words; None when neither is a concern


class Suggestion(TypedDict):
    """A helper worth adding next. `gain` is None unless the catalogue holds a verified number."""

    id: str  # a provider id, or "local"
    name: str
    rank: int  # 1 = best next step
    method: str  # oauth_pkce | paste_key | local
    minutes_estimate: int | None
    steps: int
    card_required: str  # yes | no | unknown
    phone_required: str
    gain: SuggestionGain | None
    text: dict[str, SuggestionText]  # keyed by language: en, zh-TW


class AllowanceItem(TypedDict):
    window: str  # minute | day | month | total
    limit: int
    unit: str
    scope: str | None
    condition: str | None
    headline: bool
    text: dict[str, str]  # keyed by language


class ResourceReset(TypedDict):
    rule: str
    text: dict[str, str]


class ResourceItem(TypedDict):
    """A storage, compute, chain or messaging allowance. Shown only: Muse does not manage it."""

    id: str
    kind: str  # storage | compute | chain | messaging
    name: dict[str, str]
    provider: str
    summary: dict[str, str]
    managed: bool  # always False for now
    kind_of_allowance: list[str]
    allowance: list[AllowanceItem]
    headline: AllowanceItem | None
    reset: ResourceReset
    how: dict[str, str] | None  # how it would be connected, in words
    key_url: str | None
    checked_on: str | None
    confidence: str | None
    source_urls: list[str]


class SubscriptionPrefill(TypedDict):
    web: str  # documented | unknown
    app: str | None


class SubscriptionItem(TypedDict):
    """A consumer subscription. Hand-off only: a link, never a login, token or quota."""

    id: str
    name: str
    handoff_only: bool
    description: dict[str, str]
    web_url: str | None
    app_url: str | None
    prefill: SubscriptionPrefill
    checked_on: str | None
    confidence: str | None


class Snapshot(TypedDict):
    generated_at: str
    home: str
    proxy: Proxy
    default_privacy: str
    providers: list[ProviderStatus]
    aliases: list[AliasStatus]
    recent: list[RecentCall]  # newest first, at most 50
    totals: Totals
    # Added with per-app keys and the queue. Older consumers may ignore them.
    apps: list[AppStatus]
    paid: Paid
    queue: QueueStatus
    paused: bool  # the queue is paused (`muse pause`); the proxy is not
    # Added with catalogue v2: ranked next helpers, and the allowances Muse shows but does not manage.
    suggestions: list[Suggestion]
    resources: list[ResourceItem]
    subscriptions: list[SubscriptionItem]
