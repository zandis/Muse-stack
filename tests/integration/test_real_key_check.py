"""scripts/real_key_check.py against the fake-upstream harness: it passes, and its report holds no canary."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ration_catalog import MASTER_KEY, RATION_KEYS
from test_ration_e2e import start

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "real_key_check.py"
CANARY_REPLY = "CANARY-REPLY-b81e47"
CANARY_ERR = "CANARY-ERRTEXT-5d2a90"


def plant_canaries(upstream) -> None:
    original = upstream._respond

    def respond(behaviour, model, path):
        status, payload, headers = original(behaviour, model, path)
        if isinstance(payload, dict):
            if "choices" in payload:
                payload["choices"][0]["message"]["content"] = CANARY_REPLY
            if "error" in payload:
                payload["error"]["message"] = f"{payload['error'].get('message', '')} {CANARY_ERR} try again in 7m12s"
        return status, payload, headers

    upstream._respond = respond


def run_script(proxy, tmp_path, *args, report=None):
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and not k.startswith("MUSE_")}
    env.update(RATION_KEYS)
    env.update(LITELLM_MASTER_KEY=MASTER_KEY, MUSE_HOME=str(proxy.home), MUSE_CATALOG=str(proxy.catalog_path),
               MUSE_ENV_FILE=str(tmp_path / "x.env"), MUSE_TZ="UTC", NO_PROXY="127.0.0.1")
    report = report or tmp_path / "report.json"
    r = subprocess.run([sys.executable, str(SCRIPT), "--yes", "--proxy-url", proxy.url, "--report", str(report), *args],
                       capture_output=True, text=True, env=env, timeout=240)
    return r, report


def assert_clean(text: str, proxy) -> None:
    for secret in (MASTER_KEY, *RATION_KEYS.values(), CANARY_REPLY, CANARY_ERR, "Reply with the single word", "sk-muse-app-"):
        assert secret not in text, f"{secret!r} leaked"


def test_script_passes_and_the_report_has_no_keys_prompts_or_responses(make_proxy, upstream, tmp_path):
    plant_canaries(upstream)
    proxy = start(make_proxy, local=False)
    r, report = run_script(proxy, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "at most" in r.stdout and "requests in all" in r.stdout  # the bound is printed before anything is sent
    data = json.loads(report.read_text())
    assert data["passed"] is True and stat.S_IMODE(report.stat().st_mode) == 0o600
    results = {c["id"]: c["result"] for c in data["checks"]}
    assert results["proxy"] == "pass" and results["app-token"] == "pass" and results["background-yield"] == "pass"
    assert results["privacy-local"] == "pass" and results["paid-cap-zero"] == "skip"
    assert {f"provider:{p}" for p in ("freea", "freeb", "paidp", "paidu")} <= set(results)
    assert all(v != "fail" for v in results.values())
    a = next(p for p in data["providers"] if p["id"] == "freea")
    assert a["status"] == 200 and a["headers"]["x-muse-app"] == "owner" and "x-muse-provider" in a["headers"]
    assert a["observed_limits"] and a["ratelimit_header_names"]  # names and parsed numbers, no content
    assert data["proxy_requests_sent"] <= 30
    assert_clean(report.read_text() + r.stdout + r.stderr, proxy)
    assert "rkc-int" not in (Path(proxy.home) / "apps.yaml").read_text()  # the temporary apps are removed


def test_probe_limits_records_the_shape_of_a_real_429_without_its_text(make_proxy, upstream, tmp_path):
    plant_canaries(upstream)
    upstream.set("b-model", "429_daily")
    proxy = start(make_proxy, local=False)
    r, report = run_script(proxy, tmp_path, "--probe-limits", "--provider", "freeb", "--burst", "6")
    assert "WILL CONSUME QUOTA" in r.stdout
    data = json.loads(report.read_text())
    probe = data["probes"][0]
    assert probe["provider"] == "freeb" and probe["observed_429"] is True and probe["sent"] <= 6
    raw = probe["raw"]
    assert raw["status"] == 429 and isinstance(raw["body_shape"], dict) and "keywords" in raw
    assert any(k in raw["keywords"] for k in ("quota", "per day", "retry"))
    assert raw["retry_hint"] in (None, "7m12s")
    assert "<text:" in json.dumps(raw["body_shape"])  # free text replaced by its length
    assert_clean(report.read_text() + r.stdout + r.stderr, proxy)


def test_paid_cap_zero_check(make_proxy, upstream, tmp_path):
    proxy = start(make_proxy, local=False, MUSE_DAILY_PAID_CAP_USD="0")
    r, report = run_script(proxy, tmp_path, "--expect-paid-cap-zero")
    data = json.loads(report.read_text())
    cap = next(c for c in data["checks"] if c["id"] == "paid-cap-zero")
    assert cap["result"] == "pass", (cap, r.stdout)
    assert not upstream.calls("pp-model") and not upstream.calls("pu-model")


def test_script_refuses_to_run_unconfirmed_without_a_terminal(make_proxy, tmp_path):
    proxy = start(make_proxy, local=False)
    env = {**os.environ, **RATION_KEYS, "LITELLM_MASTER_KEY": MASTER_KEY, "MUSE_HOME": str(proxy.home), "MUSE_CATALOG": str(proxy.catalog_path)}
    r = subprocess.run([sys.executable, str(SCRIPT), "--proxy-url", proxy.url], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60)
    assert r.returncode == 2 and "--yes" in r.stderr and "requests in all" in r.stdout


def _script_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("real_key_check_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_answer_state_tells_a_cut_short_reasoning_reply_from_an_empty_one():
    rkc = _script_module()

    def chat(message, finish="stop", usage=None):
        return json.dumps({"choices": [{"message": message, "finish_reason": finish}], "usage": usage or {}}).encode()

    assert rkc.answer_state("chat", chat({"content": "ok"})) == "answer"
    assert rkc.answer_state("chat", chat({"content": ""}, "length")) == "cut_short"
    assert rkc.answer_state("chat", chat({"content": None, "reasoning_content": "hm"})) == "cut_short"
    assert rkc.answer_state("chat", chat({"content": "", "reasoning": "hm"})) == "cut_short"
    assert rkc.answer_state("chat", chat({"content": ""}, "stop", {"completion_tokens_details": {"reasoning_tokens": 12}})) == "cut_short"
    assert rkc.answer_state("chat", chat({"content": "  "})) == "empty"  # nothing at all is still a failure
    assert rkc.answer_state("chat", chat({"content": ""}, "stop", {"completion_tokens_details": {"reasoning_tokens": 0}})) == "empty"
    for raw in (b"not json", b"[]", b"{}", b'{"choices": []}', b'{"choices": [{"message": "x"}]}', b'{"choices": [null]}'):
        assert rkc.answer_state("chat", raw) == "empty"
    assert rkc.answer_state("embedding", b'{"data": [{"embedding": [0.1]}]}') == "answer"
    assert rkc.answer_state("embedding", b'{"data": []}') == "empty"
    assert rkc.body_ok("chat", chat({"content": ""}, "length")) and not rkc.body_ok("chat", chat({"content": ""}))
    assert rkc.provider_busy(429, {}, b'{"error": "slow down"}')
    assert not rkc.provider_busy(429, {"x-muse-ration": "defer:yield"}, b"")  # Muse's own deferral is not "busy"
    assert not rkc.provider_busy(429, {}, b"[muse] every deployment is blocked")
    assert not rkc.provider_busy(401, {}, b"")


def test_reasoning_only_reply_passes_and_a_busy_free_tier_is_a_note_not_a_failure(make_proxy, upstream, tmp_path):
    proxy = start(make_proxy, local=False)  # no canaries here: they would fill in the empty reply
    upstream.set("a-model", "ok_reasoning_only")  # like gpt-oss at max_tokens=16
    upstream.set("b-model", "429_retry_after:60")  # like an OpenRouter :free model at a busy moment
    r, report = run_script(proxy, tmp_path)
    data = json.loads(report.read_text())
    checks = {c["id"]: c for c in data["checks"]}
    assert checks["provider:freea"]["result"] == "pass" and "output tokens on reasoning" in checks["provider:freea"]["detail"]
    assert checks["provider:freeb"]["result"] == "info" and "the key was not rejected" in checks["provider:freeb"]["detail"]
    a = next(p for p in data["providers"] if p["id"] == "freea")
    b = next(p for p in data["providers"] if p["id"] == "freeb")
    assert a["answer_state"] == "cut_short" and b["busy"] is True and b["status"] == 429
    assert b["block"] and b["block"]["kind"] == "cooldown" and b["block"]["cause"] == "rate-limit"  # what Muse decided after the 429, not the provider's text
    assert not [c for c in data["checks"] if c["result"] == "fail"], data["checks"]
    assert r.returncode == 0 and data["passed"] is True, r.stdout + r.stderr
    assert "notes" in r.stdout
    assert_clean(report.read_text() + r.stdout + r.stderr, proxy)
    assert "thinking it over" not in report.read_text() + r.stdout  # reasoning text is never kept either


def test_a_rejected_key_is_still_a_failure_not_a_busy_note(make_proxy, upstream, tmp_path):
    proxy = start(make_proxy, local=False)
    upstream.set("b-model", "401")
    r, report = run_script(proxy, tmp_path)
    data = json.loads(report.read_text())
    checks = {c["id"]: c for c in data["checks"]}
    assert checks["provider:freeb"]["result"] == "fail" and r.returncode == 1
    assert not next(p for p in data["providers"] if p["id"] == "freeb").get("busy")


def test_floor_note_reads_the_header_and_nothing_else():
    rkc = _script_module()
    assert rkc.MAX_OUT_TOKENS == 16  # the check keeps asking for a tiny answer; the router's floor is what makes it visible
    assert rkc.floor_note({}) == "" and rkc.floor_note({"x-muse-min-output": ""}) == ""
    assert rkc.floor_note({"x-muse-min-output": "raised:16->256"}) == "; output floor: max_tokens 16->256 for this model (x-muse-min-output)"
    assert "100->256;over-ceiling:100" in rkc.floor_note({"x-muse-min-output": "raised:100->256;over-ceiling:100"})
    safe = rkc.safe_headers({"x-muse-min-output": "raised:16->256", "x-muse-answer": "empty-reasoning", "x-muse-other": "a b <script>"})
    assert safe == {"x-muse-min-output": "raised:16->256", "x-muse-answer": "empty-reasoning", "x-muse-other": "<redacted>"}
    assert rkc.safe_headers({"x-muse-min-output": "raised:100->256;over-ceiling:100"})["x-muse-min-output"] == "raised:100->256;over-ceiling:100"


def floored_ration_catalog(upstream):
    """The rationing catalogue, with a-model as a reasoning model that has an output floor."""
    from ration_catalog import build_ration_catalog

    catalog = build_ration_catalog(upstream)
    freea = next(p for p in catalog["providers"] if p["id"] == "freea")
    freea["deployments"][0]["min_output_tokens"] = 256
    return catalog


def test_the_check_notes_the_output_floor_and_then_sees_a_real_answer(make_proxy, upstream, tmp_path):
    proxy = make_proxy(local=False, catalog=floored_ration_catalog, local_aliases=("chat",), env={"MUSE_TZ": "UTC"})
    upstream.set("a-model", "reasoning_below:200")  # empty at the script's max_tokens=16, fine at the floor
    r, report = run_script(proxy, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    data = json.loads(report.read_text())
    checks = {c["id"]: c for c in data["checks"]}
    detail = checks["provider:freea"]["detail"]
    assert checks["provider:freea"]["result"] == "pass"
    assert "output floor: max_tokens 16->256" in detail and "output tokens on reasoning" not in detail
    a = next(p for p in data["providers"] if p["id"] == "freea")
    assert a["answer_state"] == "answer" and a["headers"]["x-muse-min-output"] == "raised:16->256"
    b = next(p for p in data["providers"] if p["id"] == "freeb")
    assert "x-muse-min-output" not in b["headers"] and "output floor" not in checks["provider:freeb"]["detail"]
    a_calls = [c.max_tokens for c in upstream.calls("a-model") if c.max_tokens is not None]
    assert a_calls and set(a_calls) == {256}
    assert {c.max_tokens for c in upstream.calls("b-model") if c.path.endswith("/chat/completions")} <= {16}
    assert_clean(report.read_text() + r.stdout + r.stderr, proxy)
