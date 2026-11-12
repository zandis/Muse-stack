"""Ration free quota and the paid allowance between one person's applications.

Why this exists: several applications share one endpoint and one set of free
quotas. Without rules, a batch job can spend the day's Gemini requests before
the user opens a chat, or a fallback can quietly bill a paid key. These are
pure functions over plain data (no I/O, `now` passed in) so every rule can be
table-tested; muse/hook.py gathers the state from the ledger and applies them.

Order of precedence (product decisions):
  (a) spend nothing unless approved: the approval is a daily paid cap;
  (b) never interrupt the application the user is actively using;
  (c) use free quota before it resets rather than waste it.
Privacy filtering and provider blocks run before any of this and are never
relaxed by it.

Rules, applied in this order to the deployments left after privacy and blocks:
  1. app caps     `daily_requests` / `daily_tokens` reached -> refuse the app
                  until local midnight (the request that crosses may finish).
  2. paid gate    tier paid|metered is removed unless the app has allow_paid,
                  today's paid spend < MUSE_DAILY_PAID_CAP_USD, and the cost is
                  knowable (a `price:` on the deployment). A deployment named
                  directly may be used without a price (recorded as unknown cost).
                  Fails closed: an unknown spend total closes the gate.
  3. yield        a background app skips an upstream an interactive app used
                  successfully within MUSE_ACTIVE_WINDOW_S. Local models yield
                  too unless MUSE_LOCAL_YIELD=false (one GPU serving a chat and
                  a batch job at once makes the chat slow).
  4. reserve      with known remaining quota, background may not take it below
                  MUSE_RESERVE_BACKGROUND of the limit, normal below
                  MUSE_RESERVE_NORMAL. Interactive has no reserve.
  5. expiring     rule 4 (not rule 3) is waived for a day window within
                  MUSE_EXPIRING_DAY_S of its reset and a month window within
                  MUSE_EXPIRING_MONTH_S. Minute windows never count.
  6. unknown      where remaining is unknown (Gemini reports nothing) rules 4-5
                  cannot apply; rule 3 still does. No numbers are guessed.
Interactive apps are never deferred by rules 3-5.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from muse import quota

PAID_TIERS = frozenset({"paid", "metered"})
FREE_TIERS = frozenset({"free", "trial", "local"})
WINDOW_SECONDS = {"minute": 60, "day": 86400, "month": 31 * 86400}

# Machine-readable reason codes (also the x-muse-ration header values).
APP_REQUESTS = "app-requests"
APP_TOKENS = "app-tokens"
PAID_NOT_ALLOWED = "paid-not-allowed"
PAID_CAP = "paid-cap"
PAID_UNKNOWN_COST = "paid-unknown-cost"
PAID_SPEND_UNKNOWN = "paid-spend-unknown"
YIELD = "yield"
RESERVE = "reserve"
BLOCKED = "blocked"

DEFER = "defer"
REFUSE = "refuse"


# ---------------------------------------------------------------- settings


@dataclass(frozen=True, slots=True)
class Settings:
    """Ration settings from the environment. See Settings.from_env for names and defaults."""

    paid_cap_usd: Decimal = Decimal("1.00")
    active_window_s: float = 120.0
    reserve_background: float = 0.30
    reserve_normal: float = 0.10
    expiring_day_s: float = 3600.0
    expiring_month_s: float = 86400.0
    local_yields: bool = True
    tz_name: str = ""
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def tz(self) -> tzinfo:
        return resolve_tz(self.tz_name)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Parse settings. Bad values fall back to the default, except the paid
        cap, which falls to 0 (never pay) because doubt must not spend money."""
        env = os.environ if environ is None else environ
        warnings: list[str] = []
        cap = parse_paid_cap(env.get("MUSE_DAILY_PAID_CAP_USD"), warnings)

        def number(name: str, default: float, lo: float, hi: float) -> float:
            raw = env.get(name, "")
            if raw is None or not str(raw).strip():
                return default
            try:
                value = float(str(raw).strip())
            except ValueError:
                value = math.nan
            if not math.isfinite(value) or value < lo or value > hi:
                warnings.append(f"{name}={raw!r} is not a number in [{lo}, {hi}]; using {default}")
                return default
            return value

        local_raw = str(env.get("MUSE_LOCAL_YIELD", "") or "").strip().lower()
        local_yields = local_raw not in {"0", "false", "no", "off"}
        return cls(
            paid_cap_usd=cap,
            active_window_s=number("MUSE_ACTIVE_WINDOW_S", 120.0, 0, 86400),
            reserve_background=number("MUSE_RESERVE_BACKGROUND", 0.30, 0, 1),
            reserve_normal=number("MUSE_RESERVE_NORMAL", 0.10, 0, 1),
            expiring_day_s=number("MUSE_EXPIRING_DAY_S", 3600.0, 0, 86400),
            expiring_month_s=number("MUSE_EXPIRING_MONTH_S", 86400.0, 0, 31 * 86400),
            local_yields=local_yields,
            tz_name=str(env.get("MUSE_TZ", "") or "").strip(),
            warnings=tuple(warnings),
        )


def parse_paid_cap(raw: Any, warnings: list[str] | None = None) -> Decimal:
    """MUSE_DAILY_PAID_CAP_USD: unset -> 1.00; "0" -> never; anything unparsable or negative -> 0."""
    if raw is None or not str(raw).strip():
        return Decimal("1.00")
    text = str(raw).strip().lstrip("$").strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        value = Decimal("NaN")
    if not value.is_finite() or value < 0:
        if warnings is not None:
            warnings.append(f"MUSE_DAILY_PAID_CAP_USD={raw!r} is not a non-negative amount; paid use is off (0)")
        return Decimal("0")
    return value


def paid_cap_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True when the daily cap allows any paid spend (used by the renderer)."""
    env = os.environ if environ is None else environ
    return parse_paid_cap(env.get("MUSE_DAILY_PAID_CAP_USD")) > 0


def format_usd(value: Decimal | float) -> str:
    amount = Decimal(str(value))
    return f"${amount:.2f}" if amount >= Decimal("0.01") or amount == 0 else f"${amount.normalize():f}"


# ---------------------------------------------------------------- local day


def resolve_tz(name: str | None) -> tzinfo:
    """MUSE_TZ as a zone; empty means the system zone; an unknown name falls back to it too."""
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # ZoneInfoNotFoundError, ValueError on odd input, missing tzdata
            pass
    local = datetime.now().astimezone().tzinfo
    return local if local is not None else timezone.utc


def local_day_bounds(now: float, tz: tzinfo) -> tuple[float, float]:
    """[start, end) of the local calendar day containing `now`, as epoch seconds.

    Uses wall-clock midnights, so DST days are 23 or 25 hours long. A zone whose
    midnight does not exist on a switch day gets zoneinfo's fold=0 resolution.
    """
    local = datetime.fromtimestamp(now, tz=tz)
    start = datetime(local.year, local.month, local.day, tzinfo=tz)
    nxt = (start.replace(tzinfo=None) + timedelta(days=1)).replace(tzinfo=tz)
    start_ts, end_ts = start.timestamp(), nxt.timestamp()
    if start_ts > now:  # a non-existent midnight resolved forward of now
        start_ts = now
    return start_ts, end_ts


# ---------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class AppPolicy:
    """The parts of muse.apps.App the rules read."""

    name: str
    priority: str
    allow_paid: bool = False
    daily_requests: int | None = None
    daily_tokens: int | None = None

    @classmethod
    def of(cls, app: Any) -> "AppPolicy":
        return cls(
            name=str(getattr(app, "name", "anonymous")),
            priority=str(getattr(app, "priority", "normal")),
            allow_paid=bool(getattr(app, "allow_paid", False)),
            daily_requests=getattr(app, "daily_requests", None),
            daily_tokens=getattr(app, "daily_tokens", None),
        )


@dataclass(frozen=True, slots=True)
class Limit:
    """A quota whose remaining amount is known for one upstream."""

    window: str  # minute | day | month
    metric: str  # requests | tokens
    limit: int
    remaining: int
    reset_at: float | None
    source: str  # header | declared


@dataclass(slots=True)
class State:
    """Ledger state at decision time. `paid_spend_usd=None` means unknown (gate closes)."""

    limits: Mapping[str, Sequence[Limit]] = field(default_factory=dict)  # upstream -> known limits
    app_requests_today: int = 0
    app_tokens_today: int = 0
    paid_spend_usd: Decimal | None = Decimal("0")
    last_interactive: Mapping[str, float] = field(default_factory=dict)  # upstream -> ts
    day_end: float = 0.0  # next local midnight


@dataclass(frozen=True, slots=True)
class Removed:
    deployment_id: str
    upstream: str
    code: str
    kind: str  # defer | refuse
    retry_at: float | None
    detail: str


@dataclass(frozen=True, slots=True)
class Decision:
    """What to tell the caller when nothing is left."""

    kind: str  # defer | refuse
    code: str
    message: str  # starts with "[muse]"
    retry_at: float | None
    status: int = 429

    def retry_after(self, now: float) -> int | None:
        if self.retry_at is None:
            return None
        return max(1, int(math.ceil(self.retry_at - now)))


@dataclass(slots=True)
class Outcome:
    kept: list[dict]
    removed: list[Removed]
    decision: Decision | None

    @property
    def codes(self) -> list[str]:
        return sorted({item.code for item in self.removed})


# ---------------------------------------------------------------- deployment facts


def _info(deployment: Mapping[str, Any]) -> Mapping[str, Any]:
    info = deployment.get("model_info") if isinstance(deployment, Mapping) else None
    return info if isinstance(info, Mapping) else {}


def tier_of(info: Mapping[str, Any]) -> str:
    """Missing tier counts as paid: when in doubt, do not spend."""
    value = info.get("muse_tier")
    text = str(value).strip().lower() if isinstance(value, str) else ""
    if info.get("muse_kind") == "local":
        return "local"
    return text if text in FREE_TIERS | PAID_TIERS else "paid"


def is_paid(info: Mapping[str, Any]) -> bool:
    return tier_of(info) in PAID_TIERS


def price_of(info: Mapping[str, Any]) -> tuple[float, float] | None:
    """(input, output) USD per million tokens from the catalogue `price:` block, or None."""
    values = []
    for key in ("muse_price_in", "muse_price_out"):
        value = info.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        value = float(value)
        if not math.isfinite(value) or value < 0:
            return None
        values.append(value)
    return values[0], values[1]


def upstream_of(info: Mapping[str, Any]) -> str:
    return str(info.get("muse_upstream") or info.get("id") or "")


# ---------------------------------------------------------------- limits


def _fresh(row: Mapping[str, Any], now: float) -> bool:
    reset_at = row.get("reset_at")
    if reset_at is not None:
        return float(reset_at) > now
    seen = row.get("seen_at")
    return seen is not None and float(seen) > now - WINDOW_SECONDS.get(str(row.get("window")), 60)


def window_reset(window: str, provider_id: str, now: float) -> float:
    """When a declared quota window resets: provider quota day (Pacific for Gemini, else UTC) or month."""
    if window == "minute":
        return now - (now % 60) + 60
    if window == "day":
        return quota.day_start(now, provider_id) + 86400
    return quota.next_month_utc(now)


def declared_quota(raw: Any) -> list[tuple[str, str, int]]:
    """(window, metric, limit) from a catalogue `quota:` mapping or list (same shapes as muse/status.py)."""
    items: list[tuple[str, str, Any]] = []
    if isinstance(raw, list):
        items = [(str(i.get("window")), str(i.get("metric")), i.get("limit")) for i in raw if isinstance(i, Mapping)]
    elif isinstance(raw, Mapping):
        for key, value in raw.items():
            metric, _, window = str(key).partition("_per_")
            items.append((window, metric, value))
    out: list[tuple[str, str, int]] = []
    for window, metric, limit in items:
        if window in WINDOW_SECONDS and metric in ("requests", "tokens") and isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
            out.append((window, metric, limit))
    return out


def known_limits(
    info: Mapping[str, Any],
    observed: Iterable[Mapping[str, Any]],
    provider_usage: Mapping[str, Mapping[str, int]],
    now: float,
) -> list[Limit]:
    """Limits with a known remaining amount for one deployment's upstream.

    Fresh header-observed rows (ledger `limits` for the upstream and for
    `provider:<id>`) win; for windows without one, the catalogue `quota:`
    (rendered as model_info.muse_quota) minus successful calls in the ledger.
    Where neither exists the remaining amount is unknown and nothing is returned.
    """
    best: dict[tuple[str, str], Limit] = {}
    for row in observed:
        if not _fresh(row, now):
            continue
        lim, rem = row.get("limit", row.get("lim")), row.get("remaining")
        if not isinstance(lim, int) or not isinstance(rem, int) or lim <= 0:
            continue
        key = (str(row.get("window")), str(row.get("metric")))
        cand = Limit(key[0], key[1], lim, max(0, rem), row.get("reset_at"), "header")
        if key not in best or cand.remaining / cand.limit < best[key].remaining / best[key].limit:
            best[key] = cand
    provider_id = str(info.get("provider") or "")
    for window, metric, limit in declared_quota(info.get("muse_quota")):
        if (window, metric) in best:
            continue
        used = int((provider_usage.get(window) or {}).get(metric, 0))
        best[(window, metric)] = Limit(window, metric, limit, max(0, limit - used), window_reset(window, provider_id, now), "declared")
    return [best[key] for key in sorted(best)]


def expiring(limit: Limit, settings: Settings, now: float) -> bool:
    """Rule 5: about to reset, so using it now wastes nothing."""
    if limit.reset_at is None or limit.window == "minute":
        return False
    horizon = settings.expiring_day_s if limit.window == "day" else settings.expiring_month_s
    return 0 <= limit.reset_at - now <= horizon


def reserve_fraction(priority: str, settings: Settings) -> float:
    if priority == "background":
        return settings.reserve_background
    if priority == "normal":
        return settings.reserve_normal
    return 0.0


def breaches_reserve(limit: Limit, fraction: float) -> bool:
    """True if one more request would take `limit` below `fraction` of itself.

    A request costs one unit of a requests limit. Its token cost is not known
    before the call, so a tokens limit is judged on what remains now.
    """
    if fraction <= 0:
        return False
    spend = 1 if limit.metric == "requests" else 0
    return (limit.remaining - spend) < fraction * limit.limit


# ---------------------------------------------------------------- the rules


def _when(ts: float | None, tz: tzinfo | None = None) -> str:
    if ts is None:
        return "an unknown time"
    if tz is None:
        return quota.iso(ts) or "an unknown time"
    return datetime.fromtimestamp(ts, tz=tz).strftime("%Y-%m-%d %H:%M %Z").strip()


def app_cap(app: AppPolicy, state: State) -> tuple[str, str] | None:
    """Rule 1: (code, message) if the app's own daily cap is reached."""
    if app.daily_requests is not None and state.app_requests_today >= app.daily_requests:
        return APP_REQUESTS, f"app '{app.name}' reached its daily request cap ({app.daily_requests}); resets at local midnight"
    if app.daily_tokens is not None and state.app_tokens_today >= app.daily_tokens:
        return APP_TOKENS, f"app '{app.name}' reached its daily token cap ({app.daily_tokens}); resets at local midnight"
    return None


def paid_gate(
    info: Mapping[str, Any], app: AppPolicy, state: State, settings: Settings, *, direct: bool = False
) -> tuple[str, str] | None:
    """Rule 2: (code, detail) if this paid deployment may not be used now; None if it may (or is free)."""
    if not is_paid(info):
        return None
    if not app.allow_paid:
        return PAID_NOT_ALLOWED, f"app '{app.name}' may not use paid providers"
    if settings.paid_cap_usd <= 0:
        return PAID_CAP, "the daily paid cap is 0 (MUSE_DAILY_PAID_CAP_USD), so paid providers are off"
    if state.paid_spend_usd is None:
        return PAID_SPEND_UNKNOWN, "today's paid spend could not be read, so paid providers are off for now"
    if state.paid_spend_usd >= settings.paid_cap_usd:
        return PAID_CAP, f"daily paid cap of {format_usd(settings.paid_cap_usd)} reached; resets at local midnight"
    if price_of(info) is None and not direct:
        return PAID_UNKNOWN_COST, "this paid deployment has no price in the catalogue; call it by its own name to use it"
    return None


def ration(
    app: AppPolicy,
    deployments: Sequence[dict],
    state: State,
    settings: Settings,
    now: float,
    *,
    direct: bool = False,
    prior: Sequence[Removed] = (),
) -> Outcome:
    """Apply rules 1-6. `prior` are earlier removals (provider blocks) that
    count toward the final decision. `direct` marks a request that named the
    deployment itself, which may use a paid deployment without a price."""
    deployments = list(deployments)
    removed: list[Removed] = []
    cap = app_cap(app, state)
    if cap is not None:
        code, message = cap
        for dep in deployments:
            info = _info(dep)
            removed.append(Removed(str(info.get("id", "")), upstream_of(info), code, REFUSE, state.day_end, message))
        return Outcome([], removed, Decision(REFUSE, code, f"[muse] {message}", state.day_end, 429))

    kept: list[dict] = []
    fraction = reserve_fraction(app.priority, settings)
    for dep in deployments:
        info = _info(dep)
        dep_id, upstream = str(info.get("id", "")), upstream_of(info)

        gate = paid_gate(info, app, state, settings, direct=direct)
        if gate is not None:
            code, detail = gate
            retry = state.day_end if code == PAID_CAP and settings.paid_cap_usd > 0 else None
            if code == PAID_SPEND_UNKNOWN:
                retry = now + 60
            removed.append(Removed(dep_id, upstream, code, REFUSE, retry, detail))
            continue

        if app.priority == "background" and (settings.local_yields or info.get("muse_kind") != "local"):
            last = state.last_interactive.get(upstream)
            if last is not None and now - last < settings.active_window_s:
                retry = last + settings.active_window_s
                removed.append(Removed(dep_id, upstream, YIELD, DEFER, retry,
                                       f"yielding to the active app; retry in {max(1, math.ceil(retry - now))} s"))
                continue

        if fraction > 0:
            hit: Limit | None = None
            for limit in state.limits.get(upstream, ()):
                if breaches_reserve(limit, fraction) and not expiring(limit, settings, now):
                    if hit is None or (limit.reset_at or math.inf) > (hit.reset_at or math.inf):
                        hit = limit  # the latest reset is when it is really free again
            if hit is not None:
                retry = hit.reset_at if hit.reset_at is not None else now + WINDOW_SECONDS.get(hit.window, 60)
                removed.append(Removed(
                    dep_id, upstream, RESERVE, DEFER, retry,
                    f"keeping a reserve for interactive use ({hit.remaining}/{hit.limit} {hit.metric} per {hit.window} left); "
                    f"resets at {_when(retry)}",
                ))
                continue
        kept.append(dep)

    decision = None if kept else decide(app, [*prior, *removed], now, settings)
    return Outcome(kept, removed, decision)


def decide(app: AppPolicy, removed: Sequence[Removed], now: float, settings: Settings) -> Decision | None:
    """One clear answer when nothing is left: defer to the soonest retry if any
    removal is temporary, else refuse with the most relevant reason."""
    if not removed:
        return None
    order = [PAID_CAP, PAID_SPEND_UNKNOWN, PAID_NOT_ALLOWED, PAID_UNKNOWN_COST]
    refusals = sorted((r for r in removed if r.kind == REFUSE), key=lambda r: order.index(r.code) if r.code in order else len(order))
    defers = [r for r in removed if r.kind == DEFER and r.retry_at is not None]
    if defers:
        first = min(defers, key=lambda r: r.retry_at or math.inf)
        if first.code == YIELD:
            message = f"[muse] yielding to the active app; retry in {max(1, math.ceil((first.retry_at or now) - now))} s"
        elif first.code == BLOCKED and refusals and refusals[0].code == PAID_CAP:
            # Free quota is spent and the paid allowance too: name the cap, it is what the user set.
            cap = refusals[0]
            retry = min(first.retry_at or math.inf, cap.retry_at or math.inf)
            return Decision(DEFER, PAID_CAP, f"[muse] {cap.detail}; free quota is used up until {_when(first.retry_at)}",
                            None if retry == math.inf else retry, 429)
        else:
            message = f"[muse] {first.detail}"
        return Decision(DEFER, first.code, message, first.retry_at, 429)
    first = refusals[0] if refusals else removed[0]
    return Decision(REFUSE, first.code, f"[muse] {first.detail}", first.retry_at, 429)


def fail_closed(deployments: Sequence[dict]) -> list[dict]:
    """What to keep when rationing itself failed: everything but paid deployments."""
    return [dep for dep in deployments if not is_paid(_info(dep))]
