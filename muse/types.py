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


class Snapshot(TypedDict):
    generated_at: str
    home: str
    proxy: Proxy
    default_privacy: str
    providers: list[ProviderStatus]
    aliases: list[AliasStatus]
    recent: list[RecentCall]  # newest first, at most 50
    totals: Totals
