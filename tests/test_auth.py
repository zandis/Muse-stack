"""muse.auth: who a request is. The pure rules run without LiteLLM; the glue needs it."""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from wardmuse import apps
from wardmuse.auth import AuthError, authenticate

MASTER = "sk-master-for-tests-0123456789"


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, str]:
    return {"MUSE_HOME": str(tmp_path)}


@pytest.fixture()
def tokens(env):
    _, bot = apps.add_app("bot", "interactive", environ=env)
    _, batch = apps.add_app("batch", "background", daily_requests=5, environ=env)
    return {"bot": bot, "batch": batch}


def who(token, header=None, master=MASTER, env=None):
    ident = authenticate(token, header, master_key=master, environ=env)
    return ident.app.name, ident.via


def test_master_key_is_owner(env, tokens):
    assert who(MASTER, env=env) == ("owner", "master")
    assert who(f"Bearer {MASTER}", env=env) == ("owner", "master")
    assert who(MASTER, "owner", env=env) == ("owner", "master")


def test_master_key_may_select_an_app(env, tokens):
    assert who(MASTER, "batch", env=env) == ("batch", "master")
    ident = authenticate(MASTER, "batch", master_key=MASTER, environ=env)
    assert ident.app.priority == "background" and ident.app.daily_requests == 5


def test_master_key_with_unknown_app_is_400_without_echoing_secrets(env, tokens):
    with pytest.raises(AuthError) as err:
        who(MASTER, "nope", env=env)
    assert err.value.status == 400 and "'nope'" in err.value.message
    with pytest.raises(AuthError) as err:
        who(MASTER, tokens["bot"], env=env)  # someone pasted a token into the header
    assert tokens["bot"] not in err.value.message and "'?'" in err.value.message


def test_app_token_is_its_app_and_cannot_switch(env, tokens):
    assert who(tokens["batch"], env=env) == ("batch", "app")
    assert who(tokens["batch"], "batch", env=env) == ("batch", "app")
    with pytest.raises(AuthError) as err:
        who(tokens["batch"], "bot", env=env)
    assert err.value.status == 403 and tokens["batch"] not in err.value.message
    with pytest.raises(AuthError):
        who(tokens["batch"], "owner", env=env)


@pytest.mark.parametrize("token", ["sk-unknown", "sk-muse-app-forged", MASTER + "x", MASTER[:-1]])
def test_unknown_token_is_401(env, tokens, token):
    with pytest.raises(AuthError) as err:
        who(token, env=env)
    assert err.value.status == 401 and token not in err.value.message and err.value.message.startswith("[muse]")


def test_missing_token_is_401_with_a_master_key(env, tokens):
    for token in (None, "", "Bearer "):
        with pytest.raises(AuthError) as err:
            who(token, env=env)
        assert err.value.status == 401 and "missing" in err.value.message


def test_no_master_key_is_local_dev(env):
    assert who(None, master=None, env=env) == ("anonymous", "anonymous")
    assert who("anything", master=None, env=env) == ("anonymous", "anonymous")
    assert who("anything", "bot", master=None, env=env) == ("anonymous", "anonymous")  # no authority to select
    ident = authenticate(None, None, master_key=None, environ=env)
    assert ident.app.priority == "normal" and not ident.app.allow_paid


def test_no_master_key_still_honours_app_tokens(env, tokens):
    assert who(tokens["batch"], master=None, env=env) == ("batch", "app")


def test_removed_app_token_stops_working(env, tokens):
    apps.remove_app("batch", env)
    with pytest.raises(AuthError):
        who(tokens["batch"], env=env)


# ---------------------------------------------------------------- LiteLLM glue

def request(path="/v1/chat/completions", headers=None):
    return SimpleNamespace(url=SimpleNamespace(path=path), headers=headers or {})


@pytest.fixture()
def glue(env, monkeypatch):
    pytest.importorskip("litellm")
    from wardmuse import auth

    monkeypatch.setenv("MUSE_HOME", envget(env, "MUSE_HOME"))
    monkeypatch.setattr(auth, "_master_key", lambda: MASTER)
    return auth


def test_glue_owner_is_admin_with_policy(glue):
    from litellm.proxy._types import LitellmUserRoles

    out = asyncio.run(glue.user_api_key_auth(request(), f"Bearer {MASTER}"))
    assert out.user_role == LitellmUserRoles.PROXY_ADMIN
    assert out.metadata["muse_app"] == "owner" and out.metadata["muse_priority"] == "interactive"
    assert MASTER not in str(out.model_dump())


def test_glue_app_token(glue, tokens):
    from litellm.proxy._types import LitellmUserRoles

    out = asyncio.run(glue.user_api_key_auth(request(), tokens["batch"]))
    assert out.user_role == LitellmUserRoles.INTERNAL_USER and out.metadata["muse_app"] == "batch"
    assert out.metadata["muse_allow_paid"] is False and out.metadata["muse_daily_requests"] == 5
    assert tokens["batch"] not in str(out.model_dump())


def test_glue_app_token_only_model_routes(glue, tokens):
    from fastapi import HTTPException

    for route in ("/v1/models", "/models", "/model/info", "/v1/embeddings", "/chat/completions"):
        asyncio.run(glue.user_api_key_auth(request(route), tokens["batch"]))
    for route in ("/key/generate", "/config/update", "/user/new"):
        with pytest.raises(HTTPException) as err:
            asyncio.run(glue.user_api_key_auth(request(route), tokens["batch"]))
        assert err.value.status_code == 403


def test_glue_unknown_and_public(glue):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as err:
        asyncio.run(glue.user_api_key_auth(request(), "sk-nope-123"))
    assert err.value.status_code == 401 and "sk-nope-123" not in str(err.value.detail)
    out = asyncio.run(glue.user_api_key_auth(request("/health/liveliness"), ""))
    assert out.metadata in ({}, None) or "muse_app" not in out.metadata


def test_glue_header_selects_app(glue, tokens):
    out = asyncio.run(glue.user_api_key_auth(request(headers={"x-muse-app": "batch"}), MASTER))
    assert out.metadata["muse_app"] == "batch"
