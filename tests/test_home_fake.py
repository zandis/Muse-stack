"""The fake home: the same API from canned data, in every state the app must handle."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from home_fakes import ROOT, assert_error, card_schema, running, schema_errors
from muse.home import api, cards, fake
from muse.home.client import HomeClient


class Fake:
    def __init__(self, state: str) -> None:
        self.home = fake.FakeHome(state)
        self._ctx = running(self.home, fake.fake_verifier(), tcp=True)
        self.srv = self._ctx.__enter__()
        self.client = HomeClient(fake.FAKE_TOKEN, tcp_port=self.srv.port, timeout=10)

    def get(self, path: str):
        status, payload = self.client.call("GET", path)
        assert status == 200, (path, status, payload)
        return payload

    def close(self) -> None:
        self._ctx.__exit__(None, None, None)
        self.home.close()


@pytest.fixture()
def start():
    made: list[Fake] = []

    def _start(state: str = "normal") -> Fake:
        made.append(Fake(state))
        return made[-1]

    yield _start
    for f in made:
        f.close()


def left(snap) -> list[int]:
    return [l["remaining"] for p in snap["providers"] if p["configured"] and p["kind"] != "local" for l in p["limits"] if l["remaining"] is not None]


def test_the_states_are_the_ones_the_app_must_handle():
    assert fake.STATES == ("normal", "used", "paused", "cards", "empty", "error")
    with pytest.raises(ValueError, match="state must be one of"):
        fake.FakeHome("broken")


@pytest.mark.parametrize("state", ["normal", "used", "paused", "cards", "empty"])
def test_every_get_answers_in_every_state_with_the_real_shapes(start, state):
    from muse.types import Snapshot

    f = start(state)
    snap = f.get("/v1/status")
    assert set(snap) == set(Snapshot.__annotations__) | {"api_version", "money", "activity", "local_runtime", "restart_pending"}
    assert snap["api_version"] == "1"
    assert snap["home"] == "(fake home)"
    cat = f.get("/v1/catalogue")
    assert {"connected", "providers", "suggestions", "resources", "subscriptions"} == set(cat)
    listed = f.get("/v1/cards")
    assert all(schema_errors(c, card_schema()) == [] for c in listed["cards"])
    assert sum(listed["counts"].values()) == len(listed["cards"])
    jobs = f.get("/v1/jobs")["jobs"]
    assert all("payload" not in j and "prompt" not in j for j in jobs)
    apps_ = f.get("/v1/apps")["apps"]
    assert all("token_sha256" not in a for a in apps_)
    status, mandates = f.client.call("GET", "/v1/mandates/artifacts")
    assert status in (200, 503), mandates  # 503 `unavailable` without biscuit-python on the home
    if status == 200:
        assert "mandates" in mandates and mandates["enforcement"]["router"] == "enforce"
    else:
        assert_error(status, mandates, 503, "unavailable")
    shown = f.get("/v1/receipts")
    assert shown["verify"]["ok"] is True and shown["verify"]["size"] == len(shown["records"]) and shown["order"] == "newest_first"
    assert (state == "empty") == (not shown["records"])
    assert (state == "empty") == (not jobs and not apps_ and not listed["cards"])


def test_normal_has_allowance_left_services_connected_and_one_card_waiting(start):
    f = start("normal")
    snap = f.get("/v1/status")
    assert snap["paused"] is False and snap["proxy"]["reachable"] is True
    assert any(n > 0 for n in left(snap)) and any(p["state"] == "ok" for p in snap["providers"])
    age = time.time() - time.mktime(time.strptime(snap["generated_at"], "%Y-%m-%dT%H:%M:%SZ")) + time.timezone
    assert abs(age) < 120  # the canned snapshot is shifted to now
    cat = f.get("/v1/catalogue")
    assert "gemini" in cat["connected"] and "cerebras" not in cat["connected"]
    pending = f.get("/v1/cards?status=pending")["cards"]
    assert len(pending) == 1 and pending[0]["kind"] == "save_file" and pending[0]["title"] == "Save a file"  # the Today mock-up
    assert f.get("/v1/cards")["counts"] == {"pending": 1, "approved": 1, "declined": 1, "expired": 1}
    assert {j["status"] for j in f.get("/v1/jobs")["jobs"]} == {"queued", "running", "deferred", "done"}
    assert {a["name"] for a in f.get("/v1/apps")["apps"]} >= {"cline", "notes-batch"}


def test_used_has_every_allowance_at_zero_with_refill_times(start):
    snap = start("used").get("/v1/status")
    remaining = left(snap)
    assert remaining and set(remaining) == {0}
    cloud = [p for p in snap["providers"] if p["configured"] and p["enabled"] and p["kind"] != "local"]
    assert cloud and all(p["state"] == "exhausted" for p in cloud)
    assert any(p["blocked_until"] for p in cloud)  # "Refills at 16:00"
    assert all(a["available"] == 0 for a in snap["aliases"])
    assert snap["paused"] is False


def test_paused_says_so_and_resume_works(start):
    f = start("paused")
    assert f.get("/v1/status")["paused"] is True
    assert f.client.call("POST", "/v1/resume") == (200, {"paused": False, "was_paused": True, "recorded": True})
    assert f.get("/v1/status")["paused"] is False
    assert f.client.call("POST", "/v1/pause") == (200, {"paused": True, "recorded": True})
    assert f.get("/v1/status")["paused"] is True


def test_cards_state_has_one_waiting_card_of_each_kind(start):
    f = start("cards")
    listed = f.get("/v1/cards")
    pending = [c for c in listed["cards"] if c["status"] == "pending"]
    assert sorted(c["kind"] for c in pending) == sorted(cards.KINDS)
    assert listed["counts"] == {"pending": 7, "approved": 1, "declined": 1, "expired": 1}
    by_kind = {c["kind"]: c for c in pending}
    assert by_kind["save_file"]["cost"] == {"nothing": True} and by_kind["save_file"]["under"]["text"] == "Weekly digest chore"
    assert "See all" not in by_kind["message"]["extra"]["preview"] and by_kind["message"]["extra"]["preview"]
    spend = by_kind["spend"]
    assert spend["cost"] == {"amount": "6", "currency": "TWD"} and spend["extra"]["limit_left"]["currency"] == "TWD" and spend["extra"]["payee"]
    seconds = time.mktime(time.strptime(spend["expires_at"], "%Y-%m-%dT%H:%M:%SZ")) - time.mktime(time.strptime(spend["created_at"], "%Y-%m-%dT%H:%M:%SZ"))
    assert 0 < seconds <= 600  # payments expire in minutes, not a day
    assert by_kind["grant_permission"]["extra"]["sentence"] and by_kind["grant_permission"]["extra"]["limits"] and by_kind["grant_permission"]["extra"]["ends_at"]
    assert by_kind["oneoff_credit"]["extra"]["service"]
    assert by_kind["send_private"]["extra"]["badge"] and by_kind["send_private"]["extra"]["services"]
    assert by_kind["install_skill"]["extra"]["asks_for"] and by_kind["install_skill"]["extra"]["author"]
    assert [c["id"] for c in listed["cards"]] == [c["id"] for c in sorted(listed["cards"], key=lambda c: c["created_at"], reverse=True)]
    # Answering behaves as on a real home.
    card = by_kind["save_file"]["id"]
    status, payload = f.client.call("POST", f"/v1/cards/{card}/answer", {"decision": "approve", "always": True})
    assert status == 200 and payload["card"]["answered_by"] == fake.FAKE_DEVICE and payload["card"]["always"] is True
    assert_error(*f.client.call("POST", f"/v1/cards/{card}/answer", {"decision": "decline"}), 409, "card_already_answered")
    expired = next(c for c in listed["cards"] if c["status"] == "expired")
    assert_error(*f.client.call("POST", f"/v1/cards/{expired['id']}/answer", {"decision": "approve"}), 410, "card_expired")
    assert f.get("/v1/cards")["counts"]["pending"] == 6


def test_empty_has_nothing_connected_and_nothing_waiting(start):
    f = start("empty")
    snap = f.get("/v1/status")
    assert not any(p["configured"] for p in snap["providers"]) and left(snap) == []
    assert snap["totals"] == {"requests_today": 0, "tokens_today": 0} and snap["recent"] == []
    assert snap["queue"]["waiting"] == 0 and [a["name"] for a in snap["apps"]] == ["owner"]
    assert snap["suggestions"], "the first-run screen needs something to suggest"
    cat = f.get("/v1/catalogue")
    assert cat["connected"] == [] and cat["suggestions"]
    assert f.get("/v1/cards") == {"cards": [], "counts": {"pending": 0, "approved": 0, "declined": 0, "expired": 0}}


def test_error_state_answers_unavailable_everywhere_but_still_checks_the_token(start):
    f = start("error")
    for method, path, body in [("GET", "/v1/status", None), ("GET", "/v1/cards", None), ("POST", "/v1/pause", None),
                               ("POST", "/v1/jobs", {"prompt": "x"}), ("GET", "/v1/receipts", None)]:
        status, payload, headers = f.client.request(method, path, body)
        err = assert_error(status, payload, 503, "unavailable")
        assert err["retry_after"] == 5 and headers["retry-after"] == "5"
    assert_error(*f.client.call("GET", "/v1/nope"), 404, "not_found")
    assert_error(*HomeClient("wrong", tcp_port=f.srv.port).call("GET", "/v1/status"), 401, "unauthorized")


def test_writes_work_and_are_forgotten(start, monkeypatch):
    from muse import keys

    monkeypatch.setattr(keys, "set_key", lambda *a, **k: pytest.fail("a fake home must not touch the real key store"))
    f = start("normal")
    key = "csk-" + "a1B2" * 8
    status, payload = f.client.call("POST", "/v1/keys/cerebras", {"key": key})
    assert status == 200 and payload["stored"] == ["CEREBRAS_API_KEY"] and payload["backend"] == "fake" and key not in json.dumps(payload)
    assert "cerebras" in f.get("/v1/catalogue")["connected"]
    assert next(p for p in f.get("/v1/status")["providers"] if p["id"] == "cerebras")["configured"] is True
    assert f.client.call("POST", "/v1/connect/cerebras")[1]["connected"] is True
    status, made = f.client.call("POST", "/v1/apps", {"name": "my-app", "priority": "normal"})
    assert status == 201 and made["token"].startswith("sk-muse-app-")
    assert "my-app" in {a["name"] for a in f.get("/v1/apps")["apps"]}
    status, job = f.client.call("POST", "/v1/jobs", {"prompt": "hello"})
    assert status == 201 and f.client.call("DELETE", f"/v1/jobs/{job['job']['id']}")[1]["job"]["status"] == "cancelled"
    assert_error(*f.client.call("POST", "/v1/jobs", {"prompt": f"my key is {key}"}), 400, "invalid_request")
    root = f.home.root
    assert key.encode() not in b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())
    again = start("normal")
    assert "my-app" not in {a["name"] for a in again.get("/v1/apps")["apps"]}
    assert "cerebras" not in again.get("/v1/catalogue")["connected"]


def test_it_needs_no_proxy_ledger_or_keys_and_leaves_the_real_home_alone(start, monkeypatch):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("the fake home must not make a request"))
    real_home = Path(os.environ["MUSE_HOME"])
    before = sorted(p.name for p in real_home.iterdir())
    f = start("cards")
    f.get("/v1/status")
    f.client.call("POST", "/v1/pause")
    f.client.call("POST", "/v1/apps", {"name": "x", "priority": "normal"})
    assert sorted(p.name for p in real_home.iterdir()) == before
    assert f.home.root != real_home and (f.home.root / "cards.sqlite").exists() and not (f.home.root / "ledger.sqlite").exists()
    root = f.home.root
    f.home.close()
    assert not root.exists()


def test_only_the_fixed_token_signs_in(start):
    f = start("normal")
    assert fake.FAKE_TOKEN == "fake-home-token"
    for token in (None, "", "fake", "sk-muse-dev-anything"):
        assert_error(*HomeClient(token, tcp_port=f.srv.port).call("GET", "/v1/status"), 401, "unauthorized")


def test_the_fake_serves_the_same_route_table():
    assert fake.FakeHome.__mro__[1] is api.Home  # the real Home, with canned data


def read_until(proc, pattern: str, timeout: float = 30.0) -> tuple[str, re.Match]:
    seen = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        seen += line
        match = re.search(pattern, seen)
        if match:
            return seen, match
    raise AssertionError(f"never saw {pattern!r} in: {seen!r} {proc.stderr.read() if proc.poll() is not None else ''}")


@pytest.mark.parametrize("args, pattern", [
    (["--tcp", "0", "--state", "cards"], r"http://127\.0\.0\.1:(\d+)"),
    ([], r"unix:(\S+home\.sock)"),
])
def test_python_m_muse_home_fake_runs_and_cleans_up(args, pattern):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "muse.home", "--fake", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
    try:
        seen, match = read_until(proc, pattern)
        assert "FAKE home" in seen and fake.FAKE_TOKEN in seen
        if args:
            client = HomeClient(fake.FAKE_TOKEN, tcp_port=int(match.group(1)), timeout=10)
            assert client.call("GET", "/v1/cards")[1]["counts"]["pending"] == 7
            folder = None
        else:
            sock = Path(match.group(1))
            folder = sock.parent
            assert folder != Path(os.environ["MUSE_HOME"]) and oct(sock.stat().st_mode & 0o777) == "0o600"
            client = HomeClient(fake.FAKE_TOKEN, socket_path=sock, timeout=10)
            assert client.call("GET", "/v1/cards")[1]["counts"]["pending"] == 1
        assert client.call("GET", "/v1/status")[1]["api_version"] == "1"
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
    assert proc.returncode == 0
    if folder is not None:
        assert not folder.exists()  # the throw-away home is gone


def test_state_without_fake_is_a_usage_error():
    r = subprocess.run([sys.executable, "-m", "muse.home", "--state", "cards"], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=60)
    assert r.returncode == 2 and "--state only applies with --fake" in r.stderr
