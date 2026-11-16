"""muse.rules: the routing rules file (rule 2): loading, validation, resolution, explanation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from muse import rules
from muse.rules import DEFAULT_RULES, RulesError, UnknownTask, parse_rules

SHIPPED = Path(__file__).resolve().parents[1] / "config" / "routing.yaml"


def write(tmp_path: Path, text: str, name: str = "routing.yaml") -> dict[str, str]:
    path = tmp_path / name
    path.write_text(text)
    return {"MUSE_ROUTING": str(path)}


def test_shipped_file_is_valid_and_equals_the_built_in_rules():
    shipped = parse_rules(yaml.safe_load(SHIPPED.read_text()), str(SHIPPED))
    assert yaml.safe_load(SHIPPED.read_text()) == DEFAULT_RULES
    assert dict(shipped.tasks) == dict(rules.BUILTIN.tasks) and shipped.hints == rules.BUILTIN.hints
    assert rules.check(SHIPPED) == []
    loaded = rules.load_rules({})
    assert loaded.source == str(SHIPPED) and loaded.problems == ()


@pytest.mark.parametrize("task, app, tokens, alias, ceiling, handoff, chosen", [
    ("light", None, None, "fast", 300, False, "light"),
    ("everyday", None, None, "cheap", 1200, False, "everyday"),
    ("heavy", None, 1000, "smart", 4000, False, "heavy"),
    ("heavy", None, 60000, "smart", 4000, False, "heavy"),  # not over
    ("heavy", None, 60001, "long-context", 4000, False, "heavy"),
    ("frontier", None, None, "smart", None, True, "frontier"),
    ("embed", None, None, "embed", None, False, "embed"),
    ("rerank", None, None, "rerank", None, False, "rerank"),
    ("  LIGHT ", None, None, "fast", 300, False, "light"),
    (None, None, None, "cheap", 1200, False, "everyday"),
    ("", None, None, "cheap", 1200, False, "everyday"),
    (None, "queue", None, "fast", 300, False, "light"),  # per-app default
    (None, "bot", None, "cheap", 1200, False, "everyday"),
    ("heavy", "queue", None, "smart", 4000, False, "heavy"),  # a named task beats the app default
    ("classify-emails", None, None, "fast", 300, False, "light"),  # hint in a task name
    ("weekly_report", None, None, "smart", 4000, False, "heavy"),
    ("summarize this", None, None, "cheap", 1200, False, "everyday"),
])
def test_resolve(task, app, tokens, alias, ceiling, handoff, chosen):
    route = rules.BUILTIN.resolve(task, app=app, input_tokens_estimate=tokens)
    assert (route.alias, route.max_output_tokens, route.handoff, route.task) == (alias, ceiling, handoff, chosen)
    assert route.reason and "'" + alias + "'" in route.reason


def test_unknown_task_lists_the_valid_ones():
    with pytest.raises(UnknownTask) as err:
        rules.BUILTIN.resolve("poetry")
    assert err.value.valid == ["light", "everyday", "heavy", "frontier", "embed", "rerank"]
    assert "unknown task 'poetry'; valid tasks: light, everyday" in str(err.value)
    with pytest.raises(UnknownTask):
        rules.BUILTIN.resolve("classifyemails")  # hints match whole words only


def test_route_mode_and_determinism():
    assert rules.BUILTIN.resolve("embed").mode == "embedding"
    assert rules.BUILTIN.resolve("light").deterministic and not rules.BUILTIN.resolve("everyday").deterministic
    assert rules.BUILTIN.handoff_aliases == frozenset({"smart"})


GOOD = """
tasks:
  quick: {alias: fast, max_output_tokens: 100}
  deep: {alias: smart, handoff: suggest}
  vec: {alias: embed, mode: embedding}
defaults: {task: quick}
apps:
  diary: {default_task: deep}
hints:
  deep: [think hard, analyse]
"""


def test_a_custom_file(tmp_path):
    env = write(tmp_path, GOOD)
    current = rules.load_rules(env)
    assert current.problems == () and current.valid_tasks == ["quick", "deep", "vec"]
    assert current.resolve(None).alias == "fast"
    assert current.resolve(None, app="diary").task == "deep"
    assert current.resolve("please think hard now").task == "deep"  # a phrase hint
    with pytest.raises(UnknownTask):
        current.resolve("light")


@pytest.mark.parametrize("text, words", [
    ("[]", "must be a mapping"),
    ("tasks: {}", "at least one task"),
    ("tasks: {light: {alias: fast}}\nextra: 1", "unknown top-level key 'extra'"),
    ("version: 2\ntasks: {light: {alias: fast}}", "version: must be 1"),
    ("tasks: {Light: {alias: fast}}", "a task name is lower-case"),
    ("tasks: {light: fast}", "tasks.light: must be a mapping"),
    ("tasks: {light: {}}", "tasks.light.alias: required"),
    ("tasks: {light: {alias: 'two words'}}", "tasks.light.alias"),
    ("tasks: {light: {alias: fast, colour: red}}", "unknown key 'colour'"),
    ("tasks: {light: {alias: fast, mode: image}}", "tasks.light.mode: one of chat, embedding, rerank"),
    ("tasks: {light: {alias: fast, max_output_tokens: lots}}", "tasks.light.max_output_tokens: a whole number"),
    ("tasks: {light: {alias: fast, max_output_tokens: 0}}", "tasks.light.max_output_tokens"),
    ("tasks: {light: {alias: fast, max_output_tokens: true}}", "tasks.light.max_output_tokens"),
    ("tasks: {e: {alias: embed, mode: embedding, max_output_tokens: 10}}", "only for chat tasks"),
    ("tasks: {h: {alias: smart, long_input_alias: long-context}}", "go together"),
    ("tasks: {h: {alias: smart, long_input_alias: long-context, long_input_over_tokens: -5}}", "long_input_over_tokens"),
    ("tasks: {f: {alias: smart, handoff: maybe}}", "'suggest' or 'none'"),
    ("tasks: {f: {alias: smart, deterministic: sometimes}}", "deterministic: true or false"),
    ("tasks: {f: {alias: smart, examples: chat}}", "examples: a list"),
    ("tasks: {light: {alias: fast}}\ndefaults: {task: heavy}", "defaults.task: 'heavy' is not one of the tasks"),
    ("tasks: {light: {alias: fast}}\ndefaults: {tsk: light}", "defaults: unknown key 'tsk'"),
    ("tasks: {light: {alias: fast}}\napps: {queue: {default_task: heavy}}", "apps.queue.default_task"),
    ("tasks: {light: {alias: fast}}\napps: {queue: light}", "apps.queue: must be"),
    ("tasks: {light: {alias: fast}}\napps: {'Bad App': {default_task: light}}", "an app name"),
    ("tasks: {light: {alias: fast}}\nhints: {heavy: [x]}", "hints.heavy: 'heavy' is not one of the tasks"),
    ("tasks: {light: {alias: fast}}\nhints: {light: []}", "hints.light: a list"),
    ("tasks: {light: {alias: fast}, b: {alias: x}}\nhints: {light: [tag], b: [tag]}", "already a hint for 'light'"),
    ("tasks: {light: {alias: fast}}\nhints: {light: ['!!']}", "is not a word"),
])
def test_validation_messages(text, words):
    with pytest.raises(RulesError) as err:
        parse_rules(yaml.safe_load(text), "f.yaml")
    assert any(words in p for p in err.value.problems), err.value.problems
    assert all(p.startswith("f.yaml: ") for p in err.value.problems)


def test_every_problem_is_listed():
    with pytest.raises(RulesError) as err:
        parse_rules({"tasks": {"a": {"alias": "x y"}, "b": {}}, "defaults": {"task": "z"}})
    assert len(err.value.problems) == 3


def test_handoff_spellings():
    parsed = parse_rules({"tasks": {"a": {"alias": "x", "handoff": True}, "b": {"alias": "y", "handoff": False}}})
    assert parsed.tasks["a"].handoff and not parsed.tasks["b"].handoff


@pytest.mark.parametrize("text, words", [
    ("tasks: [unclosed", "not valid YAML at line 1"),
    ("tasks:\n  light: {alias: fast, max_output_tokens: lots}\n", "max_output_tokens"),
    ("", "must be a mapping"),
])
def test_a_broken_file_falls_back_to_the_built_in_rules(tmp_path, text, words):
    env = write(tmp_path, text)
    current = rules.load_rules(env)
    assert current.source == "built-in" and any(words in p for p in current.problems)
    assert current.resolve("light").alias == "fast"  # still serving
    assert rules.check(Path(env["MUSE_ROUTING"]))
    lines = rules.explain(current)
    assert any("could not be used" in line for line in lines)


def test_missing_explicit_file_is_a_problem_but_the_default_location_is_not(tmp_path):
    current = rules.load_rules({"MUSE_ROUTING": str(tmp_path / "nope.yaml")})
    assert current.source == "built-in" and "does not exist" in current.problems[0]
    assert rules.check(tmp_path / "nope.yaml") == [f"{tmp_path / 'nope.yaml'}: no such file"]


def test_reload_on_change_without_restart(tmp_path):
    env = write(tmp_path, "tasks: {light: {alias: fast}}\n")
    assert rules.load_rules(env).resolve("light").alias == "fast"
    assert rules.load_rules(env) is rules.load_rules(env)  # cached while unchanged
    path = Path(env["MUSE_ROUTING"])
    path.write_text("tasks: {light: {alias: smart}}\n")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert rules.load_rules(env).resolve("light").alias == "smart"
    path.write_text("tasks: [broken\n")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))
    assert rules.load_rules(env).source == "built-in"


def test_resolve_module_function(tmp_path):
    env = write(tmp_path, GOOD)
    assert rules.resolve(None, app="diary", environ=env).alias == "smart"


def test_explain_in_both_languages():
    en = rules.explain(rules.BUILTIN, "en")
    assert en[0] == ("Task 'light' (for example: classify, extract, tag, short rewrite) uses the alias 'fast', "
                     "and an answer may use at most 300 tokens. Exact repeats may be answered from the cache.")
    assert any("Inputs over about 60,000 tokens use 'long-context' instead" in line for line in en)
    assert any("Task 'frontier'" in line and "subscription app" in line and "approval" in line for line in en)
    assert "A request that names no task is treated as 'everyday'." in en
    assert "The app 'queue' uses 'light' unless it names a task." in en
    assert any(line.startswith("Words in a task name choose the task: 'classify' means light") for line in en)
    assert en[-1] == "These are the built-in rules (no rules file is in use)."
    zh = rules.explain(rules.BUILTIN, "zh")
    assert zh[0] == "「light」類工作（例如：classify、extract、tag、short rewrite）使用別名「fast」，回答最多 300 個 token。完全相同的請求可能直接用快取回答。"
    assert any("改用「long-context」" in line for line in zh)
    assert any("「frontier」" in line and "訂閱的 App" in line and "未經你同意絕不花錢" in line for line in zh)
    assert "沒有指定工作類型的請求一律當作「everyday」。" in zh
    assert "應用程式「queue」沒有指定工作類型時使用「light」。" in zh
    assert zh[-1] == "目前使用內建規則（沒有使用規則檔）。"
    assert len(en) == len(zh)
    assert rules.explain(rules.BUILTIN, "zh-TW") == zh


def test_as_dict():
    data = rules.as_dict(rules.BUILTIN)
    assert data["tasks"]["frontier"]["handoff"] == "suggest" and data["apps"] == {"queue": {"default_task": "light"}}
    assert data["default_task"] == "everyday" and data["hints"][0] == {"words": "classify", "task": "light"}
