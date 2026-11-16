"""muse.hook glue for the routing rules, pacing hint, one-off holding, hand-off options and the cache."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("litellm")

from fastapi import HTTPException  # noqa: E402

from muse import apps, quota, ration  # noqa: E402
from muse.hook import MuseBlockedError, MuseDirectRationError, MuseHook, MuseRationError, apply_ceiling  # noqa: E402
from muse.ledger import Ledger  # noqa: E402

NOW = 1_794_466_800.0  # 2026-11-12T07:00:00Z
ENV = {"MUSE_TZ": "UTC"}


def run(coro):
    return asyncio.run(coro)


def dep(dep_id, provider, tier="free", kind="cloud", price=None, order=1, oneoff=None, model_name="chat"):
    info = {"id": dep_id, "provider": provider, "muse_upstream": f"{provider}:{dep_id.split('__')[0]}", "muse_kind": kind,
            "muse_trains": "no" if kind == "local" else "yes", "muse_human_review": "no" if kind == "local" else "yes",
            "muse_tier": tier}
    if price:
        info["muse_price_in"], info["muse_price_out"] = price
    if oneoff is not None:
        info["muse_oneoff"] = oneoff
    return {"model_name": model_name, "litellm_params": {"model": dep_id, "order": order}, "model_info": info}


A = dep("a__chat", "freea")
B = dep("b__chat", "freeb")
L = dep("l__chat", "local_openai", tier="local", kind="local")
S = dep("s__smart", "strong", model_name="smart")
PP = dep("pp__smart", "paidp", tier="paid", price=(20.0, 40.0), order=21, model_name="smart")
T = dep("t__spare", "trialx", tier="trial", oneoff=True, model_name="spare")
OWNER = apps.OWNER.policy()
BATCH = apps.App(name="batch", priority="background", allow_paid=False).policy()
STINGY = apps.App(name="stingy", priority="interactive", allow_paid=False).policy()


def req(app_policy=OWNER, call_id="call-1", headers=None, **extra):
    md = {"headers": dict(headers or {})}
    md.update(extra.pop("md", {}))
    if app_policy is not None:
        md["user_api_key_metadata"] = app_policy
    return {"metadata": md, "litellm_call_id": call_id, **extra}


@pytest.fixture()
def db(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite", clock=lambda: NOW)


@pytest.fixture()
def h(db, tmp_path):
    return MuseHook(ledger=db, clock=lambda: NOW, environ={**ENV, "MUSE_HOME": str(tmp_path)})


def filt(h, deployments, data, model="chat", messages=None):
    return run(h.async_filter_deployments(model, deployments, messages, request_kwargs=data))


def pre(h, data, call_type="acompletion"):
    return run(h.async_pre_call_hook(None, None, data, call_type))


def headers_for(h, data, response=None):
    return run(h.async_post_call_response_headers_hook(data, None, response)) or {}


# ---------------------------------------------------------------- x-muse-task


@pytest.mark.parametrize("model, task, sent, expect_model, expect_max", [
    ("auto", "light", {}, "fast", 300),
    ("smart", "light", {"max_tokens": 50}, "fast", 50),  # a lower client limit is kept
    ("auto", "light", {"max_tokens": 5000}, "fast", 300),
    ("auto", "everyday", {"max_completion_tokens": 9000}, "cheap", 1200),
    ("auto", "frontier", {}, "smart", None),
    ("auto", None, {}, "cheap", 1200),  # model auto, no header: the default task
    ("cheap", None, {}, "cheap", None),  # nothing to do
])
def test_task_replaces_the_model(h, model, task, sent, expect_model, expect_max):
    data = req(headers={"x-muse-task": task} if task else {}, model=model, messages=[{"role": "user", "content": "hi"}], **sent)
    out = pre(h, data)
    assert out["model"] == expect_model
    got = out.get("max_tokens", out.get("max_completion_tokens"))
    assert got == expect_max
    if task or model == "auto":
        assert out["metadata"]["muse_task"] == (task or "everyday")
        assert headers_for(h, out)["x-muse-task"] == (task or "everyday")


def test_app_default_task_for_auto(h):
    queue_app = apps.App(name="queue", priority="background").policy()
    out = pre(h, req(queue_app, model="auto", messages=[{"role": "user", "content": "x"}]))
    assert out["model"] == "fast" and out["metadata"]["muse_task"] == "light"


def test_long_input_switches_alias(h):
    big = "word " * 40000  # about 66,000 estimated tokens
    out = pre(h, req(headers={"x-muse-task": "heavy"}, model="auto", messages=[{"role": "user", "content": big}]))
    assert out["model"] == "long-context"


def test_unknown_task_is_a_400_listing_the_tasks(h):
    with pytest.raises(HTTPException) as err:
        pre(h, req(headers={"x-muse-task": "poetry"}, model="auto", messages=[]))
    assert err.value.status_code == 400
    assert "unknown task 'poetry'" in err.value.detail and "light, everyday, heavy, frontier, embed, rerank" in err.value.detail


def test_task_mode_must_match_the_call(h):
    with pytest.raises(HTTPException) as err:
        pre(h, req(headers={"x-muse-task": "embed"}, model="auto", messages=[]), "acompletion")
    assert err.value.status_code == 400 and "embedding task" in err.value.detail
    out = pre(h, req(headers={"x-muse-task": "embed"}, model="auto", input="x"), "aembedding")
    assert out["model"] == "embed" and "max_tokens" not in out
    with pytest.raises(HTTPException):
        pre(h, req(headers={"x-muse-task": "light"}, model="auto", input="x"), "aembedding")


def test_broken_rules_file_falls_back_and_warns_once(h, tmp_path, caplog):
    bad = tmp_path / "routing.yaml"
    bad.write_text("tasks: {light: {alias: fast, max_output_tokens: lots}}")
    h.environ["MUSE_ROUTING"] = str(bad)
    for _ in range(3):
        out = pre(h, req(headers={"x-muse-task": "light"}, model="auto", messages=[]))
        assert out["model"] == "fast"
    assert sum("routing rules not used" in r.message for r in caplog.records) == 1


@pytest.mark.parametrize("sent, ceiling, after", [
    ({}, 300, {"max_tokens": 300}),
    ({"max_tokens": 10}, 300, {"max_tokens": 10}),
    ({"max_tokens": 1000}, 300, {"max_tokens": 300}),
    ({"max_completion_tokens": 1000}, 300, {"max_completion_tokens": 300}),
    ({"max_tokens": True}, 300, {"max_tokens": 300}),  # a bool is not a limit
])
def test_apply_ceiling(sent, ceiling, after):
    data = dict(sent)
    apply_ceiling(data, ceiling)
    assert {k: v for k, v in data.items() if v is not True} == after


# ---------------------------------------------------------------- one-off and hand-off through the hook


def test_oneoff_held_until_the_header_says_yes(h):
    with pytest.raises(MuseRationError) as err:
        filt(h, [T], req(), model="spare")
    assert err.value.decision.code == "oneoff-held" and "| options=none" in err.value.decision.message
    assert headers_for(h, req())["x-muse-ration"] == "refuse:oneoff-held"
    assert filt(h, [T], req(call_id="c2", headers={"x-muse-allow-oneoff": "true"}), model="spare") == [T]
    with pytest.raises(MuseDirectRationError):  # by its own id: the deployment hook holds it too
        run(h.async_pre_call_deployment_hook({"model_info": T["model_info"], "litellm_call_id": "c3", "metadata": {"headers": {}}}, None))


def test_frontier_holds_paid_and_offers_three_choices(h, db):
    db.set_block("strong:s", NOW + 3600, "exhausted", "daily", now=NOW)
    data = req(md={"muse_handoff": True, "muse_task": "frontier"})
    with pytest.raises(MuseRationError) as err:
        filt(h, [S, PP], data, model="smart", messages=[{"role": "user", "content": "hello"}])
    decision = err.value.decision
    assert decision.code == "handoff-first" and decision.kind == "refuse"
    options = ration.parse_options(decision.message)
    assert options == {"wait": quota.iso(NOW + 3600), "handoff": True, "pay": pytest.approx((2 * 20 + 1024 * 40) / 1e6)}
    out = headers_for(h, data)
    assert out["x-muse-handoff"] == "suggest" and out["x-muse-task"] == "frontier"
    approved = req(call_id="c2", headers={"x-muse-allow-paid": "true"}, md={"muse_handoff": True})
    assert filt(h, [S, PP], approved, model="smart") == [PP]


def test_strong_alias_refusal_suggests_handoff_without_pay(h, db):
    db.set_block("strong:s", NOW + 3600, "exhausted", "daily", now=NOW)
    with pytest.raises(MuseRationError) as err:
        filt(h, [S, PP], req(STINGY), model="smart")
    assert ration.parse_options(err.value.decision.message) == {"wait": quota.iso(NOW + 3600), "handoff": True}
    assert headers_for(h, req(STINGY))["x-muse-handoff"] == "suggest"


def test_all_blocked_message_carries_options(h, db):
    db.set_block("freea:a", NOW + 600, "exhausted", "daily", now=NOW)
    with pytest.raises(MuseBlockedError) as err:
        filt(h, [A], req(), model="chat")
    assert ration.parse_options(str(err.value.message)) == {"wait": quota.iso(NOW + 600)}
    db.set_block("strong:s", NOW + 600, "exhausted", "daily", now=NOW)
    with pytest.raises(MuseBlockedError) as err:
        filt(h, [S], req(call_id="c2"), model="smart")
    assert ration.parse_options(str(err.value.message)) == {"wait": quota.iso(NOW + 600), "handoff": True}


def test_frontier_success_says_suggest(h):
    data = req(md={"muse_handoff": True, "muse_task": "frontier"})
    assert filt(h, [S], data, model="smart") == [S]
    response = SimpleNamespace(usage=None, _hidden_params={})
    out = headers_for(h, data, response)
    assert out["x-muse-handoff"] == "suggest"


# ---------------------------------------------------------------- pacing through the hook


def test_preference_is_recorded_and_explained(h, db):
    db.record_limits("freea:a", [quota.ObservedLimit("day", "requests", 100, 80, NOW + 12 * 3600)], now=NOW)
    db.record_limits("freeb:b", [quota.ObservedLimit("day", "requests", 100, 20, NOW + 12 * 3600)], now=NOW)
    data = req()
    assert filt(h, [A, B], data) == [A]
    note = h._note("call-1", create=False)
    assert "pace:prefer" in note.codes and any(d.startswith("pace:prefer: prefer freea:a") for d in note.details)
    response = SimpleNamespace(usage=None, _hidden_params={})
    assert headers_for(h, data, response)["x-muse-ration"] == "pace:prefer"


def test_background_pacing_through_the_ledger(h, db):
    db.record_limits("freea:a", [quota.ObservedLimit("day", "requests", 100, 40, NOW + 12 * 3600)], now=NOW)
    assert filt(h, [A, B], req(BATCH)) == [B]
    with pytest.raises(MuseRationError) as err:
        filt(h, [A], req(BATCH, call_id="c2"))
    assert err.value.decision.code == "pace"
    assert headers_for(h, req(BATCH, call_id="c2"))["x-muse-ration"] == "defer:pace"


def test_privacy_still_first_for_paced_background(h, db):
    db.record_limits("freea:a", [quota.ObservedLimit("day", "requests", 100, 90, NOW + 12 * 3600)], now=NOW)
    assert filt(h, [A, L], req(BATCH, headers={"x-muse-privacy": "local"})) == [L]


def test_burn_rate_is_read_for_the_hint(h, db):
    db.record_limits("freea:a", [quota.ObservedLimit("day", "requests", 100, 60, NOW + 12 * 3600)], now=NOW)
    db.record_limits("freeb:b", [quota.ObservedLimit("day", "requests", 100, 40, NOW + 12 * 3600)], now=NOW)
    for i in range(15):  # A burns 15 an hour: it will run dry; B 3 an hour: 0.04 left over
        db.record_call(provider="freea", deployment_id="a", upstream="freea:a", ok=True, now=NOW - 60 - i)
    for i in range(3):
        db.record_call(provider="freeb", deployment_id="b", upstream="freeb:b", ok=True, now=NOW - 60 - i)
    h.environ["MUSE_PACE_PREFER_GAP"] = "0.02"
    assert filt(h, [A, B], req()) == [B]


# ---------------------------------------------------------------- cache through the hook


def test_cache_miss_store_hit_and_ledger_marking(h, db, tmp_path):
    h.environ["MUSE_CACHE"] = "exact"
    msgs = [{"role": "user", "content": "2+2?"}]
    first = req(call_id="c1", temperature=0)
    assert filt(h, [A, B], first, messages=msgs) == [A, B]
    assert h._note("c1", create=False).cache == "miss"
    kwargs = {**first, "messages": msgs, "model_info": A["model_info"],
              "litellm_params": {"metadata": first["metadata"]}, "standard_logging_object": {"model_group": "chat"}}
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="4", tool_calls=None, function_call=None))],
                               usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1), _hidden_params={})
    run(h.async_log_success_event(kwargs, response, NOW, NOW + 1))
    assert (tmp_path / "cache.sqlite").exists()

    second = req(call_id="c2", temperature=0)
    db.set_block("freea:a", NOW + 600, "exhausted", "x", now=NOW)
    db.set_block("freeb:b", NOW + 600, "exhausted", "x", now=NOW)
    assert filt(h, [A, B], second, messages=msgs) == [A, B]  # a hit needs no allowance
    dkw = {**second, "messages": msgs, "model": "openai/a", "model_info": A["model_info"]}
    out = run(h.async_pre_call_deployment_hook(dkw, None))
    assert out["mock_response"]["choices"][0]["message"]["content"] == "4"
    run(h.async_log_success_event({**dkw, "litellm_params": {"metadata": second["metadata"]}}, response, NOW, NOW))
    rows = db.recent(5)
    assert rows[0]["cached"] == 1 and rows[0]["cost_usd"] == 0 and rows[0]["provider"] == "cache" and rows[0]["tokens_in"] == 0
    assert db.usage(upstream="freea:a", now=NOW)["day"]["requests"] == 1
    out = headers_for(h, second, response)
    assert out["x-muse-cache"] == "hit" and out["x-muse-provider"] == "cache" and out["x-muse-cost-usd"] == "0.000000"

    other = req(call_id="c3", temperature=0, headers={"x-muse-privacy": "public"})
    with pytest.raises(MuseBlockedError):  # another class: no hit, so the blocks apply
        filt(h, [A, B], other, messages=msgs)


def test_cache_off_by_default(h):
    assert filt(h, [A], req(temperature=0), messages=[{"role": "user", "content": "x"}]) == [A]
    assert h._note("call-1", create=False).cache is None


# ---------------------------------------------------------------- ledger additions


def test_ledger_burn_and_cached_rows_do_not_count(db):
    db.record_call(provider="p", deployment_id="d", upstream="p:u", ok=True, tokens_in=10, tokens_out=5, now=NOW - 30, app="x")
    db.record_call(provider="p", deployment_id="d", upstream="p:v", ok=True, tokens_in=1, tokens_out=1, now=NOW - 30)
    db.record_call(provider="p", deployment_id="d", upstream="p:u", ok=False, now=NOW - 20)
    db.record_call(provider="cache", deployment_id="cache", upstream="cache", ok=True, now=NOW - 10, app="x", cached=True,
                   tokens_in=0, tokens_out=0, cost_usd=0.0, paid=False, priority="interactive")
    db.record_call(provider="p", deployment_id="d", upstream="p:u", ok=True, now=NOW - 7200)
    assert db.burn(["p:u", "provider:p", "cache"], NOW - 3600, now=NOW) == {
        "p:u": {"calls": 1, "tokens": 15}, "provider:p": {"calls": 2, "tokens": 17}, "cache": {"calls": 0, "tokens": 0}}
    assert db.app_usage("x", now=NOW)["requests"] == 1
    assert db.usage_by_app(now=NOW)["x"]["cached_calls"] == 1 and db.usage_by_app(now=NOW)["x"]["requests"] == 1
    assert db.last_interactive_success(NOW - 100) == {}
    assert db.cached_calls(now=NOW) == 1
