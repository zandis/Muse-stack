"""The home control API, call by call: the happy path of every endpoint and its main failures.

Everything goes through a real server on a Unix socket with a real `Home` on the
test's private MUSE_HOME. No network: the proxy check is injected, the key store
is the dotenv file in tmp.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import logging
import os
import stat
from pathlib import Path

import pytest

from home_fakes import DEVICE, TOKEN, assert_error, card_schema, client_for, running, schema_errors
from wardmuse import apps, keys, queue
from wardmuse.home import api

# Canaries: each must never appear in a reply or a log line (the app tokens only in the one reply that issues them).
KEY_CANARY = "gsk_CANARYkey0123456789abcdefABCDEF"
ALSO_CANARY = "CANARY-ACCOUNT-ID-5d2a90"
PROMPT_CANARY = "CANARY-PROMPT-91c2f0 summarise my private notes"
ERR_CANARY = "CANARY-ERRTEXT-b81e47"


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.env_file = tmp_path / "keys.env"
        monkeypatch.setenv("MUSE_ENV_FILE", str(self.env_file))
        monkeypatch.setenv("MUSE_TZ", "UTC")
        monkeypatch.setattr(keys, "_keyring", lambda: None)
        for name in list(keys.KNOWN_KEYS) + ["MUSE_DEFAULT_PRIVACY", "MUSE_DISABLE_PROVIDERS", "MUSE_APPS_FILE", "MUSE_ROUTING"]:
            monkeypatch.delenv(name, raising=False)
        self.proxy_up = False
        self.home = api.Home(proxy_running=lambda: self.proxy_up)
        self.replies: list[str] = []
        self._ctx = running(self.home)
        self.srv = self._ctx.__enter__()
        self.client = client_for(self.srv)

    def close(self) -> None:
        self._ctx.__exit__(None, None, None)

    def call(self, method: str, path: str, body=None):
        status, payload, headers = self.client.request(method, path, body)
        self.replies.append(json.dumps(payload))
        self.last_headers = headers
        return status, payload

    def ok(self, method: str, path: str, body=None, status: int = 200):
        got, payload = self.call(method, path, body)
        assert got == status, (got, payload)
        return payload

    def fails(self, method: str, path: str, body, status: int, code: str):
        got, payload = self.call(method, path, body)
        return assert_error(got, payload, status, code)

    def card(self, **over) -> str:
        args = dict(kind="save_file", title="Save a file", thing="Weekly digest 13 Nov.md", where="Documents/Muse",
                    under="Weekly digest chore")
        args.update(over)
        return self.home.cards.create(**args)


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.close()


# ---------------------------------------------------------------- status, pause


def test_status_is_the_snapshot_plus_api_version(rig):
    from wardmuse.status import build_snapshot

    snap = rig.ok("GET", "/v1/status")
    direct = build_snapshot()
    assert snap["api_version"] == "1" and snap["paused"] is False
    # The snapshot's own shape is unchanged; the app's handler adds four fields beside it (muse/home/app_api.py).
    assert set(snap) == set(direct) | {"api_version", "money", "activity", "local_runtime", "restart_pending"}
    for key in ("providers", "aliases", "apps", "paid", "queue", "suggestions", "resources", "subscriptions", "totals"):
        assert snap[key] == json.loads(json.dumps(direct[key])), key
    assert rig.last_headers["content-type"] == "application/json; charset=utf-8"


def test_status_uses_the_settings_in_the_env_file_as_muse_status_does_and_never_holds_the_keys(rig, monkeypatch):
    from wardmuse.status import build_snapshot

    monkeypatch.delenv("MUSE_DAILY_PAID_CAP_USD", raising=False)
    rig.env_file.write_text(
        "MUSE_DEFAULT_PRIVACY=private\nMUSE_DAILY_PAID_CAP_USD=2.50\nMUSE_TZ=Asia/Taipei\nOLLAMA_API_BASE=http://127.0.0.1:11434\n"
        f"GROQ_API_KEY={KEY_CANARY}\nMUSE_QUEUE_APP_TOKEN=sk-muse-app-CANARYtoken0123456789abcdef\nMY_SERVICE_SECRET=hunter2\n"
        f"SOME_OTHER_VALUE={KEY_CANARY}2\n", encoding="utf-8")
    snap = rig.ok("GET", "/v1/status")
    merged = build_snapshot(environ=keys.export_env())  # what `muse status` builds, from the environment that holds the keys
    for key in ("providers", "aliases", "paid", "suggestions"):
        assert snap[key] == json.loads(json.dumps(merged[key])), key
    assert snap["paid"]["cap_usd"] == 2.5  # from the file; the process environment has no cap set
    assert next(p for p in snap["providers"] if p["id"] == "groq")["configured"] is True  # known by name, never by value
    assert next(p for p in snap["providers"] if p["id"] == "ollama")["configured"] is True  # a local model's address is a setting
    assert KEY_CANARY not in json.dumps(snap) and "CANARY" not in json.dumps(snap)
    settings = rig.home.settings_env()
    assert settings["MUSE_DEFAULT_PRIVACY"] == "private" and settings["MUSE_DAILY_PAID_CAP_USD"] == "2.50"
    assert settings["OLLAMA_API_BASE"] == "http://127.0.0.1:11434"
    assert settings["MUSE_TZ"] == "UTC"  # the process's own value wins, as in keys.export_env
    for name in ("GROQ_API_KEY", "MUSE_QUEUE_APP_TOKEN", "MY_SERVICE_SECRET", "SOME_OTHER_VALUE"):
        assert name not in settings, name  # a named key, a name that says secret, a value shaped like a key
    assert "CANARY" not in json.dumps(settings) and "hunter2" not in json.dumps(settings)
    assert not any(isinstance(v, (str, dict)) and "CANARY" in json.dumps(v) for v in vars(rig.home).values())  # nothing kept on the home
    # A job queued without a privacy class takes the file's default.
    assert rig.ok("POST", "/v1/jobs", {"prompt": "x", "alias": "cheap"}, status=201)["job"]["privacy"] == "private"
    # A skill run's environment has the settings and no provider key.
    run_env = rig.home.run_env()
    assert run_env["MUSE_DEFAULT_PRIVACY"] == "private" and "GROQ_API_KEY" not in run_env and "MUSE_QUEUE_APP_TOKEN" not in run_env


def test_status_failure_is_a_plain_503_without_the_exception(rig, caplog):
    def boom():
        raise RuntimeError(f"cannot open ledger {ERR_CANARY}")

    rig.home._snapshot_fn = boom
    with caplog.at_level(logging.DEBUG):
        err = rig.fails("GET", "/v1/status", None, 503, "unavailable")
    assert err["retry_after"] == 5 and rig.last_headers["retry-after"] == "5"
    assert ERR_CANARY not in json.dumps(err) and ERR_CANARY not in caplog.text and "RuntimeError" in caplog.text


def test_pause_and_resume_are_the_same_switch_as_the_cli(rig):
    assert rig.ok("POST", "/v1/pause") == {"paused": True, "recorded": True}
    assert queue.is_paused() and (Path(envget(os.environ, "MUSE_HOME")) / "paused").exists()
    assert rig.ok("GET", "/v1/status")["paused"] is True
    assert rig.ok("POST", "/v1/pause") == {"paused": True, "recorded": True}  # idempotent
    assert rig.ok("POST", "/v1/resume") == {"paused": False, "was_paused": True, "recorded": True}
    assert not queue.is_paused() and rig.ok("GET", "/v1/status")["paused"] is False
    assert rig.ok("POST", "/v1/resume") == {"paused": False, "was_paused": False, "recorded": False}  # nothing happened
    queue.set_paused()  # `muse pause` in a terminal shows up here too
    assert rig.ok("GET", "/v1/status")["paused"] is True
    rig.fails("POST", "/v1/pause", {"why": "x"}, 400, "invalid_request")
    rig.fails("GET", "/v1/pause", None, 405, "method_not_allowed")


# ---------------------------------------------------------------- catalogue, connect, keys


def test_catalogue_lists_every_service_with_connection_state(rig):
    from wardmuse import catalog

    cat = rig.ok("GET", "/v1/catalogue")
    real = catalog.load_catalog()
    assert [p["id"] for p in cat["providers"]] == [p.id for p in real.providers]
    assert cat["connected"] == [] and not any(p["connected"] for p in cat["providers"])
    gemini = next(p for p in cat["providers"] if p["id"] == "gemini")
    assert gemini["key_env"] == "GEMINI_API_KEY" and gemini["connection"]["method"] == "paste_key"
    assert gemini["connection"]["key_url"].startswith("https://") and gemini["connection"]["steps"] >= 1
    assert set(gemini["gain_text"]) == {"en", "zh-TW"}
    assert [s["id"] for s in cat["suggestions"]] == [s.provider_id for s in catalog.suggest_next(set(), os.environ, providers=real.providers)]
    assert len(cat["resources"]) == len(real.resources) and len(cat["subscriptions"]) == len(real.subscriptions)
    assert any(p["local"] for p in cat["providers"])
    rig.fails("GET", "/v1/catalogue?lang=en", None, 400, "invalid_request")


def test_connect_returns_the_catalogues_steps_and_opens_nothing(rig, monkeypatch):
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: pytest.fail("the server must not open a browser"))
    from wardmuse import catalog

    real = next(p for p in catalog.load_catalog().providers if p.id == "gemini")
    got = rig.ok("POST", "/v1/connect/gemini")
    assert got["provider"] == "gemini" and got["name"] == real.name and got["connected"] is False
    assert got["method"] == "paste_key" and got["finish_with"] == "paste_key" and got["open_url"] == real.connection.key_url
    assert [s["id"] for s in got["steps"]] == [s.id for s in real.connection.steps]
    assert got["steps"][0]["title"] == dict(real.connection.steps[0].title) and set(got["steps"][0]["detail"]) == {"en", "zh-TW"}
    assert got["key"] == {"name": "GEMINI_API_KEY", "starts_with": "AIza"}
    assert got["next"] == {"method": "POST", "path": "/v1/keys/gemini"}
    one_tap = rig.ok("POST", "/v1/connect/openrouter")
    assert one_tap["method"] == "oauth_pkce" and one_tap["finish_with"] == "paste_key" and one_tap["open_url"]
    cf = rig.ok("POST", "/v1/connect/cloudflare")
    assert cf["also"] == [{"name": n, "configured": False} for n in next(p for p in catalog.load_catalog().providers if p.id == "cloudflare").also_env]
    assert cf["also"]


def test_connect_failures(rig):
    rig.fails("POST", "/v1/connect/nosuchservice", None, 404, "not_found")
    rig.fails("POST", "/v1/connect/ollama", None, 400, "invalid_request")  # local: no key to connect
    rig.fails("POST", "/v1/connect/gemini", {"open": True}, 400, "invalid_request")
    rig.fails("GET", "/v1/connect/gemini", None, 405, "method_not_allowed")
    rig.fails("POST", "/v1/connect/Gemini%20x", None, 404, "not_found")


def test_a_key_is_stored_through_the_key_store_and_never_echoed(rig, caplog):
    rig.proxy_up = True
    with caplog.at_level(logging.DEBUG):
        got = rig.ok("POST", "/v1/keys/groq", {"key": f"  {KEY_CANARY}\n"})
    assert got == {
        "provider": "groq", "stored": ["GROQ_API_KEY"], "backend": "dotenv", "warnings": [], "still_needed": [],
        "restart": {"needed": True, "proxy_running": True, "command": "muse restart", "managed": False, "next": None},
        "recorded": True,  # the app's wrapper writes no daily limit: the router's default is the home's
    }
    assert KEY_CANARY not in json.dumps(got) and KEY_CANARY not in caplog.text
    assert f"GROQ_API_KEY={KEY_CANARY}" in rig.env_file.read_text()  # cleaned of the stray space and newline
    assert stat.S_IMODE(rig.env_file.stat().st_mode) == 0o600
    assert keys.is_configured("GROQ_API_KEY")
    cat = rig.ok("GET", "/v1/catalogue")
    assert cat["connected"] == ["groq"] and "groq" not in [s["id"] for s in cat["suggestions"]]
    assert rig.ok("POST", "/v1/connect/groq")["connected"] is True
    snap = rig.ok("GET", "/v1/status")
    assert next(p for p in snap["providers"] if p["id"] == "groq")["configured"] is True
    assert KEY_CANARY not in "".join(rig.replies)


def test_a_key_of_an_unexpected_shape_is_stored_with_a_warning_that_does_not_quote_it(rig):
    odd = "CANARYoddshape0123456789"
    got = rig.ok("POST", "/v1/keys/groq", {"key": odd})
    assert got["stored"] == ["GROQ_API_KEY"] and len(got["warnings"]) == 1 and "gsk_" in got["warnings"][0]
    assert odd not in json.dumps(got)
    assert got["restart"] == {"needed": True, "proxy_running": False, "command": "muse up", "managed": False, "next": None}


def test_extra_settings_travel_in_also(rig):
    from wardmuse import catalog

    cf = next(p for p in catalog.load_catalog().providers if p.id == "cloudflare")
    extra = cf.also_env[0]
    first = rig.ok("POST", "/v1/keys/cloudflare", {"key": KEY_CANARY})
    assert first["stored"] == [cf.env] and first["still_needed"] == list(cf.also_env)
    assert rig.ok("GET", "/v1/catalogue")["connected"] == []  # not connected until the account id is there too
    both = rig.ok("POST", "/v1/keys/cloudflare", {"key": KEY_CANARY, "also": {extra: ALSO_CANARY}})
    assert both["stored"] == [cf.env, extra] and both["still_needed"] == []
    assert f"{extra}={ALSO_CANARY}" in rig.env_file.read_text()
    assert rig.ok("GET", "/v1/catalogue")["connected"] == ["cloudflare"]
    assert KEY_CANARY not in "".join(rig.replies) and ALSO_CANARY not in "".join(rig.replies)


@pytest.mark.parametrize("path, body, status, code", [
    ("/v1/keys/groq", {}, 400, "invalid_request"),
    ("/v1/keys/groq", {"key": ""}, 400, "invalid_request"),
    ("/v1/keys/groq", {"key": "   "}, 400, "invalid_request"),
    ("/v1/keys/groq", {"key": 12345}, 400, "invalid_request"),
    ("/v1/keys/groq", {"key": f"{KEY_CANARY}\nSECOND=line"}, 400, "invalid_request"),
    ("/v1/keys/groq", {"key": f"{KEY_CANARY} with spaces"}, 400, "invalid_request"),
    ("/v1/keys/groq", {"key": "k" * 513}, 400, "invalid_request"),
    ("/v1/keys/groq", {"key": KEY_CANARY, "note": "mine"}, 400, "invalid_request"),
    ("/v1/keys/groq", {"key": KEY_CANARY, "also": {"LITELLM_MASTER_KEY": "x"}}, 400, "invalid_request"),
    ("/v1/keys/groq", {"key": KEY_CANARY, "also": {"PATH": "/tmp"}}, 400, "invalid_request"),
    ("/v1/keys/groq", {"key": KEY_CANARY, "also": [1]}, 400, "invalid_request"),
    ("/v1/keys/nosuchservice", {"key": KEY_CANARY}, 404, "not_found"),
    ("/v1/keys/ollama", {"key": KEY_CANARY}, 400, "invalid_request"),
    ("/v1/keys/LITELLM_MASTER_KEY", {"key": KEY_CANARY}, 404, "not_found"),
])
def test_key_failures_store_nothing_and_never_repeat_the_key(rig, path, body, status, code):
    err = rig.fails("POST", path, body, status, code)
    assert "CANARY" not in json.dumps(err)
    assert not rig.env_file.exists()


def test_a_key_store_failure_is_reported_without_its_text(rig, monkeypatch, caplog):
    def boom(name, value, **kw):
        raise keys.KeysError(f"keychain said no about {value} {ERR_CANARY}")

    monkeypatch.setattr(keys, "set_key", boom)
    with caplog.at_level(logging.DEBUG):
        err = rig.fails("POST", "/v1/keys/groq", {"key": KEY_CANARY}, 503, "unavailable")
    for canary in (KEY_CANARY, ERR_CANARY):
        assert canary not in json.dumps(err) and canary not in caplog.text


# ---------------------------------------------------------------- jobs


def test_a_job_is_queued_listed_shown_and_cancelled_without_its_prompt(rig):
    made = rig.ok("POST", "/v1/jobs", {"prompt": PROMPT_CANARY}, status=201)["job"]
    job = queue.Queue().get(made["id"])
    assert job.payload == PROMPT_CANARY and job.kind == "prompt" and job.status == "queued"  # accepted and kept on the home
    assert (job.task, job.alias) == ("light", "auto")  # the routing rules' default for the queue app
    assert job.allow_paid is False and job.privacy is None
    assert made == job.public() and "payload" not in made and "prompt" not in made
    assert made["out_path"].startswith(envget(os.environ, "MUSE_HOME"))  # results stay in the home's own folder
    assert rig.ok("GET", "/v1/jobs") == {"jobs": [made]}
    assert rig.ok("GET", f"/v1/jobs/{made['id']}") == {"job": made}
    assert rig.ok("GET", "/v1/jobs?status=running,done") == {"jobs": []}
    assert rig.ok("GET", "/v1/jobs?status=queued")["jobs"][0]["id"] == made["id"]
    gone = rig.ok("DELETE", f"/v1/jobs/{made['id']}")["job"]
    assert gone["status"] == "cancelled" and queue.Queue().get(made["id"]).payload == ""  # the prompt is erased
    rig.fails("DELETE", f"/v1/jobs/{made['id']}", None, 409, "conflict")
    assert PROMPT_CANARY not in "".join(rig.replies) and "CANARY" not in "".join(rig.replies)


def test_job_options_are_passed_to_the_queue(rig, monkeypatch):
    made = rig.ok("POST", "/v1/jobs", {
        "prompt": "p", "task": "Everyday", "privacy": "private", "allow_paid": True, "max_tokens": 77, "max_attempts": 2,
        "deadline": "2099-01-01T00:00:00Z",
    }, status=201)["job"]
    assert (made["task"], made["alias"], made["privacy"], made["allow_paid"]) == ("everyday", "auto", "private", True)
    assert (made["max_tokens"], made["max_attempts"], made["deadline"]) == (77, 2, "2099-01-01T00:00:00Z")
    by_alias = rig.ok("POST", "/v1/jobs", {"prompt": "p", "alias": "cheap"}, status=201)["job"]
    assert (by_alias["task"], by_alias["alias"]) == (None, "cheap")
    monkeypatch.setenv("MUSE_DEFAULT_PRIVACY", "local")
    assert rig.ok("POST", "/v1/jobs", {"prompt": "p"}, status=201)["job"]["privacy"] == "local"
    batch = rig.ok("POST", "/v1/jobs", {"kind": "batch", "prompts": [PROMPT_CANARY, " two "]}, status=201)["job"]
    assert batch["kind"] == "batch" and json.loads(queue.Queue().get(batch["id"]).payload) == [PROMPT_CANARY, "two"]
    assert "CANARY" not in "".join(rig.replies)


@pytest.mark.parametrize("body", [
    {}, {"prompt": ""}, {"prompt": "  \n"}, {"prompt": 5}, {"prompt": "x" * 100_001}, {"kind": "file-summary", "prompt": "x"},
    {"kind": "shell", "prompt": "x"}, {"prompt": "x", "prompts": ["y"]}, {"kind": "batch", "prompt": "x"},
    {"kind": "batch", "prompts": []}, {"kind": "batch", "prompts": ["x"] * 21}, {"kind": "batch", "prompts": ["x", 3]},
    {"prompt": "x", "privacy": "secret"}, {"prompt": "x", "allow_paid": "yes"}, {"prompt": "x", "max_tokens": 0},
    {"prompt": "x", "max_tokens": True}, {"prompt": "x", "max_attempts": "3"}, {"prompt": "x", "alias": "no spaces allowed"},
    {"prompt": "x", "task": "../../etc"}, {"prompt": "x", "deadline": "2020-01-01T00:00:00Z"}, {"prompt": "x", "deadline": "tomorrow"},
    {"prompt": "x", "deadline": "2099-01-01T00:00:00"}, {"prompt": "x", "out_path": "/etc/cron.d/x"}, {"prompt": "x", "payload": "y"},
    {"prompt": "x", "instruction": "y"},
])
def test_bad_jobs_are_refused_and_nothing_is_queued(rig, body):
    rig.fails("POST", "/v1/jobs", body, 400, "invalid_request")
    assert rig.ok("GET", "/v1/jobs") == {"jobs": []}


def test_job_lookups_that_fail(rig):
    for bad in ("000000000000", "abc", "zzzzzzzzzzzz", "0123456789abcdef"):
        rig.fails("GET", f"/v1/jobs/{bad}", None, 404, "not_found")
        rig.fails("DELETE", f"/v1/jobs/{bad}", None, 404, "not_found")
    rig.fails("GET", "/v1/jobs?status=weird", None, 400, "invalid_request")
    rig.fails("GET", "/v1/jobs?limit=5", None, 400, "invalid_request")
    made = rig.ok("POST", "/v1/jobs", {"prompt": "p"}, status=201)["job"]
    rig.fails("GET", f"/v1/jobs/{made['id'][:6]}", None, 404, "not_found")  # no prefix matching over the API
    rig.fails("PATCH", f"/v1/jobs/{made['id']}", {"status": "done"}, 405, "method_not_allowed")
    assert rig.last_headers["allow"] == "DELETE, GET"


# ---------------------------------------------------------------- cards


def test_cards_are_listed_newest_first_shown_and_answered(rig):
    assert rig.ok("GET", "/v1/cards") == {"cards": [], "counts": {"pending": 0, "approved": 0, "declined": 0, "expired": 0}}
    first = rig.card(thing="one")
    second = rig.card(kind="spend", title="Pay for a service", thing="A transcript", where="Deepgram", cost={"amount": "6", "currency": "TWD"},
                      extra={"payee": "Deepgram", "limit_left": {"amount": "24", "currency": "TWD"}}, job_ref="job:7")
    listed = rig.ok("GET", "/v1/cards")
    assert [c["id"] for c in listed["cards"]] == [second, first] and listed["counts"]["pending"] == 2
    assert all(schema_errors(c, card_schema()) == [] for c in listed["cards"])
    one = rig.ok("GET", f"/v1/cards/{second}")["card"]
    assert one == listed["cards"][0] and one["cost"] == {"amount": "6", "currency": "TWD"} and one["job_ref"] == "job:7"
    answered = rig.ok("POST", f"/v1/cards/{first}/answer", {"decision": "approve", "always": True})["card"]
    assert (answered["status"], answered["answered_by"], answered["always"]) == ("approved", DEVICE, True)
    assert answered["answered_at"] and schema_errors(answered, card_schema()) == []
    assert rig.home.cards.wait(first, timeout=0) == "approved"  # what the helper will see
    declined = rig.ok("POST", f"/v1/cards/{second}/answer", {"decision": "decline"})["card"]
    assert (declined["status"], declined["always"]) == ("declined", False)
    assert [c["id"] for c in rig.ok("GET", "/v1/cards?status=pending")["cards"]] == []
    assert [c["id"] for c in rig.ok("GET", "/v1/cards?status=approved,declined")["cards"]] == [second, first]
    assert rig.ok("GET", "/v1/cards")["counts"] == {"pending": 0, "approved": 1, "declined": 1, "expired": 0}


def test_a_card_cannot_be_answered_twice(rig):
    card = rig.card()
    rig.ok("POST", f"/v1/cards/{card}/answer", {"decision": "decline"})
    for decision in ("approve", "decline"):
        rig.fails("POST", f"/v1/cards/{card}/answer", {"decision": decision}, 409, "card_already_answered")
    assert rig.ok("GET", f"/v1/cards/{card}")["card"]["status"] == "declined"


def test_an_expired_card_is_listed_as_expired_and_cannot_be_approved(rig):
    now = [1_800_000_000.0]
    rig.home.cards.clock = lambda: now[0]
    card = rig.card(kind="spend", cost={"amount": "1", "currency": "USD"}, extra={"payee": "X", "limit_left": {"amount": "1", "currency": "USD"}})
    assert rig.ok("GET", f"/v1/cards/{card}")["card"]["expires_at"] == "2027-01-15T08:05:00Z"
    now[0] += 301
    rig.fails("POST", f"/v1/cards/{card}/answer", {"decision": "approve"}, 410, "card_expired")
    got = rig.ok("GET", f"/v1/cards?status=expired")["cards"]
    assert [c["id"] for c in got] == [card] and got[0]["answered_by"] is None
    assert rig.ok("GET", "/v1/cards?status=pending")["cards"] == []


@pytest.mark.parametrize("body", [
    {}, {"decision": "maybe"}, {"decision": "approve", "always": "yes"}, {"decision": "decline", "always": True},
    {"decision": "approve", "device": "someone-else"}, {"decision": "approve", "answered_by": "x"}, {"decision": True},
])
def test_bad_answers_change_nothing(rig, body):
    card = rig.card()
    rig.fails("POST", f"/v1/cards/{card}/answer", body, 400, "invalid_request")
    assert rig.ok("GET", f"/v1/cards/{card}")["card"]["status"] == "pending"


def test_card_lookups_that_fail(rig):  # creating a card over the API: see tests/test_mcp_home_cards.py
    for bad in ("0" * 16, "nope", "0" * 12):
        rig.fails("GET", f"/v1/cards/{bad}", None, 404, "not_found")
        rig.fails("POST", f"/v1/cards/{bad}/answer", {"decision": "approve"}, 404, "not_found")
    rig.fails("GET", "/v1/cards?status=open", None, 400, "invalid_request")
    rig.fails("PUT", "/v1/cards", {"kind": "save_file", "title": "x", "thing": "y"}, 405, "method_not_allowed")
    card = rig.card()
    rig.fails("DELETE", f"/v1/cards/{card}", None, 405, "method_not_allowed")
    rig.fails("GET", f"/v1/cards/{card}/answer", None, 405, "method_not_allowed")


def test_card_text_comes_back_exactly_as_stored(rig):
    risky = '<script>alert("x")</script> & 中文'
    card = rig.card(thing=risky, where=risky)
    got = rig.ok("GET", f"/v1/cards/{card}")["card"]
    assert got["thing"] == risky and got["where"] == risky


# ---------------------------------------------------------------- apps


def test_apps_are_created_changed_rotated_and_removed(rig):
    assert rig.ok("GET", "/v1/apps") == {"apps": []}
    made = rig.ok("POST", "/v1/apps", {"name": "notes-batch", "priority": "background", "daily_requests": 500, "note": "nightly"}, status=201)
    token = made["token"]
    assert made["app"] == {"name": "notes-batch", "priority": "background", "daily_requests": 500, "daily_tokens": None,
                           "allow_paid": False, "default_privacy": None, "note": "nightly", "created_at": made["app"]["created_at"]}
    assert made["token_shown_once"] is True and token.startswith("sk-muse-app-")
    assert apps.find_by_token(token).name == "notes-batch"  # it is the router's own registry
    listed = rig.ok("GET", "/v1/apps")
    assert listed == {"apps": [made["app"]]}
    changed = rig.ok("PATCH", "/v1/apps/notes-batch", {"priority": "normal", "daily_requests": None, "daily_tokens": 9000,
                                                       "default_privacy": "private", "allow_paid": True, "note": None})["app"]
    assert changed == {**made["app"], "priority": "normal", "daily_requests": None, "daily_tokens": 9000, "default_privacy": "private",
                       "allow_paid": True, "note": None}
    assert rig.ok("PATCH", "/v1/apps/notes-batch", {"allow_paid": None})["app"]["allow_paid"] is True  # normal's default
    rotated = rig.ok("POST", "/v1/apps/notes-batch/rotate")
    assert rotated["token"] != token and rotated["app"]["name"] == "notes-batch"
    assert apps.find_by_token(token) is None and apps.find_by_token(rotated["token"]).name == "notes-batch"
    assert rig.ok("DELETE", "/v1/apps/notes-batch") == {"removed": True, "recorded": True}
    assert rig.ok("GET", "/v1/apps") == {"apps": []} and apps.find_by_token(rotated["token"]) is None
    # A token appears in exactly the reply that issued it, and no hash appears anywhere.
    everything = "".join(rig.replies)
    assert everything.count(token) == 1 and everything.count(rotated["token"]) == 1
    assert "token_sha256" not in everything and apps.hash_token(token) not in everything


@pytest.mark.parametrize("body", [
    {}, {"name": "x"}, {"priority": "normal"}, {"name": "Bad Name", "priority": "normal"}, {"name": "../x", "priority": "normal"},
    {"name": "x", "priority": "urgent"}, {"name": "x", "priority": "normal", "daily_requests": -1},
    {"name": "x", "priority": "normal", "daily_tokens": "many"}, {"name": "x", "priority": "normal", "daily_tokens": True},
    {"name": "x", "priority": "normal", "allow_paid": "yes"}, {"name": "x", "priority": "normal", "default_privacy": "secret"},
    {"name": "x", "priority": "normal", "note": "two\nlines"}, {"name": "x", "priority": "normal", "note": "n" * 201},
    {"name": "x", "priority": "normal", "token": "mine"}, {"name": "x", "priority": "normal", "token_sha256": "a" * 64},
    {"name": "owner", "priority": "normal"}, {"name": "anonymous", "priority": "normal"},
])
def test_bad_apps_are_refused_and_nothing_is_created(rig, body):
    rig.fails("POST", "/v1/apps", body, 400, "invalid_request")
    assert rig.ok("GET", "/v1/apps") == {"apps": []}
    assert not apps.apps_path().exists()


def test_app_failures(rig):
    rig.ok("POST", "/v1/apps", {"name": "one", "priority": "normal"}, status=201)
    rig.fails("POST", "/v1/apps", {"name": "one", "priority": "background"}, 409, "conflict")
    for method, path, body in [("PATCH", "/v1/apps/two", {"priority": "normal"}), ("DELETE", "/v1/apps/two", None),
                               ("POST", "/v1/apps/two/rotate", None), ("PATCH", "/v1/apps/Bad..Name", {"priority": "normal"})]:
        rig.fails(method, path, body, 404, "not_found")
    for body in ({}, {"name": "renamed"}, {"priority": "urgent"}, {"daily_requests": -3}, {"token_sha256": "a" * 64}, {"created_at": "x"}):
        rig.fails("PATCH", "/v1/apps/one", body, 400, "invalid_request")
    rig.fails("POST", "/v1/apps/one/rotate", {"token": "mine"}, 400, "invalid_request")
    rig.fails("PUT", "/v1/apps/one", {"priority": "normal"}, 405, "method_not_allowed")
    assert rig.ok("GET", "/v1/apps")["apps"][0]["priority"] == "normal"


def test_a_malformed_registry_is_reported_and_never_overwritten(rig):
    apps.apps_path().write_text("{not yaml: [")
    rig.fails("POST", "/v1/apps", {"name": "one", "priority": "normal"}, 503, "unavailable")
    rig.fails("DELETE", "/v1/apps/one", None, 503, "unavailable")
    assert apps.apps_path().read_text() == "{not yaml: ["


# ---------------------------------------------------------------- planned, and the error shape


def test_the_permissions_list_answers_or_says_the_library_is_missing(rig):
    from wardmuse import permissions as P

    if P.available():
        assert rig.ok("GET", "/v1/mandates/artifacts")["mandates"] == []
    else:
        err = rig.fails("GET", "/v1/mandates/artifacts", None, 503, "unavailable")
        assert set(err) == {"code", "message"}


def test_requests_that_cannot_be_understood(rig):
    rig.fails("GET", "/v1/nothing", None, 404, "not_found")
    rig.fails("GET", "/v1", None, 404, "not_found")
    rig.fails("POST", "/v1/status", None, 405, "method_not_allowed")
    assert rig.last_headers["allow"] == "GET"
    rig.fails("POST", "/v1/jobs", b"{not json", 400, "invalid_request")
    rig.fails("POST", "/v1/jobs", b'["a", "list"]', 400, "invalid_request")
    rig.fails("POST", "/v1/jobs", b'"just text"', 400, "invalid_request")
    rig.fails("POST", "/v1/jobs", b"\xff\xfe", 400, "invalid_request")
    rig.fails("POST", "/v1/jobs", ("[" * 5000).encode(), 400, "invalid_request")
    rig.fails("POST", "/v1/jobs", json.dumps({"prompt": "x", "task": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}}).encode(), 400, "invalid_request")
    rig.fails("GET", "/v1/status?demo=1", None, 400, "invalid_request")
    rig.fails("GET", "/v1/cards?status=pending&status=approved", None, 400, "invalid_request")
    status, payload, _ = rig.client.request("GET", "/v1/jobs", b'{"status": "queued"}')
    assert_error(status, payload, 400, "invalid_request")  # a GET takes no body
    assert rig.ok("GET", "/v1/jobs") == {"jobs": []}


def test_an_unexpected_failure_is_a_plain_500(rig, monkeypatch, caplog):
    def boom(self, *a, **k):
        raise RuntimeError(f"disk on fire {ERR_CANARY} {PROMPT_CANARY}")

    monkeypatch.setattr(queue.Queue, "list", boom)
    with caplog.at_level(logging.DEBUG):
        err = rig.fails("GET", "/v1/jobs", None, 500, "internal")
    assert "CANARY" not in json.dumps(err) and "CANARY" not in caplog.text and "RuntimeError" in caplog.text
    assert rig.ok("GET", "/v1/apps") == {"apps": []}  # the server carries on


def test_error_codes_and_statuses_are_one_table():
    assert api.ERROR_CODES == {
        "invalid_request": 400, "unauthorized": 401, "forbidden": 403, "not_found": 404, "method_not_allowed": 405, "conflict": 409,
        "card_already_answered": 409, "card_expired": 410, "length_required": 411, "payload_too_large": 413,
        "too_many_requests": 429, "internal": 500,
        "not_built": 501, "unavailable": 503,
    }
    err = api.ApiError("unavailable", "Wait a little.", retry_after=7)
    assert err.payload() == {"error": {"code": "unavailable", "message": "Wait a little.", "retry_after": 7}}
    assert api.error_response(err).headers == {"Retry-After": "7"}


# ---------------------------------------------------------------- key-shaped input, and leaks

SHAPED = [
    "sk-" + "a1B2" * 8, "gsk_" + "a1B2" * 8, "AIza" + "Sy0" * 9, "sk-muse-app-" + "Q7x" * 12, "sk-or-v1-" + "0aF" * 16,
    "Bearer " + "abcDEF123" * 3, "nvapi-" + "a1B2" * 8, "ghp_" + "a1B2c3D4" * 4, "q9Xw2Lr7Zt4Vb8Nm1Kp6Js3Hd5Gf0Ay2c",
]


def shaped_requests(key: str, card: str):
    return [
        ("POST", "/v1/jobs", {"prompt": f"use {key} to log in"}),
        ("POST", "/v1/jobs", {"kind": "batch", "prompts": ["fine", key]}),
        ("POST", "/v1/jobs", {"prompt": "fine", "task": key}),
        ("POST", "/v1/apps", {"name": "app-one", "priority": "normal", "note": key}),
        ("PATCH", "/v1/apps/existing", {"note": key}),
        ("POST", f"/v1/cards/{card}/answer", {"decision": "approve", "comment": key}),
        ("POST", f"/v1/cards/{card}/answer", {"decision": key}),
        ("POST", "/v1/pause", {"key": key}),
        ("POST", "/v1/resume", {"token": key}),
        ("POST", "/v1/connect/groq", {"key": key}),
        ("POST", "/v1/apps/existing/rotate", {"token": key}),
        ("POST", "/v1/jobs", {key: "as a field name"}),
        ("POST", "/v1/jobs", {"prompt": "x", "nested": {"deep": [{"k": key}]}}),
    ]


@pytest.mark.parametrize("key", SHAPED)
def test_key_shaped_input_is_refused_everywhere_but_the_keys_call(rig, key, caplog):
    rig.ok("POST", "/v1/apps", {"name": "existing", "priority": "normal"}, status=201)
    card = rig.card()
    with caplog.at_level(logging.DEBUG):
        for method, path, body in shaped_requests(key, card):
            err = rig.fails(method, path, body, 400, "invalid_request")
            assert "looks like a key or token" in err["message"], (path, body)
            assert key not in json.dumps(err)
        from urllib.parse import quote

        for path in (f"/v1/cards?status={quote(key)}", f"/v1/jobs?status={quote(key)}", f"/v1/receipts?since={quote(key)}"):
            err = rig.fails("GET", path, None, 400, "invalid_request")
            assert key not in json.dumps(err)
    assert key not in caplog.text
    # Nothing was acted on.
    assert rig.ok("GET", "/v1/jobs") == {"jobs": []}
    assert [a["name"] for a in rig.ok("GET", "/v1/apps")["apps"]] == ["existing"]
    assert rig.ok("GET", f"/v1/cards/{card}")["card"]["status"] == "pending"
    assert not rig.home.is_paused() and not rig.env_file.exists()
    for name in ("queue.sqlite", "cards.sqlite", "apps.yaml"):
        path = Path(envget(os.environ, "MUSE_HOME")) / name
        assert not path.exists() or key.encode() not in path.read_bytes()


def test_the_refusal_is_the_same_whatever_the_field(rig):
    card = rig.card()
    messages = {json.dumps(rig.call(m, p, b)[1]) for m, p, b in shaped_requests(SHAPED[0], card)}
    assert len(messages) == 1  # generic: it does not say which field


def test_only_the_keys_call_accepts_a_key(rig):
    for key in SHAPED[:3]:
        assert rig.ok("POST", "/v1/keys/groq", {"key": key})["stored"] == ["GROQ_API_KEY"]
    assert all(k not in "".join(rig.replies) for k in SHAPED)


def test_plain_text_that_is_not_a_key_is_let_through(rig):
    for prompt in ("Summarise https://example.com/a/very/long/path/with/segments/0123456789abcdefABCDEF?x=1 please",
                   "A long sentence with numbers 1234567890 and CAPITALS is still just a sentence.",
                   "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"):
        rig.ok("POST", "/v1/jobs", {"prompt": prompt}, status=201)


def test_no_secret_or_prompt_in_any_reply_or_log_line(rig, caplog, capfd):
    """One session touching every call, with canaries planted wherever content or secrets go in."""
    rig.proxy_up = True
    issued: list[str] = []
    with caplog.at_level(logging.DEBUG):
        rig.ok("POST", "/v1/keys/groq", {"key": KEY_CANARY})
        rig.ok("POST", "/v1/keys/cloudflare", {"key": KEY_CANARY + "cf", "also": {"CLOUDFLARE_ACCOUNT_ID": ALSO_CANARY}})
        rig.fails("POST", "/v1/keys/groq", {"key": KEY_CANARY + "\nx"}, 400, "invalid_request")
        rig.fails("POST", "/v1/keys/nosuch", {"key": KEY_CANARY}, 404, "not_found")
        job = rig.ok("POST", "/v1/jobs", {"prompt": PROMPT_CANARY}, status=201)["job"]["id"]
        batch = rig.ok("POST", "/v1/jobs", {"kind": "batch", "prompts": [PROMPT_CANARY, PROMPT_CANARY + " again"]}, status=201)["job"]["id"]
        rig.fails("POST", "/v1/jobs", {"prompt": PROMPT_CANARY, "privacy": "nope"}, 400, "invalid_request")
        rig.fails("POST", "/v1/jobs", {"prompt": PROMPT_CANARY, "alias": "bad alias " + ERR_CANARY}, 400, "invalid_request")
        rig.fails("POST", "/v1/jobs", {"prompt": PROMPT_CANARY, "deadline": ERR_CANARY}, 400, "invalid_request")
        issued.append(rig.ok("POST", "/v1/apps", {"name": "one", "priority": "normal"}, status=201)["token"])
        issued.append(rig.ok("POST", "/v1/apps/one/rotate")["token"])
        rig.fails("POST", "/v1/apps", {"name": "one", "priority": ERR_CANARY}, 400, "invalid_request")
        rig.fails("PATCH", "/v1/apps/one", {"default_privacy": ERR_CANARY}, 400, "invalid_request")
        card = rig.card()
        rig.ok("POST", f"/v1/cards/{card}/answer", {"decision": "approve"})
        rig.fails("POST", f"/v1/cards/{card}/answer", {"decision": ERR_CANARY}, 400, "invalid_request")
        for path in ("/v1/status", "/v1/catalogue", "/v1/jobs", f"/v1/jobs/{job}", f"/v1/jobs/{batch}", "/v1/cards", f"/v1/cards/{card}", "/v1/apps"):
            rig.ok("GET", path)
        rig.ok("POST", "/v1/connect/groq")
        rig.ok("POST", "/v1/pause")
        rig.ok("POST", "/v1/resume")
        rig.ok("DELETE", f"/v1/jobs/{job}")
        rig.ok("POST", "/v1/receipts", {"action": "files.write", "summary": "Wrote a file"}, status=201)
        rig.fails("POST", "/v1/receipts", {"action": "files.write", "summary": PROMPT_CANARY + "\nsecond line"}, 400, "invalid_request")
        rig.ok("GET", "/v1/receipts")
        from wardmuse import permissions as P

        if P.available():
            rig.ok("GET", "/v1/mandates")
        else:
            rig.fails("GET", "/v1/mandates", None, 503, "unavailable")
        rig.fails("GET", f"/v1/cards/{ERR_CANARY}", None, 404, "not_found")
        rig.ok("DELETE", "/v1/apps/one")
        assert client_for(rig.srv, "wrong-" + ERR_CANARY).call("GET", "/v1/status")[0] == 401
    replies = "".join(rig.replies)
    out, err = capfd.readouterr()
    logs = caplog.text + out + err
    for canary in (KEY_CANARY, ALSO_CANARY, PROMPT_CANARY, "CANARY", TOKEN, "sk-muse-dev-"):
        assert canary not in logs, f"{canary!r} in a log line"
    for canary in (KEY_CANARY, ALSO_CANARY, PROMPT_CANARY, "CANARY", TOKEN, "token_sha256"):
        assert canary not in replies, f"{canary!r} in a reply"
    for token in issued:
        assert replies.count(token) == 1 and token not in logs
    assert replies.count("sk-muse-app-") == 2
    assert "POST /v1/keys/{provider} -> 200 test-phone" in caplog.text  # the log does say what happened
    # The secrets are where they belong and nowhere else under MUSE_HOME.
    assert KEY_CANARY in rig.env_file.read_text()
    for path in Path(envget(os.environ, "MUSE_HOME")).rglob("*"):
        if path.is_file():
            assert KEY_CANARY.encode() not in path.read_bytes(), path
