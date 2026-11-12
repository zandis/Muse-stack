from datetime import datetime, timezone

from muse import probes

NOW = datetime(2026, 11, 12, 15, 30, tzinfo=timezone.utc)


def test_openrouter_free_daily_requests():
    payload = {"data": {"is_free_tier": True, "free_model_daily_requests": {"used": 12, "limit": 50, "remaining": 38}}}
    assert probes.parse_openrouter_key(payload, NOW) == [
        {
            "window": "day",
            "metric": "requests",
            "limit": 50,
            "remaining": 38,
            "reset_at": "2026-11-13T00:00:00Z",
            "source": "header",
        }
    ]


def test_remaining_is_derived_when_absent():
    payload = {"data": {"free_model_daily_requests": {"used": 60, "limit": 50}}}
    assert probes.parse_openrouter_key(payload, NOW)[0]["remaining"] == 0


def test_garbage_payloads_give_nothing():
    for payload in (None, [], {}, {"data": None}, {"data": {"free_model_daily_requests": "x"}},
                    {"data": {"free_model_daily_requests": {"limit": True, "remaining": float("nan")}}}):
        assert probes.parse_openrouter_key(payload, NOW) == []


def test_probe_never_raises_and_sends_bearer():
    seen = {}

    def ok(url, headers, timeout):
        seen.update(url=url, headers=headers)
        return 200, b'{"data":{"free_model_daily_requests":{"limit":1000,"remaining":999}}}'

    assert probes.probe_openrouter("k", NOW, fetch=ok)[0]["limit"] == 1000
    assert seen["url"] == probes.OPENROUTER_KEY_URL and seen["headers"] == {"Authorization": "Bearer k"}

    def boom(url, headers, timeout):
        raise OSError("down")

    assert probes.probe_openrouter("k", NOW, fetch=boom) == []
    assert probes.probe_openrouter("k", NOW, fetch=lambda *a: (401, b"")) == []
    assert probes.probe_openrouter("k", NOW, fetch=lambda *a: (200, b"not json")) == []
    assert probes.probe_openrouter("", NOW, fetch=boom) == []


def test_run_probes_records_to_ledger_and_snapshot(tmp_path):
    from muse.ledger import Ledger
    from muse.status import build_snapshot

    env = {"MUSE_HOME": str(tmp_path), "OPENROUTER_API_KEY": "k"}
    ledger = Ledger(environ=env)

    def ok(url, headers, timeout):
        return 200, b'{"data":{"free_model_daily_requests":{"limit":50,"remaining":38}}}'

    assert probes.run_probes(env, ledger, NOW, fetch=ok) == ["openrouter"]
    assert probes.run_probes({"OPENROUTER_API_KEY": ""}, ledger, NOW, fetch=ok) == []
    snap = build_snapshot(now=NOW.timestamp(), environ=env)
    openrouter = next(p for p in snap["providers"] if p["id"] == "openrouter")
    assert openrouter["limits"] == [
        {"window": "day", "metric": "requests", "limit": 50, "remaining": 38,
         "reset_at": "2026-11-13T00:00:00Z", "source": "header"}
    ]
