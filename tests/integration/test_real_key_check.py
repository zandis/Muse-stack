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
