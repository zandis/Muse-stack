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
  7. pacing       a background app also waits while a day or month allowance is
                  behind its straight line from full (last reset) to empty (next
                  reset): surplus = remaining/limit - (1 - elapsed/window) must be
                  >= MUSE_PACE_MARGIN. Waived with the reserve by rule 5.
  8. hand-off     a task marked `handoff: suggest` (frontier) keeps paid
                  deployments back until the request says x-muse-allow-paid: true,
                  so the user is offered "wait / open my own app / pay" first.
  9. one-off      trial credits marked one-off (model_info.muse_oneoff) are kept
                  back unless the request says x-muse-allow-oneoff: true.
Interactive apps are never deferred by rules 3-5 or 7.

After the rules, `prefer` narrows one LiteLLM order level to the upstreams
whose allowance will reset with the most left over ("spend what expires
soonest"), only when that is decisive (see Settings.prefer_gap).
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
PACE = "pace"
PACE_PREFER = "pace:prefer"  # not a removal: the ordering hint chose among survivors
ONEOFF_HELD = "oneoff-held"
HANDOFF_FIRST = "handoff-first"

# Window lengths used when only a reset time is known (headers). A provider's
# calendar windows are computed exactly from muse.quota instead (window_bounds).
NOMINAL_WINDOW_S = {"minute": 60, "day": 86400, "month": 30 * 86400}
PACED_WINDOWS = frozenset({"day", "month"})

DEFER = "defer"
REFUSE = "refuse"


# ---------------------------------------------------------------- settings

# The daily paid limit a new person starts with: the owner's decision is NT$30
# a day, at the fixed rate of 32 NT$ to the US dollar. The router counts in
# dollars, so its own default cap is that figure in dollars, and the home's
# default limit (muse/home/app_money.py) is this same constant: the two agree
# from the first start, with nothing written on the person's behalf.
DEFAULT_DAILY_LIMIT_TWD = Decimal("30")
FIXED_USD_RATE_TWD = Decimal("32")
DEFAULT_PAID_CAP_USD = DEFAULT_DAILY_LIMIT_TWD / FIXED_USD_RATE_TWD  # 0.9375


@dataclass(frozen=True, slots=True)
class Settings:
    """Ration settings from the environment. See Settings.from_env for names and defaults."""

    paid_cap_usd: Decimal = DEFAULT_PAID_CAP_USD
    active_window_s: float = 120.0
    reserve_background: float = 0.30
    reserve_normal: float = 0.10
    expiring_day_s: float = 3600.0
    expiring_month_s: float = 86400.0
    local_yields: bool = True
    tz_name: str = ""
    pace_margin: float = 0.0
    prefer_gap: float = 0.10
    burn_window_s: float = 3600.0
    burn_min_calls: int = 3
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
            pace_margin=number("MUSE_PACE_MARGIN", 0.0, -1, 1),
            prefer_gap=number("MUSE_PACE_PREFER_GAP", 0.10, 0, 2),
            burn_window_s=number("MUSE_PACE_BURN_WINDOW_S", 3600.0, 60, 86400),
            burn_min_calls=int(number("MUSE_PACE_BURN_MIN_CALLS", 3, 1, 100000)),
            warnings=tuple(warnings),
        )


def parse_paid_cap(raw: Any, warnings: list[str] | None = None) -> Decimal:
    """MUSE_DAILY_PAID_CAP_USD: unset -> DEFAULT_PAID_CAP_USD (0.9375, NT$30 at 32); "0" -> never;
    anything unparsable or negative -> 0."""
    if raw is None or not str(raw).strip():
        return DEFAULT_PAID_CAP_USD
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
    window_start: float | None = None  # the previous reset; None = window length unknown (no pacing)
    scope: str = ""  # ledger scope it was counted in (upstream or provider:<id>); "" = the upstream


@dataclass(slots=True)
class State:
    """Ledger state at decision time. `paid_spend_usd=None` means unknown (gate closes)."""

    limits: Mapping[str, Sequence[Limit]] = field(default_factory=dict)  # upstream -> known limits
    app_requests_today: int = 0
    app_tokens_today: int = 0
    paid_spend_usd: Decimal | None = Decimal("0")
    last_interactive: Mapping[str, float] = field(default_factory=dict)  # upstream -> ts
    day_end: float = 0.0  # next local midnight
    burn: Mapping[str, Mapping[str, int]] = field(default_factory=dict)  # scope -> {calls, tokens} in the burn window


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
    Each limit carries its window start (previous reset) when the window length
    is known, which is what pacing needs.
    """
    provider_id = str(info.get("provider") or "")
    best: dict[tuple[str, str], Limit] = {}
    for row in observed:
        if not _fresh(row, now):
            continue
        lim, rem = row.get("limit", row.get("lim")), row.get("remaining")
        if not isinstance(lim, int) or not isinstance(rem, int) or lim <= 0:
            continue
        key = (str(row.get("window")), str(row.get("metric")))
        reset_at = row.get("reset_at")
        bounds = window_bounds(key[0], provider_id, now, reset_at=reset_at, observed=True)
        cand = Limit(key[0], key[1], lim, max(0, rem), reset_at, "header",
                     bounds[0] if bounds else None, str(row.get("scope") or ""))
        if key not in best or cand.remaining / cand.limit < best[key].remaining / best[key].limit:
            best[key] = cand
    for window, metric, limit in declared_quota(info.get("muse_quota")):
        if (window, metric) in best:
            continue
        used = int((provider_usage.get(window) or {}).get(metric, 0))
        bounds = window_bounds(window, provider_id, now)
        best[(window, metric)] = Limit(window, metric, limit, max(0, limit - used), window_reset(window, provider_id, now),
                                       "declared", bounds[0] if bounds else None, f"provider:{provider_id}" if provider_id else "")
    return [best[key] for key in sorted(best)]


# ---------------------------------------------------------------- pacing (rule 4)


def previous_midnight_pacific(now: float) -> float:
    """Start of the Pacific calendar day containing `now` (Gemini's last reset).

    Exact on DST switch days (23 or 25 hours long): it is the reset before
    quota.next_midnight_pacific(now), found by stepping back 26 h, which is
    inside the previous local day whatever its length.
    """
    return quota.next_midnight_pacific(quota.next_midnight_pacific(now) - 26 * 3600)


def window_bounds(
    window: str, provider_id: str, now: float, *, reset_at: float | None = None, observed: bool = False
) -> tuple[float, float] | None:
    """(previous reset, next reset) of the window containing `now`, or None if unknown.

    Declared (catalogue) windows follow the provider's rule in muse.quota:
    minute = the UTC minute, day = midnight Pacific for Gemini and UTC midnight
    otherwise, month = UTC calendar month. Observed windows (headers) know only
    the next reset, so the start is reset - the window's nominal length (a
    rolling window), except a month window whose reset is a UTC month start,
    which is that calendar month. An observed reset further away than one
    window, or in the past, means the header does not describe this window:
    unknown.
    """
    if not observed:
        if window == "minute":
            start = now - (now % 60)
            return start, start + 60
        if window == "day":
            if provider_id == "gemini":
                return previous_midnight_pacific(now), quota.next_midnight_pacific(now)
            nxt = quota.next_midnight_utc(now)
            return nxt - 86400, nxt
        if window == "month":
            return quota.month_start(now), quota.next_month_utc(now)
        return None
    length = NOMINAL_WINDOW_S.get(window)
    if length is None or reset_at is None:
        return None
    reset_at = float(reset_at)
    if not math.isfinite(reset_at) or reset_at < now:
        return None
    if window == "month" and quota.month_start(reset_at) == reset_at:
        start = quota.month_start(reset_at - 1)
        return (start, reset_at) if start <= now else None
    if reset_at - now > length:
        return None
    return reset_at - length, reset_at


@dataclass(frozen=True, slots=True)
class Pace:
    """Where one allowance stands against its straight line. Fractions are of the limit.

    surplus = remaining_fraction - expected_remaining_fraction; > 0 means under
    the line (use is behind schedule). projected_leftover is the fraction left at
    reset if the recent burn rate continues (None when the rate is unknown).
    """

    applies: bool
    reason: str
    remaining_fraction: float
    expected_remaining_fraction: float | None = None
    surplus: float | None = None
    projected_leftover: float | None = None
    window_start: float | None = None
    reset_at: float | None = None


def burn_rate(units: float | None, calls: int | None, window_s: float, min_calls: int) -> float | None:
    """Units per second over the burn window; None (unknown) with fewer than `min_calls` calls.

    One or two calls are not a rate, so below the minimum the ordering hint
    falls back to the surplus instead of guessing.
    """
    if units is None or calls is None or window_s <= 0 or calls < max(1, min_calls):
        return None
    return max(0.0, float(units)) / window_s


def pace_position(limit: Limit, now: float, rate: float | None = None) -> Pace:
    """The pacing line for one limit at `now`. Pure; see Pace for the definitions.

    expected_remaining_fraction(now) = 1 - clamp((now - start) / (reset - start), 0, 1)
    surplus = remaining / limit - expected_remaining_fraction(now)
    projected_leftover = clamp((remaining - rate * (reset - now)) / limit, 0, 1)
    Minute windows are rate limits, not allowances, and a snapshot of one is stale
    within seconds, so they are never paced.
    """
    rf = min(1.0, max(0.0, limit.remaining / limit.limit)) if limit.limit > 0 else 0.0
    if limit.window not in PACED_WINDOWS:
        return Pace(False, f"{limit.window} windows are rate limits, not allowances; pacing does not apply", rf)
    if limit.window_start is None or limit.reset_at is None or limit.reset_at <= limit.window_start:
        return Pace(False, f"the {limit.window} window's length is unknown; pacing does not apply (reserve rule only)", rf)
    span = limit.reset_at - limit.window_start
    elapsed = min(1.0, max(0.0, (now - limit.window_start) / span))
    expected = 1.0 - elapsed
    leftover = None
    if rate is not None:
        leftover = min(1.0, max(0.0, (limit.remaining - rate * max(0.0, limit.reset_at - now)) / limit.limit))
    return Pace(True, "", rf, expected, rf - expected, leftover, limit.window_start, limit.reset_at)


def pace_release_at(limit: Limit, margin: float, settings: Settings, now: float) -> float:
    """When a held background app may next use this limit if nobody spends it:
    the line catches up (start + (1 - remaining_fraction + margin) * span), or the
    expiring horizon opens, whichever is first; never before `now` + 1 s."""
    assert limit.window_start is not None and limit.reset_at is not None
    span = limit.reset_at - limit.window_start
    rf = min(1.0, max(0.0, limit.remaining / limit.limit))
    caught_up = limit.window_start + (1.0 - rf + margin) * span
    horizon = settings.expiring_day_s if limit.window == "day" else settings.expiring_month_s
    return max(now + 1.0, min(caught_up, limit.reset_at - horizon, limit.reset_at))


def pace_holds(limit: Limit, settings: Settings, now: float) -> bool:
    """True if background use must wait for this limit: pacing applies and surplus < margin."""
    pos = pace_position(limit, now)
    return pos.applies and pos.surplus is not None and pos.surplus < settings.pace_margin - 1e-12


def limit_rate(limit: Limit, upstream: str, burn: Mapping[str, Mapping[str, int]], settings: Settings) -> float | None:
    """Recent burn rate for the scope a limit is counted in (requests: calls; tokens: tokens)."""
    row = burn.get(limit.scope or upstream)
    if not row:
        return None
    units = row.get("calls") if limit.metric == "requests" else row.get("tokens")
    return burn_rate(units, row.get("calls"), settings.burn_window_s, settings.burn_min_calls)


def pace_view(
    limits_by_upstream: Mapping[str, Sequence[Limit]],
    now: float,
    *,
    burn: Mapping[str, Mapping[str, int]] | None = None,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Rows for a status screen to draw the pacing line. Pure.

    One row per (upstream, window, metric) with a known limit, and one row with
    source "unknown" for an upstream with none. Keys:
      upstream, window, metric, limit, remaining, reset_at, window_start (epoch s or None),
      expected_remaining (units, None if pacing does not apply), surplus (fraction or None),
      projected_leftover (fraction or None), source (observed | declared | unknown),
      paced (bool), reason (why not paced, else "").
    """
    settings = settings or Settings()
    burn = burn or {}
    rows: list[dict[str, Any]] = []
    for upstream in sorted(limits_by_upstream):
        limits = list(limits_by_upstream[upstream])
        if not limits:
            rows.append({"upstream": upstream, "window": None, "metric": None, "limit": None, "remaining": None,
                         "reset_at": None, "window_start": None, "expected_remaining": None, "surplus": None,
                         "projected_leftover": None, "source": "unknown", "paced": False,
                         "reason": "the provider reports nothing and the catalogue declares no quota"})
            continue
        for lim in limits:
            pos = pace_position(lim, now, limit_rate(lim, upstream, burn, settings))
            rows.append({
                "upstream": upstream, "window": lim.window, "metric": lim.metric, "limit": lim.limit,
                "remaining": lim.remaining, "reset_at": lim.reset_at, "window_start": lim.window_start,
                "expected_remaining": None if pos.expected_remaining_fraction is None else pos.expected_remaining_fraction * lim.limit,
                "surplus": pos.surplus, "projected_leftover": pos.projected_leftover,
                "source": "observed" if lim.source == "header" else lim.source, "paced": pos.applies, "reason": pos.reason,
            })
    return rows


@dataclass(frozen=True, slots=True)
class Rank:
    """How much one upstream's allowance is worth spending now (higher = spend first)."""

    upstream: str
    klass: str  # surplus | unknown | deficit
    surplus: float | None
    leftover: float | None
    detail: str


def rank_upstream(upstream: str, limits: Sequence[Limit], now: float, burn: Mapping[str, Mapping[str, int]],
                  settings: Settings) -> Rank:
    """Rank by the binding paced limit (the one with the smallest surplus).

    surplus >= 0 -> class surplus; < 0 -> deficit; no paced limit -> unknown.
    """
    best: tuple[Pace, Limit] | None = None
    for lim in limits:
        pos = pace_position(lim, now, limit_rate(lim, upstream, burn, settings))
        if pos.applies and (best is None or (pos.surplus or 0.0) < (best[0].surplus or 0.0)):
            best = (pos, lim)
    if best is None:
        return Rank(upstream, "unknown", None, None, f"{upstream}: remaining or window unknown")
    pos, lim = best
    klass = "surplus" if (pos.surplus or 0.0) >= 0 else "deficit"
    detail = f"{upstream}: {lim.remaining}/{lim.limit} {lim.metric} per {lim.window}, surplus {pos.surplus:+.2f}"
    if pos.projected_leftover is not None:
        detail += f", leftover at reset {pos.projected_leftover:.2f}"
    return Rank(upstream, klass, pos.surplus, pos.projected_leftover, detail)


def deployment_order(deployment: Mapping[str, Any]) -> int | None:
    """LiteLLM's `order` for a deployment (litellm_params.order), None when unset."""
    params = deployment.get("litellm_params") if isinstance(deployment, Mapping) else None
    value = params.get("order") if isinstance(params, Mapping) else None
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def rank_values(ranks: Sequence[Rank]) -> list[float]:
    """Comparable values, higher = spend first. Pure.

    unknown remaining -> 0.0 (as if on the line).
    known, when every known rank has a burn rate -> the projected leftover
      fraction at reset if it is > 0; an upstream projected to run dry ranks by
      min(0, surplus), so one behind schedule falls below unknown.
    known, otherwise -> the surplus.
    Either way: known with something to spare > unknown > known behind schedule.
    """
    known = [r for r in ranks if r.klass != "unknown"]
    use_leftover = bool(known) and all(r.leftover is not None for r in known)
    out = []
    for r in ranks:
        surplus = float(r.surplus or 0.0)
        if r.klass == "unknown":
            out.append(0.0)
        elif use_leftover:
            leftover = float(r.leftover or 0.0)
            out.append(leftover if leftover > 0 else min(0.0, surplus))
        else:
            out.append(surplus)
    return out


def prefer(
    deployments: Sequence[dict],
    state: State,
    settings: Settings,
    now: float,
    *,
    target_order: int | None = None,
    tried: Iterable[str] = (),
) -> tuple[list[dict], str | None]:
    """Rule 4's ordering hint: narrow the order level LiteLLM will pick from to the
    upstreams whose allowance will reset with the most left over. Pure.

    The level is `target_order` (LiteLLM's order fallback) or the lowest order
    present, as litellm.utils.get_order_filtered_deployments picks it. Other
    levels are returned untouched, so LiteLLM's order and alias fallbacks work
    as before. Decisive means: some deployment's value is more than
    settings.prefer_gap below the best one's (see rank_values); only those are
    dropped, so near-equals are still load-balanced. Never narrows when fewer
    than two upstreams share the level. On a retry (a deployment of the level
    was already tried for this request) it is not a pacing choice: the upstreams
    that failed are set aside if another one at the level is left, else nothing
    changes. Returns (deployments, reason or None); a pacing reason starts with
    "pace:prefer", a retry reason with "retry:".
    """
    deployments = list(deployments)
    orders = [deployment_order(d) for d in deployments]
    present = [o for o in orders if o is not None]
    level = target_order if target_order is not None and target_order in present else (min(present) if present else None)
    members = [d for d, o in zip(deployments, orders) if o == level]
    upstreams = sorted({upstream_of(_info(d)) for d in members})
    if len(upstreams) < 2:
        return deployments, None
    tried_ids = set(tried)
    tried_here = [d for d in members if str(_info(d).get("id", "")) in tried_ids]
    if tried_here:
        # A retry after a failed attempt: not a pacing choice. Steer it to an
        # upstream of this level that has not failed yet, if there is one, so the
        # hint (which sent the first attempt to one upstream) never makes failover worse.
        failed = {upstream_of(_info(d)) for d in tried_here}
        fresh = [d for d in members if upstream_of(_info(d)) not in failed]
        if not fresh:
            return deployments, None
        out = [d for d, o in zip(deployments, orders) if o != level or upstream_of(_info(d)) not in failed]
        return out, f"retry: {', '.join(sorted(failed))} already failed for this request; trying the others at this level first"
    ranks = [rank_upstream(u, state.limits.get(u, ()), now, state.burn, settings) for u in upstreams]
    values = dict(zip(upstreams, rank_values(ranks)))
    top = max(values.values())
    keep = {u for u, v in values.items() if top - v <= settings.prefer_gap + 1e-12}
    if keep == set(upstreams):
        return deployments, None
    chosen = [r for r in ranks if r.upstream in keep]
    dropped = [r for r in ranks if r.upstream not in keep]
    reason = (f"{PACE_PREFER}: prefer {', '.join(r.detail for r in chosen)} over "
              f"{', '.join(r.detail for r in dropped)} (gap > {settings.prefer_gap:.2f})")
    out = [d for d, o in zip(deployments, orders) if o != level or upstream_of(_info(d)) in keep]
    return out, reason


# ---------------------------------------------------------------- options for the user (rule 6)


def options_trailer(wait_at: float | None, handoff: bool, pay_usd: float | None) -> str:
    """The machine-readable choices a bot turns into the three-choice card.

    Grammar: `options=` then a comma-separated list in this fixed order, only
    the choices available now: `wait:<ISO-8601 UTC, ...Z>` (the soonest reset),
    `handoff` (open the user's own subscription app), `pay:<USD, 6 decimals>`
    (estimated cost). `options=none` when nothing is available.
    """
    items = []
    if wait_at is not None:
        items.append(f"wait:{quota.iso(wait_at)}")
    if handoff:
        items.append("handoff")
    if pay_usd is not None:
        items.append(f"pay:{pay_usd:.6f}")
    return "options=" + (",".join(items) if items else "none")


def parse_options(message: str) -> dict[str, Any]:
    """Inverse of options_trailer for a message that ends with ` | options=...` (bots, tests)."""
    _, sep, tail = str(message).rpartition(" | options=")
    if not sep:
        return {}
    out: dict[str, Any] = {}
    for item in tail.strip().split(","):
        name, _, value = item.partition(":")
        if name == "wait":
            out["wait"] = value
        elif name == "pay":
            out["pay"] = float(value)
        elif name == "handoff":
            out["handoff"] = True
    return out


def available_options(
    decision: Decision,
    removed: Sequence[Removed],
    *,
    handoff: bool,
    pay_estimates: Mapping[str, float],
) -> tuple[float | None, bool, float | None]:
    """(wait_at, handoff, pay_usd) that are really available now. Pure.

    wait: the soonest retry among temporary removals (a reset, a yield, pacing),
    or the app's own cap reset; none for a permanent refusal. pay: the cheapest
    estimate among paid deployments held only for the hand-off (they passed the
    paid gate: the app may pay, the cap has room, the price is known).
    """
    waits = [r.retry_at for r in removed if r.kind == DEFER and r.retry_at is not None]
    if decision.code in (APP_REQUESTS, APP_TOKENS) and decision.retry_at is not None:
        waits.append(decision.retry_at)
    pays = [pay_estimates[r.deployment_id] for r in removed if r.code == HANDOFF_FIRST and r.deployment_id in pay_estimates]
    return (min(waits) if waits else None), bool(handoff), (min(pays) if pays else None)


def with_options(decision: Decision, removed: Sequence[Removed], *, handoff: bool,
                 pay_estimates: Mapping[str, float]) -> Decision:
    """The decision with ` | options=...` appended to its message (once)."""
    if " | options=" in decision.message:
        return decision
    wait_at, ho, pay = available_options(decision, removed, handoff=handoff, pay_estimates=pay_estimates)
    return Decision(decision.kind, decision.code, f"{decision.message} | {options_trailer(wait_at, ho, pay)}",
                    decision.retry_at, decision.status)


def is_oneoff(info: Mapping[str, Any]) -> bool:
    """Rule 7: a trial deployment whose credits never renew (catalogue marks the provider one-off)."""
    return tier_of(info) == "trial" and info.get("muse_oneoff") is True


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
    allow_oneoff: bool = False,
    handoff_hold: bool = False,
) -> Outcome:
    """Apply the rules. `prior` are earlier removals (provider blocks) that
    count toward the final decision. `direct` marks a request that named the
    deployment itself, which may use a paid deployment without a price.
    `allow_oneoff`: the request carries the user's approval to spend one-off
    trial credits. `handoff_hold`: a frontier task without approval to pay, so
    paid deployments are offered (options trailer), not used."""
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
    paced = app.priority == "background"
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

        if fraction > 0 or paced:
            hit: Limit | None = None
            behind: Limit | None = None
            for limit in state.limits.get(upstream, ()):
                if expiring(limit, settings, now):
                    continue  # about to reset: reserve and pacing are both waived
                if fraction > 0 and breaches_reserve(limit, fraction):
                    if hit is None or (limit.reset_at or math.inf) > (hit.reset_at or math.inf):
                        hit = limit  # the latest reset is when it is really free again
                elif paced and pace_holds(limit, settings, now):
                    release = pace_release_at(limit, settings.pace_margin, settings, now)
                    if behind is None or release > pace_release_at(behind, settings.pace_margin, settings, now):
                        behind = limit
            if hit is not None:
                retry = hit.reset_at if hit.reset_at is not None else now + WINDOW_SECONDS.get(hit.window, 60)
                removed.append(Removed(
                    dep_id, upstream, RESERVE, DEFER, retry,
                    f"keeping a reserve for interactive use ({hit.remaining}/{hit.limit} {hit.metric} per {hit.window} left); "
                    f"resets at {_when(retry)}",
                ))
                continue
            if behind is not None:
                pos = pace_position(behind, now)
                retry = pace_release_at(behind, settings.pace_margin, settings, now)
                removed.append(Removed(
                    dep_id, upstream, PACE, DEFER, retry,
                    f"pacing: this {behind.window}'s allowance is ahead of schedule ({behind.remaining}/{behind.limit} "
                    f"{behind.metric} left, {pos.expected_remaining_fraction * behind.limit:.0f} expected by now); "
                    f"background work resumes at {_when(retry)}",
                ))
                continue

        if handoff_hold and is_paid(info):
            removed.append(Removed(dep_id, upstream, HANDOFF_FIRST, REFUSE, None,
                                   "a frontier task: the hand-off to your own subscription app is offered before spending"))
            continue

        if not allow_oneoff and is_oneoff(info):
            removed.append(Removed(dep_id, upstream, ONEOFF_HELD, REFUSE, None,
                                   "one-off trial credits are held until you approve using them"))
            continue
        kept.append(dep)

    decision = None if kept else decide(app, [*prior, *removed], now, settings)
    return Outcome(kept, removed, decision)


HANDOFF_MESSAGE = ("[muse] this is a frontier task and no free allowance that fits it can answer now; "
                   "choose: wait, open it in your own subscription app, or approve spending (x-muse-allow-paid: true)")
ONEOFF_MESSAGE = ("[muse] only one-off trial credits could answer this now; they are held until you approve using them "
                  "(x-muse-allow-oneoff: true)")


def decide(app: AppPolicy, removed: Sequence[Removed], now: float, settings: Settings) -> Decision | None:
    """One clear answer when nothing is left. A removal the user can lift right now
    by approving (hand-off hold, one-off hold) is a refusal that needs a choice,
    not a deferral; its retry is the soonest temporary removal, if any. Otherwise
    defer to the soonest retry if any removal is temporary, else refuse with the
    most relevant reason."""
    if not removed:
        return None
    defers = [r for r in removed if r.kind == DEFER and r.retry_at is not None]
    soonest = min((r.retry_at for r in defers if r.retry_at is not None), default=None)
    for code, message in ((HANDOFF_FIRST, HANDOFF_MESSAGE), (ONEOFF_HELD, ONEOFF_MESSAGE)):
        if any(r.code == code for r in removed):
            return Decision(REFUSE, code, message, soonest, 429)
    order = [PAID_CAP, PAID_SPEND_UNKNOWN, PAID_NOT_ALLOWED, PAID_UNKNOWN_COST]
    refusals = sorted((r for r in removed if r.kind == REFUSE), key=lambda r: order.index(r.code) if r.code in order else len(order))
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
    """What to keep when rationing itself failed: everything but paid and one-off deployments."""
    return [dep for dep in deployments if not is_paid(_info(dep)) and not is_oneoff(_info(dep))]
