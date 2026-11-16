"""muse.hook rationing glue in isolation: app threading, failure policy, headers, holds."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("litellm")

import litellm  # noqa: E402
from litellm.integrations.custom_guardrail import is_guardrail_intervention  # noqa: E402

from muse import apps  # noqa: E402
from muse.hook import MuseAppCapError, MuseDirectRationError, MuseHook, MuseRationError  # noqa: E402
from muse.ledger import Ledger  # noqa: E402

NOW = 1_794_466_800.0  # 2026-11-12T07:00:00Z
ENV = {"MUSE_TZ": "UTC"}


def run(coro):
    return asyncio.run(coro)


def dep(dep_id, provider, tier="free", kind="cloud", price=None, upstream=None):
    info = {"id": dep_id, "provider": provider, "muse_upstream": upstream or f"{provider}:{dep_id.split('__')[0]}",
            "muse_kind": kind, "muse_trains": "no" if kind == "local" else "yes", "muse_human_review": "no" if kind == "local" else "yes",
            "muse_tier": tier}
    if price:
        info["muse_price_in"], info["muse_price_out"] = price
    return {"model_name": dep_id.split("__")[-1], "litellm_params": {"model": dep_id}, "model_info": info}


A = dep("a__chat", "freea")
B = dep("b__chat", "freeb")
L = dep("l__chat", "local_openai", tier="local", kind="local")
PP = dep("pp__chat", "paidp", tier="paid", price=(20.0, 40.0))
PU = dep("pu__chat", "paidu", tier="paid")


def policy(name, priority, **kw):
    return apps.App(name=name, priority=priority, allow_paid=kw.pop("allow_paid", priority != "background"), **kw).policy()


OWNER = apps.OWNER.policy()
BATCH = policy("batch", "background")
SCRIPT = policy("script", "normal")


def req(app_policy=None, call_id="call-1", privacy=None, **extra):
    headers = {"x-muse-privacy": privacy} if privacy else {}
    md = {"headers": headers}
    if app_policy is not None:
        md["user_api_key_metadata"] = app_policy
    return {"metadata": md, "litellm_call_id": call_id, **extra}


@pytest.fixture()
def db(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite", clock=lambda: NOW)


@pytest.fixture()
def h(db):
    return MuseHook(ledger=db, clock=lambda: NOW, environ=dict(ENV))


def filt(h, deployments, data, model="chat"):
    return run(h.async_filter_deployments(model, deployments, None, request_kwargs=data))


def interactive_used(db, upstream, at=NOW - 10):
    db.record_call(provider="x", deployment_id="x", upstream=upstream, ok=True, app="owner", priority="interactive", now=at)


def test_no_auth_metadata_is_anonymous_without_paid(h):
    assert filt(h, [A, PP], req(None)) == [A]


def test_owner_gets_free_and_priced_paid_not_unpriced(h):
    assert filt(h, [A, PP, PU], req(OWNER)) == [A, PP]


def test_background_yields_and_headers_explain(h, db):
    interactive_used(db, "freea:a")
    data = req(BATCH, call_id="c-yield")
    with pytest.raises(MuseRationError) as err:
        filt(h, [A], data)
    assert isinstance(err.value, litellm.RateLimitError) and not is_guardrail_intervention(err.value)
    assert "yielding to the active app; retry in 110 s" in str(err.value)
    assert not getattr(err.value, "litellm_response_headers", None)  # the router must not sleep on it
    headers = run(h.async_post_call_response_headers_hook(data, None, None))
    assert headers["retry-after"] == "110" and headers["x-muse-ration"] == "defer:yield" and headers["x-muse-app"] == "batch"
    assert db.ration_counts(now=NOW) == {("batch", "defer", "yield"): 1}
    # Another upstream is fine, and the success response lists what was skipped.
    data = req(BATCH, call_id="c-partial")
    assert filt(h, [A, B], data) == [B]
    out = run(h.async_post_call_response_headers_hook(data, None, object(), litellm_call_info={"model_info": B["model_info"]}))
    assert out["x-muse-ration"] == "yield" and out["x-muse-app"] == "batch" and out["x-muse-cost-usd"] == "0.000000"


def test_fallback_groups_accumulate_and_soonest_retry_wins(h, db):
    interactive_used(db, "freea:a", at=NOW - 100)
    interactive_used(db, "freeb:b", at=NOW - 10)
    data = req(BATCH, call_id="c-fb")
    with pytest.raises(MuseRationError):
        filt(h, [B], data, model="chat")
    with pytest.raises(MuseRationError):
        filt(h, [A], data, model="other")
    headers = run(h.async_post_call_response_headers_hook(data, None, None))
    assert headers["retry-after"] == "20"


def test_app_cap_is_a_guardrail_429(h, db):
    capped = policy("capped", "normal", daily_requests=1)
    db.record_call(provider="freea", deployment_id="a", upstream="freea:a", ok=True, app="capped", priority="normal", now=NOW - 5)
    data = req(capped, call_id="c-cap")
    with pytest.raises(MuseAppCapError) as err:
        filt(h, [A, B], data)
    assert is_guardrail_intervention(err.value) and err.value.status_code == 429
    headers = run(h.async_post_call_response_headers_hook(data, None, None))
    assert headers["x-muse-ration"] == "refuse:app-requests" and int(headers["retry-after"]) == 17 * 3600  # UTC midnight


def test_ration_crash_degrades_but_keeps_paid_out(h, monkeypatch):
    from muse import ration

    monkeypatch.setattr(ration, "ration", lambda *a, **k: 1 / 0)
    assert filt(h, [A, PP, L], req(OWNER)) == [A, L]
    assert h.errors["ration"] == 1
    with pytest.raises(MuseRationError) as err:
        filt(h, [PP], req(OWNER, call_id="c2"))
    assert "paid check failed" in str(err.value)


def test_unreadable_spend_closes_the_paid_gate(h, monkeypatch):
    monkeypatch.setattr(h.ledger, "paid_spend", lambda **k: 1 / 0)
    assert filt(h, [A, PP], req(OWNER)) == [A]
    assert h.errors["paid_spend"] == 1


def test_other_ledger_errors_degrade_to_no_rationing(h, db, monkeypatch):
    interactive_used(db, "freea:a")
    monkeypatch.setattr(h.ledger, "last_interactive_success", lambda *a: 1 / 0)
    assert filt(h, [A, PP], req(BATCH)) == [A]  # yield skipped, paid still refused (not allowed)
    assert h.errors["ration"] == 1


def test_paid_cap_reached(h, db):
    db.record_call(provider="paidp", deployment_id="pp", upstream="paidp:pp", ok=True, cost_usd=1.0, paid=True, now=NOW - 5)
    with pytest.raises(MuseRationError) as err:
        filt(h, [PP], req(OWNER, call_id="c-cap"))
    assert "daily paid cap of $0.94 reached" in str(err.value)  # the default: NT$30 at 32, US$0.9375
    h2 = MuseHook(ledger=db, clock=lambda: NOW, environ={**ENV, "MUSE_DAILY_PAID_CAP_USD": "5"})
    assert filt(h2, [PP], req(OWNER, call_id="c-cap2")) == [PP]


def test_reserve_from_observed_headers(h, db):
    from muse.quota import ObservedLimit

    db.record_limits("freea:a", [ObservedLimit("day", "requests", 100, 20, NOW + 5 * 3600)], now=NOW)
    assert filt(h, [A, B], req(BATCH)) == [B]
    assert filt(h, [A, B], req(SCRIPT)) == [A, B]
    assert filt(h, [A, B], req(OWNER)) == [A, B]


def test_reserve_from_declared_quota(h, db):
    a = dep("a__chat", "freea")
    a["model_info"]["muse_quota"] = [{"window": "day", "metric": "requests", "limit": 10}]
    for i in range(8):
        db.record_call(provider="freea", deployment_id="a", upstream="freea:a", ok=True, now=NOW - 100 - i)
    assert filt(h, [a, B], req(BATCH)) == [B]  # 2 of 10 left
    # The reserve lets a normal app use A (2 - 1 = 1 = 10 %), but A is far behind its
    # pacing line at 07:00 UTC (0.20 left vs 0.71 expected), so the ordering hint
    # prefers B, whose remaining is unknown (ration.prefer). With the hint off, both stay.
    assert filt(h, [a, B], req(SCRIPT)) == [B]
    h.environ["MUSE_PACE_PREFER_GAP"] = "2"
    assert filt(h, [a, B], req(SCRIPT)) == [a, B]


def test_app_default_privacy(h):
    private_app = policy("diary", "interactive", default_privacy="local")
    data = req(private_app)
    out = run(h.async_pre_call_hook(None, None, {**data, "messages": []}, "completion"))
    assert out["metadata"]["muse_privacy"] == "local" and out["metadata"]["muse_app"] == "diary"
    assert filt(h, [A, L], req(private_app)) == [L]
    assert filt(h, [A, L], req(private_app, privacy="standard")) == [A, L]  # the header still wins, as for the env default


# ---------------------------------------------------------------- deployment hook

def test_deployment_hook_skips_what_the_filter_allowed(h, db):
    data = req(BATCH, call_id="c-ok")
    assert filt(h, [A], data) == [A]
    interactive_used(db, "freea:a")  # changed since: the filter's answer stands for this request
    assert run(h.async_pre_call_deployment_hook({**data, "model_info": A["model_info"]}, None)) is None


def test_deployment_hook_rations_direct_ids(h, db):
    interactive_used(db, "freea:a")
    with pytest.raises(MuseDirectRationError) as err:
        run(h.async_pre_call_deployment_hook({**req(BATCH, call_id="c-direct"), "model_info": A["model_info"]}, None))
    assert is_guardrail_intervention(err.value) and err.value.status_code == 429
    assert run(h.async_pre_call_deployment_hook({**req(OWNER, call_id="c-o"), "model_info": A["model_info"]}, None)) is None


def test_deployment_hook_holds_paid_and_releases_on_success(h, db):
    data = {**req(OWNER, call_id="c-paid"), "model_info": PP["model_info"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
    assert filt(h, [PP], data) == [PP]
    assert run(h.async_pre_call_deployment_hook(data, None)) is None
    held = db.paid_spend(now=NOW)["held_usd"]
    assert held == pytest.approx((1 * 20 + 10 * 40) / 1e6)  # "hi": 2 chars // 3 + 1 = 1 token in, max_tokens out
    kwargs = {"litellm_call_id": "c-paid",
              "litellm_params": {"metadata": {"model_info": PP["model_info"], "user_api_key_metadata": OWNER, "headers": {}}},
              "standard_logging_object": {"prompt_tokens": 11, "completion_tokens": 7, "response_cost": 0.0}}
    run(h.async_log_success_event(kwargs, SimpleNamespace(_hidden_params={}, usage=None), None, None))
    row = db.recent(1)[0]
    assert (row["app"], row["priority"], row["paid"]) == ("owner", "interactive", 1)
    assert row["cost_usd"] == pytest.approx((11 * 20 + 7 * 40) / 1e6)
    assert db.paid_spend(now=NOW)["held_usd"] == 0


def test_deployment_hook_refuses_when_holds_fill_the_cap(db):
    h = MuseHook(ledger=db, clock=lambda: NOW, environ={**ENV, "MUSE_DAILY_PAID_CAP_USD": "0.0001"})
    first = {**req(OWNER, call_id="c1"), "model_info": PP["model_info"], "messages": [], "max_tokens": 10}
    second = {**req(OWNER, call_id="c2"), "model_info": PP["model_info"], "messages": [], "max_tokens": 10}
    filt(h, [PP], first)
    filt(h, [PP], second)  # both routed before either was sent
    assert run(h.async_pre_call_deployment_hook(first, None)) is None
    with pytest.raises(MuseDirectRationError) as err:
        run(h.async_pre_call_deployment_hook(second, None))
    assert "daily paid cap" in str(err.value)


def test_unpriced_paid_by_direct_name_is_recorded_as_unknown(h, db):
    pu = dep("pu", "paidu", tier="paid")
    data = {**req(OWNER, call_id="c-pu"), "model_info": pu["model_info"], "messages": []}
    assert filt(h, [pu], data, model="pu") == [pu]  # named directly
    assert run(h.async_pre_call_deployment_hook(data, None)) is None
    kwargs = {"litellm_call_id": "c-pu", "litellm_params": {"metadata": {"model_info": pu["model_info"], "user_api_key_metadata": OWNER}},
              "standard_logging_object": {"prompt_tokens": 3, "completion_tokens": 4, "response_cost": 0.0}}
    run(h.async_log_success_event(kwargs, SimpleNamespace(_hidden_params={}, usage=None), None, None))
    assert db.paid_spend(now=NOW) == {"usd": 0.0, "calls": 1, "unknown_cost_calls": 1, "held_usd": 0.0}


def test_failure_records_app_and_releases_hold(h, db):
    from types import MappingProxyType

    data = {**req(OWNER, call_id="c-fail"), "model_info": PP["model_info"], "messages": [], "max_tokens": 10}
    filt(h, [PP], data)
    run(h.async_pre_call_deployment_hook(data, None))

    class Boom(Exception):
        status_code = 500
        message = "upstream broke"

    run(h.async_post_call_failure_deployment_hook(MappingProxyType(data), Boom(), None))
    row = db.recent(1)[0]
    assert (row["ok"], row["app"], row["paid"], row["cost_usd"]) == (0, "owner", 1, None)
    assert db.paid_spend(now=NOW)["held_usd"] == 0


def test_own_ration_errors_are_not_provider_failures(h, db):
    from muse import ration

    decision = ration.Decision("defer", "yield", "[muse] yielding", NOW + 5)
    for exc in (MuseRationError(decision, "chat"), MuseAppCapError(decision), MuseDirectRationError(decision)):
        run(h.async_post_call_failure_deployment_hook(req(BATCH) | {"model_info": A["model_info"]}, exc, None))
    assert db.recent() == [] and db.active_blocks() == {}


def test_notes_are_bounded(h):
    from muse import hook as hook_module

    for i in range(hook_module.NOTE_MAX + 50):
        h._note(f"c{i}")
    assert len(h._notes) == hook_module.NOTE_MAX
