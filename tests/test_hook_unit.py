"""muse.hook in isolation: no proxy, a temp ledger, a fixed clock."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

pytest.importorskip("litellm")

import litellm  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from litellm.integrations.custom_guardrail import is_guardrail_intervention  # noqa: E402

from muse import hook as hook_module  # noqa: E402
from muse.hook import MuseBlockedError, MuseHook, MusePrivacyError  # noqa: E402
from muse.ledger import Ledger  # noqa: E402

NOW = 1_794_466_800.0  # 2026-11-12T07:00:00Z


def run(coro):
    return asyncio.run(coro)


def dep(dep_id: str, provider: str, model: str, kind: str = "cloud", trains: str = "yes", review: str = "yes", order: int = 1,
        tier: str = "free") -> dict:
    # muse_tier is always rendered; a deployment without one counts as paid (muse/ration.py).
    return {
        "model_name": dep_id.split("__")[-1],
        "litellm_params": {"model": model, "order": order},
        "model_info": {"id": dep_id, "provider": provider, "muse_upstream": f"{provider}:{model}",
                       "muse_kind": kind, "muse_trains": trains, "muse_human_review": review, "muse_tier": tier},
    }


G_FAST = dep("g__fast", "gemini", "gemini/g")
G_CHEAP = dep("g__cheap", "gemini", "gemini/g")
Q_FAST = dep("q__fast", "groq", "groq/q", trains="unknown", review="unknown", order=2)
P_CHEAP = dep("p__cheap", "privcloud", "openai/p", trains="no", review="no", order=3)
L_FAST = dep("l__fast", "ollama", "ollama/l", kind="local", trains="no", review="no", order=5)


@pytest.fixture()
def db(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite", clock=lambda: NOW)


@pytest.fixture()
def h(db: Ledger) -> MuseHook:
    return MuseHook(ledger=db, clock=lambda: NOW, environ={})


def req(privacy: str | None = None, **metadata) -> dict:
    headers = {"content-type": "application/json"}
    if privacy is not None:
        headers["x-muse-privacy"] = privacy
    return {"metadata": {"headers": headers, **metadata}, "proxy_server_request": {"headers": headers}}


# ---------------------------------------------------------------- module wiring

def test_module_instance_is_a_custom_logger():
    from litellm.integrations.custom_logger import CustomLogger

    assert isinstance(hook_module.handler, CustomLogger)
    assert hook_module.handler.turn_off_message_logging is True


# ---------------------------------------------------------------- pre-call

def test_pre_call_unknown_class_is_400(h):
    with pytest.raises(HTTPException) as err:
        run(h.async_pre_call_hook(None, None, {**req("nope"), "messages": []}, "completion"))
    assert err.value.status_code == 400 and "nope" in err.value.detail


@pytest.mark.parametrize("header, env, expected", [
    (None, {}, "standard"),
    ("LOCAL", {}, "local"),
    ("", {"MUSE_DEFAULT_PRIVACY": "private"}, "private"),
    (None, {"MUSE_DEFAULT_PRIVACY": "garbage"}, "local"),  # broken default fails closed
    ("public", {"MUSE_DEFAULT_PRIVACY": "local"}, "public"),
])
def test_pre_call_resolves_class(db, header, env, expected):
    h = MuseHook(ledger=db, clock=lambda: NOW, environ=env)
    data = run(h.async_pre_call_hook(None, None, {**req(header), "messages": [{"role": "user", "content": "hi"}]}, "completion"))
    assert data["metadata"]["muse_privacy"] == expected


def test_auto_privacy_raises_class_without_keeping_text(db):
    h = MuseHook(ledger=db, clock=lambda: NOW, environ={"MUSE_AUTO_PRIVACY": "on"})
    card = "4111 1111 1111 1111"
    data = {**req("standard"), "messages": [{"role": "user", "content": [{"type": "text", "text": f"pay with {card}"}]}]}
    out = run(h.async_pre_call_hook(None, None, data, "completion"))
    assert out["metadata"]["muse_privacy"] == "private"
    assert out["metadata"]["muse_privacy_findings"] == ["credit_card"]
    assert card not in json.dumps(out["metadata"])
    off = MuseHook(ledger=db, clock=lambda: NOW, environ={})
    assert run(off.async_pre_call_hook(None, None, {**req("standard"), "messages": data["messages"]}, "completion"))["metadata"]["muse_privacy"] == "standard"


def test_auto_privacy_detector_crash_fails_closed(db, monkeypatch):
    from muse import policy

    def boom(*a, **k):
        raise RuntimeError("detector bug")

    monkeypatch.setattr(policy, "effective_class", boom)
    h = MuseHook(ledger=db, clock=lambda: NOW, environ={"MUSE_AUTO_PRIVACY": "1"})
    out = run(h.async_pre_call_hook(None, None, {**req("public"), "messages": []}, "completion"))
    assert out["metadata"]["muse_privacy"] == "private"
    assert h.errors["auto_privacy"] == 1


# ---------------------------------------------------------------- filter

def filt(h, deployments, data, model="fast"):
    return run(h.async_filter_deployments(model, deployments, None, request_kwargs=data))


def test_filter_passes_through_without_blocks(h):
    assert filt(h, [G_FAST, Q_FAST], req()) == [G_FAST, Q_FAST]


def test_upstream_block_applies_to_every_alias(h, db):
    db.set_block("gemini:gemini/g", NOW + 3600, "exhausted", "daily")
    assert filt(h, [G_FAST, Q_FAST], req()) == [Q_FAST]
    assert filt(h, [G_CHEAP, P_CHEAP], req(), model="cheap") == [P_CHEAP]


def test_provider_block(h, db):
    db.set_block("provider:groq", NOW + 900, "exhausted", "bad key")
    assert filt(h, [G_FAST, Q_FAST], req()) == [G_FAST]


def test_blocks_leave_nothing_raises_rate_limit_with_reset(h, db):
    db.set_block("gemini:gemini/g", NOW + 3600, "exhausted", "daily")
    db.set_block("provider:groq", NOW + 60, "cooldown", "rpm")
    with pytest.raises(MuseBlockedError) as err:
        filt(h, [G_FAST, Q_FAST], req())
    assert isinstance(err.value, litellm.RateLimitError)  # LiteLLM may fall back
    assert not is_guardrail_intervention(err.value)
    assert "2026-11-12T07:01:00Z" in str(err.value)  # soonest reset


@pytest.mark.parametrize("privacy, deployments, expected", [
    ("local", [G_FAST, Q_FAST, L_FAST], [L_FAST]),
    ("private", [G_CHEAP, P_CHEAP], [P_CHEAP]),
    ("private", [G_FAST, Q_FAST, L_FAST], [L_FAST]),
    ("public", [G_FAST, Q_FAST], [G_FAST, Q_FAST]),
])
def test_privacy_filter(h, privacy, deployments, expected):
    assert filt(h, deployments, req(privacy)) == expected


def test_privacy_leaving_nothing_fails_closed_without_fallback(h, db):
    with pytest.raises(MusePrivacyError) as err:
        filt(h, [G_CHEAP, P_CHEAP], req("local"), model="cheap")
    assert err.value.status_code == 403 and "'local'" in err.value.message
    assert is_guardrail_intervention(err.value)  # router: no retry, no cross-alias fallback


def test_privacy_beats_blocks(h, db):
    """With the only allowed deployment blocked, the answer is a block, never a wider class."""
    db.set_block("ollama:ollama/l", NOW + 60, "cooldown", "rpm")
    with pytest.raises(MuseBlockedError):
        filt(h, [G_FAST, Q_FAST, L_FAST], req("local"))


def test_request_metadata_cannot_lower_the_header_class(h):
    data = req("local", muse_privacy="public")
    assert filt(h, [G_FAST, L_FAST], data) == [L_FAST]


def test_metadata_class_from_pre_call_can_raise_it(h):
    assert filt(h, [G_FAST, L_FAST], req("standard", muse_privacy="local")) == [L_FAST]


def test_bad_header_in_filter_is_400(h):
    with pytest.raises(MusePrivacyError) as err:
        filt(h, [G_FAST], req("nope"))
    assert err.value.status_code == 400


def test_ledger_failure_degrades_but_privacy_holds(h, monkeypatch):
    def boom(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(h.ledger, "active_blocks", boom)
    assert filt(h, [G_FAST, Q_FAST], req()) == [G_FAST, Q_FAST]
    assert filt(h, [G_FAST, L_FAST], req("local")) == [L_FAST]
    assert h.errors["active_blocks"] == 2


def test_policy_crash_fails_closed(h, monkeypatch):
    from muse import policy

    monkeypatch.setattr(policy, "filter_deployments", lambda *a: 1 / 0)
    with pytest.raises(MusePrivacyError):
        filt(h, [G_FAST], req())


# ---------------------------------------------------------------- pre-call deployment

def test_deployment_hook_blocks_direct_ids(h):
    kwargs = {**req("local"), "model_info": G_FAST["model_info"]}
    with pytest.raises(MusePrivacyError):
        run(h.async_pre_call_deployment_hook(kwargs, None))
    assert run(h.async_pre_call_deployment_hook({**req("local"), "model_info": L_FAST["model_info"]}, None)) is None
    assert run(h.async_pre_call_deployment_hook({"model": "x"}, None)) is None


# ---------------------------------------------------------------- outcomes

class FakeError(Exception):
    def __init__(self, status, body=None, headers=None, message=""):
        super().__init__(message)
        self.status_code = status
        self.body = body
        self.litellm_response_headers = headers or {}
        self.message = message


def failure_data(info, privacy="standard"):
    started = datetime.now() - timedelta(milliseconds=40)
    data = {**req(privacy, model_group="fast"), "model_info": info, "litellm_logging_obj": SimpleNamespace(start_time=started)}
    return MappingProxyType(data)  # the router passes a read-only mapping


def test_failure_daily_quota_blocks_upstream(h, db):
    body = {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}
    run(h.async_post_call_failure_deployment_hook(failure_data(G_FAST["model_info"]), FakeError(429, body, message="quota"), None))
    block = db.active_blocks()["gemini:gemini/g"]
    assert block["kind"] == "exhausted" and "Pacific" in block["reason"]
    row = db.recent(1)[0]
    assert (row["ok"], row["status"], row["alias"], row["deployment_id"]) == (0, 429, "fast", "g__fast")
    assert row["latency_ms"] is not None and row["latency_ms"] >= 0


def test_failure_retry_after_and_prefixed_headers(h, db):
    run(h.async_post_call_failure_deployment_hook(failure_data(Q_FAST["model_info"]), FakeError(429, headers={"retry-after": "7"}), None))
    assert db.active_blocks()["groq:groq/q"]["until"] == NOW + 7


@pytest.mark.parametrize("status", [400, 404, 422, 500, None])
def test_failure_without_block(h, db, status):
    run(h.async_post_call_failure_deployment_hook(failure_data(G_FAST["model_info"]), FakeError(status), None))
    assert db.active_blocks() == {}
    assert db.recent(1)[0]["status"] == status


def test_own_errors_and_foreign_deployments_are_ignored(h, db):
    run(h.async_post_call_failure_deployment_hook(failure_data(G_FAST["model_info"]), MusePrivacyError("x"), None))
    run(h.async_post_call_failure_deployment_hook(failure_data(G_FAST["model_info"]), MuseBlockedError("x", "fast"), None))
    run(h.async_post_call_failure_deployment_hook(failure_data({"id": "other"}), FakeError(401), None))
    assert db.recent() == [] and db.active_blocks() == {}


def test_failure_bookkeeping_error_is_swallowed(h, monkeypatch):
    monkeypatch.setattr(h.ledger, "record_call", lambda **k: 1 / 0)
    run(h.async_post_call_failure_deployment_hook(failure_data(G_FAST["model_info"]), FakeError(500), None))
    assert h.errors["failure_event"] == 1


def success_kwargs(info, privacy="standard"):
    return {
        "litellm_params": {"metadata": {**req(privacy)["metadata"], "model_info": info, "model_group": "fast"}},
        "standard_logging_object": {"prompt_tokens": 11, "completion_tokens": 7, "model_group": "fast"},
    }


def success_response(headers=None):
    return SimpleNamespace(_hidden_params={"additional_headers": headers or {}}, usage=None)


def test_success_records_call_and_limits(h, db):
    headers = {"llm_provider-x-ratelimit-limit-requests": "14400", "llm_provider-x-ratelimit-remaining-requests": "14000",
               "llm_provider-x-ratelimit-reset-requests": "1m"}
    start = datetime(2026, 11, 12, 7, 0, 0)
    run(h.async_log_success_event(success_kwargs(Q_FAST["model_info"], "private"), success_response(headers), start, start + timedelta(milliseconds=250)))
    row = db.recent(1)[0]
    assert (row["ok"], row["tokens_in"], row["tokens_out"], row["latency_ms"], row["privacy"]) == (1, 11, 7, 250, "private")
    limits = db.limits("groq:groq/q")
    assert limits[0]["window"] == "day" and limits[0]["remaining"] == 14000  # Groq: requests are per day


def test_late_success_keeps_active_blocks(h, db):
    db.set_block("gemini:gemini/g", NOW + 3600, "exhausted", "daily")
    run(h.async_log_success_event(success_kwargs(G_FAST["model_info"]), success_response(), None, None))
    assert "gemini:gemini/g" in db.active_blocks()
    db.set_block("groq:groq/q", NOW + 30, "cooldown", "rpm")
    run(h.async_log_success_event(success_kwargs(Q_FAST["model_info"]), success_response(), None, None))
    assert "groq:groq/q" in db.active_blocks()  # active cooldown is not cleared either


def test_success_clears_expired_cooldown(db):
    db.set_block("groq:groq/q", NOW - 1, "cooldown", "rpm", now=NOW - 60)
    h = MuseHook(ledger=db, clock=lambda: NOW, environ={})
    run(h.async_log_success_event(success_kwargs(Q_FAST["model_info"]), success_response(), None, None))
    import sqlite3

    assert sqlite3.connect(db.path).execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 0


def test_success_error_is_swallowed(h):
    run(h.async_log_success_event({"litellm_params": None}, None, None, None))
    run(h.async_log_success_event(success_kwargs(G_FAST["model_info"]), object(), "x", "y"))
    assert db_rows(h) == 1


def db_rows(h):
    return len(h.ledger.recent())


# ---------------------------------------------------------------- response headers

def test_response_headers(h):
    data = {**req("private"), "metadata": {**req("private")["metadata"], "muse_privacy": "private"}}
    out = run(h.async_post_call_response_headers_hook(data, None, object(), litellm_call_info={"model_info": P_CHEAP["model_info"]}))
    assert out == {"x-muse-privacy": "private", "x-muse-provider": "privcloud", "x-muse-deployment": "p__cheap",
                   "x-muse-cost-usd": "0.000000"}  # free tier: known to cost nothing
    out = run(h.async_post_call_response_headers_hook(req("bogus"), None, None))
    assert out is None  # a bad class has no header; the 400 explains it
    assert run(h.async_post_call_response_headers_hook(req(), None, None)) == {"x-muse-privacy": "standard"}


# ---------------------------------------------------------------- deployments the proxy itself is cooling


@pytest.fixture()
def router_with(monkeypatch):
    """Make the hook see a running proxy whose router holds these deployments for any group."""
    import sys

    def install(*deployments: dict) -> None:
        router = SimpleNamespace(get_model_list=lambda model_name=None: list(deployments))
        monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", SimpleNamespace(llm_router=router))

    return install


def test_cooled_deployment_with_a_quota_block_is_a_429_with_the_reset_not_a_privacy_refusal(h, db, router_with):
    # LiteLLM cooled the only deployment after its 401/429, so the hook is handed an empty list.
    router_with(Q_FAST)
    db.set_block("provider:groq", NOW + 900, "exhausted", "bad key")
    with pytest.raises(MuseBlockedError) as err:
        filt(h, [], req())
    assert "2026-11-12T07:15:00Z" in str(err.value) and "options=wait:2026-11-12T07:15:00Z" in str(err.value)
    assert not is_guardrail_intervention(err.value)  # fallbacks stay possible


def test_cooled_deployment_without_a_block_says_resting(h, router_with):
    router_with(G_FAST)
    with pytest.raises(MuseBlockedError) as err:
        filt(h, [], req())
    assert "resting after a provider error" in str(err.value) and "options=none" in str(err.value)


def test_cooled_deployment_never_weakens_privacy(h, db, router_with):
    # The cooled one may not receive a private request, so this is still a privacy refusal.
    router_with(G_FAST, Q_FAST)
    with pytest.raises(MusePrivacyError):
        filt(h, [], req("private"))
    # And a cooled deployment is never handed back to the router.
    router_with(G_FAST, Q_FAST)
    assert filt(h, [Q_FAST], req()) == [Q_FAST]


def test_cooled_block_supplies_the_wait_when_others_are_blocked_too(h, db, router_with):
    router_with(G_FAST, Q_FAST)
    db.set_block("gemini:gemini/g", NOW + 60, "cooldown", "rpm")  # cooled by the proxy as well
    db.set_block("provider:groq", NOW + 3600, "exhausted", "daily")
    with pytest.raises(MuseBlockedError) as err:
        filt(h, [Q_FAST], req())
    assert "2026-11-12T07:01:00Z" in str(err.value)  # the cooled one comes back first


def test_no_running_proxy_keeps_the_old_answer(h, monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "litellm.proxy.proxy_server", raising=False)
    with pytest.raises(MusePrivacyError):
        filt(h, [], req())
