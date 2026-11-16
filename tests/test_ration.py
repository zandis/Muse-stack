"""muse.ration: every rule alone and in combination, boundaries, unknown limits, clocks, settings."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from wardmuse import ration
from wardmuse.ration import (
    APP_REQUESTS, APP_TOKENS, BLOCKED, DEFER, PAID_CAP, PAID_NOT_ALLOWED, PAID_SPEND_UNKNOWN, PAID_UNKNOWN_COST,
    REFUSE, RESERVE, YIELD, AppPolicy, Limit, Removed, Settings, State,
)

NOW = datetime(2026, 11, 12, 7, 0, 0, tzinfo=timezone.utc).timestamp()
DAY_END = NOW + 17 * 3600


def dep(dep_id: str, tier: str = "free", kind: str = "cloud", price: tuple[float, float] | None = None,
        upstream: str | None = None, provider: str = "p", quota=None) -> dict:
    info = {"id": dep_id, "provider": provider, "muse_upstream": upstream or f"{provider}:{dep_id}", "muse_tier": tier, "muse_kind": kind}
    if price is not None:
        info["muse_price_in"], info["muse_price_out"] = price
    if quota is not None:
        info["muse_quota"] = quota
    return {"model_name": "chat", "model_info": info}


FREE = dep("free", upstream="a:up")
FREE_B = dep("freeb", upstream="b:up")
LOCAL = dep("local", tier="local", kind="local", upstream="l:up")
PAID = dep("paid", tier="paid", price=(1.0, 2.0), upstream="pp:up")
METERED = dep("metered", tier="metered", price=(1.0, 2.0), upstream="m:up")
PAID_NOPRICE = dep("paidu", tier="paid", upstream="pu:up")
NO_TIER = {"model_info": {"id": "x", "muse_upstream": "x:up"}}

S = Settings()
INTERACTIVE = AppPolicy("chat-ui", "interactive", allow_paid=True)
NORMAL = AppPolicy("script", "normal", allow_paid=True)
BACKGROUND = AppPolicy("batch", "background", allow_paid=False)
BACKGROUND_PAID = AppPolicy("batch", "background", allow_paid=True)


def state(**kw) -> State:
    kw.setdefault("day_end", DAY_END)
    return State(**kw)


def ids(outcome) -> list[str]:
    return [d["model_info"]["id"] for d in outcome.kept]


def codes(outcome) -> dict[str, str]:
    return {r.deployment_id: r.code for r in outcome.removed}


def run(app, deps, st=None, settings=S, now=NOW, **kw):
    return ration.ration(app, deps, st or state(), settings, now, **kw)


# ---------------------------------------------------------------- nothing applies

@pytest.mark.parametrize("app", [INTERACTIVE, NORMAL, BACKGROUND])
def test_free_and_local_pass_with_no_state(app):
    out = run(app, [FREE, FREE_B, LOCAL])
    assert ids(out) == ["free", "freeb", "local"] and out.removed == [] and out.decision is None


def test_empty_input_is_no_decision():
    out = run(NORMAL, [])
    assert out.kept == [] and out.decision is None


# ---------------------------------------------------------------- rule 1: app caps

@pytest.mark.parametrize("cap_field, used_field, cap, used, refused, code", [
    ("daily_requests", "app_requests_today", 10, 9, False, None),
    ("daily_requests", "app_requests_today", 10, 10, True, APP_REQUESTS),
    ("daily_requests", "app_requests_today", 0, 0, True, APP_REQUESTS),
    ("daily_tokens", "app_tokens_today", 1000, 999, False, None),
    ("daily_tokens", "app_tokens_today", 1000, 1000, True, APP_TOKENS),
    ("daily_tokens", "app_tokens_today", 1000, 5000, True, APP_TOKENS),  # the crossing request finished
])
def test_app_caps(cap_field, used_field, cap, used, refused, code):
    app = AppPolicy("x", "interactive", allow_paid=True, **{cap_field: cap})
    out = run(app, [FREE, PAID], state(**{used_field: used}))
    if refused:
        assert out.kept == []
        assert out.decision.kind == REFUSE and out.decision.code == code and out.decision.status == 429
        assert out.decision.retry_at == DAY_END and "'x'" in out.decision.message
        assert out.decision.message.startswith("[muse]")
    else:
        assert out.decision is None and "free" in ids(out)


def test_app_cap_applies_even_to_interactive_and_before_everything():
    app = AppPolicy("x", "interactive", allow_paid=True, daily_requests=1)
    out = run(app, [LOCAL], state(app_requests_today=1, last_interactive={"l:up": NOW}))
    assert out.decision.code == APP_REQUESTS and {r.code for r in out.removed} == {APP_REQUESTS}


# ---------------------------------------------------------------- rule 2: paid gate

@pytest.mark.parametrize("app, deps, spend, cap, direct, kept, removed_code", [
    (INTERACTIVE, [PAID], Decimal("0"), Decimal("1"), False, ["paid"], None),
    (INTERACTIVE, [METERED], Decimal("0"), Decimal("1"), False, ["metered"], None),
    (INTERACTIVE, [PAID], Decimal("0.999999"), Decimal("1"), False, ["paid"], None),  # just under: may cross
    (INTERACTIVE, [PAID], Decimal("1"), Decimal("1"), False, [], PAID_CAP),  # at the cap: refused
    (INTERACTIVE, [PAID], Decimal("1.5"), Decimal("1"), False, [], PAID_CAP),
    (INTERACTIVE, [PAID], Decimal("0"), Decimal("0"), False, [], PAID_CAP),  # 0 = never
    (INTERACTIVE, [PAID], None, Decimal("1"), False, [], PAID_SPEND_UNKNOWN),  # fail closed
    (BACKGROUND, [PAID], Decimal("0"), Decimal("1"), False, [], PAID_NOT_ALLOWED),
    (BACKGROUND_PAID, [PAID], Decimal("0"), Decimal("1"), False, ["paid"], None),
    (INTERACTIVE, [PAID_NOPRICE], Decimal("0"), Decimal("1"), False, [], PAID_UNKNOWN_COST),
    (INTERACTIVE, [PAID_NOPRICE], Decimal("0"), Decimal("1"), True, ["paidu"], None),  # named directly
    (INTERACTIVE, [PAID_NOPRICE], Decimal("1"), Decimal("1"), True, [], PAID_CAP),  # still under the cap
    (BACKGROUND, [PAID_NOPRICE], Decimal("0"), Decimal("1"), True, [], PAID_NOT_ALLOWED),
    (INTERACTIVE, [NO_TIER], Decimal("0"), Decimal("1"), False, [], PAID_UNKNOWN_COST),  # no tier = paid
    (INTERACTIVE, [FREE, PAID], Decimal("5"), Decimal("1"), False, ["free"], PAID_CAP),
])
def test_paid_gate(app, deps, spend, cap, direct, kept, removed_code):
    settings = Settings(paid_cap_usd=cap)
    out = run(app, deps, state(paid_spend_usd=spend), settings, direct=direct)
    assert ids(out) == kept
    if removed_code:
        assert [r.code for r in out.removed] == [removed_code]
        assert out.removed[0].kind == REFUSE
    else:
        assert out.removed == []


def test_local_deployment_is_never_paid_whatever_its_tier_says():
    odd = dep("odd", tier="paid", kind="local")
    assert ids(run(BACKGROUND, [odd])) == ["odd"]


def test_paid_cap_decision_and_message():
    out = run(INTERACTIVE, [PAID], state(paid_spend_usd=Decimal("0.9375")))  # the default cap: NT$30 at 32
    assert out.decision.kind == REFUSE and out.decision.code == PAID_CAP
    assert "daily paid cap of $0.94 reached" in out.decision.message and out.decision.retry_at == DAY_END
    assert run(INTERACTIVE, [PAID], state(paid_spend_usd=Decimal("0.9374"))).decision is None  # just under it: allowed


def test_tiny_cap_formatting():
    out = run(INTERACTIVE, [PAID], state(paid_spend_usd=Decimal("0.002")), Settings(paid_cap_usd=Decimal("0.001")))
    assert "$0.001" in out.decision.message


# ---------------------------------------------------------------- rule 3: yield

@pytest.mark.parametrize("app, last_age, local_yields, deployment, yielded", [
    (BACKGROUND, 10, True, FREE, True),
    (BACKGROUND, 119.9, True, FREE, True),
    (BACKGROUND, 120, True, FREE, False),  # window is [last, last+120)
    (BACKGROUND, 500, True, FREE, False),
    (BACKGROUND, 10, True, LOCAL, True),  # local yields by default
    (BACKGROUND, 10, False, LOCAL, False),  # MUSE_LOCAL_YIELD=false
    (BACKGROUND, 10, False, FREE, True),  # cloud yields regardless
    (NORMAL, 10, True, FREE, False),  # only background yields
    (INTERACTIVE, 10, True, FREE, False),
])
def test_yield(app, last_age, local_yields, deployment, yielded):
    up = deployment["model_info"]["muse_upstream"]
    out = run(app, [deployment], state(last_interactive={up: NOW - last_age}), Settings(local_yields=local_yields))
    if yielded:
        assert out.kept == [] and out.removed[0].code == YIELD and out.removed[0].kind == DEFER
        assert out.decision.kind == DEFER and out.decision.retry_at == pytest.approx(NOW - last_age + 120)
        assert out.decision.message.startswith("[muse] yielding to the active app; retry in ")
        assert out.decision.retry_after(NOW) == max(1, int(-(-(120 - last_age) // 1)))
    else:
        assert ids(out) == [deployment["model_info"]["id"]]


def test_yield_is_per_upstream():
    out = run(BACKGROUND, [FREE, FREE_B], state(last_interactive={"a:up": NOW - 5}))
    assert ids(out) == ["freeb"] and codes(out) == {"free": YIELD}


def test_yield_window_setting_zero_disables():
    out = run(BACKGROUND, [FREE], state(last_interactive={"a:up": NOW}), Settings(active_window_s=0))
    assert ids(out) == ["free"]


# ---------------------------------------------------------------- rules 4-6: reserve, expiring, unknown

def lim(remaining: int, limit: int = 100, window: str = "day", metric: str = "requests", reset_in: float | None = 5 * 3600):
    return Limit(window, metric, limit, remaining, None if reset_in is None else NOW + reset_in, "header")


@pytest.mark.parametrize("app, remaining, kept", [
    (BACKGROUND, 31, True),   # 31 - 1 = 30 = 30 %: allowed (not below)
    (BACKGROUND, 30, False),  # 29 % after this request
    (BACKGROUND, 0, False),
    (NORMAL, 11, True),
    (NORMAL, 10, False),
    (NORMAL, 25, True),
    (INTERACTIVE, 0, True),   # no reserve for interactive
    (INTERACTIVE, 1, True),
])
def test_reserve_boundaries(app, remaining, kept):
    out = run(app, [FREE], state(limits={"a:up": [lim(remaining)]}))
    assert (ids(out) == ["free"]) is kept
    if not kept:
        assert out.removed[0].code == RESERVE and out.decision.kind == DEFER
        assert out.decision.retry_at == NOW + 5 * 3600
        assert "keeping a reserve for interactive use" in out.decision.message and "resets at" in out.decision.message


def test_reserve_tokens_metric_judged_on_what_remains():
    assert ids(run(BACKGROUND, [FREE], state(limits={"a:up": [lim(30, metric="tokens")]}))) == ["free"]
    assert ids(run(BACKGROUND, [FREE], state(limits={"a:up": [lim(29, metric="tokens")]}))) == []


@pytest.mark.parametrize("window, reset_in, waived", [
    ("day", 3600, True),        # exactly at the horizon
    ("day", 3599, True),
    ("day", 3601, False),
    ("day", 0, True),
    ("month", 86400, True),
    ("month", 86401, False),
    ("minute", 5, False),       # minute windows never count
    ("day", None, False),       # reset unknown: cannot be expiring
])
def test_expiring_waives_reserve(window, reset_in, waived):
    out = run(BACKGROUND, [FREE], state(limits={"a:up": [lim(1, window=window, reset_in=reset_in)]}))
    assert (ids(out) == ["free"]) is waived


def test_expiring_does_not_waive_yield():
    st = state(limits={"a:up": [lim(1, reset_in=60)]}, last_interactive={"a:up": NOW - 1})
    out = run(BACKGROUND, [FREE], st)
    assert codes(out) == {"free": YIELD}


def test_expiring_horizon_is_a_setting():
    st = state(limits={"a:up": [lim(1, reset_in=7000)]})
    assert ids(run(BACKGROUND, [FREE], st)) == []
    assert ids(run(BACKGROUND, [FREE], st, Settings(expiring_day_s=7200))) == ["free"]


def test_any_breached_unexpired_limit_defers_and_latest_reset_wins():
    st = state(limits={"a:up": [lim(1, reset_in=1800), lim(5, window="minute", reset_in=20)]})
    out = run(BACKGROUND, [FREE], st)  # day expiring (waived), minute is not
    assert codes(out) == {"free": RESERVE} and out.decision.retry_at == NOW + 20
    st = state(limits={"a:up": [lim(1, reset_in=9000), lim(5, window="minute", reset_in=20)]})
    assert run(BACKGROUND, [FREE], st).decision.retry_at == NOW + 9000


def test_unknown_remaining_means_no_reserve_but_yield_still_applies():
    assert ids(run(BACKGROUND, [FREE], state(limits={}))) == ["free"]
    out = run(BACKGROUND, [FREE], state(limits={}, last_interactive={"a:up": NOW - 1}))
    assert codes(out) == {"free": YIELD}


def test_reserve_without_reset_retries_after_a_window():
    out = run(BACKGROUND, [FREE], state(limits={"a:up": [lim(0, window="minute", reset_in=None)]}))
    assert out.decision.retry_at == NOW + 60


# ---------------------------------------------------------------- combinations and decisions

def test_rules_in_order_paid_gate_before_yield():
    st = state(last_interactive={"pp:up": NOW})
    out = run(BACKGROUND, [PAID], st)
    assert codes(out) == {"paid": PAID_NOT_ALLOWED}


def test_defer_wins_over_refuse_and_picks_soonest():
    st = state(last_interactive={"a:up": NOW - 100, "b:up": NOW - 10})
    out = run(BACKGROUND, [FREE, FREE_B, PAID], st)
    assert out.decision.kind == DEFER and out.decision.code == YIELD
    assert out.decision.retry_at == NOW + 20  # a frees first


def test_blocked_prior_plus_paid_cap_names_the_cap():
    prior = [Removed("free", "a:up", BLOCKED, DEFER, NOW + 3600, "blocked by a provider quota until x")]
    out = run(INTERACTIVE, [PAID], state(paid_spend_usd=Decimal("2")), prior=prior)
    assert out.decision.kind == DEFER and out.decision.code == PAID_CAP
    assert "daily paid cap" in out.decision.message and "free quota is used up" in out.decision.message
    assert out.decision.retry_at == NOW + 3600


def test_blocked_prior_with_other_refusals_defers_to_reset():
    prior = [Removed("free", "a:up", BLOCKED, DEFER, NOW + 3600, "blocked by a provider quota until x")]
    out = run(BACKGROUND, [PAID], state(), prior=prior)
    assert out.decision.kind == DEFER and out.decision.code == BLOCKED and out.decision.retry_at == NOW + 3600


def test_refuse_only_unknown_cost():
    out = run(INTERACTIVE, [PAID_NOPRICE])
    assert out.decision.kind == REFUSE and out.decision.code == PAID_UNKNOWN_COST and out.decision.retry_at is None
    assert out.decision.retry_after(NOW) is None


def test_combination_background_full_picture():
    st = state(
        last_interactive={"a:up": NOW - 30},
        limits={"b:up": [lim(10)], "l:up": [lim(90)]},
        paid_spend_usd=Decimal("0"),
    )
    out = run(BACKGROUND, [FREE, FREE_B, LOCAL, PAID], st)
    assert ids(out) == ["local"]
    assert codes(out) == {"free": YIELD, "freeb": RESERVE, "paid": PAID_NOT_ALLOWED}
    assert out.codes == sorted({YIELD, RESERVE, PAID_NOT_ALLOWED})


def test_interactive_is_never_deferred_by_rules_3_to_5():
    st = state(last_interactive={"a:up": NOW}, limits={"a:up": [lim(0)]})
    out = run(INTERACTIVE, [FREE], st)
    assert ids(out) == ["free"] and out.decision is None


def test_input_list_is_not_mutated():
    deps = [FREE, PAID]
    run(BACKGROUND, deps)
    assert deps == [FREE, PAID]


def test_fail_closed_drops_paid_only():
    assert [d["model_info"]["id"] for d in ration.fail_closed([FREE, PAID, METERED, LOCAL, NO_TIER])] == ["free", "local"]


# ---------------------------------------------------------------- limits from ledger rows

def row(window, metric, lim_, rem, reset_at=None, seen_at=NOW - 1):
    return {"window": window, "metric": metric, "limit": lim_, "remaining": rem, "reset_at": reset_at, "seen_at": seen_at}


def test_known_limits_prefers_fresh_headers_then_declared():
    info = {"provider": "cohere", "muse_quota": [{"window": "month", "metric": "requests", "limit": 1000}]}
    rows = [row("day", "requests", 100, 40, NOW + 10), row("minute", "tokens", 6000, 10, NOW - 1)]  # minute row expired
    out = ration.known_limits(info, rows, {"month": {"requests": 900}}, NOW)
    assert [(l.window, l.metric, l.remaining, l.source) for l in out] == [
        ("day", "requests", 40, "header"), ("month", "requests", 100, "declared")]
    assert out[1].reset_at == datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()


def test_known_limits_tightest_of_several_rows_and_garbage_rows():
    rows = [row("day", "requests", 100, 80, NOW + 10), row("day", "requests", 50, 10, NOW + 10),
            row("day", "requests", None, 5, NOW + 10), row("day", "requests", 0, 0, NOW + 10)]
    out = ration.known_limits({}, rows, {}, NOW)
    assert [(l.limit, l.remaining) for l in out] == [(50, 10)]


def test_known_limits_without_reset_uses_seen_at():
    assert ration.known_limits({}, [row("minute", "requests", 10, 1, None, NOW - 30)], {}, NOW)
    assert not ration.known_limits({}, [row("minute", "requests", 10, 1, None, NOW - 61)], {}, NOW)


def test_known_limits_unknown_is_empty():
    assert ration.known_limits({"provider": "gemini"}, [], {}, NOW) == []


@pytest.mark.parametrize("raw, expected", [
    ({"requests_per_month": 1000}, [("month", "requests", 1000)]),
    ([{"window": "day", "metric": "tokens", "limit": 5}], [("day", "tokens", 5)]),
    ({"requests_per_week": 3, "bananas_per_day": 2, "requests_per_day": -1, "tokens_per_day": True}, []),
    (None, []),
    ("junk", []),
])
def test_declared_quota_shapes(raw, expected):
    assert ration.declared_quota(raw) == expected


def test_declared_quota_resets_on_provider_day():
    info = {"provider": "gemini", "muse_quota": [{"window": "day", "metric": "requests", "limit": 10}]}
    out = ration.known_limits(info, [], {"day": {"requests": 3}}, NOW)
    assert out[0].remaining == 7
    assert out[0].reset_at == datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc).timestamp()  # 07:00Z is 23:00 PST: one hour left


# ---------------------------------------------------------------- settings

@pytest.mark.parametrize("raw, expected", [
    (None, Decimal("0.9375")), ("", Decimal("0.9375")), ("  ", Decimal("0.9375")),
    ("0", Decimal("0")), ("0.00", Decimal("0")), ("2.5", Decimal("2.5")), ("$3", Decimal("3")), ("0.001", Decimal("0.001")),
    ("abc", Decimal("0")), ("-1", Decimal("0")), ("nan", Decimal("0")), ("inf", Decimal("0")), ("1,00", Decimal("0")),
])
def test_paid_cap_parsing_fails_closed(raw, expected):
    warnings: list[str] = []
    assert ration.parse_paid_cap(raw, warnings) == expected
    if expected == 0 and raw not in ("0", "0.00"):
        assert warnings


def test_settings_from_env_defaults_and_overrides():
    s = Settings.from_env({})
    assert (s.paid_cap_usd, s.active_window_s, s.reserve_background, s.reserve_normal, s.expiring_day_s, s.expiring_month_s, s.local_yields) == (
        Decimal("0.9375"), 120.0, 0.30, 0.10, 3600.0, 86400.0, True)
    s = Settings.from_env({"MUSE_DAILY_PAID_CAP_USD": "0.5", "MUSE_ACTIVE_WINDOW_S": "30", "MUSE_RESERVE_BACKGROUND": "0.5",
                           "MUSE_RESERVE_NORMAL": "0", "MUSE_EXPIRING_DAY_S": "60", "MUSE_EXPIRING_MONTH_S": "600",
                           "MUSE_LOCAL_YIELD": "false", "MUSE_TZ": "Asia/Taipei"})
    assert (s.paid_cap_usd, s.active_window_s, s.reserve_background, s.reserve_normal, s.expiring_day_s, s.expiring_month_s, s.local_yields) == (
        Decimal("0.5"), 30.0, 0.5, 0.0, 60.0, 600.0, False)
    assert str(s.tz) == "Asia/Taipei"


@pytest.mark.parametrize("name, value", [
    ("MUSE_RESERVE_BACKGROUND", "1.5"), ("MUSE_RESERVE_BACKGROUND", "-0.1"), ("MUSE_RESERVE_NORMAL", "x"),
    ("MUSE_ACTIVE_WINDOW_S", "nan"), ("MUSE_EXPIRING_DAY_S", "999999"),
])
def test_bad_settings_fall_back_to_defaults_with_a_warning(name, value):
    s = Settings.from_env({name: value})
    assert s == Settings()  # compare=False on warnings
    assert s.warnings and name in s.warnings[0]


def test_paid_cap_enabled():
    assert ration.paid_cap_enabled({}) and ration.paid_cap_enabled({"MUSE_DAILY_PAID_CAP_USD": "0.01"})
    assert not ration.paid_cap_enabled({"MUSE_DAILY_PAID_CAP_USD": "0"})
    assert not ration.paid_cap_enabled({"MUSE_DAILY_PAID_CAP_USD": "free"})


# ---------------------------------------------------------------- local day and DST

def ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


@pytest.mark.parametrize("zone, at, start, hours", [
    ("Asia/Taipei", "2026-11-12T23:59:59+08:00", "2026-11-12T00:00:00+08:00", 24),
    ("Asia/Taipei", "2026-11-13T00:00:00+08:00", "2026-11-13T00:00:00+08:00", 24),
    ("America/New_York", "2026-03-08T12:00:00-04:00", "2026-03-08T00:00:00-05:00", 23),  # spring forward
    ("America/New_York", "2026-11-01T12:00:00-05:00", "2026-11-01T00:00:00-04:00", 25),  # fall back
    ("America/New_York", "2026-11-01T01:30:00-05:00", "2026-11-01T00:00:00-04:00", 25),  # the repeated hour, second pass
    ("Europe/London", "2026-03-29T00:30:00+00:00", "2026-03-29T00:00:00+00:00", 23),
    ("UTC", "2026-12-31T23:59:59+00:00", "2026-12-31T00:00:00+00:00", 24),
])
def test_local_day_bounds(zone, at, start, hours):
    lo, hi = ration.local_day_bounds(ts(at), ZoneInfo(zone))
    assert lo == ts(start)
    assert hi - lo == hours * 3600
    assert lo <= ts(at) < hi


def test_local_day_where_midnight_does_not_exist():
    # America/Santiago springs forward at 24:00 -> the local day 2026-09-06 starts at 01:00.
    zone = ZoneInfo("America/Santiago")
    at = datetime(2026, 9, 6, 12, 0, tzinfo=zone).timestamp()
    lo, hi = ration.local_day_bounds(at, zone)
    assert lo <= at < hi and 22 * 3600 <= hi - lo <= 24 * 3600


def test_resolve_tz_unknown_falls_back_to_system():
    assert ration.resolve_tz("Not/AZone") is not None
    assert str(ration.resolve_tz("Asia/Tokyo")) == "Asia/Tokyo"
