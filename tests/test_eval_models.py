"""scripts/eval_models.py: the scorer, the item files, and the harness against a fake proxy. No network beyond 127.0.0.1."""

from __future__ import annotations

import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import eval_models as em

ROOT = Path(__file__).resolve().parents[1]
MASTER = "sk-master-CANARY-7f3a91"
APP_TOKEN = "sk-muse-app-CANARY-55d0c2"
PROVIDER_KEY = "provider-key-CANARY-0b6e44"
JUNK = ["", "   ", "asdf", "我不知道。", "{}", "- a\n- b\n- c", "好的！", "42", '{"answer": "", "sources": [9], "not_found": "maybe"}',
        "A, B, C 或 D", "是", "[1][2][3] 這是答案。", "I cannot help with that."]


def item(expect, lang="zh-TW", **extra):
    return {"id": "t-001", "lang": lang, "tier": "light", "skill": "chores", "task": "classify", "prompt": "x", "expect": expect, "notes": "", **extra}


def passes(expect, output, **kw):
    return em.score(item(expect, **kw), output)["pass"]


@pytest.fixture(scope="module")
def items():
    return em.load_items([em.ZH_DIR, em.EN_DIR])


@pytest.fixture(scope="module")
def gold():
    out = {}
    for directory in (em.ZH_DIR, em.EN_DIR):
        for line in (directory / "gold.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            out[row["id"]] = row["output"]
    return out


# ================================================================ scoring kinds


@pytest.mark.parametrize("output, ok", [
    ("帳單", True), (" 帳單。\n", True), ("「帳單」", True), ("**帳單**", True), ("答案：帳單", True), ("類別是帳單", True),
    ("```\n帳單\n```", True), ("帳單！", True), ("账单", True),  # Simplified: content right, script point lost (below)
    ("帳單和廣告", False), ("廣告", False), ("這是一則帳單通知", False), ("", False),
])
def test_exact(output, ok):
    assert passes({"exact": "帳單"}, output) is ok


def test_exact_ignores_width_case_and_inner_spaces():
    assert passes({"exact": "+886912345678"}, "＋８８６９１２３４５６７８")
    assert passes({"exact": "B"}, "b.")
    assert passes({"exact": "2025-03-05"}, "２０２５－０３－０５")
    assert passes({"exact": "15:55"}, "15：55")
    assert not passes({"exact": "2025-03-05"}, "114-03-05")
    assert not passes({"exact": "2025-03-05"}, "民國114年3月5日")
    assert not passes({"exact": "2025-03-05"}, "2025-03-05 或 2025-03-06")


def test_one_of():
    expect = {"one_of": ["zh-TW", "zh-TW（繁體中文）", "繁體中文"]}
    for good in ("zh-TW", "zh-tw", "ZH-TW。", "zh-TW（繁體中文）", "zh-TW(繁體中文)", "繁體中文"):
        assert passes(expect, good), good
    for bad in ("zh-CN", "zh", "zh-TW 或 zh-CN", "簡體中文"):
        assert not passes(expect, bad), bad


def test_extract_json_bare_fenced_and_inside_prose():
    assert em.extract_json('{"a": 1}') == {"a": 1}
    assert em.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert em.extract_json('好的，以下是結果：\n{"a": 1, "b": {"c": 2}}\n希望有幫助。') == {"a": 1, "b": {"c": 2}}
    assert em.extract_json('結果如下 ```\n{"a": "x"}\n``` 完成') == {"a": "x"}
    assert em.extract_json('{"a": 1,}') == {"a": 1}  # trailing comma
    assert em.extract_json("{“a”: “甲”}") == {"a": "甲"}  # curly quotes
    assert em.extract_json("沒有 JSON") is None
    assert em.extract_json("[1, 2]") is None
    assert em.extract_json("{壞掉") is None


FIELDS = {"store": "好日子文具行", "date": {"type": "date", "value": "2025-03-14"}, "total": 165}


@pytest.mark.parametrize("output, ok", [
    ('{"store": "好日子文具行", "date": "2025-03-14", "total": 165}', True),
    ('```json\n{"store": "好日子文具行", "date": "2025-03-14", "total": 165}\n```', True),
    ('這是取出的資料：{"store": "好日子文具行", "date": "2025/3/14", "total": "165 元"}，請參考。', True),
    ('{"store": " 好日子文具行。", "date": "２０２５－０３－１４", "total": "１６５"}', True),  # full-width digits
    ('{"Store": "好日子文具行", "DATE": "2025-03-14", "Total": 165.0}', True),  # key case
    ('{"store": "好日子文具行", "date": "2025-03-14", "total": "NT$165"}', True),
    ('{"store": "好日子文具行", "date": "114-03-14", "total": 165}', False),  # 民國 year left as it was
    ('{"store": "好日子文具行", "date": "民國114年3月14日", "total": 165}', False),
    ('{"store": "好日子文具行", "date": "2025年3月14日", "total": 165}', False),  # not the ISO form asked for
    ('{"store": "好日子文具行", "date": "2025-03-14", "total": 166}', False),
    ('{"store": "好日子", "date": "2025-03-14", "total": 165}', False),
    ('{"store": "好日子文具行", "date": "2025-03-14"}', False),  # field missing
    ('{"store": "好日子文具行", "date": "2025-03-14", "total": true}', False),
    ("store: 好日子文具行, date: 2025-03-14, total: 165", False),
])
def test_json_fields(output, ok):
    assert passes({"json_fields": FIELDS}, output) is ok


def test_json_field_types():
    def field(spec, got):
        return passes({"json_fields": {"f": spec}}, json.dumps({"f": got}, ensure_ascii=False))

    assert field({"type": "date", "value": "2025-03-14", "roc_ok": True}, "民國114年3月14日")
    assert field({"type": "date", "value": "2025-03-14", "roc_ok": True}, "114/03/14")
    assert field({"type": "date", "value": "2025-03-14", "loose": True}, "2025年3月14日")
    assert not field({"type": "date", "value": "2025-03-14"}, "2025-13-14")
    assert field({"type": "number", "value": 1200}, "1,200") and field(1200, "１，２００元") and field(1.2, "1.2 公斤")
    assert field({"type": "number", "value": 100, "tol": 1}, 100.6) and not field({"type": "number", "value": 100, "tol": 1}, 102)
    assert not field(1200, "1200 或 1300") and not field(1200, "一千二")
    assert field(True, True) and field(True, "true") and not field(True, 1) and not field(False, None) and field(False, False)
    assert field({"type": "string", "values": ["台北市", "臺北市"]}, "臺北市") and not field({"type": "string", "values": ["台北市"]}, "新北市")
    assert field("100號", "100 號") and field("4471", 4471)
    assert field(["a", "b"], ["b", "a"]) and not field({"type": "list", "value": ["a", "b"], "ordered": True}, ["b", "a"]) and not field(["a"], "a")
    assert field({"type": "present"}, ["x"]) and not field({"type": "present"}, []) and not field({"type": "present"}, "")
    assert field({"type": "contains", "all": ["Demo", ["改期", "延期"]]}, "Demo 延期通知") and not field({"type": "contains", "all": ["Demo"]}, "會議")
    assert field(None, None) and field({"type": "null"}, "") and not field(None, "有東西")
    assert passes({"json_fields": {"f": None, "g": 1}}, '{"g": 1}')  # a null field may be absent
    assert field("軟體", "软件")  # folded: content credit, script point lost


def test_json_keys():
    assert passes({"json_keys": ["a", "b"], "json_fields": {"a": 1}}, '{"b": 2, "a": 1}')
    assert not passes({"json_keys": ["a", "b"], "json_fields": {"a": 1}}, '{"a": 1, "b": 2, "c": 3}')
    assert not passes({"json_keys": ["a", "b"], "json_fields": {"a": 1}}, '{"a": 1}')


def test_contains_all_any_none():
    expect = {"contains_all": ["下週一", ["十點", "10點", "10:00"], {"re": r"七\s*樓|7\s*樓"}]}
    assert passes(expect, "月會改到下週一上午十點，在七樓。")
    assert passes(expect, "月會改到下週一上午 10 點，在 7 樓。")  # white space inside is ignored for strings
    assert passes(expect, "月會改到下週一１０：００，在７樓")  # full-width
    assert not passes(expect, "月會改到下週二上午十點，在七樓。")
    assert not passes(expect, "月會改到下週一，在七樓。")
    assert passes({"contains_any": ["免費", "不收費"]}, "這是不收費的用量。") and not passes({"contains_any": ["免費", "不收費"]}, "這是用量。")
    polite = {"contains_all": ["您"], "contains_none": ["到底", ["拖", "快點"]]}
    assert passes(polite, "想請問您報告何時可以提供？")
    assert not passes(polite, "您到底什麼時候交？") and not passes(polite, "請您快點交。") and not passes(polite, "請問報告何時提供？")
    assert passes({"contains_all": ["KPI report"]}, "the kpi  Report is ready", lang="en")


def test_simplified_output_is_detected_and_is_a_sub_score_unless_the_item_says_hard():
    soft = em.score(item({"contains_all": ["軟體", "預設"]}), "这个软件的默认设置在这里。")
    assert soft["pass"] is True and soft["script_ok"] is False  # right content, wrong script
    mainland_words = em.score(item({"contains_all": ["軟體"]}), "這個軟件的默認設置在這裡。")
    assert mainland_words["pass"] is True and mainland_words["script_ok"] is False  # Traditional characters, mainland wording
    good = em.score(item({"contains_all": ["軟體", "預設"]}), "這個軟體的預設設定在這裡。")
    assert good["pass"] is True and good["script_ok"] is True
    hard = {"contains_all": ["軟體", "預設"], "contains_none": ["軟件"], "script": "hard"}
    assert em.score(item(hard), "這個軟體的預設設定在哪裡？")["pass"] is True
    for wrong in ("这个软件的默认设置在哪里？", "這個軟件的默認設置在哪裡？", "這個軟體的預設設定在哪里？這個没問題"):
        result = em.score(item(hard), wrong)
        assert result["pass"] is False, wrong
    assert em.score(item(hard), "這個軟體的預設設定在哪裡？这")["checks"]["script"] is False
    assert em.score(item({"exact": "ok"}, lang="en"), "ok")["script_ok"] is None  # not checked for English items
    assert em.score(item({"exact": "zh-CN"}), "zh-CN")["script_ok"] is None  # no Chinese in the answer


def test_script_check_and_table():
    assert em.script_check("請把影片上傳到網路硬碟，謝謝您。") == {"ok": True, "simplified": [], "mainland_terms": []}
    report = em.script_check("请把视频上传到网络硬盘")
    assert report["ok"] is False and "请" in report["simplified"] and "視頻" in report["mainland_terms"] and "硬盤" in report["mainland_terms"]
    assert em.script_check("Hello 123")["ok"] is None
    assert em.fold_script("软件和鼠标") == "軟體和滑鼠"
    assert 300 <= len(em.S2T) <= 1000  # small on purpose
    for simplified, traditional in em.S2T.items():
        assert simplified != traditional and traditional not in em.S2T, (simplified, traditional)
    # characters that are ordinary Traditional usage must never be flagged
    assert not set("台里後后面只干表準准采松系制云余並郁願著髮發") & set(em.S2T)
    for mainland, taiwan in em.MAINLAND_TERMS.items():
        assert em.script_check(mainland)["simplified"] == [] and mainland not in taiwan, mainland


def test_cites():
    expect = {"cites": {"passages": [2], "mode": "exact"}, "contains_all": ["14"]}
    assert passes(expect, '{"answer": "14 天內退還", "sources": [2], "not_found": false}')
    assert passes(expect, '{"answer": "14 天內退還", "sources": ["[2]"], "not_found": false}')
    assert passes(expect, '{"answer": "14 天內退還", "sources": 2}')
    assert passes(expect, "房東應在 14 天內退還 [2]。") and passes(expect, "房東應在 14 天內退還［２］。") and passes(expect, "14 天內退還【2】")
    assert not passes(expect, '{"answer": "14 天內退還", "sources": [1, 2]}')  # an extra passage
    assert not passes(expect, '{"answer": "14 天內退還", "sources": []}')
    assert not passes(expect, "房東應在 14 天內退還。") and not passes(expect, "房東應在 14 天內退還 [3]。")
    both = {"cites": {"passages": [1, 2], "mode": "exact"}, "contains_all": ["A"]}
    assert passes(both, "A 較早 [1][2]") and passes(both, "A 較早 [2]，另見 [1]") and not passes(both, "A 較早 [1]")
    superset = {"cites": {"passages": [2], "mode": "superset"}, "contains_all": ["14"]}
    assert passes(superset, "14 天 [2][3]") and not passes(superset, "14 天 [3]") and not passes(superset, "14 天")
    none = {"json_fields": {"not_found": True}, "cites": {"passages": [], "mode": "exact"}}
    assert passes(none, '{"answer": "", "sources": [], "not_found": true}')
    assert not passes(none, '{"answer": "可以", "sources": [1], "not_found": true}') and not passes(none, '{"answer": "", "sources": [], "not_found": false}')


@pytest.mark.parametrize("output, ok", [
    ("745", True), ("745 元", True), ("７４５", True), ("答案是 745。", True), ("85 × 3 = 255，1000 − 255 = 745", True),
    ("先算 255 元，所以找回 745 元。", True), ('{"answer": 745}', True), ("745.0", True), ("NT$745", True),
    ("答案：745（共買 3 盒）", True),  # the number after 答案 wins over a later one
    ("744", False), ("七百四十五", False), ("745 或 755", False), ("", False), ("不知道", False), ('{"answer": "約 700"}', False),
])
def test_number(output, ok):
    assert passes({"number": {"value": 745, "tol": 0}}, output) is ok


def test_number_tolerance_and_thousands():
    assert passes({"number": 1192}, "1,192") and passes({"number": 1192}, "１，１９２ 元")
    assert passes({"number": {"value": 23.3, "tol": 0.1}}, "約 23.33 小時") and not passes({"number": {"value": 23.3, "tol": 0.1}}, "23.5")


def test_max_chars_and_over_length():
    expect = {"contains_all": ["十點"], "max_chars": 12}
    assert passes(expect, "月會改到下週一十點。") and passes(expect, "\n  月會改到下週一十點。  \n")
    assert not passes(expect, "各位同仁，月會改到下週一的上午十點。")
    assert em.visible_len("  一二三\n") == 3


def test_bullets():
    three = {"bullets": 3, "contains_all": ["甲"]}
    for good in ("- 甲\n- 乙\n- 丙", "• 甲\n• 乙\n• 丙", "1. 甲\n2. 乙\n3. 丙", "1、甲\n2、甲\n3、丙", "（1）甲\n（2）乙\n（3）丙", "一、甲\n二、乙\n三、丙",
                 "以下是重點：\n\n- 甲\n- 乙\n- 丙\n", "* 甲\n* 乙\n* 丙", "１. 甲\n２. 乙\n３. 丙"):
        assert passes(three, good), good
    assert not passes(three, "- 甲\n- 乙\n- 丙\n- 丁")  # an extra bullet
    assert not passes(three, "- 甲\n- 乙") and not passes(three, "甲、乙、丙")
    assert passes({"bullets": {"min": 2, "max": 4}, "contains_all": ["甲"]}, "- 甲\n- 乙\n- 丙\n- 丁")
    assert not passes({"bullets": {"min": 2, "max": 4}, "contains_all": ["甲"]}, "- 甲")
    short = {"bullets": {"count": 2, "max_chars_each": 5}, "contains_all": ["甲"]}
    assert passes(short, "- 甲乙丙丁戊\n- 己") and not passes(short, "- 甲乙丙丁戊己\n- 庚")
    assert em.bullet_lines("- a\n文字\n  2) b") == ["a", "b"]


def test_an_item_passes_only_when_every_hard_check_passes_and_sub_scores_are_kept():
    expect = {"bullets": 2, "contains_all": ["甲"], "contains_none": ["丁"], "max_chars": 20}
    result = em.score(item(expect), "- 甲\n- 丁")
    assert result["pass"] is False
    assert result["checks"] == {"not_empty": True, "bullets": True, "contains_all": True, "contains_none": False, "max_chars": True}
    empty = em.score(item(expect), "  ")
    assert empty["pass"] is False and empty["checks"]["not_empty"] is False and not any(empty["checks"].values())
    assert em.score(item(expect), None)["pass"] is False


def test_unknown_kind_is_an_error_not_a_pass():
    with pytest.raises(ValueError):
        em.score(item({"judge": "nice"}), "x")


def test_dates():
    assert em.parse_date("2025-3-5") == "2025-03-05" and em.parse_date("2025/03/05") == "2025-03-05" and em.parse_date("２０２５.３.５") == "2025-03-05"
    assert em.parse_date("114/3/5") is None and em.parse_date("114/3/5", roc_ok=True) == "2025-03-05"
    assert em.parse_date("民國99年7月1日", roc_ok=True) == "2010-07-01" and em.parse_date("中華民國114年10月10日", roc_ok=True) == "2025-10-10"
    assert em.parse_date("2025-02-31") == "2025-02-31"  # the form is checked, not the calendar
    assert em.parse_date("tomorrow") is None and em.parse_date(20250305) is None


# ================================================================ the item files


def test_item_files_parse_and_follow_the_rules(items):
    ids = [i["id"] for i in items]
    assert len(ids) == len(set(ids))
    for path in sorted(em.ZH_DIR.glob("items*.jsonl")) + sorted(em.EN_DIR.glob("items*.jsonl")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            row = json.loads(line)  # every line parses
            assert em.item_problems(row) == [], f"{path.name}:{number}"
            assert set(row["expect"]) <= set(em.KINDS)
            assert row["tier"] in em.TIERS and row["skill"] in em.SKILLS and row["task"] in em.TASKS[row["tier"]]
            assert row["notes"].strip(), f"{row['id']}: notes"
            assert row["lang"] == ("zh-TW" if path.parent == em.ZH_DIR else "en")


def test_counts_are_within_the_targets(items):
    zh = [i for i in items if i["lang"] == "zh-TW"]
    en = [i for i in items if i["lang"] == "en"]
    tiers = Counter(i["tier"] for i in zh)
    assert 190 <= len(zh) <= 210 and 80 <= tiers["light"] <= 100 and 100 <= tiers["everyday"] <= 120
    assert 18 <= len(en) <= 24 and {i["tier"] for i in en} == set(em.TIERS)
    tasks = Counter(i["task"] for i in zh)
    assert set(tasks) == {t for tier in em.TASKS.values() for t in tier}  # every category is used
    assert min(tasks.values()) >= 10
    assert set(Counter(i["skill"] for i in zh)) == set(em.SKILLS)
    assert sum(1 for i in zh if "not_found" == i["task"]) >= 10
    used = {kind for i in items for kind in i["expect"]}
    assert used >= set(em.KINDS) - {"script"} and any(i["expect"].get("script") == "hard" for i in zh)


def test_zh_tw_prompts_are_traditional_with_taiwan_wording(items):
    for i in items:
        if i["lang"] != "zh-TW":
            continue
        report = em.script_check(em.item_text(i))
        if i.get("non_tw_text"):
            assert i["task"] in ("lang", "s2t"), i["id"]  # only items that are about Simplified or other languages
            continue
        assert report["simplified"] == [] and report["mainland_terms"] == [], (i["id"], report)
        for spec_text in json.dumps(i["expect"], ensure_ascii=False), i["notes"]:
            if i["task"] != "s2t":
                assert em.script_check(spec_text)["simplified"] == [], i["id"]
    assert sum(1 for i in items if i.get("non_tw_text")) >= 15  # Simplified detection and conversion are really covered


def test_item_problems_catches_bad_items():
    good = item({"exact": "a"}, id="ok-001")
    assert em.item_problems(good) == []
    assert em.item_problems({**good, "tier": "heavy"}) and em.item_problems({**good, "skill": "poetry"}) and em.item_problems({**good, "task": "cite_qa"})
    assert em.item_problems({**good, "expect": {"judge": 1}}) and em.item_problems({**good, "expect": {}}) and em.item_problems({**good, "id": "Bad ID"})
    assert em.item_problems({**good, "expect": {"max_chars": 10}})  # no positive check: junk could pass
    assert em.item_problems({**good, "messages": [{"role": "user", "content": "x"}]})  # prompt and messages together
    assert em.item_problems({**good, "expect": {"json_fields": {"a": {"type": "fuzzy"}}}}) and em.item_problems({**good, "extra": 1})
    chat = {k: v for k, v in good.items() if k != "prompt"} | {"messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]}
    assert em.item_problems(chat) == [] and em.item_messages(chat)[0]["role"] == "system" and em.item_text(chat) == "s\nu"


def test_load_items_stops_on_a_broken_file(tmp_path):
    (tmp_path / "items-a.jsonl").write_text('{"id": "x"}\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        em.load_items([tmp_path])
    row = json.dumps(item({"exact": "a"}, id="dup-001"))
    (tmp_path / "items-a.jsonl").write_text(row + "\n" + row + "\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="twice"):
        em.load_items([tmp_path])


# ================================================================ gold and junk replays


def test_gold_has_a_passing_answer_for_every_item(items, gold):
    assert set(gold) == {i["id"] for i in items}
    for i in items:
        result = em.score(i, gold[i["id"]])
        assert result["pass"], (i["id"], result["checks"])
        if i["lang"] == "zh-TW":
            assert result["script_ok"] is not False, i["id"]  # the gold answers are themselves Taiwan usage


def test_no_item_passes_on_empty_or_garbage_output(items):
    for i in items:
        for junk in JUNK:
            assert not em.score(i, junk)["pass"], (i["id"], junk)


def test_answering_another_items_gold_rarely_passes(items, gold):
    """Shift every gold answer by one item within its task: the scorer must tell answers apart."""
    by_task: dict[tuple[str, str], list[dict]] = {}
    for i in items:
        by_task.setdefault((i["lang"], i["task"]), []).append(i)
    wrong = total = 0
    for group in by_task.values():
        for index, i in enumerate(group):
            other = group[(index + 1) % len(group)]
            if gold[other["id"]] == gold[i["id"]] or len(group) < 2:
                continue
            total += 1
            wrong += em.score(i, gold[other["id"]])["pass"]
    assert total > 150 and wrong / total < 0.08, (wrong, total)


def test_replay_gold_and_junk_through_the_command_line(items, gold, tmp_path, capsys):
    report_path = tmp_path / "replay.json"
    assert em.main(["--replay", str(em.ZH_DIR / "gold.jsonl"), "--report", str(report_path), "--no-control"]) == 0
    out = capsys.readouterr().out
    assert "100% (200/200)" in out and "replay" in out
    report = json.loads(report_path.read_text(encoding="utf-8"))
    summary = report["targets"]["replay"]["summary"]
    assert summary["zh_tw"]["rate"] == 1.0 and summary["script"]["rate"] == 1.0 and summary["zh_tw"]["by_tier"]["light"]["scored"] == 90
    assert '"output"' not in json.dumps(report)  # outputs are not kept by default

    mixed = tmp_path / "mixed.jsonl"
    rows = [{"id": i["id"], "output": gold[i["id"]], "target": "good"} for i in items]
    rows += [{"id": i["id"], "output": "asdf", "target": "junk"} for i in items]
    rows += [{"id": i["id"], "output": gold[i["id"]].translate({ord(v): k for k, v in em.S2T.items()}), "target": "simplified"}
             for i in items if i["lang"] == "zh-TW" and i["task"] == "summarise"]
    mixed.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    assert em.main(["--replay", str(mixed), "--report", str(report_path), "--keep-outputs", "--suggest"]) == 0
    out = capsys.readouterr().out
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["targets"]["good"]["summary"]["en_control"]["rate"] == 1.0 and report["targets"]["good"]["summary"]["zh_tw_gap"] == 0
    assert report["targets"]["junk"]["summary"]["zh_tw"]["passed"] == 0 and report["targets"]["junk"]["summary"]["en_control"]["passed"] == 0
    simplified = report["targets"]["simplified"]["summary"]
    assert simplified["zh_tw"]["rate"] == 1.0 and simplified["script"]["rate"] == 0.0  # right content, Simplified script
    assert report["targets"]["good"]["items"]["zt-cls-01"]["output"] == gold["zt-cls-01"]
    assert "No free target" in out
    # a report written with --keep-outputs can itself be replayed
    assert em.main(["--replay", str(report_path), "--tier", "light"]) == 0
    assert "(90/90)" in capsys.readouterr().out
    assert em.main(["--replay", str(tmp_path / "mixed.jsonl"), "--tasks", "nothing"]) == 2


def test_sample_limit_and_tier_choose_items_repeatably(items):
    def choose(*argv):
        return [i["id"] for i in em.choose_items(list(items), em.build_parser().parse_args(list(argv)))]

    assert len(choose()) == 220 and len(choose("--no-control")) == 200 and len(choose("--tier", "light")) == 100
    sample = choose("--sample", "30", "--seed", "7")
    assert len(sample) == 30 and sample == choose("--sample", "30", "--seed", "7") and sample != choose("--sample", "30", "--seed", "8")
    langs = Counter(i["lang"] for i in items if i["id"] in sample)
    tiers = Counter(i["tier"] for i in items if i["id"] in sample)
    assert langs["en"] >= 2 and tiers["light"] >= 10 and tiers["everyday"] >= 10  # spread over sets and tiers
    assert choose("--limit", "5") == [i["id"] for i in items[:5]]
    assert set(choose("--tasks", "s2t,lang")) == {i["id"] for i in items if i["task"] in ("s2t", "lang")}


# ================================================================ the harness against a fake proxy


class FakeProxy:
    """A tiny OpenAI-compatible server. `fast` answers correctly, `smart` answers junk, `cheap` always defers."""

    MODELS = ["fast", "cheap", "smart", "long-context", "embed", "gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-embedding-2", "deepseek-flash"]

    def __init__(self, items, gold):
        self.gold = {em.item_text(i): gold[i["id"]] for i in items}
        self.calls: list[dict] = []
        self.auth_seen: set[str] = set()
        self.first_429 = {"fast": 1}  # the first chat call to `fast` gets a short Retry-After
        self.defer_forever = {"cheap"}
        self.down: set[str] = set()
        self.tokens = {MASTER, APP_TOKEN}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload, headers=None):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _authorised(self):
                value = self.headers.get("Authorization", "")
                outer.auth_seen.add(value)
                return value in {f"Bearer {token}" for token in outer.tokens}

            def do_GET(self):
                if not self._authorised():
                    return self._send(401, {"error": {"message": "no"}})
                if self.path == "/v1/models":
                    return self._send(200, {"data": [{"id": m} for m in outer.MODELS]})
                self._send(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if not self._authorised():
                    return self._send(401, {"error": {"message": "no"}})
                model = body["model"]
                outer.calls.append({"model": model, "body": body, "headers": {k.lower(): v for k, v in self.headers.items()}})
                if model in outer.down:
                    return self._send(500, {"error": {"message": "CANARY-ERROR-TEXT boom"}})
                if model in outer.defer_forever:
                    return self._send(429, {"error": {"message": "[muse] later"}}, {"Retry-After": "3", "x-muse-wait": "provider-limit"})
                if outer.first_429.get(model, 0) > 0:
                    outer.first_429[model] -= 1
                    return self._send(429, {"error": {"message": "[muse] pace"}}, {"Retry-After": "2", "x-muse-ration": "defer:pace"})
                prompt = "\n".join(m["content"] for m in body["messages"])
                headers = {"x-muse-provider": "gemini", "x-muse-deployment": f"gemini-3.1-flash-lite__{model}" if model in ("fast", "auto") else model,
                           "x-muse-cost-usd": "0.000125" if model == "deepseek-flash" else "0"}
                if model == "auto":
                    headers["x-muse-task"] = self.headers.get("x-muse-task", "")
                if model == "smart":
                    headers["x-muse-answer"] = "empty-reasoning"
                    content = ""
                else:
                    content = outer.gold.get(prompt, "CANARY-UNKNOWN-PROMPT")
                self._send(200, {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}, headers)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake(items, gold):
    proxy = FakeProxy(items, gold)
    yield proxy
    proxy.close()


@pytest.fixture
def sleeps(monkeypatch):
    waited: list[float] = []
    monkeypatch.setattr(em, "_sleep", waited.append)
    return waited


def environ(tmp_path, **extra):
    return {"LITELLM_MASTER_KEY": MASTER, "GEMINI_API_KEY": PROVIDER_KEY, "MUSE_HOME": str(tmp_path / "home"),
            "MUSE_ENV_FILE": str(tmp_path / "none.env"), "MUSE_CATALOG": str(ROOT / "config" / "providers.yaml"), "PATH": "", **extra}


def run(fake, tmp_path, *argv, env=None):
    report = tmp_path / "report.json"
    code = em.main(["--proxy-url", fake.url, "--report", str(report), "--delay", "0", *argv], environ=env or environ(tmp_path))
    return code, (json.loads(report.read_text(encoding="utf-8")) if report.exists() else None)


def assert_no_secret(*texts):
    for text in texts:
        for secret in (MASTER, APP_TOKEN, PROVIDER_KEY, "Bearer ", "Authorization", "CANARY"):
            assert secret not in text, f"{secret!r} leaked"


def test_plan_is_printed_and_nothing_is_sent_without_confirmation(fake, tmp_path, capsys, sleeps, monkeypatch):
    monkeypatch.setattr("sys.stdin", type("NoTty", (), {"isatty": lambda self: False})())
    code, report = run(fake, tmp_path, "--limit", "4")
    out, err = capsys.readouterr()
    assert code == 2 and "pass --yes" in err and report is None and fake.calls == []
    assert "what this will send" in out and "at most 4 targets x 4 items = 16 requests in all" in out and "at most 32 HTTP calls" in out
    for alias in ("fast", "cheap", "smart", "long-context"):
        assert alias in out
    assert "free" in out and "Free targets only" in out and "master key (LITELLM_MASTER_KEY)" in out
    assert "embed" not in out.replace("gemini-embedding", "") and "deepseek" not in out  # not chat aliases; deployments only on request
    assert_no_secret(out, err)

    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    code, report = run(fake, tmp_path, "--limit", "4")
    assert code == 0 and "Nothing was sent." in capsys.readouterr().out and fake.calls == [] and report is None

    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    code, report = run(fake, tmp_path, "--limit", "2", "--only", "long-context")
    assert code == 0 and len(fake.calls) == 2 and report["targets"]["long-context"]["summary"]["zh_tw"]["passed"] == 2


def test_run_report_shape_retry_deferral_and_no_secrets(fake, tmp_path, capsys, sleeps, items, gold):
    code, report = run(fake, tmp_path, "--yes", "--limit", "12", "--stop-after", "3", "--suggest")
    out, err = capsys.readouterr()
    assert code == 0, out + err
    raw = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert_no_secret(out, err, raw)
    for text in list(fake.gold) + list(fake.gold.values()):
        if len(text) > 12:
            assert text not in raw  # neither prompts nor outputs are stored by default
    assert fake.auth_seen == {f"Bearer {MASTER}"}

    assert set(report["targets"]) == {"fast", "cheap", "smart", "long-context"} and report["auth"] == "master key (LITELLM_MASTER_KEY)"
    assert report["set"]["items"] == 12 and len(report["set"]["hash"]) == 16 and report["finished"]
    fast = report["targets"]["fast"]
    assert fast["kind"] == "alias" and fast["status"] == "free" and fast["providers"] == ["gemini"]
    summary = fast["summary"]
    assert summary["zh_tw"] == {"scored": 12, "passed": 12, "rate": 1.0,
                                "by_tier": {"light": {"scored": 12, "passed": 12, "rate": 1.0}},
                                "by_task": {"classify": {"scored": 12, "passed": 12, "rate": 1.0}},
                                "by_skill": {"chores": {"scored": 10, "passed": 10, "rate": 1.0}, "setup": {"scored": 2, "passed": 2, "rate": 1.0}}}
    assert summary["script"] == {"checked": 12, "ok": 12, "rate": 1.0} and summary["deferred"] == 0 and summary["not_run"] == 0
    assert summary["answered_by"] == {"gemini-3.1-flash-lite__fast": 12} and summary["cost_usd"] == 0 and summary["mean_latency_ms"] is not None
    one = fast["items"]["zt-cls-01"]
    assert one["state"] == "pass" and one["http"] == 200 and one["provider"] == "gemini" and one["checks"] == {"not_empty": True, "exact": True}
    assert one["script_ok"] is True and "output" not in one and one["waited_s"] == 2.5  # the first call was a 429 with Retry-After: 2
    assert 2.5 in sleeps  # Retry-After honoured, plus half a second

    cheap = report["targets"]["cheap"]  # every call 429: each item is tried twice, then deferred; the target stops after 3 in a row
    assert cheap["summary"]["deferred"] == 3 and cheap["summary"]["not_run"] == 9 and "3 items in a row" in cheap["stopped"]
    assert cheap["items"]["zt-cls-01"] == {"state": "deferred", "http": 429, "retry_after_s": 3.0, "waited_s": 3.5, "why": "provider-limit"}
    assert sum(1 for c in fake.calls if c["model"] == "cheap") == 6 and "stopped after 3" in out

    smart = report["targets"]["smart"]["summary"]  # 200 with no visible text
    assert smart["zh_tw"]["passed"] == 0 and smart["zh_tw"]["scored"] == 12 and smart["empty_reasoning"] == 12
    assert report["requests_sent"] == len(fake.calls) == 13 + 6 + 12 + 12

    for call in fake.calls:  # what each request carried
        assert call["body"]["temperature"] == 0 and call["body"]["max_tokens"] == em.MAX_TOKENS["classify"]
        assert "x-muse-task" not in call["headers"] and "x-muse-allow-paid" not in call["headers"]
        assert call["headers"]["x-muse-privacy"] == "public" and set(call["body"]) == {"model", "messages", "temperature", "max_tokens"}

    assert "target" in out and "zh-TW" in out and "100% (12/12)" in out  # the table
    assert "Suggestion from this report" in out and "too few" in out and "Nothing is edited" in out


def test_resume_sends_only_what_is_missing(fake, tmp_path, capsys, sleeps):
    code, first = run(fake, tmp_path, "--yes", "--limit", "8", "--only", "fast,cheap", "--stop-after", "2")
    assert code == 0 and first["targets"]["cheap"]["summary"]["deferred"] == 2
    sent = len(fake.calls)
    fake.defer_forever.clear()  # the allowance came back
    capsys.readouterr()
    code, second = run(fake, tmp_path, "--yes", "--limit", "8", "--only", "fast,cheap", "--stop-after", "2", "--resume")
    out = capsys.readouterr().out
    assert code == 0 and "8 already answered" in out and "at most 2 targets x 8 items = 8 requests in all" in out
    assert [c["model"] for c in fake.calls[sent:]] == ["cheap"] * 8  # nothing for `fast` again
    assert second["targets"]["cheap"]["summary"]["zh_tw"]["passed"] == 8 and second["targets"]["cheap"]["summary"]["deferred"] == 0
    assert "stopped" not in second["targets"]["cheap"] and second["targets"]["fast"]["summary"]["zh_tw"]["passed"] == 8
    assert second["requests_sent"] == len(fake.calls) and second["started"] == first["started"]

    code, _ = run(fake, tmp_path, "--yes", "--limit", "9", "--only", "fast", "--resume")  # a different set of items
    assert code == 2 and "different set of items" in capsys.readouterr().err
    code, _ = em.main(["--proxy-url", fake.url, "--report", str(tmp_path / "absent.json"), "--yes", "--resume"], environ=environ(tmp_path)), None
    assert code == 2


def test_long_retry_after_is_not_waited_for_and_refusals_defer_at_once(fake, tmp_path, capsys, sleeps):
    code, report = run(fake, tmp_path, "--yes", "--limit", "3", "--only", "cheap", "--max-wait", "1")
    assert code == 0 and report["targets"]["cheap"]["summary"]["deferred"] == 3
    assert sleeps == [] and len(fake.calls) == 3  # Retry-After 3 > --max-wait 1: no wait, no second call


def test_deployments_paid_targets_and_the_app_token(fake, tmp_path, capsys, sleeps):
    env = environ(tmp_path, DEEPSEEK_API_KEY=PROVIDER_KEY, MUSE_EVAL_APP_TOKEN=APP_TOKEN)
    code, report = run(fake, tmp_path, "--yes", "--limit", "2", "--deployments", "--only", "gemini-3.1-flash-lite,deepseek-flash", env=env)
    out, err = capsys.readouterr()
    assert code == 0 and "deepseek-flash" in out and "LEFT OUT" in out and "app token from MUSE_EVAL_APP_TOKEN (app not found" in out
    assert set(report["targets"]) == {"gemini-3.1-flash-lite"} and report["left_out"] == {"deepseek-flash": {"kind": "deployment", "status": "paid"}}
    assert report["targets"]["gemini-3.1-flash-lite"]["kind"] == "deployment" and {c["model"] for c in fake.calls} == {"gemini-3.1-flash-lite"}
    assert fake.auth_seen == {f"Bearer {APP_TOKEN}"}  # the app token, not the master key
    assert_no_secret(out, err, json.dumps(report))

    fake.calls.clear()
    code, report = run(fake, tmp_path, "--yes", "--limit", "2", "--deployments", "--only", "gemini-3.1-flash-lite,deepseek-flash", "--allow-paid", env=env)
    out = capsys.readouterr().out
    assert code == 0 and "CAN COST MONEY" in out and set(report["targets"]) == {"gemini-3.1-flash-lite", "deepseek-flash"}
    for call in fake.calls:  # the paid header goes only to the paid target
        assert (call["headers"].get("x-muse-allow-paid") == "true") is (call["model"] == "deepseek-flash")
    assert report["targets"]["deepseek-flash"]["status"] == "paid" and report["targets"]["deepseek-flash"]["summary"]["cost_usd"] == 0.00025

    fake.calls.clear()
    code, report = run(fake, tmp_path, "--yes", "--limit", "3", "--deployments", env=environ(tmp_path))
    assert code == 0 and set(report["targets"]) == {"fast", "cheap", "smart", "long-context", "gemini-3.1-flash-lite", "gemini-3.8-flash"}


def test_a_background_app_token_that_may_not_pay_keeps_mixed_aliases_in(fake, tmp_path, capsys, sleeps):
    from wardmuse import apps

    env = environ(tmp_path, DEEPSEEK_API_KEY=PROVIDER_KEY)
    code, report = run(fake, tmp_path, "--yes", "--limit", "1", env=env)  # the master key may pay, and every alias can reach DeepSeek
    out = capsys.readouterr().out
    assert code == 0 and out.count("LEFT OUT") == 4 and "Nothing to send" in out and fake.calls == [] and report["targets"] == {}

    _, token = apps.add_app("eval", "background", environ=env)
    fake.tokens.add(token)
    code, report = run(fake, tmp_path, "--yes", "--limit", "1", "--only", "fast,deepseek-flash", "--deployments", env={**env, "MUSE_EVAL_APP_TOKEN": token})
    out, err = capsys.readouterr()
    assert code == 0 and "app 'eval', background, may not pay" in out and "This app may not pay" in out
    assert report["targets"]["fast"]["status"] == "free" and report["left_out"] == {"deepseek-flash": {"kind": "deployment", "status": "paid"}}
    assert token not in out + err + json.dumps(report)
    assert [c["model"] for c in fake.calls] == ["fast", "fast"]  # the fake's first answer is a 429, then the retry
    assert all("x-muse-allow-paid" not in c["headers"] for c in fake.calls)


def test_auto_sends_the_task_header_and_named_targets_do_not(fake, tmp_path, capsys, sleeps, items):
    code, report = run(fake, tmp_path, "--yes", "--sample", "6", "--seed", "1", "--only", "auto,long-context")
    assert code == 0 and set(report["targets"]) == {"auto", "long-context"}
    tiers = {em.item_text(i): i["tier"] for i in items}
    auto_calls = [c for c in fake.calls if c["model"] == "auto"]
    assert len(auto_calls) == 6
    for call in auto_calls:
        assert call["headers"]["x-muse-task"] == tiers["\n".join(m["content"] for m in call["body"]["messages"])]
    assert all("x-muse-task" not in c["headers"] for c in fake.calls if c["model"] != "auto")
    assert {r.get("task") for r in report["targets"]["auto"]["items"].values()} <= {"light", "everyday"}
    max_tokens = {c["body"]["max_tokens"] for c in fake.calls}
    assert max_tokens <= set(em.MAX_TOKENS.values())


def test_errors_keep_outputs_and_an_unreachable_proxy(fake, tmp_path, capsys, sleeps, gold):
    fake.down.add("long-context")
    code, report = run(fake, tmp_path, "--yes", "--limit", "2", "--only", "long-context,smart", "--keep-outputs", "--quiet")
    out, err = capsys.readouterr()
    assert code == 0 and report["targets"]["long-context"]["summary"]["errors"] == 2
    assert report["targets"]["long-context"]["items"]["zt-cls-01"] == {"http": 500, "latency_ms": report["targets"]["long-context"]["items"]["zt-cls-01"]["latency_ms"], "state": "error"}
    assert report["targets"]["smart"]["items"]["zt-cls-01"]["output"] == ""  # kept on request
    assert_no_secret(out, err, json.dumps(report))  # the error body text is never copied

    code = em.main(["--proxy-url", "http://127.0.0.1:9", "--report", str(tmp_path / "x.json"), "--yes"], environ=environ(tmp_path))
    assert code == 2 and "Could not list models" in capsys.readouterr().err
    code = em.main(["--proxy-url", fake.url, "--report", str(tmp_path / "x.json"), "--yes"], environ={**environ(tmp_path), "LITELLM_MASTER_KEY": "wrong"})
    out, err = capsys.readouterr()
    assert code == 2 and "wrong" not in out + err


def test_local_view_tells_free_from_paid_for_the_shipped_catalogue(tmp_path):
    view = em.local_view(environ(tmp_path, DEEPSEEK_API_KEY="x", SILICONFLOW_API_KEY="x", COHERE_API_KEY="x"))
    assert view["gemini-3.8-flash"]["status"] == "free" and view["gemini-3.8-flash"]["kind"] == "deployment"
    assert view["deepseek-flash"]["status"] == "paid" and view["cohere-command-a"]["status"] == "trial"
    assert view["fast"]["kind"] == "alias" and view["fast"]["status"] == "paid" and "siliconflow" in view["fast"]["all_providers"]  # a metered member
    assert view["embed"]["mode"] == "embedding"
    assert view["fast"]["status_no_paid"] == "trial" and view["deepseek-flash"]["status_no_paid"] == "paid" and view["gemini-3.8-flash"]["status_no_paid"] == "free"
    plain = em.local_view(environ(tmp_path))
    assert plain["fast"]["status"] == "free" and plain["smart"]["all_providers"] == ["gemini"] and "deepseek-flash" not in plain
    assert em.status_of({"free", "local"}, False) == "free" and em.status_of({"free", "trial"}, False) == "trial"
    assert em.status_of({"free"}, True) == "paid" and em.status_of({"free", "metered"}, False) == "paid" and em.status_of(set(), False) == "unknown"
    assert em.local_view({"MUSE_CATALOG": str(tmp_path / "missing.yaml")}) == {}


def test_suggest_prints_lines_for_routing_yaml_and_never_edits_it(items, gold, tmp_path, capsys):
    routing = ROOT / "config" / "routing.yaml"
    before = routing.read_bytes()
    by_id = {i["id"]: i for i in items}

    def target(kind, status, light_ok, everyday_ok):
        results = {}
        for i in items:
            ok = light_ok if i["tier"] == "light" else everyday_ok
            results[i["id"]] = {"state": "pass" if ok(i) else "fail", "script_ok": True, "latency_ms": 100}
        return {"kind": kind, "status": status, "items": results, "summary": em.summarise(results, by_id, len(items))}

    always, never = (lambda i: True), (lambda i: False)
    half = lambda i: int(i["id"][-2:]) % 2 == 0  # noqa: E731
    report = {"targets": {
        "fast": target("alias", "free", half, never), "cheap": target("alias", "free", always, half), "smart": target("alias", "free", half, always),
        "paid-one": target("deployment", "paid", always, always), "gemini-3.8-flash": target("deployment", "free", always, always),
    }}
    text = em.suggest(report)
    assert "light:\n      alias: cheap        # now: fast" in text and "everyday:\n      alias: smart        # now: cheap" in text
    assert "paid-one" not in text and "best single model: gemini-3.8-flash" in text and "config/providers.yaml" in text
    assert "Nothing is edited" in text and routing.read_bytes() == before
    same = {"targets": {"fast": target("alias", "free", always, never), "cheap": target("alias", "free", never, always)}}
    assert "No change to config/routing.yaml follows" in em.suggest(same)
    table = em.render_table(report)
    assert table.splitlines()[0].startswith("target") and "50%" in table and len(table.splitlines()) == 7


def test_summarise_counts_only_answered_items_in_a_rate(items):
    by_id = {i["id"]: i for i in items}
    results = {"zt-cls-01": {"state": "pass", "script_ok": True, "latency_ms": 100, "deployment": "a", "cost_usd": 0.001},
               "zt-cls-02": {"state": "fail", "script_ok": False, "latency_ms": 300, "deployment": "b", "empty_reasoning": True},
               "zt-cls-03": {"state": "deferred"}, "zt-cls-04": {"state": "error", "http": 500},
               "en-cls-01": {"state": "pass", "script_ok": None, "latency_ms": 200, "deployment": "a"}}
    s = em.summarise(results, by_id, planned=10)
    assert s["zh_tw"]["scored"] == 2 and s["zh_tw"]["rate"] == 0.5 and s["en_control"]["rate"] == 1.0 and s["zh_tw_gap"] == 0.5
    assert s["script"] == {"checked": 2, "ok": 1, "rate": 0.5} and s["deferred"] == 1 and s["errors"] == 1 and s["not_run"] == 5
    assert s["empty_reasoning"] == 1 and s["mean_latency_ms"] == 200 and s["cost_usd"] == 0.001 and s["answered_by"] == {"a": 2, "b": 1}
    empty = em.summarise({}, by_id)
    assert empty["zh_tw"]["rate"] is None and empty["zh_tw_gap"] is None and empty["mean_latency_ms"] is None


def test_tokens_bound_is_above_the_output_cap_and_uses_the_floor(items):
    first = items[0]
    assert em.tokens_bound(first, 0) == 2 * len(first["prompt"]) + 16 + em.MAX_TOKENS["classify"]
    assert em.tokens_bound(first, 1000) == 2 * len(first["prompt"]) + 16 + 1000
    assert set(em.MAX_TOKENS) == {t for tier in em.TASKS.values() for t in tier}
    assert max(em.MAX_TOKENS[t] for t in em.TASKS["light"]) <= 300  # the light ceiling in config/routing.yaml
