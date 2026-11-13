"""Recurring chores: the sentence rules, the schedule maths, the store, the three templates, cards, the CLI.

No network and no real provider: the router is queue_fakes.FakeProxy with scripted answers.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from muse import apps, cli, queue
from muse.home.cards import CardStore
from muse.skills import chores, loader, runner, schedule
from muse.skills.schedule import ChoreStore, Spec
from queue_fakes import CANARY_REPLY, Clock, ration_429
from test_skills_support import chat, env, proxy  # noqa: F401  (fixtures)

SKILL = "recurring-chore"
TPE = ZoneInfo("Asia/Taipei")
LA = ZoneInfo("America/Los_Angeles")
CANARY = "CANARY-FEED-TEXT-7f3a91"
CANARY_DEEP = "CANARY-DEEP-IN-FILE-c04e55"
HOME = str(Path.home())


def mod(name: str | None = None):
    return loader.import_skill(loader.find(SKILL), name)


def ts(zone: ZoneInfo, *parts: int) -> float:
    return datetime(*parts, tzinfo=zone).timestamp()


def local(stamp: float, zone: ZoneInfo) -> str:
    return datetime.fromtimestamp(stamp, zone).strftime("%Y-%m-%d %H:%M")


# ================================================================ sentence -> spec (rules only)

Q = "question"

# (sentence, expected). Expected is a dict of spec fields that must match, or (Q, [question codes]).
SENTENCES: list[tuple[str, object]] = [
    # ---- English: every day
    ("Every day at 08:00, summarise the feeds in /data/feeds", {"freq": "daily", "time": "08:00", "template": "summarise-feeds", "sources": ["/data/feeds"], "lang": "en"}),
    ("daily at 7am summarise the feeds in /data/feeds", {"freq": "daily", "time": "07:00"}),
    ("Each day at 6:45 pm, summarise what changed in /data/notes", {"freq": "daily", "time": "18:45", "template": "summarise-folder-changes", "sources": ["/data/notes"]}),
    ("every morning summarise the news in /data/feeds", {"freq": "daily", "time": "08:00", "assumed": ["time"]}),
    ("Every evening, tell me what changed in /data/notes", {"freq": "daily", "time": "18:00", "assumed": ["time"]}),
    ("every night at 11pm summarise the rss in /data/feeds", {"freq": "daily", "time": "23:00"}),
    ("Summarise the feeds in /data/feeds every day at noon", {"freq": "daily", "time": "12:00"}),
    ("every day at midnight write a daily brief", {"freq": "daily", "time": "00:00", "template": "daily-brief", "sources": []}),
    ("Every day at 12am summarise feeds in /data/feeds", {"freq": "daily", "time": "00:00"}),
    ("Every day at 12pm summarise feeds in /data/feeds", {"freq": "daily", "time": "12:00"}),
    ("every day at 8 summarise feeds in /data/feeds", {"freq": "daily", "time": "08:00"}),
    ("every day at 8 in the evening summarise feeds in /data/feeds", {"freq": "daily", "time": "20:00"}),
    ("once a day, summarise the newsletters in /data/feeds", {"freq": "daily", "time": "08:00", "assumed": ["time"]}),
    ("daily brief at 7am", {"freq": "daily", "time": "07:00", "template": "daily-brief"}),
    ("Send me a daily brief every Monday at 9am", {"freq": "weekly", "days": [0], "time": "09:00", "template": "daily-brief"}),
    # ---- English: weekdays, days of the week
    ("Every weekday at 7:30am summarise what changed in /data/notes", {"freq": "weekdays", "time": "07:30"}),
    ("On weekdays at 17:00, summarise the feeds in /data/feeds", {"freq": "weekdays", "time": "17:00"}),
    ("Monday to Friday at 9am, summarise new files in /data/inbox", {"freq": "weekdays", "time": "09:00", "template": "summarise-folder-changes"}),
    ("every working day at 8.15 am summarise feeds in /data/feeds", {"freq": "weekdays", "time": "08:15"}),
    ("Every Monday 08:00, summarise the feeds in /data/feeds into /data/out", {"freq": "weekly", "days": [0], "time": "08:00", "sources": ["/data/feeds"], "dest": "/data/out"}),
    ("Every Monday 08:00, summarise the feeds in ~/feeds into Documents/Muse", {"freq": "weekly", "days": [0], "sources": [HOME + "/feeds"], "dest": HOME + "/Documents/Muse"}),
    ("every Monday and Thursday at 20:15 summarise the feeds in /data/feeds", {"freq": "weekly", "days": [0, 3], "time": "20:15"}),
    ("Every Tue, Thu and Sat at 6am summarise feeds in /data/feeds", {"freq": "weekly", "days": [1, 3, 5], "time": "06:00"}),
    ("Fridays at 4pm summarise what changed in /data/notes", {"freq": "weekly", "days": [4], "time": "16:00", "assumed": []}),
    ("On Sunday at 10:00 summarise the feeds in /data/feeds", {"freq": "weekly", "days": [6], "time": "10:00", "assumed": ["weekly"]}),
    ("every weekend at 9am summarise feeds in /data/feeds", {"freq": "weekly", "days": [5, 6], "time": "09:00"}),
    ("weekly on Wednesday at 13:30 summarise feeds in /data/feeds", {"freq": "weekly", "days": [2], "time": "13:30"}),
    ("Every Saturday morning, summarise the articles in /data/feeds", {"freq": "weekly", "days": [5], "time": "08:00", "assumed": ["time"]}),
    # ---- English: monthly
    ("Every month on the 1st at 9am, write a brief of what Muse did", {"freq": "monthly", "day_of_month": 1, "time": "09:00", "template": "daily-brief"}),
    ("On the 15th of every month at 18:00 summarise feeds in /data/feeds", {"freq": "monthly", "day_of_month": 15, "time": "18:00"}),
    ("Every month on the 31st at 6pm, summarise new files in /srv/share to /data/out", {"freq": "monthly", "day_of_month": 31, "time": "18:00", "sources": ["/srv/share"], "dest": "/data/out"}),
    ("On the last day of every month at noon, summarise what changed in /data/notes", {"freq": "monthly", "day_of_month": -1, "time": "12:00"}),
    ("monthly on the 3rd at 07:00 summarise the feeds in /data/feeds", {"freq": "monthly", "day_of_month": 3, "time": "07:00"}),
    ("At the end of every month at 5pm summarise what changed in /data/notes", {"freq": "monthly", "day_of_month": -1, "time": "17:00"}),
    # ---- English: spare allowance
    ("When there is allowance to spare, summarise the feeds in /data/feeds", {"freq": "daily", "time": "", "spare_only": True}),
    ("Whenever there's spare allowance, summarise what changed in /data/notes", {"freq": "daily", "time": "", "spare_only": True}),
    ("Every Monday at 08:00, when there is allowance to spare, summarise the feeds in /data/feeds", {"freq": "weekly", "days": [0], "time": "08:00", "spare_only": True}),
    ("summarise the feeds in /data/feeds with leftover allowance every weekday", {"freq": "weekdays", "spare_only": True, "time": "08:00"}),
    # ---- English: places and kinds
    ("Every day at 9am summarise https://example.com/feed.xml and the feeds in /data/feeds", {"freq": "daily", "urls": ["https://example.com/feed.xml"], "sources": ["/data/feeds"], "template": "summarise-feeds"}),
    ('Every day at 9am summarise the feeds in "/data/my feeds" and save it in "/data/out box"', {"sources": ["/data/my feeds"], "dest": "/data/out box"}),
    ("Every day at 9am digest /data/inbox", {"template": "summarise-folder-changes", "assumed": ["what"]}),
    ("Every day at 9am summarise the feeds from /data/a and /data/b", {"sources": ["/data/a", "/data/b"], "dest": ""}),
    ("EVERY MONDAY AT 8AM SUMMARISE THE FEEDS IN /data/feeds", {"freq": "weekly", "days": [0], "time": "08:00"}),
    # ---- Traditional Chinese
    ("每天早上8點摘要 /data/feeds 的訂閱", {"freq": "daily", "time": "08:00", "template": "summarise-feeds", "sources": ["/data/feeds"], "lang": "zh-TW"}),
    ("每日下午三點半整理 /data/notes 的變動", {"freq": "daily", "time": "15:30", "template": "summarise-folder-changes"}),
    ("每週一早上8點把 ~/feeds 的訂閱摘要存到 ~/Documents/Muse", {"freq": "weekly", "days": [0], "time": "08:00", "sources": [HOME + "/feeds"], "dest": HOME + "/Documents/Muse"}),
    ("每周三晚上9點15分摘要 /data/feeds 的新聞", {"freq": "weekly", "days": [2], "time": "21:15"}),
    ("每星期五下午5點摘要 /data/feeds 的訂閱", {"freq": "weekly", "days": [4], "time": "17:00"}),
    ("每禮拜日上午十點半摘要 /data/feeds 的文章", {"freq": "weekly", "days": [6], "time": "10:30"}),
    ("每週一、三、五早上七點摘要 /data/feeds 的訂閱", {"freq": "weekly", "days": [0, 2, 4], "time": "07:00"}),
    ("每週二和週四中午摘要 /data/feeds 的訂閱", {"freq": "weekly", "days": [1, 3], "time": "12:00"}),
    ("每個工作天早上八點半摘要 /data/feeds 的訂閱", {"freq": "weekdays", "time": "08:30"}),
    ("週一到週五下午6點整理 /data/notes 有什麼新的檔案", {"freq": "weekdays", "time": "18:00", "template": "summarise-folder-changes"}),
    ("平日晚上十一點摘要 /data/feeds 的訂閱", {"freq": "weekdays", "time": "23:00"}),
    ("每月1號早上九點寫一份簡報", {"freq": "monthly", "day_of_month": 1, "time": "09:00", "template": "daily-brief"}),
    ("每個月15日下午2點摘要 /data/feeds 的訂閱", {"freq": "monthly", "day_of_month": 15, "time": "14:00"}),
    ("每月三十一號晚上8點整理 /data/notes 的異動", {"freq": "monthly", "day_of_month": 31, "time": "20:00"}),
    ("每月最後一天中午摘要 /data/feeds 的訂閱", {"freq": "monthly", "day_of_month": -1, "time": "12:00"}),
    ("每月底下午五點整理 /data/notes 的變更", {"freq": "monthly", "day_of_month": -1, "time": "17:00"}),
    ("有剩餘額度時，摘要 /data/feeds 的訂閱", {"freq": "daily", "time": "", "spare_only": True}),
    ("額度有剩的時候整理 /data/notes 的變動", {"freq": "daily", "time": "", "spare_only": True}),
    ("每週六有剩餘額度時摘要 /data/feeds 的訂閱", {"freq": "weekly", "days": [5], "spare_only": True, "time": "08:00"}),
    ("每晚整理 /data/notes 的異動", {"freq": "daily", "time": "21:00", "assumed": ["time"]}),
    ("每天凌晨2點摘要 /data/feeds 的訂閱", {"freq": "daily", "time": "02:00"}),
    ("每天中午12點摘要 /data/feeds 的訂閱", {"freq": "daily", "time": "12:00"}),
    ("每天下午3:30摘要 /data/feeds 的訂閱", {"freq": "daily", "time": "15:30"}),
    ("每天早上８點摘要 /data/feeds 的訂閱", {"freq": "daily", "time": "08:00"}),  # a full-width digit
    ("每週末早上九點摘要 /data/feeds 的訂閱，用英文", {"freq": "weekly", "days": [5, 6], "time": "09:00", "lang": "en"}),
    ("每日簡報，早上七點", {"freq": "daily", "time": "07:00", "template": "daily-brief"}),
    ("每天早上8點摘要「/data/我的 訂閱」的訂閱，放到「/data/輸出」", {"sources": ["/data/我的 訂閱"], "dest": "/data/輸出"}),
    # ---- questions: unsupported, ambiguous, unparseable
    ("Every other Monday summarise the feeds in /data/feeds", (Q, ["interval"])),
    ("every 2 weeks summarise the feeds in /data/feeds", (Q, ["interval"])),
    ("Every hour summarise the feeds in /data/feeds", (Q, ["interval"])),
    ("twice a day summarise the feeds in /data/feeds", (Q, ["interval"])),
    ("每兩週摘要 /data/feeds 的訂閱", (Q, ["interval"])),
    ("每小時摘要 /data/feeds 的訂閱", (Q, ["interval"])),
    ("每隔一天摘要 /data/feeds 的訂閱", (Q, ["interval"])),
    ("Tomorrow at 8am summarise the feeds in /data/feeds", (Q, ["one_off"])),
    ("明天早上八點摘要 /data/feeds 的訂閱", (Q, ["one_off"])),
    ("Summarise the feeds in /data/feeds", (Q, ["when"])),
    ("摘要 /data/feeds 的訂閱", (Q, ["when"])),
    ("every week summarise the feeds in /data/feeds", (Q, ["which_day"])),
    ("every month summarise the feeds in /data/feeds", (Q, ["which_day"])),
    ("每週摘要 /data/feeds 的訂閱", (Q, ["which_day"])),
    ("每月摘要 /data/feeds 的訂閱", (Q, ["which_day"])),
    ("Every day at 25:00 summarise the feeds in /data/feeds", (Q, ["time"])),
    ("Every day at 13pm summarise the feeds in /data/feeds", (Q, ["time"])),
    ("每天下午25點摘要 /data/feeds 的訂閱", (Q, ["time"])),
    ("Every day at 8am and 6pm summarise the feeds in /data/feeds", (Q, ["two_times"])),
    ("每天早上8點和晚上8點摘要 /data/feeds 的訂閱", (Q, ["two_times"])),
    ("Every Monday and every month on the 1st at 9am summarise the feeds in /data/feeds", (Q, ["conflict"])),
    ("On the 1st and the 15th of every month at 9am summarise the feeds in /data/feeds", (Q, ["conflict"])),
    ("Every day at 8am do the thing", (Q, ["what"])),
    ("每天早上8點幫我處理一下", (Q, ["what"])),
    ("Every Monday 08:00, summarise these 5 feeds", (Q, ["sources"])),
    ("每週一早上8點摘要這五個訂閱", (Q, ["sources"])),
    ("Every day at 8am tell me what changed", (Q, ["sources"])),
    ("do the thing", (Q, ["when", "what"])),
    ("幫我處理一下", (Q, ["when", "what"])),
    ("", (Q, ["empty"])),
    ("   ", (Q, ["empty"])),
    ("every day summarise feeds in /data/feeds " + "and more " * 80, (Q, ["too_long"])),
]


def test_the_sentence_table_is_large_enough_and_covers_both_languages():
    assert len(SENTENCES) >= 60
    chinese = [s for s, _ in SENTENCES if any("一" <= ch <= "鿿" for ch in s)]
    asked = [s for s, e in SENTENCES if isinstance(e, tuple)]
    assert len(chinese) >= 25 and len(SENTENCES) - len(chinese) >= 30 and len(asked) >= 20


@pytest.mark.parametrize("sentence, expected", SENTENCES, ids=[f"{i:03d}" for i in range(len(SENTENCES))])
def test_sentence_to_spec_or_question(sentence, expected):
    got = schedule.understand(sentence, check_folders=False, tz_name="Asia/Taipei")
    assert got["parsed_by"] == "rules"
    if isinstance(expected, tuple):
        assert not got["ok"] and got["spec"] is None
        assert [q["code"] for q in got["questions"]] == expected[1]
        assert all(q["text"] for q in got["questions"])
        return
    assert got["ok"], got["questions"]
    assert got["questions"] == [] and schedule.spec_problems(got["spec"]) == []
    for key, value in expected.items():
        if key == "assumed":
            assert got["assumed"] == value
        else:
            assert got["spec"][key] == value, key
    for line in ("what", "when", "where", "budget", "permission"):
        assert got[line], line  # everything ux 4.3 shows before the one confirmation
    assert Spec.from_dict(got["spec"]).to_dict() == got["spec"]


def test_what_was_understood_reads_as_ux_4_3_shows_it(tmp_path):
    feeds = tmp_path / "feeds"
    feeds.mkdir()
    got = schedule.understand(f"Every Monday 08:00, summarise the feeds in {feeds} into {tmp_path}/out", tz_name="Asia/Taipei")
    assert got["ok"]
    assert got["what"] == f"Summarise the feed files in {feeds}"
    assert got["when"] == "Every Monday at 08:00 (Asia/Taipei time)"
    assert got["where"] == f"A new dated Markdown file in {tmp_path}/out, saved only after you approve it"
    assert got["budget"] == "up to 40 requests, nothing paid"
    assert got["permission"] == (f"Read the files in {feeds}; propose one new file in {tmp_path}/out each run "
                                 "(each is saved only after you approve it)")
    text = schedule.understood_text(got)
    assert text.splitlines()[0].startswith("What: ") and "Budget: up to 40 requests, nothing paid" in text
    zh = schedule.understand(f"每週一早上8點把 {feeds} 的訂閱摘要存到 {tmp_path}/out", tz_name="Asia/Taipei")
    assert zh["when"] == "每週一 08:00（Asia/Taipei 時間）" and zh["budget"] == "最多 40 次請求，不會付費"
    assert "做什麼：" in schedule.understood_text(zh)


def test_when_is_described_for_every_kind_of_schedule():
    def when(**kw):
        return schedule.describe_when(Spec(template="daily-brief", **kw))

    assert when(freq="daily", time="07:05") == "Every day at 07:05"
    assert when(freq="weekdays") == "Every weekday (Monday to Friday) at 08:00"
    assert when(freq="weekly", days=(0, 2, 4)) == "Every Monday, Wednesday and Friday at 08:00"
    assert when(freq="monthly", day_of_month=15) == "Every month on day 15 at 08:00"
    assert when(freq="monthly", day_of_month=31) == "Every month on day 31 at 08:00 (on the last day when the month is shorter)"
    assert when(freq="monthly", day_of_month=-1) == "On the last day of every month at 08:00"
    assert when(freq="daily", time="", spare_only=True) == "Every day, when there is allowance to spare"
    assert when(freq="weekly", days=(5,), spare_only=True) == "Every Saturday at 08:00, as soon after that as there is allowance to spare"
    assert when(freq="weekly", days=(0, 3), lang="zh-TW") == "每週一、週四 08:00"
    assert when(freq="daily", time="", spare_only=True, lang="zh-TW") == "每天，有剩餘額度時執行"


def test_folders_picked_beside_the_sentence_answer_these_feeds(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    bare = "Every Monday 08:00, summarise these 5 feeds into Documents/Muse"
    assert [q["code"] for q in schedule.understand(bare)["questions"]] == ["sources"]
    got = schedule.understand(bare, folders=[a, b], out=tmp_path / "out")
    assert got["ok"] and got["spec"]["sources"] == [str(a), str(b)] and got["spec"]["dest"] == str(tmp_path / "out")
    missing = schedule.understand(f"Every day at 8am summarise the feeds in {tmp_path}/nowhere")
    assert [q["code"] for q in missing["questions"]] == ["folder_missing"] and "nowhere" in missing["questions"][0]["text"]
    many = schedule.understand("Every day at 8am summarise the feeds in " + " and ".join(f"/data/f{i}" for i in range(9)), check_folders=False)
    assert [q["code"] for q in many["questions"]] == ["too_many"]


def test_web_addresses_are_kept_and_the_person_is_told_they_are_not_fetched():
    got = schedule.understand("Every day at 9am summarise https://example.com/a.xml, example.org/feed and the feeds in /data/feeds",
                              check_folders=False)
    assert got["spec"]["urls"] == ["https://example.com/a.xml", "https://example.org/feed"]
    assert any("cannot fetch web addresses yet" in n and "sandbox" in n for n in got["notes"])
    only = schedule.understand("Every day at 9am summarise https://example.com/a.xml", check_folders=False)
    assert [q["code"] for q in only["questions"]] == ["sources"]  # nothing it could read today


def test_spec_validation_is_one_set_of_rules():
    good = {"freq": "weekly", "template": "summarise-feeds", "time": "08:00", "days": [0], "sources": ["/x"]}
    assert schedule.spec_problems(good) == []
    for change in ({"freq": "hourly"}, {"template": "delete-everything"}, {"time": "24:00"}, {"time": ""}, {"days": []}, {"days": [7]},
                   {"day_of_month": 3}, {"lang": "fr"}, {"sources": "x"}, {"spare_only": "yes"}):
        assert schedule.spec_problems({**good, **change}), change
    assert schedule.spec_problems({"freq": "monthly", "template": "daily-brief", "day_of_month": 0})
    assert schedule.spec_problems({"freq": "monthly", "template": "daily-brief", "day_of_month": 32})
    assert schedule.spec_problems({"freq": "daily", "template": "daily-brief", "time": "", "spare_only": True}) == []
    with pytest.raises(schedule.ChoreError):
        Spec.from_dict({**good, "freq": "sometimes"})
    assert schedule.spec_problems("nope") and schedule.spec_problems(None)


# ================================================================ a model's proposal


def test_a_proposal_fills_only_what_the_rules_could_not_read_and_never_a_place():
    vague = "Catch me up on the news in /data/feeds at the start of each working week"
    assert [q["code"] for q in schedule.understand(vague, check_folders=False)["questions"]] == ["when"]
    proposal = {"frequency": "weekly", "days": ["Monday"], "time": "08:00", "template": "daily-brief",
                "sources": ["/etc"], "destination": "/root/.ssh"}
    got = schedule.understand(vague, proposal=proposal, check_folders=False)
    assert got["ok"] and got["parsed_by"] == "model"
    assert got["spec"]["freq"] == "weekly" and got["spec"]["days"] == [0] and got["spec"]["time"] == "08:00"
    assert got["spec"]["template"] == "summarise-feeds"  # the rules read "news": the model does not overrule them
    assert got["spec"]["sources"] == ["/data/feeds"] and got["spec"]["dest"] == ""  # places come from the sentence only
    assert any("read by a model" in n for n in got["notes"])


@pytest.mark.parametrize("proposal, kept", [
    ({"frequency": "daily"}, {"freq": "daily", "days": (), "day_of_month": 0}),
    ({"frequency": "weekly", "days": ["mon", "Friday"]}, {"freq": "weekly", "days": (0, 4), "day_of_month": 0}),
    ({"frequency": "weekly", "days": [1, 3]}, {"freq": "weekly", "days": (1, 3), "day_of_month": 0}),
    ({"frequency": "weekly"}, {}),  # weekly without days is not a schedule
    ({"frequency": "weekly", "days": ["someday"]}, {}),
    ({"frequency": "monthly", "day_of_month": "last"}, {"freq": "monthly", "days": (), "day_of_month": -1}),
    ({"frequency": "monthly", "day_of_month": 40}, {}),
    ({"frequency": "spare"}, {"freq": "daily", "days": (), "day_of_month": 0, "spare_only": True}),
    ({"frequency": "every 5 minutes"}, {}),
    ({"time": "7:05"}, {"time": "07:05"}),
    ({"time": "25:00"}, {}),
    ({"time": "soon"}, {}),
    ({"template": "daily-brief"}, {"template": "daily-brief"}),
    ({"template": "rm -rf"}, {}),
    ({"days": True, "day_of_month": True, "time": 8}, {}),
    ("not an object", {}),
    (None, {}),
    ([1, 2], {}),
])
def test_proposal_validation(proposal, kept):
    assert schedule.validate_proposal(proposal) == kept


def test_a_proposal_is_ignored_when_only_the_person_can_answer():
    asks = schedule.understand("Every other Monday summarise the feeds in /data/feeds", check_folders=False,
                               proposal={"frequency": "weekly", "days": ["monday"]})
    assert [q["code"] for q in asks["questions"]] == ["interval"] and asks["parsed_by"] == "rules"
    bad_time = schedule.understand("every day at 25:00 do the thing", proposal={"template": "daily-brief"}, check_folders=False)
    assert not bad_time["ok"] and bad_time["parsed_by"] == "rules"


def understand_with_skill(env, sentence, folders=(), budget=None, **inputs):
    return runner.run(SKILL, {"mode": "understand", "sentence": sentence, **inputs}, folders=folders, environ=env,
                      budget=budget if budget is not None else {"requests": 1})


def test_the_skill_uses_no_model_when_the_rules_can_read_the_sentence(env, proxy, tmp_path):
    result = understand_with_skill(env, "Every Monday 08:00, summarise these 5 feeds", folders=[tmp_path])
    assert result.status == "done" and result.check["status"] == "passed" and proxy.requests == []
    u = result.output["understood"]
    assert u["ok"] and u["spec"]["sources"] == [str(tmp_path.resolve())] and result.output["asked_model"] is False
    asks = understand_with_skill(env, "Every other Monday summarise these feeds", folders=[tmp_path])
    assert asks.status == "done" and not asks.output["understood"]["ok"] and proxy.requests == []  # a model cannot answer this


def test_the_skill_asks_one_light_call_when_the_rules_cannot_and_reads_the_reply_defensively(env, proxy, tmp_path):
    reply = 'Sure! Here is the JSON:\n```json\n{"frequency": "weekly", "days": ["monday"], "time": "08:00", "template": "summarise-feeds"}\n```\nHope it helps.'
    proxy.script.append(chat(reply, task="light"))
    result = understand_with_skill(env, "Get me up to speed at the start of the working week", folders=[tmp_path])
    assert result.status == "done" and result.check["status"] == "passed"
    assert len(proxy.requests) == 1 and proxy.requests[0]["task"] == "light"
    u = result.output["understood"]
    assert u["ok"] and u["parsed_by"] == "model" and u["spec"]["days"] == [0] and u["spec"]["template"] == "summarise-feeds"
    assert "Note: Part of this was read by a model" in result.output["text"]


@pytest.mark.parametrize("reply", ["I cannot help with that.", "{broken json", '{"frequency": "hourly", "template": "format-disk"}', "", "[]"])
def test_a_useless_reply_leaves_the_questions_standing(env, proxy, reply):
    proxy.script.append(chat(reply, task="light"))
    result = understand_with_skill(env, "Get me up to speed sometimes")
    assert result.status == "done" and len(proxy.requests) == 1
    u = result.output["understood"]
    assert not u["ok"] and [q["code"] for q in u["questions"]] == ["when", "what"] and u["parsed_by"] == "rules"


def test_understanding_survives_a_router_that_holds_back_and_a_budget_of_zero(env, proxy):
    proxy.script.append(ration_429())
    held = understand_with_skill(env, "Get me up to speed sometimes")
    assert held.status == "done" and not held.output["understood"]["ok"] and len(proxy.requests) == 1
    none = understand_with_skill(env, "Get me up to speed sometimes", budget={"requests": 0})
    assert none.status == "done" and len(proxy.requests) == 1  # no second call
    off = understand_with_skill(env, "Get me up to speed sometimes", model="off")
    assert off.status == "done" and len(proxy.requests) == 1


def test_the_check_refuses_an_understanding_a_model_bent(env, tmp_path):
    m = mod()
    good = understand_with_skill(env, "Every Monday 08:00, summarise these 5 feeds", folders=[tmp_path]).output
    ctx = type("Ctx", (), {"workspace": type("W", (), {"roots": lambda self: [str(tmp_path.resolve())]})()})()
    assert m.check(good, ctx) == []
    bent = json.loads(json.dumps(good))
    bent["understood"]["spec"]["time"] = "03:00"
    assert any("time differs" in r for r in m.check(bent, ctx))
    bent = json.loads(json.dumps(good))
    bent["understood"]["spec"]["sources"] = ["/etc"]
    assert any("did not name" in r for r in m.check(bent, ctx))
    bent = json.loads(json.dumps(good))
    bent["understood"]["spec"]["urls"] = ["https://evil.example/x"]
    assert any("web address" in r for r in m.check(bent, ctx))
    bent = json.loads(json.dumps(good))
    bent["understood"]["spec"]["freq"] = "hourly"
    assert m.check(bent, ctx)
    bent = json.loads(json.dumps(good))
    bent["understood"]["questions"] = [{"code": "when", "text": "?"}]
    assert any("not clear" in r for r in m.check(bent, ctx))
    assert m.read_json('noise {"a": {"b": "}"}} tail') == {"a": {"b": "}"}} and m.read_json("{nope") is None


# ================================================================ schedule maths


def seq(spec: Spec, start: float, zone, n: int) -> list[str]:
    out, now = [], start
    for _ in range(n):
        now = schedule.next_after(spec, now, zone)
        out.append(local(now, zone))
    return out


def test_daily_weekday_and_weekly_schedules_in_taipei():
    start = ts(TPE, 2026, 11, 13, 7, 0)  # a Friday
    assert seq(Spec("daily", "daily-brief", "08:00"), start, TPE, 3) == ["2026-11-13 08:00", "2026-11-14 08:00", "2026-11-15 08:00"]
    assert seq(Spec("daily", "daily-brief", "06:00"), start, TPE, 2) == ["2026-11-14 06:00", "2026-11-15 06:00"]
    assert seq(Spec("weekdays", "daily-brief", "08:00"), start, TPE, 4) == [
        "2026-11-13 08:00", "2026-11-16 08:00", "2026-11-17 08:00", "2026-11-18 08:00"]
    assert seq(Spec("weekly", "daily-brief", "08:00", days=(0,)), start, TPE, 2) == ["2026-11-16 08:00", "2026-11-23 08:00"]
    assert seq(Spec("weekly", "daily-brief", "21:30", days=(4, 6)), start, TPE, 3) == [
        "2026-11-13 21:30", "2026-11-15 21:30", "2026-11-20 21:30"]
    exactly = ts(TPE, 2026, 11, 13, 8, 0)
    assert local(schedule.next_after(Spec("daily", "daily-brief", "08:00"), exactly, TPE), TPE) == "2026-11-14 08:00"  # strictly after
    assert local(schedule.next_after(Spec("daily", "daily-brief", "08:00"), exactly - 1, TPE), TPE) == "2026-11-13 08:00"


def test_a_wall_clock_time_stays_put_across_daylight_saving_changes():
    eight = Spec("daily", "daily-brief", "08:00")
    spring = seq(eight, ts(LA, 2027, 3, 12, 9, 0), LA, 4)  # clocks go forward on 2027-03-14
    assert spring == ["2027-03-13 08:00", "2027-03-14 08:00", "2027-03-15 08:00", "2027-03-16 08:00"]
    a = schedule.next_after(eight, ts(LA, 2027, 3, 13, 9, 0), LA)
    b = schedule.next_after(eight, a, LA)
    assert a - ts(LA, 2027, 3, 13, 8, 0) == 23 * 3600 and b - a == 24 * 3600  # a 23-hour day, then normal ones
    autumn = seq(eight, ts(LA, 2027, 11, 5, 9, 0), LA, 3)  # clocks go back on 2027-11-07
    assert autumn == ["2027-11-06 08:00", "2027-11-07 08:00", "2027-11-08 08:00"]
    c = schedule.next_after(eight, ts(LA, 2027, 11, 6, 9, 0), LA)
    assert c - ts(LA, 2027, 11, 6, 8, 0) == 25 * 3600
    assert seq(eight, ts(TPE, 2027, 3, 13, 9, 0), TPE, 2) == ["2027-03-14 08:00", "2027-03-15 08:00"]  # no clock changes in Taipei


def test_a_time_that_does_not_exist_runs_after_the_gap_and_one_that_happens_twice_runs_once():
    gap = Spec("daily", "daily-brief", "02:30")
    around = seq(gap, ts(LA, 2027, 3, 13, 3, 0), LA, 3)
    assert around == ["2027-03-14 03:30", "2027-03-15 02:30", "2027-03-16 02:30"]  # 02:30 does not exist on the 14th
    twice = Spec("daily", "daily-brief", "01:30")
    first = schedule.next_after(twice, ts(LA, 2027, 11, 6, 2, 0), LA)
    second = schedule.next_after(twice, first, LA)
    assert local(first, LA) == "2027-11-07 01:30" and local(second, LA) == "2027-11-08 01:30"
    assert second - first == 25 * 3600  # the repeated 01:30 an hour later is not a second run
    weekly = Spec("weekly", "daily-brief", "02:15", days=(6,))
    assert seq(weekly, ts(LA, 2027, 3, 8, 0, 0), LA, 2) == ["2027-03-14 03:15", "2027-03-21 02:15"]


def test_month_ends_the_29th_to_31st_and_leap_day():
    def monthly(day: int, start: float, n: int) -> list[str]:
        return [x[:10] for x in seq(Spec("monthly", "daily-brief", "09:00", day_of_month=day), start, TPE, n)]

    jan = ts(TPE, 2027, 1, 1, 0, 0)
    assert monthly(31, jan, 6) == ["2027-01-31", "2027-02-28", "2027-03-31", "2027-04-30", "2027-05-31", "2027-06-30"]
    assert monthly(30, jan, 4) == ["2027-01-30", "2027-02-28", "2027-03-30", "2027-04-30"]
    assert monthly(29, jan, 3) == ["2027-01-29", "2027-02-28", "2027-03-29"]
    assert monthly(-1, jan, 4) == ["2027-01-31", "2027-02-28", "2027-03-31", "2027-04-30"]
    assert monthly(1, jan, 3) == ["2027-01-01", "2027-02-01", "2027-03-01"]
    leap = ts(TPE, 2028, 1, 30, 0, 0)
    assert monthly(29, leap, 2) == ["2028-02-29", "2028-03-29"]
    assert monthly(31, leap, 3) == ["2028-01-31", "2028-02-29", "2028-03-31"]
    assert monthly(-1, ts(TPE, 2028, 2, 1, 0, 0), 2) == ["2028-02-29", "2028-03-31"]
    assert monthly(15, ts(TPE, 2027, 12, 20, 0, 0), 2) == ["2028-01-15", "2028-02-15"]  # across the year end
    late = schedule.next_after(Spec("monthly", "daily-brief", "09:00", day_of_month=31), ts(TPE, 2027, 2, 28, 9, 0), TPE)
    assert local(late, TPE) == "2027-03-31 09:00"  # the short month's run has just happened: not twice


def test_schedules_never_repeat_or_go_backwards_over_two_years():
    specs = [Spec("daily", "daily-brief", "02:30"), Spec("weekdays", "daily-brief", "23:59"),
             Spec("weekly", "daily-brief", "01:30", days=(6, 0)), Spec("monthly", "daily-brief", "00:00", day_of_month=31),
             Spec("monthly", "daily-brief", "12:00", day_of_month=-1), Spec("daily", "daily-brief", "", spare_only=True)]
    for zone in (LA, TPE, ZoneInfo("UTC")):
        for spec in specs:
            now = ts(zone, 2027, 1, 1, 0, 0)
            seen: list[float] = []
            while now < ts(zone, 2029, 1, 1, 0, 0):
                nxt = schedule.next_after(spec, now, zone)
                assert nxt > now
                seen.append(nxt)
                now = nxt
            gaps = [b - a for a, b in zip(seen, seen[1:])]
            longest = {"daily": 25, "weekdays": 73, "weekly": 6 * 24 + 1, "monthly": 31 * 24 + 1}[spec.freq] * 3600
            assert min(gaps) >= 23 * 3600 - 1 and max(gaps) <= longest, (spec, zone)
            if spec.freq == "daily":
                inside = [s for s in seen if s < ts(zone, 2029, 1, 1, 0, 0)]
                assert len(inside) in (730, 731)  # 365 + 366 days: one run each day, no day skipped or doubled


def test_the_zone_can_be_a_name_and_an_unknown_name_falls_back(monkeypatch):
    spec = Spec("daily", "daily-brief", "08:00")
    now = ts(TPE, 2026, 11, 13, 7, 0)
    assert schedule.next_after(spec, now, "Asia/Taipei") == schedule.next_after(spec, now, TPE)
    assert schedule.next_after(spec, now, "Not/AZone") > now
    assert schedule.local_date(now, "Asia/Taipei") == "2026-11-13" and schedule.local_date(now, "America/Los_Angeles") == "2026-11-12"


# ================================================================ the store


def feeds_spec(folder: Path, **kw) -> Spec:
    base = dict(freq="daily", template="summarise-feeds", time="08:00", sources=(str(folder),))
    base.update(kw)
    return Spec(**base)


def test_the_store_is_owner_only_and_keeps_what_was_confirmed(env, tmp_path):
    clock = Clock(ts(TPE, 2026, 11, 13, 7, 0))
    store = ChoreStore(env, clock.now)
    chore = store.add("Every day at 08:00, summarise the feeds in /x", feeds_spec(tmp_path), tz="Asia/Taipei")
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600 and store.path.name == "chores.sqlite"
    assert chore.enabled and chore.runs == 0 and chore.skill == SKILL and chore.budget["requests"] == 40
    assert chore.inputs == {"mode": "run", "template": "summarise-feeds", "lang": "en"}
    assert local(chore.next_run, TPE) == "2026-11-13 08:00"  # the next time it comes round: never at once
    assert store.find(chore.id[:4]).id == chore.id and store.list()[0].spec == chore.spec
    public = chore.public()
    assert public["budget_text"] == "up to 40 requests, nothing paid" and public["when"] == "Every day at 08:00 (Asia/Taipei time)"
    with pytest.raises(schedule.ChoreError):
        store.find("zz")
    with pytest.raises(schedule.ChoreError):
        store.find("0000")
    with pytest.raises(schedule.ChoreError):
        store.add("x", feeds_spec(tmp_path), budget={"requests": 0, "tokens": 5, "seconds": 5})
    small = store.add("x", feeds_spec(tmp_path), budget={"requests": 3, "tokens": 9000, "seconds": 30}, privacy="private")
    assert small.budget == {"requests": 3, "tokens": 9000, "seconds": 30.0, "paid_usd": 0.0} and small.privacy == "private"


def test_tick_queues_each_due_chore_once_and_moves_it_on(env, tmp_path):
    clock = Clock(ts(TPE, 2026, 11, 13, 7, 0))
    store = ChoreStore(env, clock.now)
    daily = store.add("d", feeds_spec(tmp_path), tz="Asia/Taipei")
    weekly = store.add("w", feeds_spec(tmp_path, freq="weekly", days=(0,)), tz="Asia/Taipei")
    assert store.tick() == [] and store.due() == []
    clock.t = ts(TPE, 2026, 11, 13, 8, 0)
    assert [c.id for c in store.due()] == [daily.id]
    made = store.tick()
    assert [r.chore_id for r in made] == [daily.id] and made[0].status == "queued" and made[0].trigger == "schedule"
    assert made[0].due_at == daily.next_run and local(made[0].deadline, TPE) == "2026-11-14 08:00"
    assert local(store.get(daily.id).next_run, TPE) == "2026-11-14 08:00"
    assert store.tick() == [] and len(store.runs()) == 1  # the same minute again: nothing more
    clock.t = ts(TPE, 2026, 12, 25, 12, 0)  # the home was off for six weeks
    made = store.tick()
    assert sorted(r.chore_id for r in made) == sorted([daily.id, weekly.id])
    assert len(store.runs(weekly.id)) == 1  # six missed Mondays are one run, not six
    assert local(store.get(weekly.id).next_run, TPE) == "2026-12-28 08:00"
    assert local(store.get(daily.id).next_run, TPE) == "2026-12-26 08:00"
    statuses = sorted(r.status for r in store.runs(daily.id))
    assert statuses == ["queued", "skipped"]  # forty-two missed days are one run; the stale one from November is given up
    old = next(r for r in store.runs(daily.id) if r.status == "skipped")
    assert "next run came round" in old.summary


def test_pause_resume_and_remove(env, tmp_path):
    clock = Clock(ts(TPE, 2026, 11, 13, 7, 0))
    store = ChoreStore(env, clock.now)
    chore = store.add("d", feeds_spec(tmp_path), tz="Asia/Taipei")
    clock.t = ts(TPE, 2026, 11, 13, 8, 1)
    store.tick()
    paused = store.set_enabled(chore.id, False)
    assert not paused.enabled and paused.public()["next_run"] is None
    assert store.runs(chore.id)[0].status == "skipped"  # what was queued does not run after a pause
    clock.t = ts(TPE, 2026, 11, 20, 9, 0)
    assert store.tick() == [] and store.due() == []
    resumed = store.set_enabled(chore.id, True)
    assert local(resumed.next_run, TPE) == "2026-11-21 08:00"  # from now on: the week it missed is not run late
    assert len(store.remove(chore.id)) == 1 and store.list() == [] and store.runs() == []
    with pytest.raises(schedule.ChoreError):
        store.get(chore.id)


def test_claiming_a_run_is_exclusive_and_a_dead_runner_is_recovered(env, tmp_path):
    clock = Clock(ts(TPE, 2026, 11, 13, 8, 0, 1))
    store = ChoreStore(env, clock.now)
    chore = store.add("d", feeds_spec(tmp_path, time="08:00"), tz="Asia/Taipei")
    clock.t = ts(TPE, 2026, 11, 14, 8, 0)
    run = store.tick()[0]
    assert store.claim(clock.t - 60, "a") is None  # not before its time
    mine = store.claim(clock.t, "a")
    assert mine.id == run.id and mine.status == "running" and store.claim(clock.t, "b") is None
    assert store.settle(run.id, expect="queued", status="done") is False  # no longer queued
    with pytest.raises(schedule.ChoreError):
        store.settle(run.id, nonsense=1)
    with pytest.raises(schedule.ChoreError):
        store.settle(run.id, status="exploded")
    clock.t += schedule.STALE_RUNNING_S + 1
    store.tick()
    again = store.get_run(run.id)
    assert again.status == "queued" and again.attempts == 1 and store.claim(clock.t, "b").id == run.id
    store.mark_ran(chore.id, "done", "2 source(s) summarised, 0 not read")
    after = store.get(chore.id)
    assert after.runs == 1 and after.last_status == "done" and after.last_summary.startswith("2 source")
    assert store.last_ok_run(chore.id) == ""


TICKER = """
import sys, time
from muse.skills.schedule import ChoreStore
home, start, now = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
store = ChoreStore({"MUSE_HOME": home})
while time.time() < start:
    time.sleep(0.001)
made = 0
for step in range(30):
    made += len(store.tick(now + step))
print(made)
"""


def test_two_processes_ticking_at_once_do_not_queue_a_run_twice(env, tmp_path):
    clock = Clock(ts(TPE, 2026, 11, 13, 7, 0))
    store = ChoreStore(env, clock.now)
    for n in range(6):
        store.add(f"chore {n}", feeds_spec(tmp_path), tz="Asia/Taipei")
    due = ts(TPE, 2026, 11, 13, 8, 0)
    start = time.time() + 2.0
    child_env = {**os.environ, "PYTHONPATH": str(Path(schedule.__file__).resolve().parents[2])}
    procs = [subprocess.Popen([sys.executable, "-c", TICKER, env["MUSE_HOME"], str(start), str(due)], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=child_env, text=True) for _ in range(4)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), [e for _, e in outs]
    made = [int(out.strip()) for out, _ in outs]
    assert sum(made) == 6  # six chores, six runs, however the four processes interleaved
    runs = store.runs(limit=500)
    assert len(runs) == 6 and len({r.chore_id for r in runs}) == 6 and all(r.status == "queued" for r in runs)


def test_many_threads_ticking_and_claiming_never_double_up(env, tmp_path):
    clock = Clock(ts(TPE, 2026, 11, 13, 7, 0))
    store = ChoreStore(env, clock.now)
    for n in range(5):
        store.add(f"chore {n}", feeds_spec(tmp_path), tz="Asia/Taipei")
    due = ts(TPE, 2026, 11, 13, 8, 0)
    made: list[int] = []
    claimed: list[str] = []
    gate = threading.Barrier(8)

    def work(name: str) -> None:
        mine = ChoreStore(env, lambda: due)
        gate.wait()
        made.append(len(mine.tick(due)))
        while (run := mine.claim(due, name)) is not None:
            claimed.append(run.id)

    threads = [threading.Thread(target=work, args=(f"t{n}",)) for n in range(8)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert sum(made) == 5 and len(claimed) == 5 and len(set(claimed)) == 5


# ================================================================ templates, end to end


@pytest.fixture()
def feeds(tmp_path) -> Path:
    folder = tmp_path / "feeds"
    folder.mkdir()
    (folder / "alpha.xml").write_text(
        "<?xml version='1.0'?><rss><channel><title>Alpha</title>"
        f"<item><title>Alpha ships version two</title><description><![CDATA[The <b>release</b> adds offline mode. {CANARY}]]></description></item>"
        "<item><title>Alpha hires</title><description>Ten new engineers &amp; a designer.</description></item>"
        "</channel></rss>", encoding="utf-8")
    (folder / "beta.atom").write_text(
        "<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>Beta opens an office</title>"
        "<summary>The office is in Taipei.</summary></entry></feed>", encoding="utf-8")
    (folder / "gamma.json").write_text(json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": [
        {"title": "Gamma report", "content_text": "Revenue rose in the third quarter."}]}), encoding="utf-8")
    (folder / "notes.txt").write_text("Delta newsletter. " + "filler words " * 700 + CANARY_DEEP, encoding="utf-8")
    return folder


def bullets(*points: str) -> str:
    return "\n".join(f"- {p}" for p in points)


def run_chore(env, template: str, folders=(), out=None, budget=None, **inputs):
    inputs.setdefault("date", "2026-11-16")
    return runner.run(SKILL, {"mode": "run", "template": template, **inputs}, folders=folders, out_dir=out, environ=env, budget=budget)


def good_feed_reply(request):
    if request["path"].endswith("/embeddings"):
        return None
    prompt = request["prompt"]
    if "POINTS:" in prompt:
        return chat("Several products shipped and one company grew.", task="everyday")
    if "Alpha ships" in prompt:
        return chat(bullets("Alpha released version two with an offline mode.", "It hired ten engineers and a designer."), task="light")
    if "Beta opens" in prompt:
        return chat(bullets("Beta opened an office in Taipei."), task="light")
    if "Gamma report" in prompt:
        return chat("1. Gamma's revenue rose in the third quarter.\n2) It published a report.", task="light")
    return chat(f"* The newsletter repeats filler words. {CANARY_REPLY}", task="light")


def test_feeds_are_summarised_one_light_call_each_and_the_file_is_only_proposed(env, proxy, feeds, tmp_path):
    proxy.responder = good_feed_reply
    out = tmp_path / "out"
    result = run_chore(env, "summarise-feeds", [feeds], out)
    assert result.status == "done" and result.check == {"status": "passed", "reasons": []}
    tasks = [r["task"] for r in proxy.requests]
    assert tasks == ["light"] * 4 + ["everyday"] and result.used["requests"] == 5
    o = result.output
    assert o["given"] == ["alpha.xml", "beta.atom", "gamma.json", "notes.txt"] and o["could_not_read"] == []
    assert [len(s["points"]) for s in o["sources"]] == [2, 1, 2, 1]
    assert o["filename"] == "feed-digest-2026-11-16.md" and o["write"] == str(out / "feed-digest-2026-11-16.md")
    md = o["markdown"]
    assert md.startswith("# Feed digest · 2026-11-16\n") and "## Overview" in md and "## alpha.xml" in md
    assert "- Gamma's revenue rose in the third quarter." in md and "Sources read: 4. Not read: 0." in md
    assert not out.exists() and len(result.writes) == 1 and not result.writes[0].applied  # nothing written without consent
    assert result.writes[0].data == md.encode() and result.writes[0].reason == "Feed digest"
    runner.apply_writes(result)
    assert (out / "feed-digest-2026-11-16.md").read_text() == md
    again = run_chore(env, "summarise-feeds", [feeds], out)
    assert again.output["filename"] == "feed-digest-2026-11-16-2.md" and again.check["status"] == "passed"  # never the same name twice


def test_only_the_budgeted_excerpt_of_a_feed_is_ever_sent(env, proxy, feeds):
    proxy.responder = good_feed_reply
    result = run_chore(env, "summarise-feeds", [feeds])
    limit = mod("templates").LIMITS["summarise-feeds"]["excerpt"]
    sent = "".join(json.dumps(r["body"], ensure_ascii=False) for r in proxy.requests)
    assert CANARY in sent and CANARY_DEEP not in sent  # the start of a file may go; what lies past the excerpt never does
    assert all(s["sent_chars"] <= limit for s in result.output["sources"]) and result.output["sources"][3]["sent_chars"] == limit
    assert "<b>" not in sent and "CDATA" not in sent and "&amp;" not in sent  # mark-up is stripped before anything is sent


def test_file_contents_never_reach_events_notes_or_the_stores(env, proxy, feeds, tmp_path, capsys):
    proxy.responder = good_feed_reply
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 59))
    store = ChoreStore(env, clock.now)
    chore = store.add("Every day at 08:00 summarise my feeds", feeds_spec(feeds, dest=str(tmp_path / "out")), tz="Asia/Taipei")
    clock.t += 120
    summary = chores.tick(env, clock=clock.now)
    run = store.runs(chore.id)[0]
    assert run.status == "waiting_card" and summary["ran"][0]["status"] == "waiting_card"
    direct = run_chore(env, "summarise-feeds", [feeds])
    printed = capsys.readouterr()
    for secret in (CANARY, CANARY_DEEP, CANARY_REPLY, "Alpha ships", "offline mode", "Taipei."):
        assert secret not in json.dumps(direct.events) and secret not in json.dumps(direct.notes)
        assert secret not in json.dumps(direct.used) and secret not in json.dumps(summary, ensure_ascii=False)
        assert secret not in printed.out and secret not in printed.err
        for name in ("chores.sqlite", "cards.sqlite", "apps.yaml"):
            path = Path(env["MUSE_HOME"]) / name
            assert not path.exists() or secret.encode() not in path.read_bytes(), (secret, name)
        record = Path(env["MUSE_HOME"]) / "skills-data" / SKILL
        assert not record.exists() or all(secret.encode() not in p.read_bytes() for p in record.rglob("*") if p.is_file())
    pending = Path(env["MUSE_HOME"]) / "chores-pending" / f"{run.id}.md"
    assert CANARY_REPLY in pending.read_text()  # the digest itself, waiting for the answer: owner-only
    assert stat.S_IMODE(pending.stat().st_mode) == 0o600 and stat.S_IMODE(pending.parent.stat().st_mode) == 0o700


def test_a_digest_with_a_source_missing_fails_its_check_and_is_retried_one_tier_stronger(env, proxy, feeds):
    def weak_then_good(request):
        if request["task"] == "light" and "Beta opens" in request["prompt"]:
            return chat("", task="light")  # the small model says nothing for this feed
        return good_feed_reply(request)

    proxy.responder = weak_then_good
    result = run_chore(env, "summarise-feeds", [feeds])
    assert result.status == "done" and result.check["status"] == "passed"
    assert any("first answer did not pass its check" in n for n in result.notes)
    tasks = [r["task"] for r in proxy.requests]
    assert tasks[:4] == ["light"] * 4 and tasks[-5:] == ["everyday"] * 4 + ["heavy"]  # the retry: every step one tier up
    assert [len(s["points"]) for s in result.output["sources"]] == [2, 1, 2, 1] and len(result.writes) == 1


def test_a_digest_that_fails_twice_is_a_draft_and_names_only_counts(env, proxy, feeds):
    proxy.responder = lambda r: None if r["path"].endswith("/embeddings") else (
        chat("", task=r["task"]) if "Beta opens" in r["prompt"] or "Gamma report" in r["prompt"] else good_feed_reply(r))
    result = run_chore(env, "summarise-feeds", [feeds])
    assert result.status == "needs_choice" and result.check["status"] == "failed"
    assert result.check["reasons"] == ["the digest missed 2 source(s)"]
    assert result.writes == [] and "beta" not in json.dumps(result.notes).lower()
    assert result.output["markdown"]  # still returned, for "See what it got"


def test_the_wrong_language_fails_the_check(env, proxy, feeds):
    proxy.responder = good_feed_reply
    result = run_chore(env, "summarise-feeds", [feeds], lang="zh-TW")
    assert result.status == "needs_choice" and "the digest is not in the language that was asked for" in result.check["reasons"]
    proxy.responder = lambda r: None if r["path"].endswith("/embeddings") else chat(
        "各家產品都有更新，其中一家公司擴大了規模。" if "POINTS:" in r["prompt"] else bullets("這個來源提到產品更新與公司擴張的消息。", "也提到第三季的營收成長。"),
        task=r["task"])
    zh = run_chore(env, "summarise-feeds", [feeds], lang="zh-TW")
    assert zh.status == "done" and zh.output["markdown"].startswith("# 訂閱摘要 · 2026-11-16") and "## 總覽" in zh.output["markdown"]
    assert "Traditional Chinese" in proxy.requests[-1]["prompt"]
    english_wanted = run_chore(env, "summarise-feeds", [feeds], lang="en")
    assert "the digest is not in the language that was asked for" in english_wanted.check["reasons"]


def test_a_run_that_would_exceed_its_budget_stops_with_a_partial_result_and_says_so(env, proxy, feeds):
    proxy.responder = good_feed_reply
    result = run_chore(env, "summarise-feeds", [feeds], budget={"requests": 2})
    assert result.status == "partial" and result.check["status"] == "passed"
    assert len(proxy.requests) == 2 and result.used["requests"] == 2
    o = result.output
    assert [s["name"] for s in o["sources"]] == ["alpha.xml", "beta.atom"]
    assert [(m["name"], m["reason"]) for m in o["could_not_read"]] == [("gamma.json", "budget"), ("notes.txt", "budget")]
    assert any("budget was used up" in n and "2 source(s)" in n for n in result.notes)
    assert "## Could not read" in o["markdown"] and "- gamma.json: this run's budget was used up first" in o["markdown"]
    assert o["overview"] == "" and len(result.writes) == 1  # what was done is kept and still offered
    tokens = run_chore(env, "summarise-feeds", [feeds], budget={"tokens": 2500})
    assert tokens.status == "partial" and any(m["reason"] == "budget" for m in tokens.output["could_not_read"])
    nothing = run_chore(env, "summarise-feeds", [feeds], budget={"requests": 0})
    assert nothing.status == "partial" and nothing.output["sources"] == [] and len(nothing.output["could_not_read"]) == 4


def test_sources_that_cannot_be_read_are_listed_with_the_reason(env, proxy, tmp_path):
    folder = tmp_path / "mixed"
    folder.mkdir()
    (folder / "good.md").write_text("A short newsletter about gardens.")
    (folder / "links.txt").write_text("https://example.com/feed.xml\n- https://example.org/rss\n")
    (folder / "subs.opml").write_text("<opml><body><outline xmlUrl='https://example.com/x'/></body></opml>")
    (folder / "empty.html").write_text("<html><script>var x = 1;</script></html>")
    (folder / "binary.txt").write_bytes(b"\x00\x01\x02\x03")
    (folder / "big.md").write_bytes(b"x" * 2_000_001)
    (folder / "photo.png").write_bytes(b"\x89PNG")
    (folder / ".hidden.md").write_text("not a source")
    result = run_chore(env, "summarise-feeds", [folder], urls=json.dumps(["https://news.example/feed", "ftp://nope", 7]))
    assert result.status == "partial" and result.check["status"] == "passed" and len(proxy.requests) == 1
    reasons = {m["name"]: m["reason"] for m in result.output["could_not_read"]}
    assert reasons == {"links.txt": "addresses", "subs.opml": "addresses", "empty.html": "empty", "binary.txt": "not-text",
                       "big.md": "large", "https://news.example/feed": "sandbox"}
    assert "- https://news.example/feed: a web address; fetching needs Muse's sandbox, which is not built yet" in result.output["markdown"]
    assert any("1 file(s) of other kinds" in n for n in result.notes) and any("sandbox" in n for n in result.notes)
    assert mod("fetch").fetcher().available is False
    with pytest.raises(mod("fetch").NeedsSandbox):
        mod("fetch").fetcher().fetch("https://example.com", 10)


def test_an_empty_folder_proposes_nothing_and_a_missing_folder_stops(env, proxy, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run_chore(env, "summarise-feeds", [empty])
    assert result.status == "partial" and result.writes == [] and proxy.requests == []
    assert run_chore(env, "summarise-feeds").status == "failed"
    assert run_chore(env, "no-such-template", [empty]).status == "failed"
    assert runner.run(SKILL, {"mode": "dance"}, environ=env).status == "failed"


def test_the_check_names_what_is_wrong_with_a_digest(env, proxy, feeds):
    proxy.responder = good_feed_reply
    m = mod()
    good = run_chore(env, "summarise-feeds", [feeds]).output
    assert m.check(json.loads(json.dumps(good))) == []

    def broken(change) -> list[str]:
        out = json.loads(json.dumps(good))
        change(out)
        return m.check(out)

    assert "the digest missed 1 source(s)" in broken(lambda o: o["sources"].pop(0))
    assert any("point(s) are empty or too long" in r for r in broken(lambda o: o["sources"][0]["points"].append("x" * 301)))
    assert any("too many points" in r for r in broken(lambda o: o["sources"][0].update(points=["p"] * 6)))
    assert any("budgeted excerpt" in r for r in broken(lambda o: o["sources"][0].update(sent_chars=6001)))
    assert any("not shown in the file" in r for r in broken(lambda o: o.update(markdown=o["markdown"].replace("## alpha.xml", "## other"))))
    assert any("dated name" in r for r in broken(lambda o: o.update(filename="digest.md")))
    assert any("dated name" in r for r in broken(lambda o: o.update(filename="feed-digest-2026-11-17.md")))
    assert any("longer than a digest" in r for r in broken(lambda o: o.update(overview="y" * 900)))
    assert any("no file was made" in r for r in broken(lambda o: o.update(markdown="")))
    assert m.check(None) and m.check({"mode": "run", "template": "x"})


# ---- folder changes


def test_folder_changes_first_run_records_the_starting_point_then_reports_new_changed_deleted(env, proxy, tmp_path):
    folder, out = tmp_path / "notes", tmp_path / "out"
    folder.mkdir()
    (folder / "keep.md").write_text("Unchanged notes.")
    (folder / "plan.md").write_text("The plan, first version.")
    (folder / "old.md").write_text("To be removed.")
    first = run_chore(env, "summarise-folder-changes", [folder], out, chore="abcd1234", run_id="r1", last_ok_run="")
    assert first.status == "done" and first.writes == [] and proxy.requests == [] and first.output["first_run"] is True
    assert any("First run" in n and "3 file(s)" in n for n in first.notes)

    same = run_chore(env, "summarise-folder-changes", [folder], out, chore="abcd1234", run_id="r2", last_ok_run="r1")
    assert same.status == "done" and same.writes == [] and any("Nothing changed" in n for n in same.notes) and proxy.requests == []

    (folder / "plan.md").write_text("The plan, second version, with a budget section.")
    os.utime(folder / "plan.md", (time.time() + 5, time.time() + 5))
    (folder / "new.txt").write_text(f"Meeting notes from Monday. {CANARY}")
    (folder / "image.bin").write_bytes(b"\x00\x01")
    (folder / "old.md").unlink()
    proxy.responder = lambda r: None if r["path"].endswith("/embeddings") else chat(
        bullets("Meeting notes from Monday.") if "Meeting notes" in r["prompt"] else bullets("The plan now has a budget section."), task="light")
    third = run_chore(env, "summarise-folder-changes", [folder], out, chore="abcd1234", run_id="r3", last_ok_run="r2")
    assert third.status == "partial" and third.check["status"] == "passed" and len(proxy.requests) == 2
    o = third.output
    assert sorted(o["new"]) == ["image.bin", "new.txt"] and o["changed"] == ["plan.md"] and o["deleted"] == ["old.md"]
    assert o["could_not_read"] == [{"name": "image.bin", "reason": "not-text", "text": "not a text file"}]
    md = o["markdown"]
    assert md.startswith("# What changed · 2026-11-16") and "2 new, 1 changed, 1 deleted since 2026-11-16." in md
    assert "### new.txt" in md and "### plan.md" in md and "## Deleted\n\n- old.md" in md and "keep.md" not in md
    assert o["filename"] == "folder-changes-2026-11-16.md" and len(third.writes) == 1
    assert all(r["task"] == "light" for r in proxy.requests)


def test_a_run_that_did_not_deliver_does_not_swallow_the_changes_it_saw(env, proxy, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.md").write_text("First file.")
    args = dict(chore="feed0001")
    run_chore(env, "summarise-folder-changes", [folder], **args, run_id="r1", last_ok_run="")
    (folder / "b.md").write_text("Second file, added later.")
    proxy.responder = lambda r: None if r["path"].endswith("/embeddings") else chat("", task=r["task"])
    failed = run_chore(env, "summarise-folder-changes", [folder], **args, run_id="r2", last_ok_run="r1")
    assert failed.status == "needs_choice" and failed.output["new"] == ["b.md"]
    assert len(proxy.requests) == 2 and [r["task"] for r in proxy.requests] == ["light", "everyday"]  # the retry saw the same change
    proxy.responder = lambda r: None if r["path"].endswith("/embeddings") else chat(bullets("A second file was added."), task="light")
    later = run_chore(env, "summarise-folder-changes", [folder], **args, run_id="r3", last_ok_run="r1")
    assert later.status == "done" and later.output["new"] == ["b.md"]  # still reported: r2 never delivered it
    after = run_chore(env, "summarise-folder-changes", [folder], **args, run_id="r4", last_ok_run="r3")
    assert after.output["new"] == [] and any("Nothing changed" in n for n in after.notes)
    record = Path(env["MUSE_HOME"]) / "skills-data" / SKILL / "changes-feed0001.json"
    saved = json.loads(record.read_text())
    assert set(saved["candidate"]) == {"a.md", "b.md"} and "Second file" not in record.read_text()  # names and sizes only
    assert stat.S_IMODE(record.stat().st_mode) == 0o600


def test_folder_changes_outside_a_chore_still_work_run_after_run(env, proxy, tmp_path):
    folder = tmp_path / "notes"
    folder.mkdir()
    (folder / "a.md").write_text("First file.")
    assert run_chore(env, "summarise-folder-changes", [folder]).output["first_run"] is True
    (folder / "b.md").write_text("Second.")
    second = run_chore(env, "summarise-folder-changes", [folder])
    assert second.status == "done" and second.output["new"] == ["b.md"]
    assert run_chore(env, "summarise-folder-changes", [folder]).output["new"] == []
    assert run_chore(env, "summarise-folder-changes").status == "failed"


# ---- daily brief


def seed_yesterday(env, day: datetime) -> None:
    from muse.ledger import Ledger

    stamp = day.timestamp()
    ledger = Ledger(environ=env, clock=lambda: stamp)
    for n in range(7):
        ledger.record_call(provider="freea", deployment_id="a", upstream="freea", ok=n != 6, tokens_in=10, tokens_out=5, app="skills")
    q = queue.Queue(env, clock=lambda: stamp)
    job = q.add(kind="prompt", payload="secret prompt text")
    with q._conn(write=True) as conn:  # noqa: SLF001 - a finished job, as the runner would leave it
        conn.execute("UPDATE jobs SET status='done', finished_at=?, payload='' WHERE id=?", (stamp, job.id))
    cards = CardStore(env, clock=lambda: stamp)
    cards.answer(cards.create(kind="save_file", title="Save a file", thing="a.md"), "approve", "phone")
    cards.create(kind="save_file", title="Save a file", thing="b.md", ttl_s=20 * 86400)


def test_the_daily_brief_counts_yesterday_and_needs_no_model(env, proxy, monkeypatch):
    monkeypatch.setenv("MUSE_TZ", "Asia/Taipei")
    seed_yesterday(env, datetime(2026, 11, 15, 10, 0, tzinfo=TPE))
    facts = chores.brief_facts(dict(os.environ), "2026-11-16")
    assert facts["day"] == "2026-11-15" and [s["name"] for s in facts["sections"]] == ["Requests", "Background jobs", "Chores", "Cards"]
    assert facts["sections"][0]["lines"] == ["6 request(s) answered, 1 failed", "nothing spent"]
    assert facts["sections"][1]["lines"] == ["1 finished, 0 failed, 0 waiting now"]
    assert facts["sections"][2]["lines"] == ["nothing recorded"]
    assert facts["sections"][3]["lines"] == ["1 approved, 0 declined, 0 expired, 1 waiting now"]
    assert chores.brief_facts(dict(os.environ), "2026-11-18")["sections"][0]["lines"] == ["nothing recorded"]  # another day

    result = run_chore(env, "daily-brief", budget={"requests": 0})
    assert result.status == "done" and result.check["status"] == "passed" and proxy.requests == []
    md = result.output["markdown"]
    assert md.startswith("# Daily brief · 2026-11-16\n\nWhat Muse did on 2026-11-15.")
    assert "## Requests\n\n- 6 request(s) answered, 1 failed" in md and "## Cards" in md
    assert result.output["filename"] == "daily-brief-2026-11-16.md" and len(result.writes) == 1
    assert "secret prompt text" not in md

    proxy.script.append(chat("Muse answered 6 requests and 1 failed; nothing was spent.", task="light"))
    with_words = run_chore(env, "daily-brief")
    assert with_words.status == "done" and "Muse answered 6 requests and 1 failed" in with_words.output["markdown"]
    assert proxy.requests[0]["task"] == "light" and "6 request(s) answered" in proxy.requests[0]["prompt"]

    proxy.script.append(chat("Muse answered 600 requests and saved you 42 dollars.", task="light"))
    invented = run_chore(env, "daily-brief")
    assert invented.status == "done" and "600" not in invented.output["markdown"] and invented.output["overview"] == ""
    assert any("overview was left out" in n for n in invented.notes)

    zh = run_chore(env, "daily-brief", lang="zh-TW", budget={"requests": 0})
    assert zh.status == "done" and "## 請求" in zh.output["markdown"] and "回答了 6 次請求，1 次失敗" in zh.output["markdown"]


def test_the_daily_brief_degrades_cleanly_when_records_cannot_be_read(env, proxy, monkeypatch):
    home = Path(env["MUSE_HOME"])
    (home / "queue.sqlite").write_bytes(b"this is not a database")
    (home / "cards.sqlite").write_bytes(b"neither is this")
    result = run_chore(env, "daily-brief", budget={"requests": 0})
    assert result.status == "partial" and result.check["status"] == "passed"
    assert sorted(m["name"] for m in result.output["could_not_read"]) == ["Background jobs", "Cards"]
    assert "- Cards: Muse could not read that part of its own records" in result.output["markdown"]
    assert "## Requests" in result.output["markdown"]  # the rest still stands

    monkeypatch.setattr(chores, "brief_facts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    gone = run_chore(env, "daily-brief", budget={"requests": 0})
    assert gone.status == "partial" and gone.output["sources"] == [] and len(gone.output["could_not_read"]) == 1
    held = run_chore(env, "daily-brief")  # nothing to summarise: no call either
    assert held.status == "partial" and proxy.requests == []


def test_a_held_back_summary_call_does_not_stop_the_brief(env, proxy, monkeypatch):
    monkeypatch.setenv("MUSE_TZ", "Asia/Taipei")
    seed_yesterday(env, datetime(2026, 11, 15, 10, 0, tzinfo=TPE))
    proxy.script.append(ration_429())
    result = run_chore(env, "daily-brief")
    assert result.status == "done" and result.output["overview"] == "" and len(proxy.requests) == 1


# ================================================================ the card path (run by tick)


def chore_due(env, feeds, tmp_path, clock, **kw):
    store = ChoreStore(env, clock.now)
    chore = store.add("Every day at 08:00, summarise my feeds", feeds_spec(feeds, dest=str(tmp_path / "out"), **kw), tz="Asia/Taipei")
    clock.t = ts(TPE, 2026, 11, 16, 8, 0, 30)
    return store, chore


def test_tick_runs_a_due_chore_and_leaves_a_save_file_card(env, proxy, feeds, tmp_path):
    proxy.responder = good_feed_reply
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store, chore = chore_due(env, feeds, tmp_path, clock)
    summary = chores.tick(env, clock=clock.now)
    assert len(summary["queued"]) == 1 and len(summary["ran"]) == 1 and summary["paused"] is False
    run = store.runs(chore.id)[0]
    assert run.status == "waiting_card" and run.summary == "4 source(s) summarised, 0 not read; waiting for your approval"
    assert run.requests == 5 and run.cost_usd == 0.0 and not (tmp_path / "out").exists()
    card = CardStore(env).get(run.card_id)
    assert card["kind"] == "save_file" and card["status"] == "pending" and card["title"] == "Save a file"
    assert card["thing"] == "feed-digest-2026-11-16.md" and card["where"].endswith("/out")
    assert card["under"]["text"] == "Feed digest chore" and card["cost"] == {"nothing": True}
    assert card["extra"]["size_bytes"] == len((Path(env["MUSE_HOME"]) / "chores-pending" / f"{run.id}.md").read_bytes())
    assert card["job_ref"] == f"chore:{chore.id}:{run.id}"
    after = store.get(chore.id)
    assert after.runs == 1 and after.last_status == "waiting_card" and local(after.next_run, TPE) == "2026-11-17 08:00"

    assert chores.tick(env, clock=clock.now)["ran"] == [] and len(proxy.requests) == 5  # still waiting: nothing is redone
    CardStore(env).answer(run.card_id, "approve", "phone")
    settled = chores.tick(env, clock=clock.now)["settled"]
    assert [r["status"] for r in settled] == ["done"]
    written = tmp_path / "out" / "feed-digest-2026-11-16.md"
    assert written.read_text().startswith("# Feed digest · 2026-11-16") and stat.S_IMODE(written.stat().st_mode) == 0o600
    done = store.get_run(run.id)
    assert done.status == "done" and done.out_path == str(written) and done.summary.endswith("saved")
    assert not (Path(env["MUSE_HOME"]) / "chores-pending" / f"{run.id}.md").exists()
    assert store.get(chore.id).last_status == "done" and store.get(chore.id).runs == 1
    assert store.last_ok_run(chore.id) == run.id


@pytest.mark.parametrize("how, status, word", [("decline", "declined", "not approved"), ("expire", "expired", "not approved in time")])
def test_a_declined_or_expired_card_writes_nothing_and_drops_the_pending_result(env, proxy, feeds, tmp_path, how, status, word):
    proxy.responder = good_feed_reply
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store, chore = chore_due(env, feeds, tmp_path, clock)
    chores.tick(env, clock=clock.now)
    run = store.runs(chore.id)[0]
    pending = Path(env["MUSE_HOME"]) / "chores-pending" / f"{run.id}.md"
    assert pending.exists()
    if how == "decline":
        CardStore(env, clock=clock.now).answer(run.card_id, "decline", "phone")
    else:
        clock.t += 25 * 3600  # a card expires after 24 hours
    chores.tick(env, clock=clock.now, run=False)
    after = store.get_run(run.id)
    assert after.status == status and after.summary == word and not pending.exists() and not (tmp_path / "out").exists()
    assert store.get(chore.id).last_status == status


def test_tick_can_wait_a_bounded_time_for_the_answer(env, proxy, feeds, tmp_path):
    proxy.responder = good_feed_reply
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store, chore = chore_due(env, feeds, tmp_path, clock)

    def approve_soon():
        deadline = time.time() + 20
        while time.time() < deadline:
            waiting = store.runs(chore.id, statuses=["waiting_card"])
            if waiting:
                CardStore(env).answer(waiting[0].card_id, "approve", "desktop")
                return
            time.sleep(0.05)

    helper = threading.Thread(target=approve_soon)
    helper.start()
    started = time.monotonic()
    summary = chores.tick(env, clock=clock.now, wait_card=20)
    helper.join()
    assert summary["ran"][0]["status"] == "done" and time.monotonic() - started < 15
    assert (tmp_path / "out" / "feed-digest-2026-11-16.md").exists()

    clock.t += 86400
    started = time.monotonic()
    unanswered = chores.tick(env, clock=clock.now, wait_card=0.4)
    assert unanswered["ran"][0]["status"] == "waiting_card" and 0.3 < time.monotonic() - started < 10  # bounded, then left pending


def test_an_approved_result_never_overwrites_a_file_that_appeared_meanwhile(env, proxy, feeds, tmp_path):
    proxy.responder = good_feed_reply
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store, chore = chore_due(env, feeds, tmp_path, clock)
    chores.tick(env, clock=clock.now)
    run = store.runs(chore.id)[0]
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "feed-digest-2026-11-16.md").write_text("someone else's file")
    CardStore(env).answer(run.card_id, "approve", "phone")
    chores.tick(env, clock=clock.now)
    assert (tmp_path / "out" / "feed-digest-2026-11-16.md").read_text() == "someone else's file"
    done = store.get_run(run.id)
    assert done.status == "done" and done.out_path.endswith("feed-digest-2026-11-16-2.md") and Path(done.out_path).exists()


def test_a_chore_that_fails_its_check_twice_says_so_in_counts_and_keeps_a_draft(env, proxy, feeds, tmp_path):
    proxy.responder = lambda r: None if r["path"].endswith("/embeddings") else (
        chat("", task=r["task"]) if "Beta opens" in r["prompt"] or "Gamma report" in r["prompt"] else good_feed_reply(r))
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store, chore = chore_due(env, feeds, tmp_path, clock)
    chores.tick(env, clock=clock.now)
    run = store.runs(chore.id)[0]
    assert run.status == "needs_choice" and run.summary == "Couldn't finish: the digest missed 2 source(s)"
    assert run.card_id is None and CardStore(env).list() == []
    draft = Path(env["MUSE_HOME"]) / "chores-pending" / f"{run.id}.draft.md"
    assert draft.read_text().startswith("# Feed digest") and stat.S_IMODE(draft.stat().st_mode) == 0o600
    assert store.get(chore.id).last_status == "needs_choice" and store.last_ok_run(chore.id) == ""


def test_a_missing_folder_fails_the_run_plainly_and_pause_stops_running(env, proxy, feeds, tmp_path):
    proxy.responder = good_feed_reply
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store, chore = chore_due(env, feeds, tmp_path, clock)
    queue.set_paused(env)
    paused = chores.tick(env, clock=clock.now)
    assert paused["paused"] is True and len(paused["queued"]) == 1 and paused["ran"] == [] and proxy.requests == []
    queue.clear_paused(env)
    for path in sorted(feeds.iterdir()):
        path.unlink()
    feeds.rmdir()
    ran = chores.tick(env, clock=clock.now)["ran"]
    assert ran[0]["status"] == "failed" and ran[0]["summary"] == "a folder this chore reads is no longer there"
    assert proxy.requests == []


# ================================================================ spare allowance


def test_a_spare_allowance_chore_goes_out_as_the_background_app(env, proxy, feeds, tmp_path):
    proxy.responder = good_feed_reply
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store, chore = chore_due(env, feeds, tmp_path, clock, time="", spare_only=True)
    assert chore.spare_only and local(chore.next_run, TPE) == "2026-11-17 00:00"
    clock.t = ts(TPE, 2026, 11, 17, 0, 0, 5)
    chores.tick(env, clock=clock.now)
    registry = apps.load_apps(env)
    assert registry["skills-background"].priority == "background" and registry["skills-background"].allow_paid is False
    token = env["MUSE_SKILLS_BACKGROUND_APP_TOKEN"]
    assert {r["auth"] for r in proxy.requests} == {f"Bearer {token}"}  # every call as the background app
    assert "x-muse-allow-paid" not in proxy.requests[0]["headers"]
    assert store.runs(chore.id)[0].status == "waiting_card"

    ordinary = run_chore(env, "summarise-feeds", [feeds])
    assert ordinary.status == "done" and proxy.requests[-1]["auth"] == f"Bearer {env['MUSE_SKILLS_APP_TOKEN']}"
    assert apps.load_apps(env)["skills"].priority == "normal"


def test_a_held_back_run_waits_for_the_time_the_router_named_and_is_not_a_failure(env, proxy, feeds, tmp_path):
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store, chore = chore_due(env, feeds, tmp_path, clock, time="", spare_only=True)
    clock.t = ts(TPE, 2026, 11, 17, 0, 0, 5)
    proxy.script.append(ration_429("defer:pace", retry_after=1800))
    first = chores.tick(env, clock=clock.now)["ran"][0]
    assert first["status"] == "queued" and first["summary"] == "waiting for allowance (defer:pace)"
    run = store.runs(chore.id)[0]
    assert run.deferrals == 1 and run.attempts == 0 and run.not_before == clock.t + 1800
    assert store.get(chore.id).runs == 0 and len(proxy.requests) == 1

    clock.t += 600
    assert chores.tick(env, clock=clock.now)["ran"] == [] and len(proxy.requests) == 1  # not before the router said
    clock.t += 1300
    proxy.responder = good_feed_reply
    second = chores.tick(env, clock=clock.now)["ran"][0]
    assert second["status"] == "waiting_card" and store.get(chore.id).runs == 1


def test_a_run_still_held_when_the_next_one_comes_round_is_skipped(env, proxy, feeds, tmp_path):
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store, chore = chore_due(env, feeds, tmp_path, clock, time="", spare_only=True)
    clock.t = ts(TPE, 2026, 11, 17, 0, 0, 5)
    proxy.script.append(ration_429("defer:reserve", retry_after=999999))
    chores.tick(env, clock=clock.now)
    held = store.runs(chore.id)[0]
    assert held.status == "queued" and held.not_before == clock.t + chores.MAX_RETRY_S  # never further out than six hours
    clock.t = ts(TPE, 2026, 11, 18, 0, 0, 5)
    proxy.responder = good_feed_reply
    summary = chores.tick(env, clock=clock.now)
    assert store.get_run(held.id).status == "skipped"
    assert store.get_run(held.id).summary == "no allowance was free before the next run came round"
    assert len(summary["queued"]) == 1 and summary["ran"][0]["status"] == "waiting_card"  # the new day's run goes ahead


def test_an_unreachable_router_is_retried_a_few_times_then_reported(env, proxy, feeds, tmp_path, monkeypatch):
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store, chore = chore_due(env, feeds, tmp_path, clock)
    dead = dict(env, MUSE_PROXY_URL="http://127.0.0.1:9")
    statuses = []
    for _ in range(4):
        ran = chores.tick(dead, clock=clock.now)["ran"]
        statuses.append(ran[0]["status"] if ran else None)
        clock.t += 1000
    assert statuses == ["queued", "queued", "failed", None]
    run = store.runs(chore.id)[0]
    assert run.status == "failed" and run.summary == "the router could not be reached" and run.attempts == 2
    assert store.get(chore.id).last_status == "failed" and proxy.requests == []


def test_run_now_does_not_requeue_and_reports_a_hold(env, proxy, feeds, tmp_path):
    clock = Clock(ts(TPE, 2026, 11, 16, 7, 0))
    store = ChoreStore(env, clock.now)
    chore = store.add("d", feeds_spec(feeds, dest=str(tmp_path / "out")), tz="Asia/Taipei")
    proxy.script.append(ration_429("defer:reserve", retry_after=600))
    queued = store.add_manual_run(chore.id)
    done = chores.run_one(store, store.claim(clock.t, "me", queued.id), env, deliver="write", requeue=False)
    assert done.status == "needs_choice" and done.summary == "waiting for allowance (defer:reserve)" and done.trigger == "manual"
    proxy.responder = good_feed_reply
    asked: list[tuple[str, int]] = []
    queued = store.add_manual_run(chore.id)
    no = chores.run_one(store, store.claim(clock.t, "me", queued.id), env, deliver="ask",
                        confirm=lambda path, size: asked.append((path, size)) or False)
    assert no.status == "waiting_card" and asked and asked[0][0].endswith("feed-digest-2026-11-16.md")  # a "no" leaves a card
    queued = store.add_manual_run(chore.id)
    yes = chores.run_one(store, store.claim(clock.t, "me", queued.id), env, deliver="ask", confirm=lambda path, size: True)
    assert yes.status == "done" and Path(yes.out_path).exists() and yes.summary.endswith("saved")
    usual = store.typical_time(chore.id, "Asia/Taipei")
    assert usual == "07:00"


# ================================================================ the command line


def test_cli_add_shows_what_was_understood_and_needs_one_confirmation(env, proxy, feeds, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MUSE_TZ", "Asia/Taipei")
    sentence = f"Every Monday 08:00, summarise the feeds in {feeds} into {tmp_path}/out"
    assert cli.main(["chores", "add", sentence]) == 1  # no terminal, no --yes: shown, not added
    shown = capsys.readouterr()
    assert "What: Summarise the feed files in" in shown.out and "When: Every Monday at 08:00 (Asia/Taipei time)" in shown.out
    assert "Budget: up to 40 requests, nothing paid" in shown.out and "Needs: Read the files in" in shown.out
    assert "Not added" in shown.err and ChoreStore().list() == []

    assert cli.main(["chores", "add", sentence, "--yes", "--requests", "12", "--privacy", "private"]) == 0
    added = capsys.readouterr()
    assert "Budget: up to 12 requests, nothing paid" in added.out and "Added chore" in added.out and "muse chores tick" in added.err
    chore = ChoreStore().list()[0]
    assert chore.budget["requests"] == 12 and chore.privacy == "private" and chore.tz == "Asia/Taipei"
    assert chore.spec.sources == (str(feeds),) and chore.spec.dest == f"{tmp_path}/out" and proxy.requests == []

    assert cli.main(["chores", "list"]) == 0
    listed = capsys.readouterr().out
    assert chore.id in listed and sentence in listed and "up to 12 requests, nothing paid" in listed and "not run yet" in listed
    assert cli.main(["chores", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["chores"][0]["id"] == chore.id
    assert cli.main(["chores", "show", chore.id[:5]]) == 0
    detail = capsys.readouterr().out
    assert "Privacy: private" in detail and "History: not run yet" in detail and "Result: A new dated Markdown file" in detail
    assert cli.main(["chores", "show", "ffff"]) == 1 and cli.main(["chores"]) == 2
    assert cli.main(["chores", "add", sentence, "--requests", "0"]) == 2


def test_cli_add_asks_instead_of_guessing(env, proxy, feeds, capsys):
    assert cli.main(["chores", "add", "Every other Monday summarise the feeds in " + str(feeds), "--yes"]) == 4
    asked = capsys.readouterr()
    assert "Question: Muse can repeat a chore every day" in asked.out and "Not added" in asked.err
    assert cli.main(["chores", "add", "每週一早上8點摘要這五個訂閱", "--yes"]) == 4
    assert "請問：要讀哪個資料夾？" in capsys.readouterr().out
    assert cli.main(["chores", "add", "每週一早上8點摘要這五個訂閱", "--yes", "--folder", str(feeds)]) == 0
    assert "做什麼：摘要" in capsys.readouterr().out
    assert cli.main(["chores", "add", "Every day at 8am do the thing", "--json"]) == 4
    data = json.loads(capsys.readouterr().out)
    assert data["added"] is False and [q["code"] for q in data["understood"]["questions"]] == ["what"]
    assert len(ChoreStore().list()) == 1 and len(proxy.requests) == 1  # "what" alone: one model call was tried, and it did not help


def test_cli_add_uses_the_model_only_when_rules_cannot_read_and_no_model_forbids_it(env, proxy, feeds, capsys):
    vague = "Get me up to speed on things at the start of the working week"
    assert cli.main(["chores", "add", vague, "--yes", "--no-model", "--folder", str(feeds)]) == 4
    assert proxy.requests == []
    capsys.readouterr()
    proxy.script.append(chat('{"frequency": "weekly", "days": ["monday"], "time": "07:30", "template": "summarise-feeds"}', task="light"))
    assert cli.main(["chores", "add", vague, "--yes", "--folder", str(feeds)]) == 0
    out = capsys.readouterr().out
    assert "Every Monday at 07:30" in out and "Note: Part of this was read by a model" in out
    assert len(proxy.requests) == 1 and vague in proxy.requests[0]["prompt"]
    assert ChoreStore().list()[0].spec.days == (0,)


def test_cli_run_now_pause_resume_edit_rm_and_tick(env, proxy, feeds, tmp_path, capsys, monkeypatch):
    proxy.responder = good_feed_reply
    monkeypatch.setenv("MUSE_TZ", "Asia/Taipei")
    out = tmp_path / "out"
    assert cli.main(["chores", "add", f"Every day at 08:00, summarise the feeds in {feeds} into {out}", "--yes"]) == 0
    chore = ChoreStore().list()[0]
    capsys.readouterr()

    assert cli.main(["chores", "run-now", chore.id, "--yes"]) == 0
    ran = capsys.readouterr().out
    assert "done: 4 source(s) summarised, 0 not read; saved" in ran and "Wrote " in ran
    written = list(out.iterdir())
    assert len(written) == 1 and written[0].name.startswith("feed-digest-") and written[0].read_text().startswith("# Feed digest")

    assert cli.main(["chores", "run-now", chore.id, "--json"]) == 0  # nobody to ask: the result waits on a card
    waiting = json.loads(capsys.readouterr().out)["run"]
    assert waiting["status"] == "waiting_card" and len(list(out.iterdir())) == 1
    CardStore().answer(waiting["card_id"], "approve", "phone")
    assert cli.main(["chores", "tick"]) == 0
    assert "done:" in capsys.readouterr().out and len(list(out.iterdir())) == 2
    assert cli.main(["chores", "tick"]) == 0 and "Nothing is due." in capsys.readouterr().out
    assert cli.main(["chores", "tick", "--json", "--enqueue-only"]) == 0
    assert json.loads(capsys.readouterr().out) == {"settled": [], "queued": [], "ran": [], "paused": False}

    assert cli.main(["chores", "show", chore.id]) == 0
    history = capsys.readouterr().out
    assert history.count("done") >= 2 and "saved" in history

    assert cli.main(["chores", "pause", chore.id]) == 0 and not ChoreStore().get(chore.id).enabled
    assert cli.main(["chores", "list"]) == 0 and "paused" in capsys.readouterr().out
    assert cli.main(["chores", "resume", chore.id]) == 0 and ChoreStore().get(chore.id).enabled
    capsys.readouterr()

    assert cli.main(["chores", "edit", chore.id, f"每週五下午三點半摘要 {feeds} 的訂閱"]) == 1  # nobody confirmed
    assert ChoreStore().get(chore.id).spec.freq == "daily"
    assert cli.main(["chores", "edit", chore.id, "every fortnight summarise feeds", "--yes"]) == 4
    assert cli.main(["chores", "edit", chore.id, f"每週五下午三點半摘要 {feeds} 的訂閱", "--yes"]) == 0
    edited = ChoreStore().get(chore.id)
    assert edited.spec.freq == "weekly" and edited.spec.days == (4,) and edited.spec.time == "15:30" and edited.spec.lang == "zh-TW"
    assert edited.runs == 2 and edited.id == chore.id  # same chore, same history
    capsys.readouterr()

    assert cli.main(["chores", "rm", chore.id]) == 1 and len(ChoreStore().list()) == 1  # nobody confirmed
    assert cli.main(["chores", "rm", chore.id, "--yes"]) == 0 and ChoreStore().list() == []
    assert len(list(out.iterdir())) == 2  # files it wrote stay
    assert cli.main(["chores", "run-now", chore.id]) == 1 and cli.main(["chores", "pause", "abcd"]) == 1
    assert cli.main(["chores", "list"]) == 0 and "No chores yet" in capsys.readouterr().out


def test_cli_tick_when_paused_queues_but_does_not_run(env, proxy, feeds, tmp_path, capsys):
    clock = Clock(ts(TPE, 2020, 1, 1, 7, 0))
    store = ChoreStore(env, clock.now)
    store.add("d", feeds_spec(feeds), tz="Asia/Taipei")  # due long ago, by the real clock
    queue.set_paused(env)
    assert cli.main(["chores", "tick"]) == 0
    assert "Muse is paused" in capsys.readouterr().out and proxy.requests == []
    assert [r.status for r in ChoreStore().runs()] == ["queued"]


def test_the_skill_is_listed_shows_its_limits_and_runs_from_the_skills_command(env, proxy, feeds, tmp_path, capsys):
    proxy.responder = good_feed_reply
    assert cli.main(["skills", "show", SKILL]) == 0
    shown = capsys.readouterr().out
    assert "cannot fetch web addresses" in shown and "sandbox" in shown and "up to 40 requests" in shown
    skill = loader.find(SKILL)
    assert skill.needs.net == () and skill.writes is True and skill.tier_default == "light"
    code = cli.main(["skills", "run", SKILL, "--input", "template=summarise-feeds", "--input", "date=2026-11-16", "--folder", str(feeds),
                     "--out", str(tmp_path / "o"), "--yes"])
    printed = capsys.readouterr()
    assert code == 0 and "# Feed digest · 2026-11-16" in printed.out and "Wrote " in printed.err
    assert (tmp_path / "o" / "feed-digest-2026-11-16.md").exists()
    assert cli.main(["skills", "run", SKILL, "--input", "mode=understand", "--input", f"sentence=每個工作天下午三點半整理 {feeds} 的變動"]) == 0
    assert "什麼時候：每個工作天（週一到週五）15:30" in capsys.readouterr().out
