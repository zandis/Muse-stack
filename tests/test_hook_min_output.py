"""muse.hook: the output floor for reasoning models and the empty-answer flag. No proxy, a temp ledger, a fixed clock."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("litellm")

from muse import apps, quota, ration  # noqa: E402
from muse import hook as hook_module  # noqa: E402
from muse.hook import (  # noqa: E402
    MuseHook,
    MuseRationError,
    _estimate_tokens,
    apply_ceiling,
    apply_floor,
    empty_reasoning,
    min_output_header,
    min_output_of,
)
from muse.ledger import Ledger  # noqa: E402

NOW = 1_794_466_800.0  # 2026-11-12T07:00:00Z
ENV = {"MUSE_TZ": "UTC"}
FLOOR = 256
CHAT = "acompletion"  # what LiteLLM passes for /v1/chat/completions


def run(coro):
    return asyncio.run(coro)


def dep(dep_id, provider, tier="free", price=None, floor=None, model_name="chat", order=1):
    info = {"id": dep_id, "provider": provider, "mode": "chat", "muse_upstream": f"{provider}:{dep_id.split('__')[0]}",
            "muse_kind": "cloud", "muse_trains": "yes", "muse_human_review": "yes", "muse_tier": tier}
    if price:
        info["muse_price_in"], info["muse_price_out"] = price
    if floor is not None:
        info["muse_min_output_tokens"] = floor
    return {"model_name": model_name, "litellm_params": {"model": dep_id, "order": order}, "model_info": info}


R = dep("r__chat", "reason", floor=FLOOR)  # a reasoning model with a floor
U = dep("u__chat", "plain")  # no floor
PR = dep("pr__chat", "paidr", tier="paid", price=(20.0, 40.0), floor=FLOOR)
PP = dep("pp__smart", "paidp", tier="paid", price=(20.0, 40.0), floor=FLOOR, model_name="smart", order=21)
S = dep("s__smart", "strong", model_name="smart")
OWNER = apps.OWNER.policy()


def req(call_id="call-1", headers=None, md=None, **extra):
    metadata = {"headers": dict(headers or {}), "user_api_key_metadata": OWNER}
    metadata.update(md or {})
    return {"metadata": metadata, "litellm_call_id": call_id, **extra}


@pytest.fixture()
def db(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite", clock=lambda: NOW)


@pytest.fixture()
def h(db, tmp_path):
    return MuseHook(ledger=db, clock=lambda: NOW, environ={**ENV, "MUSE_HOME": str(tmp_path)})


def send(h, deployment, data, call_type=CHAT):
    """One attempt, as LiteLLM makes it: the hook gets its own copy of the request plus the chosen deployment."""
    kwargs = {**data, "model_info": deployment["model_info"]}
    out = run(h.async_pre_call_deployment_hook(kwargs, call_type))
    return kwargs, out


def answer(text="fake reply", finish="stop", reasoning_tokens=None, tool_calls=None, model_id=None):
    details = SimpleNamespace(reasoning_tokens=reasoning_tokens) if reasoning_tokens is not None else None
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=text, tool_calls=tool_calls, function_call=None))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7, completion_tokens_details=details),
        _hidden_params={"model_id": model_id} if model_id else {},
    )


def headers_for(h, data, response, deployment=None):
    info = {"model_info": deployment["model_info"], "model_id": deployment["model_info"]["id"]} if deployment else None
    return run(h.async_post_call_response_headers_hook(data, None, response, litellm_call_info=info)) or {}


# ---------------------------------------------------------------- the pure pieces

@pytest.mark.parametrize("sent, floor, after, changed", [
    ({"max_tokens": 16}, 256, {"max_tokens": 256}, {"max_tokens": (16, 256)}),
    ({"max_completion_tokens": 20}, 256, {"max_completion_tokens": 256}, {"max_completion_tokens": (20, 256)}),
    ({"max_output_tokens": 1}, 256, {"max_output_tokens": 256}, {"max_output_tokens": (1, 256)}),
    ({"max_tokens": 255}, 256, {"max_tokens": 256}, {"max_tokens": (255, 256)}),
    ({"max_tokens": 256}, 256, {"max_tokens": 256}, {}),  # at the floor: nothing to do
    ({"max_tokens": 300}, 256, {"max_tokens": 300}, {}),  # never lowered
    ({"max_tokens": 9000}, 256, {"max_tokens": 9000}, {}),
    ({}, 256, {}, {}),  # no cap set: none is invented
    ({"max_tokens": None}, 256, {"max_tokens": None}, {}),
    ({"max_tokens": 0}, 256, {"max_tokens": 0}, {}),  # not a usable cap; left for the provider to judge
    ({"max_tokens": -4}, 256, {"max_tokens": -4}, {}),
    ({"max_tokens": True}, 256, {"max_tokens": True}, {}),  # a boolean is not a number of tokens
    ({"max_tokens": "16"}, 256, {"max_tokens": "16"}, {}),
    ({"max_tokens": 16.0}, 256, {"max_tokens": 16.0}, {}),
    ({"max_tokens": 16, "max_completion_tokens": 400}, 256, {"max_tokens": 256, "max_completion_tokens": 400}, {"max_tokens": (16, 256)}),
    ({"max_tokens": 16, "max_completion_tokens": 32}, 256, {"max_tokens": 256, "max_completion_tokens": 256},
     {"max_tokens": (16, 256), "max_completion_tokens": (32, 256)}),
    ({"max_tokens": 16}, None, {"max_tokens": 16}, {}),  # a deployment with no floor
    ({"max_tokens": 16}, 0, {"max_tokens": 16}, {}),
])
def test_apply_floor(sent, floor, after, changed):
    data = dict(sent)
    assert apply_floor(data, floor) == changed
    assert data == after


def test_apply_floor_touches_nothing_else():
    data = {"max_tokens": 16, "messages": [{"role": "user", "content": "hi"}], "temperature": 0, "n": 1}
    apply_floor(data, FLOOR)
    assert data == {"max_tokens": FLOOR, "messages": [{"role": "user", "content": "hi"}], "temperature": 0, "n": 1}


@pytest.mark.parametrize("info, expected", [
    ({"muse_min_output_tokens": 256}, 256),
    ({"muse_min_output_tokens": 1}, 1),
    ({}, None),
    (None, None),
    ({"muse_min_output_tokens": 0}, None),
    ({"muse_min_output_tokens": -1}, None),
    ({"muse_min_output_tokens": True}, None),
    ({"muse_min_output_tokens": "256"}, None),
    ({"muse_min_output_tokens": 256.0}, None),
    ({"muse_min_output_tokens": None}, None),
])
def test_min_output_of(info, expected):
    assert min_output_of(info) == expected


@pytest.mark.parametrize("changed, ceiling, expected", [
    ({"max_tokens": (16, 256)}, None, "raised:16->256"),
    ({"max_tokens": (16, 256)}, 300, "raised:16->256"),  # the ceiling is not exceeded
    ({"max_tokens": (16, 256)}, 256, "raised:16->256"),
    ({"max_tokens": (100, 256)}, 100, "raised:100->256;over-ceiling:100"),
    ({"max_tokens": (16, 256)}, 100, "raised:16->256;over-ceiling:100"),  # the client asked for less than the ceiling
    ({"max_tokens": (16, 256), "max_completion_tokens": (32, 256)}, None, "raised:16->256"),  # the smallest cap is the effective one
    ({"max_tokens": (16, 256)}, True, "raised:16->256"),
    ({"max_tokens": (16, 256)}, "100", "raised:16->256"),
    ({}, 100, None),
])
def test_min_output_header(changed, ceiling, expected):
    assert min_output_header(changed, ceiling) == expected


# ---------------------------------------------------------------- the deployment hook

def test_a_small_cap_is_raised_for_the_floored_deployment_and_reported(h):
    data = req(max_tokens=16, messages=[{"role": "user", "content": "hi"}])
    kwargs, out = send(h, R, data)
    assert out is kwargs and kwargs["max_tokens"] == FLOOR  # LiteLLM sends what the hook returns
    assert data["max_tokens"] == 16  # the request the router keeps for a retry or fallback is not touched
    got = headers_for(h, data, answer(), R)
    assert got["x-muse-min-output"] == "raised:16->256"
    assert "x-muse-answer" not in got
    assert "min-output: r__chat: raised:16->256" in h._note("call-1", create=False).details


@pytest.mark.parametrize("key", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
def test_each_cap_name_is_raised(h, key):
    kwargs, _ = send(h, R, req(**{key: 20}))
    assert kwargs[key] == FLOOR
    assert headers_for(h, req(), answer(), R)["x-muse-min-output"] == "raised:20->256"


@pytest.mark.parametrize("sent", [{"max_tokens": 256}, {"max_tokens": 300}, {"max_tokens": 4096}, {"max_completion_tokens": 1000}])
def test_a_cap_at_or_above_the_floor_is_never_lowered(h, sent):
    data = req(**sent)
    kwargs, out = send(h, R, data)
    assert out is None and {k: kwargs[k] for k in sent} == sent
    assert "x-muse-min-output" not in headers_for(h, data, answer(), R)


def test_no_cap_stays_unset_and_there_is_no_header(h):
    data = req(messages=[{"role": "user", "content": "hi"}])
    kwargs, out = send(h, R, data)
    assert out is None
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & set(kwargs)
    assert "x-muse-min-output" not in headers_for(h, data, answer(), R)


def test_a_deployment_without_a_floor_is_left_alone(h):
    data = req(max_tokens=16)
    kwargs, out = send(h, U, data)
    assert out is None and kwargs["max_tokens"] == 16
    assert "x-muse-min-output" not in headers_for(h, data, answer(), U)


@pytest.mark.parametrize("call_type", ["aembedding", "embedding", "arerank", "rerank", "aimage_generation", "atranscription", None, ""])
def test_only_chat_calls_are_raised(h, call_type):
    data = req(max_tokens=16)
    kwargs, out = send(h, R, data, call_type=call_type)
    assert out is None and kwargs["max_tokens"] == 16
    assert "x-muse-min-output" not in headers_for(h, data, answer(), R)


@pytest.mark.parametrize("call_type", ["acompletion", "completion", "atext_completion", "aresponses", "anthropic_messages"])
def test_every_chat_shaped_call_is_raised(h, call_type):
    kwargs, _ = send(h, R, req(max_tokens=16, max_output_tokens=16), call_type=call_type)
    assert kwargs["max_tokens"] == FLOOR and kwargs["max_output_tokens"] == FLOOR


def test_call_type_enum_is_understood(h):
    from litellm.types.utils import CallTypes

    assert send(h, R, req(max_tokens=16), call_type=CallTypes.acompletion)[0]["max_tokens"] == FLOOR
    assert send(h, R, req(call_id="c2", max_tokens=16), call_type=CallTypes.aembedding)[0]["max_tokens"] == 16


def test_a_request_that_is_not_a_router_call_is_ignored(h):
    kwargs = {"model": "x", "max_tokens": 16}
    assert run(h.async_pre_call_deployment_hook(kwargs, CHAT)) is None and kwargs["max_tokens"] == 16


def test_a_bad_floor_in_model_info_means_no_floor(h):
    for bad in (0, -1, True, "256", 12.5):
        broken = dep("x__chat", "reason", floor=bad)
        kwargs, out = send(h, broken, req(call_id=f"c-{bad!r}", max_tokens=16))
        assert out is None and kwargs["max_tokens"] == 16


def test_an_error_in_the_floor_is_counted_and_the_request_goes_out_as_sent(h, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("the prompt must not appear here")

    monkeypatch.setattr(hook_module, "apply_floor", boom)
    kwargs, out = send(h, R, req(max_tokens=16))
    assert out is None and kwargs["max_tokens"] == 16 and h.errors["min_output"] == 1


def test_privacy_still_comes_first(h):
    from muse.hook import MusePrivacyError

    kwargs = {**req(max_tokens=16, headers={"x-muse-privacy": "local"}), "model_info": R["model_info"]}
    with pytest.raises(MusePrivacyError):
        run(h.async_pre_call_deployment_hook(kwargs, CHAT))
    assert kwargs["max_tokens"] == 16


def test_nothing_from_the_request_is_logged(h, caplog):
    secret = "SECRET-PROMPT-7f3a"
    with caplog.at_level(logging.DEBUG):
        send(h, R, req(max_tokens=16, messages=[{"role": "user", "content": secret}]))
        headers_for(h, req(), answer(text="", finish="length"), R)
    note = h._note("call-1", create=False)
    assert secret not in caplog.text and secret not in " ".join(note.details)


# ---------------------------------------------------------------- the routing rules' ceiling against the floor

def pre(h, data):
    return run(h.async_pre_call_hook(None, None, data, CHAT))


def test_ceiling_above_the_floor_is_kept(h):
    """Task `light` caps the answer at 300; a floor of 256 is under it, so nothing changes."""
    data = pre(h, req(headers={"x-muse-task": "light"}, model="auto", messages=[{"role": "user", "content": "hi"}]))
    assert data["max_tokens"] == 300 and data["metadata"]["muse_output_ceiling"] == 300
    kwargs, out = send(h, R, data)
    assert out is None and kwargs["max_tokens"] == 300
    assert "x-muse-min-output" not in headers_for(h, data, answer(), R)


def test_client_cap_under_ceiling_and_floor_is_raised_to_the_floor_only(h):
    data = pre(h, req(headers={"x-muse-task": "light"}, model="auto", max_tokens=50, messages=[]))
    assert data["max_tokens"] == 50  # the ceiling keeps a lower client limit
    kwargs, _ = send(h, R, data)
    assert kwargs["max_tokens"] == FLOOR  # 256 is still within the task's 300
    assert headers_for(h, data, answer(), R)["x-muse-min-output"] == "raised:50->256"


def test_floor_wins_over_a_lower_ceiling_and_the_header_says_so(h):
    """A ceiling below the floor would guarantee an empty answer on this model, so the floor wins here."""
    tall = dep("t__chat", "reason", floor=512)
    data = pre(h, req(headers={"x-muse-task": "light"}, model="auto", max_tokens=5000, messages=[]))
    assert data["max_tokens"] == 300  # the ceiling was applied first, for every deployment
    kwargs, _ = send(h, tall, data)
    assert kwargs["max_tokens"] == 512
    assert headers_for(h, data, answer(), tall)["x-muse-min-output"] == "raised:300->512;over-ceiling:300"
    # The same request on a deployment without a floor keeps the ceiling.
    other, out = send(h, U, data)
    assert out is None and other["max_tokens"] == 300


def test_ceiling_set_for_a_request_with_no_cap_is_also_raised(h):
    """The client set no cap, but the task did: that cap is real, and too small for this model."""
    tall = dep("t__chat", "reason", floor=512)
    data = pre(h, req(headers={"x-muse-task": "light"}, model="auto", messages=[]))
    assert data["max_tokens"] == 300
    kwargs, _ = send(h, tall, data)
    assert kwargs["max_tokens"] == 512
    assert headers_for(h, data, answer(), tall)["x-muse-min-output"] == "raised:300->512;over-ceiling:300"


def test_a_ceiling_claimed_in_request_metadata_is_dropped(h):
    data = pre(h, req(md={"muse_output_ceiling": 5}, model="cheap", max_tokens=16, messages=[]))
    assert "muse_output_ceiling" not in data["metadata"]  # no task applied, so there is no ceiling to report
    send(h, R, data)
    assert headers_for(h, data, answer(), R)["x-muse-min-output"] == "raised:16->256"


def test_apply_ceiling_is_unchanged():
    data = {"max_tokens": 5000}
    apply_ceiling(data, 300)
    assert data == {"max_tokens": 300}
    data = {}
    apply_ceiling(data, 300)
    assert data == {"max_tokens": 300}


# ---------------------------------------------------------------- fallbacks: the floor of the deployment actually used

def test_fallback_to_a_deployment_without_a_floor_gets_the_original_value(h):
    data = req(max_tokens=16)
    first, _ = send(h, R, data)
    assert first["max_tokens"] == FLOOR
    second, out = send(h, U, data)  # the router's next attempt, from the request it kept
    assert out is None and second["max_tokens"] == 16
    got = headers_for(h, data, answer(model_id="u__chat"), U)
    assert got["x-muse-deployment"] == "u__chat" and "x-muse-min-output" not in got


def test_fallback_gets_the_original_value_even_when_one_dict_is_reused(h):
    """LiteLLM copies the request per attempt; if a caller ever does not, the earlier raise is undone first."""
    shared = {**req(max_tokens=16, max_completion_tokens=32), "model_info": R["model_info"]}
    run(h.async_pre_call_deployment_hook(shared, CHAT))
    assert shared["max_tokens"] == FLOOR and shared["max_completion_tokens"] == FLOOR
    shared["model_info"] = U["model_info"]
    assert run(h.async_pre_call_deployment_hook(shared, CHAT)) is None
    assert shared["max_tokens"] == 16 and shared["max_completion_tokens"] == 32
    assert "x-muse-min-output" not in headers_for(h, shared, answer(model_id="u__chat"))


def test_fallback_from_no_floor_to_a_floor_is_raised_and_reported(h):
    data = req(max_tokens=16)
    first, _ = send(h, U, data)
    assert first["max_tokens"] == 16
    second, _ = send(h, R, data)
    assert second["max_tokens"] == FLOOR
    got = headers_for(h, data, answer(model_id="r__chat"), R)
    assert got["x-muse-deployment"] == "r__chat" and got["x-muse-min-output"] == "raised:16->256"


def test_two_floors_in_a_row_each_start_from_the_clients_value(h):
    low = dep("low__chat", "reasonb", floor=64)
    shared = {**req(max_tokens=16), "model_info": R["model_info"]}
    run(h.async_pre_call_deployment_hook(shared, CHAT))
    assert shared["max_tokens"] == FLOOR
    shared["model_info"] = low["model_info"]
    run(h.async_pre_call_deployment_hook(shared, CHAT))
    assert shared["max_tokens"] == 64  # not left at the 256 raised for the first deployment
    got = headers_for(h, shared, answer(model_id="low__chat"))
    assert got["x-muse-min-output"] == "raised:16->64" and got["x-muse-deployment"] == "low__chat"


def test_the_header_is_for_the_deployment_that_answered_not_the_stale_request_data(h):
    """After a fallback the request data still names the first deployment (see _Note.sent)."""
    data = req(max_tokens=16)
    send(h, R, data)
    send(h, U, data)
    stale = {**data, "model_info": R["model_info"]}
    got = run(h.async_post_call_response_headers_hook(stale, None, answer(model_id="u__chat")))
    assert got["x-muse-deployment"] == "u__chat" and "x-muse-min-output" not in got


def test_a_retry_on_the_same_deployment_is_raised_again(h):
    data = req(max_tokens=16)
    assert send(h, R, data)[0]["max_tokens"] == FLOOR
    assert send(h, R, data)[0]["max_tokens"] == FLOOR
    assert headers_for(h, data, answer(), R)["x-muse-min-output"] == "raised:16->256"


def test_no_floor_header_on_a_failed_request(h):
    data = req(max_tokens=16)
    send(h, R, data)
    got = run(h.async_post_call_response_headers_hook(data, None, None)) or {}
    assert "x-muse-min-output" not in got and "x-muse-answer" not in got


# ---------------------------------------------------------------- the paid hold counts the raised figure

def test_paid_hold_is_estimated_on_the_raised_cap(h, db):
    data = req(call_id="c-paid", max_tokens=16, messages=[{"role": "user", "content": "hi"}])
    kwargs, _ = send(h, PR, data)
    assert kwargs["max_tokens"] == FLOOR
    tokens_in, tokens_out = _estimate_tokens(kwargs)
    assert (tokens_in, tokens_out) == (1, FLOOR)
    held = db.paid_spend(now=NOW)["held_usd"]
    assert held == pytest.approx((1 * 20 + FLOOR * 40) / 1e6)  # not 16 tokens out: the cap is not undercounted
    assert held > (1 * 20 + 16 * 40) / 1e6


def test_paid_hold_without_a_floor_uses_the_clients_cap(h, db):
    plain_paid = dep("pq__chat", "paidq", tier="paid", price=(20.0, 40.0))
    send(h, plain_paid, req(call_id="c-plain", max_tokens=16, messages=[{"role": "user", "content": "hi"}]))
    assert db.paid_spend(now=NOW)["held_usd"] == pytest.approx((1 * 20 + 16 * 40) / 1e6)


def test_paid_hold_with_no_cap_keeps_the_default_estimate(h, db):
    kwargs, out = send(h, PR, req(call_id="c-none", messages=[{"role": "user", "content": "hi"}]))
    assert out is None and "max_tokens" not in kwargs
    assert db.paid_spend(now=NOW)["held_usd"] == pytest.approx((1 * 20 + hook_module.DEFAULT_OUTPUT_ESTIMATE * 40) / 1e6)


def test_the_raised_figure_can_fill_the_daily_cap(db, tmp_path):
    """The cap admits a call while the spend and holds before it are under the cap. Held at 16
    tokens the first call would leave room for a second; held at the 256 actually sent it does not."""
    from muse.hook import MuseDirectRationError

    as_asked = (1 * 20 + 16 * 40) / 1e6
    as_sent = (1 * 20 + FLOOR * 40) / 1e6
    cap = (as_asked + as_sent) / 2
    h = MuseHook(ledger=db, clock=lambda: NOW, environ={**ENV, "MUSE_HOME": str(tmp_path), "MUSE_DAILY_PAID_CAP_USD": f"{cap:.8f}"})
    msgs = [{"role": "user", "content": "hi"}]
    send(h, PR, req(call_id="c-1", max_tokens=16, messages=msgs))
    assert db.paid_spend(now=NOW)["held_usd"] == pytest.approx(as_sent) and as_asked < cap < as_sent
    with pytest.raises(MuseDirectRationError) as err:
        send(h, PR, req(call_id="c-2", max_tokens=16, messages=msgs))
    assert "daily paid cap" in str(err.value)


def test_the_pay_offer_uses_the_raised_figure_too(h, db):
    """The `pay:` estimate shown with the hand-off is the same estimate the hold will use."""
    db.set_block("strong:s", NOW + 3600, "exhausted", "daily", now=NOW)
    plain = dep("pz__smart", "paidz", tier="paid", price=(20.0, 40.0), model_name="smart", order=22)
    data = req(md={"muse_handoff": True, "muse_task": "frontier"}, max_tokens=16)
    with pytest.raises(MuseRationError) as err:
        run(h.async_filter_deployments("smart", [S, PP], [{"role": "user", "content": "hello"}], request_kwargs=data))
    options = ration.parse_options(err.value.decision.message)
    assert options == {"wait": quota.iso(NOW + 3600), "handoff": True, "pay": pytest.approx((2 * 20 + FLOOR * 40) / 1e6)}
    assert data["max_tokens"] == 16  # estimating changes nothing in the request
    other = req(call_id="c2", md={"muse_handoff": True, "muse_task": "frontier"}, max_tokens=16)
    with pytest.raises(MuseRationError) as err:
        run(h.async_filter_deployments("smart", [S, plain], [{"role": "user", "content": "hello"}], request_kwargs=other))
    assert ration.parse_options(err.value.decision.message)["pay"] == pytest.approx((2 * 20 + 16 * 40) / 1e6)


def test_estimate_tokens_is_unchanged():
    assert _estimate_tokens({"messages": [{"role": "user", "content": "hi"}], "max_tokens": 16}) == (1, 16)
    assert _estimate_tokens({"messages": [{"role": "user", "content": "hi"}]}) == (1, 1024)
    assert _estimate_tokens({"max_completion_tokens": 7, "max_output_tokens": 9}) == (1, 7)


# ---------------------------------------------------------------- the safety net: x-muse-answer

@pytest.mark.parametrize("response, expected", [
    (answer(text="", finish="length"), True),
    (answer(text=None, finish="length"), True),
    (answer(text="  \n", finish="length"), True),
    (answer(text="", finish="stop", reasoning_tokens=16), True),  # no content, and the usage says it reasoned
    (answer(text=None, finish="stop", reasoning_tokens=1), True),
    (answer(text="fake reply", finish="stop"), False),
    (answer(text="fake reply", finish="length"), False),  # cut short, but there is text to show
    (answer(text="fake reply", finish="stop", reasoning_tokens=200), False),
    (answer(text="", finish="stop"), False),  # empty for some other reason: not this header's business
    (answer(text="", finish="stop", reasoning_tokens=0), False),
    (answer(text="", finish="content_filter", reasoning_tokens=0), False),
    (answer(text=None, finish="tool_calls", reasoning_tokens=30, tool_calls=[{"id": "t1"}]), False),  # a tool call is an answer
    (answer(text="", finish="length", tool_calls=[{"id": "t1"}]), False),
    (answer(text=[{"type": "text", "text": "hi"}], finish="length"), False),
    (SimpleNamespace(choices=[], usage=None), False),
    (SimpleNamespace(usage=None), False),  # an embedding or rerank response
    (object(), False),
    (None, False),
    ({"choices": [{"finish_reason": "length", "message": {"content": ""}}]}, True),  # a plain dict works too
    ({"choices": [{"finish_reason": "stop", "message": {"content": ""}}],
      "usage": {"completion_tokens_details": {"reasoning_tokens": 16}}}, True),
    ({"choices": [{"finish_reason": "length", "message": {"content": "text"}}]}, False),
    ({"choices": [{"finish_reason": "length", "text": ""}]}, False),  # a text completion has no message
    ({"choices": [{"finish_reason": "length", "delta": {"content": ""}}]}, False),  # a stream chunk
])
def test_empty_reasoning(response, expected):
    assert empty_reasoning(response) is expected


def test_empty_reasoning_needs_every_choice_empty():
    full = SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="text", tool_calls=None, function_call=None))
    cut = SimpleNamespace(finish_reason="length", message=SimpleNamespace(content="", tool_calls=None, function_call=None))
    assert empty_reasoning(SimpleNamespace(choices=[cut, full], usage=None)) is False
    assert empty_reasoning(SimpleNamespace(choices=[cut, cut], usage=None)) is True


def test_empty_answer_header_on_a_reasoning_only_reply_and_the_body_is_untouched(h):
    data = req(max_tokens=16)
    send(h, U, data)
    response = answer(text="", finish="length", reasoning_tokens=16)
    before = (response.choices[0].message.content, response.choices[0].finish_reason, response.usage.completion_tokens)
    got = headers_for(h, data, response, U)
    assert got["x-muse-answer"] == "empty-reasoning" and "x-muse-min-output" not in got
    assert (response.choices[0].message.content, response.choices[0].finish_reason, response.usage.completion_tokens) == before


def test_both_headers_when_even_the_floor_was_not_enough(h):
    data = req(max_tokens=16)
    send(h, R, data)
    got = headers_for(h, data, answer(text="", finish="length", reasoning_tokens=FLOOR), R)
    assert got["x-muse-min-output"] == "raised:16->256" and got["x-muse-answer"] == "empty-reasoning"


def test_no_answer_header_on_a_normal_reply_or_a_non_chat_one(h):
    assert "x-muse-answer" not in headers_for(h, req(), answer(), U)
    embedding = SimpleNamespace(data=[{"embedding": [0.1]}], usage=SimpleNamespace(prompt_tokens=3, completion_tokens=None), _hidden_params={})
    assert "x-muse-answer" not in headers_for(h, req(call_id="c2"), embedding, U)


def test_answer_header_does_not_need_a_note(h):
    """A response whose request carried no call id still gets the flag."""
    data = {"metadata": {"headers": {}}}
    got = run(h.async_post_call_response_headers_hook(data, None, answer(text="", finish="length"),
                                                      litellm_call_info={"model_info": U["model_info"]}))
    assert got["x-muse-answer"] == "empty-reasoning"


def test_a_cached_answer_is_not_raised_and_has_no_floor_header(h, db):
    h.environ["MUSE_CACHE"] = "exact"
    msgs = [{"role": "user", "content": "2+2?"}]
    first = req(call_id="c1", temperature=0, max_tokens=16)
    assert run(h.async_filter_deployments("chat", [R], msgs, request_kwargs=first)) == [R]
    kwargs, _ = send(h, R, {**first, "messages": msgs})
    assert kwargs["max_tokens"] == FLOOR
    stored = {**first, "messages": msgs, "model_info": R["model_info"], "litellm_params": {"metadata": first["metadata"]},
              "standard_logging_object": {"model_group": "chat"}}
    run(h.async_log_success_event(stored, answer(text="4"), NOW, NOW + 1))

    second = req(call_id="c2", temperature=0, max_tokens=16)  # the same request as the client sent it: a hit
    assert run(h.async_filter_deployments("chat", [R], msgs, request_kwargs=second)) == [R]
    again, out = send(h, R, {**second, "messages": msgs, "model": "openai/r"})
    assert out["mock_response"]["choices"][0]["message"]["content"] == "4"
    assert again["max_tokens"] == 16  # nothing is sent upstream, so nothing is raised
    got = headers_for(h, second, answer(text="4"), R)
    assert got["x-muse-cache"] == "hit" and "x-muse-min-output" not in got


# ---------------------------------------------------------------- H1, the first real run: the floor under the light ceiling


def test_the_old_floor_never_applied_under_the_light_ceiling_and_the_new_one_does(h):
    """2026-11-13: gpt-oss carried a floor of 256 while task light capped at 300, so a light call stayed at 300
    and reasoning used it all. The shipped floor is now 1024: above the ceiling, so it wins for that deployment."""
    import yaml

    catalog = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "providers.yaml").read_text(encoding="utf-8"))
    floors = {d["id"]: d.get("min_output_tokens") for p in catalog["providers"] for d in p.get("deployments") or []}
    assert {k: v for k, v in floors.items() if "gpt-oss" in k} == {
        "groq-gpt-oss-20b": 1024, "groq-gpt-oss-120b": 1024, "novita-gpt-oss-120b": 1024, "nim-gpt-oss-20b": 1024,
        "cerebras-gpt-oss-120b": 1024}
    data = pre(h, req(headers={"x-muse-task": "light"}, model="auto", messages=[{"role": "user", "content": "hi"}]))
    old, out = send(h, dep("o__chat", "reason", floor=256), data)
    assert old["max_tokens"] == 300 and out is None
    new, _ = send(h, dep("n__chat", "reason", floor=1024), data)
    assert new["max_tokens"] == 1024
    assert headers_for(h, data, answer(), dep("n__chat", "reason", floor=1024))["x-muse-min-output"] == "raised:300->1024;over-ceiling:300"


# ---------------------------------------------------------------- the reasoning setting


def reasoner(dep_id="g__chat", setting=None):
    d = dep(dep_id, "groq", floor=1024)
    d["model_info"]["muse_reasoning"] = setting or {"param": "reasoning_effort", "light": "low", "everyday": "low"}
    return d


def test_reasoning_of_and_apply_reasoning():
    from muse.hook import apply_reasoning, reasoning_of

    info = reasoner()["model_info"]
    assert reasoning_of(info, "light") == ("reasoning_effort", "low") and reasoning_of(info, "heavy") is None
    assert reasoning_of(info, None) is None and reasoning_of({}, "light") is None
    assert reasoning_of({"muse_reasoning": {"param": "thinking", "light": "low"}}, "light") is None
    assert reasoning_of({"muse_reasoning": {"param": "reasoning_effort", "light": "none"}}, "light") is None
    data = {}
    assert apply_reasoning(data, ("reasoning_effort", "low")) == "low" and data == {"reasoning_effort": "low"}
    mine = {"reasoning_effort": "high"}
    assert apply_reasoning(mine, ("reasoning_effort", "low")) is None and mine == {"reasoning_effort": "high"}


def test_a_light_call_to_a_reasoning_deployment_is_sent_with_low_effort_and_says_so(h):
    data = pre(h, req(headers={"x-muse-task": "light"}, model="auto", messages=[{"role": "user", "content": "hi"}]))
    kwargs, out = send(h, reasoner(), data)
    assert kwargs["reasoning_effort"] == "low" and out is kwargs and "reasoning_effort" not in data
    got = headers_for(h, data, answer(), reasoner())
    assert got["x-muse-reasoning"] == "low" and got["x-muse-min-output"] == "raised:300->1024;over-ceiling:300"


def test_the_clients_own_reasoning_choice_wins_and_other_tasks_are_left_alone(h):
    data = pre(h, req(headers={"x-muse-task": "light"}, model="auto", reasoning_effort="high", messages=[]))
    kwargs, _ = send(h, reasoner(), data)
    assert kwargs["reasoning_effort"] == "high" and "x-muse-reasoning" not in headers_for(h, data, answer(), reasoner())
    heavy = pre(h, req(call_id="c-heavy", headers={"x-muse-task": "heavy"}, model="auto", messages=[]))
    kwargs, _ = send(h, reasoner(), heavy)
    assert "reasoning_effort" not in kwargs
    plain = pre(h, req(call_id="c-plain", headers={"x-muse-task": "light"}, model="auto", messages=[]))
    kwargs, _ = send(h, U, plain)
    assert "reasoning_effort" not in kwargs and "x-muse-reasoning" not in headers_for(h, plain, answer(), U)


def test_a_fallback_without_the_setting_gets_the_request_back_as_the_client_sent_it(h):
    data = pre(h, req(headers={"x-muse-task": "light"}, model="auto", messages=[]))
    kwargs = {**data, "model_info": reasoner()["model_info"]}
    run(h.async_pre_call_deployment_hook(kwargs, CHAT))
    assert kwargs["reasoning_effort"] == "low"
    kwargs["model_info"] = U["model_info"]  # the same dict reused for the next deployment
    run(h.async_pre_call_deployment_hook(kwargs, CHAT))
    assert "reasoning_effort" not in kwargs
    assert "x-muse-reasoning" not in headers_for(h, data, answer(), U)


def test_only_chat_calls_get_a_reasoning_setting(h):
    data = pre(h, req(headers={"x-muse-task": "light"}, model="auto", messages=[]))
    kwargs, _ = send(h, reasoner(), data, call_type="aembedding")
    assert "reasoning_effort" not in kwargs
