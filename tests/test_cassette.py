"""Record from the fake provider, replay with no outbound call, and refuse key-shaped text."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from muse import cassette
from muse.fakeprov import FakeProvider

ROOT = Path(__file__).resolve().parents[1]
SHAPES = ROOT / "tests" / "cassettes"
KEY = "sk-" + "aB3" * 12  # mixed case, digits, long enough to look like a key
GROQ_KEY = "gsk_" + "aB3" * 8


def post(url: str, payload: dict, headers: dict | None = None) -> tuple[int, bytes]:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def test_record_replays_offline_and_drops_keys(tmp_path: Path):
    fake = FakeProvider().start()
    recorded = tmp_path / "tapes"
    try:
        fake.set_reply("echo", f"fine, ignore {KEY}")
        recorder = cassette.Proxy("record", recorded).start()
        payload = {"model": "echo", "messages": [{"role": "user", "content": f"the secret is {KEY}"}]}
        headers = {"authorization": f"Bearer {KEY}", "x-api-key": GROQ_KEY}
        status, body = post(
            recorder.v1 + "/chat/completions",
            payload,
            {**headers, "x-muse-cassette-upstream": fake.base()},
        )
        assert status == 200 and KEY.encode() in body  # the live reply still has it; the file must not
        assert recorder.outbound == 1 and len(fake.calls()) == 1
        files = list(recorded.glob("*.json"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert KEY not in text and GROQ_KEY not in text and "authorization" not in text.lower()
        assert "[redacted]" in text
        saved = json.loads(text)
        assert "content" not in json.dumps(saved["request"])  # fingerprint, not the prompt
        assert saved["request"]["model"] == "echo"
        assert cassette.secret_problems(recorded) == []
        recorder.stop()

        before = len(fake.calls())
        player = cassette.Proxy("replay", recorded).start()
        status, body = post(
            player.v1 + "/chat/completions",
            payload,
            {"authorization": f"Bearer {KEY}", "x-muse-cassette-upstream": fake.base()},
        )
        assert status == 200 and b"[redacted]" in body
        assert player.outbound == 0 and len(fake.calls()) == before  # the fake was not asked again
        status, body = post(player.v1 + "/chat/completions", {"model": "other", "messages": []})
        assert status == 404 and b"cassette_miss" in body
        assert len(fake.calls()) == before
        player.stop()
    finally:
        fake.stop()
        cassette.stop_proxy()


def test_replay_of_a_stream_an_error_and_an_empty_answer(tmp_path: Path):
    fake = FakeProvider().start()
    recorded = tmp_path / "tapes"
    try:
        fake.set("oss", "empty_reasoning")
        fake.set("day", "429_gemini_day")
        fake.set("flow", "sse_error")
        recorder = cassette.Proxy("record", recorded).start()
        for model in ("oss", "day", "flow"):
            post(recorder.v1 + "/chat/completions", {"model": model, "messages": [{"role": "user", "content": model}]},
                 {"x-muse-cassette-upstream": fake.base()})
        recorder.stop()
        player = cassette.Proxy("replay", recorded).start()
        status, body = post(player.v1 + "/chat/completions", {"model": "oss", "messages": [{"role": "user", "content": "oss"}]})
        assert status == 200 and json.loads(body)["choices"][0]["message"]["content"] == ""
        status, body = post(player.v1 + "/chat/completions", {"model": "day", "messages": [{"role": "user", "content": "day"}]})
        assert status == 429 and b"PerDay" in body
        status, body = post(player.v1 + "/chat/completions", {"model": "flow", "messages": [{"role": "user", "content": "flow"}]})
        assert status == 200 and b'"error"' in body
        saved = json.loads(next(p.read_text(encoding="utf-8") for p in recorded.glob("*.json") if '"flow"' in p.read_text(encoding="utf-8") or "sse" in p.read_text()))
        # The stream is stored as chunks, not only as one blob.
        stream = next(json.loads(p.read_text(encoding="utf-8")) for p in recorded.glob("*.json") if "event-stream" in p.read_text(encoding="utf-8"))
        assert stream["response"]["chunks"] and any("error" in chunk for chunk in stream["response"]["chunks"])
        assert player.outbound == 0
        player.stop()
        assert saved or stream
    finally:
        fake.stop()


def test_a_planted_key_fails_the_scan(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"response": {"body": f"leaked {KEY}"}}) + "\n", encoding="utf-8")
    assert cassette.secret_problems(tmp_path)


def test_committed_cassettes_have_no_key_shaped_text():
    """The files under tests/cassettes/ are the ones a review would merge."""
    assert SHAPES.is_dir()
    assert cassette.secret_problems(SHAPES) == []
    files = list(SHAPES.rglob("*.json"))
    assert files, "record at least the shape examples into tests/cassettes/"


def test_reroute_is_a_no_op_until_record_or_replay_is_asked_for(tmp_path: Path):
    kwargs = {"api_base": "https://api.groq.com/openai/v1"}
    assert cassette.reroute(kwargs, {}) is False
    assert kwargs == {"api_base": "https://api.groq.com/openai/v1"}
    assert cassette.reroute(dict(kwargs), {"MUSE_PROVIDER_MODE": "live"}) is False
    # A local env reference is not a provider call.
    assert cassette.reroute({"api_base": "os.environ/MUSE_LOCAL_OPENAI_BASE"}, {"MUSE_PROVIDER_MODE": "replay", "MUSE_CASSETTE_DIR": str(tmp_path)}) is False
    try:
        routed = {"api_base": "https://api.groq.com/openai/v1"}
        assert cassette.reroute(routed, {"MUSE_PROVIDER_MODE": "replay", "MUSE_CASSETTE_DIR": str(tmp_path)}) is True
        assert routed["api_base"].startswith("http://127.0.0.1:") and routed["api_base"].endswith("/v1")
        assert routed["extra_headers"]["x-muse-cassette-upstream"] == "https://api.groq.com/openai/v1"
    finally:
        cassette.stop_proxy()


def test_replay_without_a_directory_refuses_rather_than_calling_out():
    with pytest.raises(cassette.CassetteError):
        cassette.reroute({"api_base": "https://api.groq.com/openai/v1"}, {"MUSE_PROVIDER_MODE": "replay"})


def test_upstream_url_does_not_double_the_v1_prefix():
    assert cassette.upstream_url("https://api.groq.com/openai/v1", "/v1/chat/completions") == "https://api.groq.com/openai/v1/chat/completions"
    assert cassette.upstream_url("https://api.groq.com/openai/v1/", "/v1/embeddings") == "https://api.groq.com/openai/v1/embeddings"
