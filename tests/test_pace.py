"""muse.ration pacing (rule 4), the ordering hint, one-off holding and the hand-off options: pure tables."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from wardmuse import quota, ration
from wardmuse.ration import (
    DEFER, HANDOFF_FIRST, ONEOFF_HELD, PACE, PAID_NOT_ALLOWED, REFUSE, RESERVE, YIELD, AppPolicy, Decision, Limit,
    Removed, Settings, State,
)

UTC = timezone.utc
LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 11, 12, 7, 0, 0, tzinfo=UTC).timestamp()
S = Settings()
BACKGROUND = AppPolicy("batch", "background", allow_paid=False)
NORMAL = AppPolicy("script", "normal", allow_paid=True)
INTERACTIVE = AppPolicy("chat", "interactive", allow_paid=True)


def ts(*args) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


def day_limit(remaining: int, hours_to_reset: float, limit: int = 100, now: float = NOW, metric: str = "requests",
              scope: str = "") -> Limit:
    reset = now + hours_to_reset * 3600
    return Limit("day", metric, limit, remaining, reset, "header", reset - 86400, scope)


def dep(dep_id: str, upstream: str, *, tier: str = "free", order: int | None = 1, price=None, oneoff=None, kind="cloud") -> dict:
    info = {"id": dep_id, "provider": upstream.split(":")[0], "muse_upstream": upstream, "muse_tier": tier, "muse_kind": kind}
    if price:
        info["muse_price_in"], info["muse_price_out"] = price
    if oneoff is not None:
        info["muse_oneoff"] = oneoff
    params = {"model": dep_id}
    if order is not None:
        params["order"] = order
    return {"model_name": "chat", "litellm_params": params, "model_info": info}


A = dep("a", "a:up")
B = dep("b", "b:up")
C = dep("c", "c:up")


def ids(items) -> list[str]:
    return [d["model_info"]["id"] for d in items]


# ---------------------------------------------------------------- window bounds


@pytest.mark.parametrize("window, provider, now, start, end", [
    ("minute", "groq", ts(2026, 11, 12, 7, 0, 30), ts(2026, 11, 12, 7, 0), ts(2026, 11, 12, 7, 1)),
    ("day", "groq", ts(2026, 11, 12, 7), ts(2026, 11, 12), ts(2026, 11, 13)),
    ("day", "groq", ts(2026, 11, 12), ts(2026, 11, 12), ts(2026, 11, 13)),  # exactly at the reset: a new window
    ("day", "groq", ts(2026, 11, 12, 23, 59, 59), ts(2026, 11, 12), ts(2026, 11, 13)),
    ("month", "cohere", ts(2026, 2, 14), ts(2026, 2, 1), ts(2026, 3, 1)),  # 28 days
    ("month", "cohere", ts(2028, 2, 14), ts(2028, 2, 1), ts(2028, 3, 1)),  # 29 days
    ("month", "cohere", ts(2026, 4, 30, 23), ts(2026, 4, 1), ts(2026, 5, 1)),  # 30 days
    ("month", "cohere", ts(2026, 12, 31, 12), ts(2026, 12, 1), ts(2027, 1, 1)),  # 31 days, year end
    ("month", "cohere", ts(2026, 1, 1), ts(2026, 1, 1), ts(2026, 2, 1)),
])
def test_declared_window_bounds(window, provider, now, start, end):
    assert ration.window_bounds(window, provider, now) == (start, end)


def la_midnight(y, m, d) -> float:
    return datetime(y, m, d, tzinfo=LA).timestamp()


@pytest.mark.parametrize("local, day, hours", [
    (datetime(2026, 3, 8, 0, 30, tzinfo=LA), (2026, 3, 8), 23),   # spring forward: a 23-hour day
    (datetime(2026, 3, 8, 1, 59, tzinfo=LA), (2026, 3, 8), 23),
    (datetime(2026, 3, 8, 3, 1, tzinfo=LA), (2026, 3, 8), 23),
    (datetime(2026, 3, 8, 23, 59, tzinfo=LA), (2026, 3, 8), 23),
    (datetime(2026, 3, 7, 12, 0, tzinfo=LA), (2026, 3, 7), 24),
    (datetime(2026, 3, 9, 0, 0, tzinfo=LA), (2026, 3, 9), 24),
    (datetime(2026, 11, 1, 0, 30, tzinfo=LA), (2026, 11, 1), 25),  # fall back: a 25-hour day
    (datetime(2026, 11, 1, 1, 30, tzinfo=LA, fold=1), (2026, 11, 1), 25),
    (datetime(2026, 11, 1, 23, 0, tzinfo=LA), (2026, 11, 1), 25),
    (datetime(2026, 11, 12, 0, 0, tzinfo=LA), (2026, 11, 12), 24),
    (datetime(2026, 12, 31, 23, 0, tzinfo=LA), (2026, 12, 31), 24),
])
def test_gemini_day_follows_pacific_midnight_across_dst(local, day, hours):
    start, end = ration.window_bounds("day", "gemini", local.timestamp())
    assert start == la_midnight(*day)
    assert end - start == hours * 3600
    assert ration.previous_midnight_pacific(local.timestamp()) == start


@pytest.mark.parametrize("window, reset_in, expected", [
    ("day", 5 * 3600, (NOW + 5 * 3600 - 86400, NOW + 5 * 3600)),  # rolling: start = reset - one day
    ("day", 86400, (NOW, NOW + 86400)),
    ("day", 86401, None),  # further than one window: the header is not about this window
    ("day", -1, None),  # in the past
    ("minute", 30, (NOW + 30 - 60, NOW + 30)),
    ("minute", 180, None),  # e.g. Groq's 2m59s token reset on a "minute" row
    ("month", 10 * 86400, (NOW + 10 * 86400 - 30 * 86400, NOW + 10 * 86400)),
    ("week", 3600, None),  # unknown window
])
def test_observed_window_bounds(window, reset_in, expected):
    assert ration.window_bounds(window, "x", NOW, reset_at=NOW + reset_in, observed=True) == expected
    assert ration.window_bounds(window, "x", NOW, reset_at=None, observed=True) is None


def test_observed_month_on_a_calendar_boundary_is_that_month():
    now = ts(2026, 2, 10)
    assert ration.window_bounds("month", "x", now, reset_at=ts(2026, 3, 1), observed=True) == (ts(2026, 2, 1), ts(2026, 3, 1))


def test_known_limits_carry_window_start_and_scope():
    info = {"id": "d", "provider": "gemini", "muse_quota": [{"window": "day", "metric": "requests", "limit": 250}]}
    now = datetime(2026, 11, 12, 9, 0, tzinfo=LA).timestamp()
    (lim,) = ration.known_limits(info, [], {"day": {"requests": 50}}, now)
    assert lim.window_start == la_midnight(2026, 11, 12) and lim.reset_at == la_midnight(2026, 11, 13)
    assert lim.scope == "provider:gemini" and lim.remaining == 200
    rows = [{"scope": "groq:m", "window": "day", "metric": "requests", "limit": 1000, "remaining": 10, "reset_at": NOW + 600, "seen_at": NOW}]
    (lim,) = ration.known_limits({"provider": "groq"}, rows, {}, NOW)
    assert lim.window_start == NOW + 600 - 86400 and lim.scope == "groq:m" and lim.source == "header"


# ---------------------------------------------------------------- the line


@pytest.mark.parametrize("remaining, hours_to_reset, expected, surplus", [
    (100, 24, 1.0, 0.0),  # start of the window: full is on the line
    (50, 12, 0.5, 0.0),
    (60, 12, 0.5, 0.10),  # under the line: surplus
    (40, 12, 0.5, -0.10),  # over the line: deficit
    (0, 0, 0.0, 0.0),  # at the reset
    (10, 1, 1 / 24, 0.1 - 1 / 24),
    (100, 0.001, 0.001 / 24, 1 - 0.001 / 24),
])
def test_pace_position_line(remaining, hours_to_reset, expected, surplus):
    pos = ration.pace_position(day_limit(remaining, hours_to_reset), NOW)
    assert pos.applies and pos.reason == ""
    assert pos.expected_remaining_fraction == pytest.approx(expected)
    assert pos.surplus == pytest.approx(surplus)
    assert pos.remaining_fraction == pytest.approx(remaining / 100)
    assert pos.projected_leftover is None


def test_pace_position_clamps_outside_the_window():
    lim = Limit("day", "requests", 100, 30, NOW + 3600, "header", NOW + 600)  # start in the future
    assert ration.pace_position(lim, NOW).expected_remaining_fraction == 1.0
    lim = Limit("day", "requests", 100, 30, NOW - 1, "header", NOW - 86401)  # reset passed
    assert ration.pace_position(lim, NOW).expected_remaining_fraction == 0.0
    lim = Limit("day", "requests", 100, 130, NOW + 3600, "header", NOW - 3600)  # more than the limit
    assert ration.pace_position(lim, NOW).remaining_fraction == 1.0


@pytest.mark.parametrize("lim, words", [
    (Limit("minute", "tokens", 6000, 10, NOW + 30, "header", NOW - 30), "rate limits"),
    (Limit("day", "requests", 100, 10, NOW + 30, "header", None), "length is unknown"),
    (Limit("day", "requests", 100, 10, None, "header", NOW - 30), "length is unknown"),
    (Limit("month", "requests", 100, 10, NOW, "header", NOW), "length is unknown"),
])
def test_pacing_does_not_apply(lim, words):
    pos = ration.pace_position(lim, NOW)
    assert not pos.applies and words in pos.reason and pos.surplus is None
    assert not ration.pace_holds(lim, S, NOW)


@pytest.mark.parametrize("units, calls, window, min_calls, rate", [
    (36, 36, 3600, 3, 0.01),
    (36, 2, 3600, 3, None),  # too few calls to be a rate
    (0, 0, 3600, 3, None),
    (7200, 3, 3600, 3, 2.0),  # tokens
    (5, 5, 0, 3, None),
    (None, 5, 3600, 3, None),
])
def test_burn_rate(units, calls, window, min_calls, rate):
    assert ration.burn_rate(units, calls, window, min_calls) == (pytest.approx(rate) if rate is not None else None)


@pytest.mark.parametrize("remaining, hours, rate, leftover", [
    (60, 12, 0.0, 0.60),
    (60, 12, 50 / 43200, 0.10),  # 50 more requests before the reset
    (60, 12, 1.0, 0.0),  # clamped
    (5, 1, 1 / 3600, 0.04),
])
def test_projected_leftover(remaining, hours, rate, leftover):
    assert ration.pace_position(day_limit(remaining, hours), NOW, rate).projected_leftover == pytest.approx(leftover)


@pytest.mark.parametrize("margin, remaining, holds", [
    (0.0, 50, False),  # on the line is allowed
    (0.0, 49, True),
    (0.0, 51, False),
    (0.1, 59, True),
    (0.1, 60, False),
    (-0.2, 31, False),
    (-1.0, 0, False),  # -1 turns pacing off
])
def test_pace_holds_against_margin(margin, remaining, holds):
    settings = Settings(pace_margin=margin)
    assert ration.pace_holds(day_limit(remaining, 12), settings, NOW) is holds


@pytest.mark.parametrize("remaining, hours, margin, release_in_h", [
    (40, 12, 0.0, 2.4),  # start + 0.6 * 24 h = now - 12 h + 14.4 h
    (40, 12, 0.1, 4.8),
    (10, 3, 0.0, 0.6),  # start (now - 21 h) + 0.9 * 24 h
    (0, 3, 0.0, 2.0),  # the line would catch up at the reset; the expiring horizon opens first
    (0, 24, 0.0, 23.0),  # empty: only the expiring horizon releases it
])
def test_pace_release_at(remaining, hours, margin, release_in_h):
    lim = day_limit(remaining, hours)
    assert ration.pace_release_at(lim, margin, S, NOW) - NOW == pytest.approx(release_in_h * 3600)


# ---------------------------------------------------------------- pacing inside ration()


def run(app, deps, limits, settings=S, now=NOW, **kw):
    st = State(limits=limits, day_end=now + 17 * 3600, **kw)
    return ration.ration(app, deps, st, settings, now)


def test_background_is_held_over_the_line_with_a_release_time():
    out = run(BACKGROUND, [A, B], {"a:up": [day_limit(40, 12)]})
    assert ids(out.kept) == ["b"]
    (removed,) = out.removed
    assert removed.code == PACE and removed.kind == DEFER
    assert removed.retry_at - NOW == pytest.approx(2.4 * 3600)
    assert "ahead of schedule" in removed.detail and "40/100" in removed.detail


@pytest.mark.parametrize("app, limits, kept, code", [
    (BACKGROUND, [day_limit(60, 12)], ["a"], None),  # under the line
    (BACKGROUND, [day_limit(20, 12)], [], RESERVE),  # reserve wins over pacing (latest reset)
    (BACKGROUND, [day_limit(10, 0.5)], ["a"], None),  # expiring: both waived
    (BACKGROUND, [day_limit(40, 12), day_limit(990, 12, limit=1000, metric="tokens")], [], PACE),
    (BACKGROUND, [Limit("day", "requests", 100, 40, NOW + 12 * 3600, "header")], ["a"], None),  # window unknown: reserve only
    (BACKGROUND, [Limit("minute", "requests", 100, 40, NOW + 30, "header", NOW - 30)], ["a"], None),
    (NORMAL, [day_limit(40, 12)], ["a"], None),  # normal: reserve only
    (NORMAL, [day_limit(5, 12)], [], RESERVE),
    (INTERACTIVE, [day_limit(1, 12)], ["a"], None),  # never held
])
def test_pacing_with_the_other_rules(app, limits, kept, code):
    out = run(app, [A], {"a:up": limits})
    assert ids(out.kept) == kept
    assert [r.code for r in out.removed] == ([code] if code else [])


def test_yield_comes_before_pacing_and_pacing_never_overrides_it():
    out = run(BACKGROUND, [A], {"a:up": [day_limit(90, 12)]}, last_interactive={"a:up": NOW - 5})
    assert out.removed[0].code == YIELD and out.decision.code == YIELD


def test_paid_gate_comes_before_pacing():
    paid = dep("p", "p:up", tier="paid", price=(1.0, 2.0))
    out = run(BACKGROUND, [paid], {"p:up": [day_limit(40, 12)]})
    assert out.removed[0].code == PAID_NOT_ALLOWED


def test_margin_off_never_holds():
    assert ids(run(BACKGROUND, [A], {"a:up": [day_limit(31, 12)]}, settings=Settings(pace_margin=-1)).kept) == ["a"]


def test_pacing_decision_defers_to_the_soonest_release():
    out = run(BACKGROUND, [A, B], {"a:up": [day_limit(40, 12)], "b:up": [day_limit(45, 12)]})
    assert out.decision.kind == DEFER and out.decision.code == PACE
    assert out.decision.retry_at - NOW == pytest.approx(1.2 * 3600)
    assert out.decision.message.startswith("[muse] pacing:")


def test_settings_from_env():
    s = Settings.from_env({"MUSE_PACE_MARGIN": "0.05", "MUSE_PACE_PREFER_GAP": "0.3", "MUSE_PACE_BURN_WINDOW_S": "600",
                           "MUSE_PACE_BURN_MIN_CALLS": "5"})
    assert (s.pace_margin, s.prefer_gap, s.burn_window_s, s.burn_min_calls) == (0.05, 0.3, 600, 5)
    bad = Settings.from_env({"MUSE_PACE_MARGIN": "2", "MUSE_PACE_PREFER_GAP": "-1", "MUSE_PACE_BURN_WINDOW_S": "x"})
    assert (bad.pace_margin, bad.prefer_gap, bad.burn_window_s) == (0.0, 0.10, 3600.0) and len(bad.warnings) == 3


# ---------------------------------------------------------------- the ordering hint


def pref(deps, limits, settings=S, burn=None, **kw):
    st = State(limits=limits, burn=burn or {})
    return ration.prefer(deps, st, settings, NOW, **kw)


@pytest.mark.parametrize("a, b, kept, decisive", [
    (day_limit(80, 12), day_limit(20, 12), ["a"], True),  # surplus +0.3 vs -0.3
    (day_limit(20, 12), day_limit(80, 12), ["b"], True),  # flips
    (day_limit(55, 12), day_limit(50, 12), ["a", "b"], False),  # within the gap: load-balance
    (day_limit(70, 12), None, ["a"], True),  # known surplus beats unknown
    (day_limit(20, 12), None, ["b"], True),  # unknown beats known deficit
    (day_limit(52, 12), None, ["a", "b"], False),  # a small surplus is not decisive over unknown
    (day_limit(48, 12), None, ["a", "b"], False),
    (None, None, ["a", "b"], False),
    (day_limit(30, 1), day_limit(90, 23), ["a"], True),  # a: +0.26 with 1 h to go; b: -0.06 with 23 h
])
def test_prefer_table(a, b, kept, decisive):
    limits = {"a:up": [a] if a else [], "b:up": [b] if b else []}
    out, reason = pref([A, B], limits)
    assert ids(out) == kept
    assert (reason is not None) is decisive
    if decisive:
        assert reason.startswith("pace:prefer")


def test_prefer_uses_projected_leftover_when_every_rate_is_known():
    limits = {"a:up": [day_limit(60, 12, scope="a:up")], "b:up": [day_limit(40, 12, scope="b:up")]}
    # b has too few calls for a rate, so both are compared by surplus: a +0.10, b -0.10.
    out, reason = pref([A, B], limits, burn={"a:up": {"calls": 15, "tokens": 0}, "b:up": {"calls": 1, "tokens": 0}})
    assert ids(out) == ["a"] and reason.endswith("surplus -0.10 (gap > 0.10)")
    # Both rates known (15/h and 3/h over the 1 h burn window): a runs dry (0.00), b keeps 0.04.
    burn = {"a:up": {"calls": 15, "tokens": 0}, "b:up": {"calls": 3, "tokens": 0}}
    assert pref([A, B], limits, burn=burn)[1] is None  # 0.04 apart: within the gap
    out, reason = pref([A, B], limits, burn=burn, settings=Settings(prefer_gap=0.02))
    assert ids(out) == ["b"] and "leftover at reset 0.04" in reason


def test_prefer_only_touches_the_level_litellm_picks():
    a2 = dep("a2", "a:up", order=2)
    c2 = dep("c2", "c:up", order=2)
    limits = {"a:up": [day_limit(20, 12)], "b:up": [day_limit(80, 12)], "c:up": [day_limit(100, 12)]}
    out, _ = pref([A, B, a2, c2], limits)
    assert ids(out) == ["b", "a2", "c2"]
    out, reason = pref([A, B, a2, c2], limits, target_order=2)  # LiteLLM's order fallback
    assert ids(out) == ["a", "b", "c2"] and reason is not None
    unordered = [dep("x", "a:up", order=None), dep("y", "b:up", order=None)]
    assert ids(pref(unordered, limits)[0]) == ["y"]
    mixed = [A, dep("y", "b:up", order=None)]  # LiteLLM drops an unordered one when others have orders
    assert ids(pref(mixed, limits)[0]) == ["a", "y"]


def test_a_retry_goes_to_an_upstream_that_has_not_failed():
    limits = {"a:up": [day_limit(80, 12)], "b:up": [day_limit(20, 12)]}
    out, reason = pref([A, B], limits, tried=["a"])  # the preferred one failed: the retry goes to b
    assert ids(out) == ["b"] and reason.startswith("retry: a:up already failed")
    out, reason = pref([A, B], limits, tried=["b"])
    assert ids(out) == ["a"] and reason.startswith("retry:")
    assert pref([A, B], limits, tried=["a", "b"]) == ([A, B], None)  # all failed: LiteLLM decides
    a2 = dep("a2", "a:up", order=2)
    assert ids(pref([A, B, a2], limits, tried=["a2"])[0]) == ["a", "a2"]  # another level's attempt: a normal choice


def test_prefer_never_narrows_with_one_upstream():
    limits = {"a:up": [day_limit(80, 12)], "b:up": [day_limit(20, 12)]}
    assert pref([A, dep("a-other", "a:up")], limits)[1] is None
    assert pref([], limits) == ([], None)
    out, _ = pref([A, B, C], {**limits, "c:up": [day_limit(78, 12)]})
    assert ids(out) == ["a", "c"]  # near-equals stay load-balanced


def test_prefer_gap_setting():
    limits = {"a:up": [day_limit(80, 12)], "b:up": [day_limit(20, 12)]}
    assert pref([A, B], limits, settings=Settings(prefer_gap=0.7))[1] is None
    assert ids(pref([A, B], limits, settings=Settings(prefer_gap=0.0))[0]) == ["a"]
    assert ids(pref([A, B], {"a:up": [day_limit(51, 12)], "b:up": [day_limit(50, 12)]}, settings=Settings(prefer_gap=0.0))[0]) == ["a"]


def test_pace_view_shape():
    rows = ration.pace_view({"a:up": [day_limit(40, 12)], "g:up": [],
                             "m:up": [Limit("minute", "tokens", 6000, 5900, NOW + 7, "header", NOW - 53)]}, NOW)
    by = {r["upstream"]: r for r in rows}
    assert set(by["a:up"]) == {"upstream", "window", "metric", "limit", "remaining", "reset_at", "window_start",
                               "expected_remaining", "surplus", "projected_leftover", "source", "paced", "reason"}
    assert by["a:up"]["expected_remaining"] == pytest.approx(50) and by["a:up"]["surplus"] == pytest.approx(-0.1)
    assert by["a:up"]["source"] == "observed" and by["a:up"]["paced"] is True
    assert by["g:up"]["source"] == "unknown" and by["g:up"]["limit"] is None
    assert by["m:up"]["paced"] is False and "rate limits" in by["m:up"]["reason"]
    declared = Limit("month", "requests", 1000, 900, ts(2026, 12, 1), "declared", ts(2026, 11, 1))
    (row,) = ration.pace_view({"c:up": [declared]}, NOW)
    assert row["source"] == "declared"
    assert row["surplus"] == pytest.approx(0.9 - (1 - (NOW - ts(2026, 11, 1)) / (30 * 86400)))


# ---------------------------------------------------------------- one-off credits (rule 7)


TRIAL_ONEOFF = dep("t", "t:up", tier="trial", oneoff=True)
TRIAL_RENEWS = dep("r", "r:up", tier="trial")


@pytest.mark.parametrize("info, expected", [
    ({"muse_tier": "trial", "muse_oneoff": True}, True),
    ({"muse_tier": "trial"}, False),  # the renderer does not emit the field yet: not held
    ({"muse_tier": "trial", "muse_oneoff": "true"}, False),  # only a real boolean counts
    ({"muse_tier": "free", "muse_oneoff": True}, False),
    ({"muse_tier": "paid", "muse_oneoff": True}, False),
])
def test_is_oneoff(info, expected):
    assert ration.is_oneoff(info) is expected


@pytest.mark.parametrize("app", [INTERACTIVE, NORMAL, BACKGROUND])
def test_oneoff_is_held_for_every_app(app):
    out = ration.ration(app, [TRIAL_ONEOFF, TRIAL_RENEWS], State(), S, NOW)
    assert ids(out.kept) == ["r"] and out.removed[0].code == ONEOFF_HELD
    out = ration.ration(app, [TRIAL_ONEOFF], State(), S, NOW)
    assert out.decision.kind == REFUSE and out.decision.code == ONEOFF_HELD and out.decision.retry_at is None
    assert "approve" in out.decision.message
    assert ids(ration.ration(app, [TRIAL_ONEOFF], State(), S, NOW, allow_oneoff=True).kept) == ["t"]


def test_oneoff_refusal_beats_a_deferral_and_names_the_wait():
    out = ration.ration(BACKGROUND, [A, TRIAL_ONEOFF], State(last_interactive={"a:up": NOW - 10}), S, NOW)
    assert out.decision.code == ONEOFF_HELD and out.decision.kind == REFUSE
    assert out.decision.retry_at - NOW == pytest.approx(110)


def test_fail_closed_drops_oneoff():
    assert ids(ration.fail_closed([A, TRIAL_ONEOFF, TRIAL_RENEWS])) == ["a", "r"]


# ---------------------------------------------------------------- hand-off (rule 6)


PAID = dep("p", "p:up", tier="paid", price=(20.0, 40.0), order=21)


def test_handoff_hold_keeps_paid_back_only_after_the_gate():
    out = ration.ration(INTERACTIVE, [PAID], State(), S, NOW, handoff_hold=True)
    assert out.removed[0].code == HANDOFF_FIRST and out.decision.code == HANDOFF_FIRST
    assert out.decision.kind == REFUSE and "subscription app" in out.decision.message
    stingy = AppPolicy("stingy", "interactive", allow_paid=False)
    out = ration.ration(stingy, [PAID], State(), S, NOW, handoff_hold=True)
    assert out.removed[0].code == PAID_NOT_ALLOWED  # pay is not an option for this app
    capped = State(paid_spend_usd=Decimal("5"))
    assert ration.ration(INTERACTIVE, [PAID], capped, S, NOW, handoff_hold=True).removed[0].code == "paid-cap"
    assert ids(ration.ration(INTERACTIVE, [A, PAID], State(), S, NOW, handoff_hold=True).kept) == ["a"]
    assert ids(ration.ration(INTERACTIVE, [PAID], State(), S, NOW).kept) == ["p"]  # approved (or not a frontier task)


def rem(code, kind, retry=None, dep_id="x"):
    return Removed(dep_id, "u", code, kind, retry, "detail")


@pytest.mark.parametrize("removed, handoff, estimates, expected", [
    ([rem("blocked", DEFER, NOW + 600)], False, {}, (NOW + 600, False, None)),  # wait only
    ([rem("blocked", DEFER, NOW + 600), rem(HANDOFF_FIRST, REFUSE, dep_id="p")], True, {"p": 0.041},
     (NOW + 600, True, 0.041)),  # wait + handoff + pay
    ([rem("blocked", DEFER, NOW + 600), rem(PAID_NOT_ALLOWED, REFUSE)], True, {}, (NOW + 600, True, None)),  # wait + handoff
    ([rem(HANDOFF_FIRST, REFUSE, dep_id="p")], True, {"p": 0.5}, (None, True, 0.5)),  # handoff + pay
    ([rem(PAID_NOT_ALLOWED, REFUSE)], True, {}, (None, True, None)),  # handoff only
    ([rem(HANDOFF_FIRST, REFUSE, dep_id="p")], False, {"p": 0.5}, (None, False, 0.5)),  # pay only
    ([rem(PAID_NOT_ALLOWED, REFUSE)], False, {}, (None, False, None)),  # nothing
    ([rem(HANDOFF_FIRST, REFUSE, dep_id="p"), rem(HANDOFF_FIRST, REFUSE, dep_id="q")], True, {"p": 0.5, "q": 0.2},
     (None, True, 0.2)),  # the cheapest
    ([rem(HANDOFF_FIRST, REFUSE, dep_id="p")], True, {}, (None, True, None)),  # no price: no pay option
    ([rem("yield", DEFER, NOW + 30), rem("reserve", DEFER, NOW + 900)], False, {}, (NOW + 30, False, None)),
])
def test_available_options(removed, handoff, estimates, expected):
    decision = Decision(REFUSE, "x", "[muse] no", None)
    assert ration.available_options(decision, removed, handoff=handoff, pay_estimates=estimates) == expected


def test_app_cap_waits_until_its_reset():
    decision = Decision(REFUSE, "app-requests", "[muse] cap", NOW + 3600)
    assert ration.available_options(decision, [], handoff=False, pay_estimates={})[0] == NOW + 3600


@pytest.mark.parametrize("args, text", [
    ((ts(2026, 11, 13), True, 0.041), "options=wait:2026-11-13T00:00:00Z,handoff,pay:0.041000"),
    ((ts(2026, 11, 13), False, None), "options=wait:2026-11-13T00:00:00Z"),
    ((None, True, None), "options=handoff"),
    ((None, False, 1.5), "options=pay:1.500000"),
    ((None, False, None), "options=none"),
])
def test_options_trailer_and_parse(args, text):
    assert ration.options_trailer(*args) == text
    parsed = ration.parse_options(f"[muse] something | {text}")
    wait, handoff, pay = args
    assert parsed.get("wait") == quota.iso(wait) if wait else "wait" not in parsed
    assert parsed.get("handoff", False) is handoff
    assert parsed.get("pay") == (pytest.approx(pay) if pay is not None else None)


def test_with_options_appends_once():
    d = Decision(DEFER, "blocked", "[muse] blocked", NOW + 60)
    once = ration.with_options(d, [rem("blocked", DEFER, NOW + 60)], handoff=True, pay_estimates={})
    assert once.message == f"[muse] blocked | options=wait:{quota.iso(NOW + 60)},handoff"
    assert ration.with_options(once, [], handoff=False, pay_estimates={}) == once
    assert ration.parse_options("[muse] no trailer") == {}


def test_handoff_refusal_names_the_soonest_wait():
    out = ration.ration(INTERACTIVE, [PAID], State(), S, NOW, handoff_hold=True,
                        prior=[Removed("s", "s:up", "blocked", DEFER, NOW + 7200, "blocked")])
    assert out.decision.code == HANDOFF_FIRST and out.decision.retry_at == NOW + 7200


@pytest.mark.parametrize("ranks, values", [
    ([("surplus", 0.3, None), ("unknown", None, None), ("deficit", -0.2, None)], [0.3, 0.0, -0.2]),
    ([("surplus", 0.3, 0.1), ("deficit", -0.2, 0.05)], [0.1, 0.05]),  # every rate known: leftover
    ([("surplus", 0.3, 0.0), ("deficit", -0.2, 0.0)], [0.0, -0.2]),  # both run dry: behind schedule ranks lower
    ([("surplus", 0.3, 0.1), ("deficit", -0.2, None)], [0.3, -0.2]),  # a rate missing: surplus for all
    ([("unknown", None, None), ("unknown", None, None)], [0.0, 0.0]),
])
def test_rank_values(ranks, values):
    built = [ration.Rank(f"u{i}", k, s, l, "") for i, (k, s, l) in enumerate(ranks)]
    assert ration.rank_values(built) == pytest.approx(values)
