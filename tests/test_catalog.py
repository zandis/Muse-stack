"""Catalogue v2: loading, validation, suggestions, strings, renderer and snapshot additions."""

from __future__ import annotations

import copy
import json
import re
from datetime import date
from pathlib import Path

import pytest
import yaml

import render_config
from muse import catalog, cli
from muse.catalog import CATALOG_PATH, RESOURCES_DIR
from muse.status import build_snapshot

TODAY = date(2026, 11, 12)


def real() -> dict:
    return yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))


def validate_with(tmp_path: Path, data: dict, today: date = TODAY) -> list[str]:
    path = tmp_path / "providers.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return catalog.validate_catalog(path, RESOURCES_DIR, today)


def provider(data: dict, pid: str) -> dict:
    return next(p for p in data["providers"] if p["id"] == pid)


def has(problems: list[str], *words: str) -> bool:
    return any(all(w in p for w in words) for p in problems)


# ----------------------------------------------------------- loading / good


def test_shipped_catalogue_is_valid():
    assert catalog.validate_catalog(today=TODAY) == []


def test_loader_types():
    cat = catalog.load_catalog()
    orr = cat.provider("openrouter")
    assert orr.connection.method == "oauth_pkce" and orr.connection.auth_url.startswith("https://")
    assert orr.headline().limit == 50 and orr.headline().window == "day"
    assert orr.reset["rule"] == "utc_day" and orr.confidence == "H"
    assert cat.provider("voyage").one_off is True and cat.provider("gemini").one_off is False
    assert cat.provider("cloudflare").also_env == ("CLOUDFLARE_ACCOUNT_ID",)
    assert {r.kind for r in cat.resources} == {"storage", "compute", "chain"} and all(not r.managed for r in cat.resources)
    assert {s.id for s in cat.subscriptions} == {"chatgpt", "claude", "gemini"} and all(s.handoff_only for s in cat.subscriptions)
    assert cat.provider("deepseek").terms_flags["hosted_in"] == "cn"


def test_every_fact_is_dated_and_sourced():
    for p in catalog.load_providers():
        if p.local:
            continue
        assert p.checked_on and date.fromisoformat(p.checked_on) <= TODAY, p.id
        assert p.confidence in ("H", "M", "L"), p.id
        if p.confidence in ("H", "M"):
            assert p.source_urls, p.id
    for r in catalog.load_resources():
        assert r.checked_on and r.confidence and r.source_urls, r.id


def test_no_unconfirmed_number_in_gemini_and_groq():
    """Their free limits are not published on a page that could be read, so none may be invented."""
    cat = catalog.load_catalog()
    assert cat.provider("gemini").allowance == () and cat.provider("groq").allowance == ()
    assert cat.provider("gemini").read_remaining["method"] == "dashboard_only"
    assert cat.provider("groq").read_remaining["method"] == "headers"


# ------------------------------------------------------------- bad catalogues


def test_bad_date_and_provenance(tmp_path):
    d = real()
    provider(d, "gemini")["checked_on"] = "2026-12-01"
    assert has(validate_with(tmp_path, d), "gemini", "future")
    d = real()
    provider(d, "gemini")["checked_on"] = "yesterday"
    assert has(validate_with(tmp_path, d), "checked_on")
    d = real()
    del provider(d, "gemini")["checked_on"]
    assert has(validate_with(tmp_path, d), "every entry is dated")
    d = real()
    provider(d, "openrouter")["source_urls"] = []
    assert has(validate_with(tmp_path, d), "source_urls", "confidence H")
    d = real()
    provider(d, "openrouter")["confidence"] = "high"
    assert has(validate_with(tmp_path, d), "confidence")


def test_oauth_needs_https_on_both_urls(tmp_path):
    for field in ("auth_url", "exchange_url"):
        d = real()
        provider(d, "openrouter")["connection"][field] = "http://openrouter.ai/x"
        assert has(validate_with(tmp_path, d), "oauth_pkce needs " + field)
    d = real()
    del provider(d, "openrouter")["connection"]["exchange_url"]
    assert has(validate_with(tmp_path, d), "exchange_url")
    d = real()
    provider(d, "groq")["connection"]["auth_url"] = "https://x.example/auth"
    assert has(validate_with(tmp_path, d), "oauth_pkce only")


def test_every_step_needs_both_languages(tmp_path):
    d = real()
    del provider(d, "gemini")["connection"]["steps"][1]["detail"]["zh-TW"]
    assert has(validate_with(tmp_path, d), "steps[1].detail", "zh-TW")
    d = real()
    provider(d, "gemini")["connection"]["steps"][0]["title"] = "English only"
    assert has(validate_with(tmp_path, d), "steps[0].title", "both languages")
    d = real()
    provider(d, "gemini")["connection"]["steps"][1]["id"] = provider(d, "gemini")["connection"]["steps"][0]["id"]
    assert has(validate_with(tmp_path, d), "duplicate id")
    d = real()
    provider(d, "gemini")["connection"]["steps"] = []
    assert has(validate_with(tmp_path, d), "at least one step")


def test_china_hosting_rules(tmp_path):
    d = real()
    provider(d, "deepseek")["terms_flags"]["hosted_in"] = "unknown"
    assert has(validate_with(tmp_path, d), "policy.region is cn")
    d = real()
    provider(d, "gemini")["terms_flags"]["hosted_in"] = "cn"
    assert has(validate_with(tmp_path, d), "hosted_in is cn", "policy.region must be cn")
    d = real()
    ds = provider(d, "deepseek")
    ds["policy"]["trains_on_inputs"] = False  # yaml `no`
    ds["source_urls"] = []
    assert has(validate_with(tmp_path, d), "China-hosted", "source URL")
    ds["source_urls"] = ["https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html"]
    assert not has(validate_with(tmp_path, d), "China-hosted")


def test_flags_kinds_allowance_and_reset(tmp_path):
    cases = [
        (lambda p: p["terms_flags"].update(aggregation_or_reselling="maybe"), "aggregation_or_reselling"),
        (lambda p: p["terms_flags"].update(hosted_in="China"), "hosted_in"),
        (lambda p: p["terms_flags"].update(invented="yes"), "unknown flag"),
        (lambda p: p.update(kind_of_allowance=["weekly"]), "kind_of_allowance"),
        (lambda p: p.update(one_off=True), "one_off"),
        (lambda p: p.update(allowance=[{"window": "day", "limit": -1, "unit": "requests"}]), "limit"),
        (lambda p: p.update(allowance=[{"window": "hour", "limit": 5, "unit": "requests"}]), "window"),
        (lambda p: p.update(allowance=[{"window": "day", "limit": 5, "unit": "Requests"}]), "unit"),
        (lambda p: p.update(allowance=[{"window": "day", "limit": 5, "unit": "requests", "headline": True}] * 2), "more than one headline"),
        (lambda p: p["reset"].update(rule="whenever"), "rule"),
        (lambda p: p["read_remaining"].update(method="telepathy"), "method"),
        (lambda p: p["connection"].update(minutes_estimate=0), "minutes_estimate"),
        (lambda p: p["connection"].update(card_required="maybe"), "card_required"),
        (lambda p: p["connection"].update(key_url="https://other.example/"), "must equal key_url"),
        (lambda p: p["connection"].update(test={"url": "http://x.example", "auth": "bearer"}), "test"),
        (lambda p: p.update(good_for=["embeddings"]), "embeddings"),
        (lambda p: p.update(good_for=["poetry"]), "good_for"),
    ]
    for mutate, word in cases:
        d = real()
        mutate(provider(d, "groq"))
        assert has(validate_with(tmp_path, d), word), word
    d = real()
    cohere = provider(d, "cohere")
    cohere["allowance"][0]["limit"] = 999  # quota says 1000
    assert has(validate_with(tmp_path, d), "quota", "disagrees")
    d = real()
    v = provider(d, "voyage")
    v["reset"] = {"rule": "utc_day"}
    assert has(validate_with(tmp_path, d), "total never refills")


def test_suggested_providers_need_minutes(tmp_path):
    d = real()
    del provider(d, "gemini")["connection"]["minutes_estimate"]
    assert has(validate_with(tmp_path, d), "minutes_estimate is needed")


def test_secrets_are_rejected_anywhere(tmp_path):
    for secret in ("sk-or-v1-0123456789abcdef0123456789abcdef", "AIzaSyD1234567890abcdefghijklmnopqrstu", "gsk_abcdefghijklmnopqrstuvwxyz012345",
                   "Xk3Jf92LmQp0RzVb7Yt8Wd4Ns1Hc6GaE5uI0"):
        d = real()
        provider(d, "gemini")["connection"]["steps"][0]["detail"]["en"] = f"Paste {secret} here"
        assert has(validate_with(tmp_path, d), "looks like a key or token"), secret
    d = real()
    provider(d, "gemini")["notes"] = "openrouter-models-api-2026-11-11 and https://aistudio.google.com/apikey and hosted_vllm/BAAI/bge-reranker-v2-m3"
    assert not has(validate_with(tmp_path, d), "looks like a key")
    assert not catalog.find_secretlike(yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")))


def test_duplicate_and_missing_name(tmp_path):
    d = real()
    d["providers"].append(copy.deepcopy(provider(d, "groq")))
    assert has(validate_with(tmp_path, d), "duplicate id")
    d = real()
    del provider(d, "groq")["name"]
    assert has(validate_with(tmp_path, d), "name is needed")


def _resources_copy(tmp_path: Path) -> Path:
    out = tmp_path / "res"
    out.mkdir()
    for f in RESOURCES_DIR.glob("*.yaml"):
        (out / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    return out


def test_resource_and_subscription_rules(tmp_path):
    base = _resources_copy(tmp_path)
    assert catalog.validate_catalog(CATALOG_PATH, base, TODAY) == []
    storage = yaml.safe_load((base / "storage.yaml").read_text(encoding="utf-8"))
    storage["resources"][0]["managed"] = True
    (base / "storage.yaml").write_text(yaml.safe_dump(storage, allow_unicode=True), encoding="utf-8")
    assert has(catalog.validate_catalog(CATALOG_PATH, base, TODAY), "managed must be false")
    storage["resources"][0]["managed"] = False
    storage["resources"][0]["allowance"] = []
    (base / "storage.yaml").write_text(yaml.safe_dump(storage, allow_unicode=True), encoding="utf-8")
    assert has(catalog.validate_catalog(CATALOG_PATH, base, TODAY), "needs at least one allowance")
    subs = yaml.safe_load((base / "subscriptions.yaml").read_text(encoding="utf-8"))
    subs["subscriptions"][0]["token"] = "abc"
    subs["subscriptions"][1]["source_urls"] = []
    subs["subscriptions"][2]["handoff_only"] = False
    (base / "subscriptions.yaml").write_text(yaml.safe_dump(subs, allow_unicode=True), encoding="utf-8")
    problems = catalog.validate_catalog(CATALOG_PATH, base, TODAY)
    assert has(problems, "subscription chatgpt", "no token field")
    assert has(problems, "subscription claude", "documented prefill needs a source URL")
    assert has(problems, "subscription gemini", "handoff_only must be true")


def test_resource_files_must_match_their_kind(tmp_path):
    base = _resources_copy(tmp_path)
    chain = yaml.safe_load((base / "chain.yaml").read_text(encoding="utf-8"))
    chain["kind"] = "storage"
    (base / "chain.yaml").write_text(yaml.safe_dump(chain), encoding="utf-8")
    assert has(catalog.validate_catalog(CATALOG_PATH, base, TODAY), "chain.yaml", "kind: chain")


# ---------------------------------------------------------------- suggestions


def names(connected=(), env=None):
    return [s.provider_id for s in catalog.suggest_next(set(connected), env or {})]


def test_ranking_order_and_local_last():
    order = names()
    assert order[0] == "openrouter"  # one tap first
    assert order[-1] == "local"
    assert order.index("cerebras") < order.index("cohere") < order.index("gemini")  # verified gain per minute before unconfirmed
    assert order.index("cloudflare") < order.index("gemini")  # a verified (non-convertible) gain before none
    assert set(order) == {"openrouter", "cerebras", "cohere", "cloudflare", "gemini", "groq", "jina", "local"}
    assert [s.rank for s in catalog.suggest_next(set(), {})] == list(range(1, len(order) + 1))


def test_connected_disabled_and_env_keys_are_skipped():
    assert "openrouter" not in names({"openrouter"})
    assert "groq" not in names(env={"GROQ_API_KEY": "x"})
    assert "gemini" not in names(env={"MUSE_DISABLE_PROVIDERS": "gemini, nope"})
    assert "cloudflare" in names(env={"CLOUDFLARE_API_KEY": "x"})  # needs the account id too
    assert "cloudflare" not in names(env={"CLOUDFLARE_API_KEY": "x", "CLOUDFLARE_ACCOUNT_ID": "y"})
    assert "local" not in names(env={"OLLAMA_API_BASE": "http://127.0.0.1:11434"})
    assert names(set(p.id for p in catalog.load_providers() if not p.local)) == ["local"]


def test_card_and_phone_rank_after_no_card_options():
    provs = list(catalog.load_providers())
    from dataclasses import replace

    def tweak(pid, **conn):
        for i, p in enumerate(provs):
            if p.id == pid:
                provs[i] = replace(p, connection=replace(p.connection, **conn))

    tweak("cerebras", card_required="yes")
    tweak("jina", card_required="no", phone_required="no")
    tweak("gemini", card_required="no", phone_required="no")
    order = [s.provider_id for s in catalog.suggest_next(set(), {}, providers=tuple(provs))]
    assert order[0] == "openrouter"  # one tap stays first
    assert order.index("gemini") < order.index("cohere") < order.index("cerebras")  # known no-card, then unknown, then needs a card
    assert order.index("jina") < order.index("cohere")


def test_gain_only_from_verified_numbers():
    by = {s.provider_id: s for s in catalog.suggest_next(set(), {})}
    assert by["openrouter"].gain_text("en") == "adds roughly 50 requests a day"
    assert by["cohere"].gain_text("en") == "adds roughly 1,000 requests a month"
    assert by["cerebras"].gain_text("en") == "adds roughly 1,000,000 tokens a day"
    assert by["cloudflare"].gain_text("en").startswith("adds roughly 10,000 neurons a day")
    for pid in ("gemini", "groq", "jina"):
        assert by[pid].gain is None and by[pid].gain_text("en") == "allowance not confirmed"
        assert by[pid].gain_text("zh-TW") == "額度尚未確認"
    assert by["openrouter"].gain_text("zh-TW") == "每天多約 50 次請求"
    assert "minute" not in by["openrouter"].gain_text("en")  # a per-minute cap is not "what you gain"
    assert by["openrouter"].meta_text("en") == "about 1 min, one tap"
    assert by["gemini"].meta_text("en") == "about 4 min, 3 steps"
    assert by["gemini"].status_line("en") == "Next helper to add: Gemini (about 4 min, 3 steps)"
    json.dumps([s.to_dict() for s in by.values()])


# ------------------------------------------------------------------- strings


def test_string_tables_are_complete():
    from muse import connect

    for table in (catalog.STRINGS, connect.CONNECT_STRINGS):
        assert set(table) == {"en", "zh-TW"}
        assert set(table["en"]) == set(table["zh-TW"])
        for key, en in table["en"].items():
            zh = table["zh-TW"][key]
            assert en.strip() and zh.strip(), key
            assert catalog.placeholders(en) == catalog.placeholders(zh), key
            letters = re.sub(r"\{\w+\}", "", en)
            assert zh != en or not re.search(r"[A-Za-z]{3}", letters) or key in {"unit_gb", "reason_http"}, f"{key} is not translated"
    assert re.search(r"[一-鿿]", catalog.STRINGS["zh-TW"]["gain_unknown"])


def test_catalogue_text_has_both_languages():
    for p in catalog.load_providers():
        if p.connection:
            for step in p.connection.steps:
                for lang in catalog.LANGS:
                    assert catalog.pick(step.title, lang).strip() and catalog.pick(step.detail, lang).strip(), (p.id, step.id)
                assert re.search(r"[一-鿿]", step.title["zh-TW"]) and re.search(r"[一-鿿]", step.detail["zh-TW"]), (p.id, step.id)
    for r in catalog.load_resources():
        for lang in catalog.LANGS:
            assert catalog.pick(r.name, lang) and catalog.pick(r.summary, lang) and catalog.pick(r.connection.how, lang), r.id
    for s in catalog.load_subscriptions():
        assert all(catalog.pick(s.description, lang) for lang in catalog.LANGS)


def test_language_choice():
    assert catalog.default_lang({"MUSE_LANG": "zh_TW"}) == "zh-TW"
    assert catalog.default_lang({"MUSE_LANG": "en", "LANG": "zh_TW.UTF-8"}) == "en"
    assert catalog.default_lang({"LANG": "zh_TW.UTF-8"}) == "zh-TW"
    assert catalog.default_lang({"LC_ALL": "zh_CN.UTF-8", "LANG": "en_US"}) == "zh-TW"
    assert catalog.default_lang({"LANG": "en_US.UTF-8"}) == "en"
    assert catalog.default_lang({"LANG": "C"}) == "en" and catalog.default_lang({}) == "en"
    assert catalog.normalise_lang("fr") == "en" and catalog.normalise_lang("zh-Hant") == "zh-TW"


# --------------------------------------------------------------- the renderer


def test_renderer_writes_muse_oneoff_as_boolean():
    cat = real()
    env = {"LITELLM_MASTER_KEY": "sk-test-master", "VOYAGE_API_KEY": "v", "GEMINI_API_KEY": "g", "MUSE_ALLOW_PAID_FALLBACK": "true"}
    config, _ = render_config.render(cat, env)
    seen = set()
    for entry in config["model_list"]:
        flag = entry["model_info"]["muse_oneoff"]
        assert isinstance(flag, bool)
        assert flag == (entry["model_info"]["provider"] == "voyage")
        seen.add(entry["model_info"]["provider"])
    assert {"voyage", "gemini"} <= seen
    cat["providers"][0]["one_off"] = "yes please"  # not a real boolean: never trusted
    config, _ = render_config.render(cat, env)
    assert all(e["model_info"]["muse_oneoff"] is False for e in config["model_list"] if e["model_info"]["provider"] == "gemini")


def test_also_env_gates_a_provider():
    cat = real()
    only_key, enabled = render_config.render(cat, {"CLOUDFLARE_API_KEY": "c"})
    assert "cloudflare" not in enabled and not only_key["model_list"]
    both, enabled = render_config.render(cat, {"CLOUDFLARE_API_KEY": "c", "CLOUDFLARE_ACCOUNT_ID": "acct"})
    assert enabled == ["cloudflare"]
    entry = next(e for e in both["model_list"] if e["model_info"]["id"] == "cloudflare-llama-3-1-8b__fast")
    assert entry["litellm_params"]["model"] == "cloudflare/@cf/meta/llama-3.1-8b-instruct-fp8"
    assert entry["litellm_params"]["api_key"] == "os.environ/CLOUDFLARE_API_KEY" and "acct" not in json.dumps(both)


# ---------------------------------------------------------- snapshot / status


def snap_for(home: Path, **env: str):
    return build_snapshot(now=1_794_000_000, environ={"MUSE_HOME": str(home), **env})


def test_snapshot_additions(_isolated_muse_home: Path):
    snap = snap_for(_isolated_muse_home, GROQ_API_KEY="x")
    json.dumps(snap)
    gemini = next(p for p in snap["providers"] if p["id"] == "gemini")
    assert gemini["connection_method"] == "paste_key" and gemini["minutes_estimate"] == 4
    assert gemini["good_for"] and gemini["kind_of_allowance"] == ["daily"] and gemini["checked_on"] == "2026-11-12" and gemini["confidence"] == "M"
    assert next(p for p in snap["providers"] if p["id"] == "openrouter")["connection_method"] == "oauth_pkce"
    ids = [s["id"] for s in snap["suggestions"]]
    assert ids[0] == "openrouter" and "groq" not in ids and ids[-1] == "local"
    first = snap["suggestions"][0]
    assert first["text"]["en"]["gain"] == "adds roughly 50 requests a day" and "每天" in first["text"]["zh-TW"]["gain"]
    assert {r["kind"] for r in snap["resources"]} == {"storage", "compute", "chain"} and all(r["managed"] is False for r in snap["resources"])
    assert [s["id"] for s in snap["subscriptions"]] == ["chatgpt", "claude", "gemini"] and all(s["handoff_only"] for s in snap["subscriptions"])
    text = json.dumps(snap)
    assert "token" not in " ".join(k for s in snap["subscriptions"] for k in s)  # a link only: no token, login or quota
    assert '"x"' not in text  # the key value is never in a snapshot


def test_snapshot_survives_a_catalogue_without_v2_fields(_isolated_muse_home: Path, tmp_path: Path):
    small = tmp_path / "small.yaml"
    small.write_text(yaml.safe_dump({"aliases": {"fast": {"mode": "chat"}}, "providers": [
        {"id": "p", "env": "P_KEY", "tier": "free", "policy": {}, "key_url": "https://x.example", "deployments": []}]}), encoding="utf-8")
    snap = snap_for(_isolated_muse_home, MUSE_CATALOG=str(small))
    p = snap["providers"][0]
    assert p["name"] == "p" and p["connection_method"] == "none" and p["minutes_estimate"] is None and p["checked_on"] is None
    assert snap["suggestions"][-1]["id"] == "local"


def test_status_line_and_older_snapshots(_isolated_muse_home: Path):
    snap = snap_for(_isolated_muse_home)
    screen = cli.render_status(snap, now=None)
    assert "Next helper to add: OpenRouter (about 1 min, one tap)" in screen
    old = {k: v for k, v in snap.items() if k not in ("suggestions", "resources", "subscriptions")}
    assert "Next helper to add" not in cli.render_status(old)
    assert max(len(cli.strip_ansi(line)) for line in screen.splitlines()) <= cli.WIDTH


def test_status_line_zh(monkeypatch):
    from muse import connect

    snap = {"suggestions": [{"id": "gemini", "name": "Gemini", "text": {"en": {"meta": "about 4 min, 3 steps"}, "zh-TW": {"meta": "約 4 分鐘，3 個步驟"}}}]}
    assert connect.next_helper_line(snap, "zh-TW") == "下一個建議新增的幫手：Gemini（約 4 分鐘，3 個步驟）"
    assert connect.next_helper_line({"suggestions": [{"id": "local", "name": "x", "text": {}}]}, "en") is None
    assert connect.next_helper_line({}, "en") is None


def test_handoff_links():
    subs = {s.id: s for s in catalog.load_subscriptions()}
    assert catalog.handoff_link(subs["claude"], "hello & goodbye?") == "claude://claude.ai/new?q=hello%20%26%20goodbye%3F"
    assert len(catalog.handoff_link(subs["claude"], "a" * 20000)) < 14100
    assert catalog.handoff_link(subs["chatgpt"], "hi", "web") == "https://chatgpt.com/"  # no documented prefill: plain page
    assert catalog.handoff_link(subs["gemini"], "hi", "web") == "https://gemini.google.com/app"
