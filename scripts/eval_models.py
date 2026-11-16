#!/usr/bin/env python3
"""Rank the models a running Muse proxy can reach on a small Traditional Chinese (Taiwan) test set.

Work package A10: the routing defaults for Taiwan users should be chosen from data.
The set lives in `evals/zh-tw/` (about 200 short items) with an English control
set in `evals/en-control/`. Every item is scored by fixed rules in this file; no
judge model is used. Read `evals/zh-tw/README.md` for what a pass does and does
not show, and `docs/evals.md` for how to run this.

Run it on your own machine, after `muse up`. It talks to the local proxy only.

Before anything is sent it prints the plan: the targets, the worst-case number of
requests and an upper bound on tokens, and which targets are free. It then asks
for confirmation unless you pass --yes. Targets that can reach a paid provider
are left out unless you pass --allow-paid.

What it writes: `$MUSE_HOME/eval-report.json` (or --report PATH). The report
holds NO key. It holds item ids, scores, timings and the `x-muse-*` routing
headers. Model outputs are stored only with --keep-outputs. Prompts are never
stored (they are in the public item files).

`--replay FILE` scores recorded outputs with no network at all; the tests use it.

Exit code: 0 when the run (or replay) finished, 2 on a usage problem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
ZH_DIR = ROOT / "evals" / "zh-tw"
EN_DIR = ROOT / "evals" / "en-control"

TIERS = ("light", "everyday")
SKILLS = ("setup", "files", "research", "chores")  # the four first skills, docs/muse-plan.md section 7.1
TASKS = {
    "light": ("classify", "extract", "yesno", "pick", "lang", "s2t", "normalise"),
    "everyday": ("cite_qa", "not_found", "summarise", "rewrite", "arithmetic", "format", "mixed"),
}
KINDS = ("exact", "one_of", "json_fields", "json_keys", "contains_all", "contains_any", "contains_none",
         "cites", "number", "max_chars", "bullets", "script")
POSITIVE_KINDS = ("exact", "one_of", "json_fields", "contains_all", "contains_any", "cites", "number")
SCRIPT_MODES = ("soft", "hard", "ignore")
ITEM_KEYS = {"id", "lang", "tier", "skill", "task", "prompt", "messages", "expect", "notes", "non_tw_text"}

# max_tokens per task. Light work stays at the light ceiling in config/routing.yaml (300).
MAX_TOKENS = {
    "classify": 160, "pick": 160, "lang": 160, "yesno": 200, "normalise": 200, "s2t": 300, "extract": 300,
    "cite_qa": 600, "not_found": 400, "summarise": 600, "rewrite": 500, "arithmetic": 500, "format": 600, "mixed": 600,
}
DEFAULT_MAX_WAIT = 120.0
DEFAULT_STOP_AFTER = 5
DEFAULT_DELAY = 1.0
TIMEOUT = 90.0
FREE_TIERS = {"free", "local"}
PAID_TIERS = {"paid", "metered"}
SAFE_HEADER = re.compile(r"^[A-Za-z0-9_.@/:,;>\-]{1,120}$")

_sleep: Callable[[float], None] = time.sleep  # tests replace this


# ============================================================ Traditional / Simplified awareness
#
# A SMALL built-in table, not OpenCC. Left: a character used in Simplified Chinese
# that is not the standard Taiwan form. Right: the usual Traditional form. It is
# used (1) to notice an answer written in Simplified, as the `script` sub-score,
# and (2) to fold such an answer to Traditional before the content checks, so
# that a right answer in the wrong script loses the script point, not both.
# One-to-many cases take the commonest form (发 -> 發), which is good enough for
# matching and never used to produce text.

_S2T_PAIRS = (
    "这這个個们們来來时時为為说說国國过過发發对對会會学學经經现現动動进進种種长長开開问問关關点點业業还還电電实實机機让讓给給没沒见見间間样樣应應无無产產员員体體与與"
    "当當变變东東车車书書话話认認语語请請头頭买買卖賣钱錢门門马馬鸟鳥鱼魚风風飞飛饭飯馆館银銀铁鐵钟鐘错錯颜顏页頁题題须須顾顧预預领領网網络絡线線级級纪紀约約组組终終"
    "结結绝絕统統继繼续續维維编編练練细細红紅纸紙师師岁歲帮幫广廣厂廠张張归歸总總态態忆憶战戰户戶护護报報择擇拥擁挂掛换換据據数數断斷旧舊显顯术術杂雜权權条條极極构構"
    "标標检檢楼樓欢歡气氣汉漢汤湯沟溝济濟满滿灯燈灵靈热熱爱愛状狀独獨环環画畫码碼确確离離积積称稱笔筆简簡类類粮糧艺藝节節药藥获獲营營虽雖补補观觀规規视視觉覺计計订訂"
    "记記设設许許论論证證评評识識词詞试試该該调調谁誰谢謝质質购購资資赛賽输輸运運远遠连連适適选選递遞邮郵郑鄭释釋钥鑰锁鎖销銷键鍵闭閉闻聞阅閱队隊际際险險难難顺順额額"
    "饮飲驾駕验驗齐齊龙龍办辦务務劳勞势勢区區医醫华華单單压壓历歷县縣参參双雙号號听聽响響园園图圖团團场場块塊处處备備复復够夠夹夾奖獎妈媽孙孫宁寧审審宽寬导導尔爾尽盡"
    "层層属屬币幣带帶库庫庆慶异異弹彈强強录錄径徑忧憂怀懷惯慣执執扩擴扫掃抢搶损損摄攝敌敵毕畢汇匯浅淺测測湾灣灭滅烧燒爷爺牵牽犹猶猪豬疗療监監盘盤礼禮稳穩窝窩竞競纳納"
    "纠糾缩縮罗羅职職联聯肤膚脑腦脸臉腾騰舰艦萨薩蓝藍虑慮衬襯袜襪览覽讨討访訪详詳误誤读讀谈談贵貴费費贴貼赶趕跃躍轮輪软軟转轉载載较較辆輛达達迁遷违違遗遺邻鄰针針钢鋼"
    "铜銅链鏈锅鍋镇鎮闲閒闹鬧阳陽阴陰陈陳随隨隐隱雾霧韩韓顿頓频頻饱飽饼餅驶駛骑騎鸡雞麦麥黄黃齿齒账帳优優传傳价價众眾儿兒内內写寫军軍农農决決况況净淨击擊创創则則刚剛"
    "别別剧劇劝勸励勵胜勝协協卫衛却卻厅廳吗嗎呜嗚喷噴围圍坏壞垫墊声聲婴嬰宝寶寻尋将將岛島帐帳庄莊弃棄彻徹恶惡悬懸惊驚戏戲扑撲担擔拟擬拣揀挤擠举舉撑撐晓曉暂暫杀殺杨楊"
    "枪槍柜櫃栏欄桥橋梦夢歼殲毁毀沪滬泪淚洁潔浓濃涨漲渐漸温溫湿濕灾災炼煉烂爛烟煙烦煩牺犧猎獵献獻玛瑪痒癢盐鹽矿礦础礎穷窮窃竊签簽篮籃粤粵紧緊纤纖纯純纷紛绍紹绘繪绕繞"
    "绩績绪緒绿綠缓緩缘緣罚罰翘翹肠腸肿腫胁脅胆膽胶膠脏髒腊臘舆輿艰艱芦蘆苏蘇荣榮莱萊萝蘿蒋蔣虾蝦蚂螞蛮蠻装裝裤褲誉譽讲講讶訝诉訴诊診译譯诗詩诚誠询詢诸諸谅諒谋謀谎謊"
    "谨謹谱譜贝貝财財责責贤賢败敗货貨贫貧贯貫贸貿贺賀赁賃赔賠赖賴赞贊赠贈赵趙趋趨践踐踊踴轨軌轩軒轰轟轻輕辅輔辈輩辉輝辞辭边邊迈邁迟遲逊遜逻邏遥遙邓鄧酱醬鉴鑑钉釘钓釣"
    "钞鈔钦欽钩鉤钻鑽铃鈴铅鉛铝鋁铺鋪锈鏽锋鋒锐銳锦錦锻鍛镜鏡镶鑲闪閃闯闖闷悶闸閘阁閣阔闊陆陸陕陝隶隸雏雛韵韻顶頂项項颁頒颂頌颈頸颗顆飘飄饥飢饰飾饲飼馈饋驱驅驳駁驻駐"
    "骂罵骄驕骗騙骤驟鲁魯鲜鮮鸣鳴鸭鴨鸿鴻鹅鵝鹰鷹龄齡龟龜从從两兩乐樂习習亲親仅僅兴興减減养養么麼圆圓坚堅树樹盖蓋脱脫赏賞赢贏亚亞欧歐猫貓阶階奋奮妇婦壮壯夺奪岗崗扬揚"
    "挥揮摆擺摇搖残殘泽澤润潤滚滾潜潛炉爐狮獅疯瘋聪聰肃肅胀脹苍蒼贡貢辩辯铭銘锡錫颤顫驰馳鹤鶴伟偉侧側债債倾傾党黨凤鳳凭憑厉厲厌厭吓嚇启啟圣聖墙牆尝嘗帅帥废廢弯彎恋戀"
    "惧懼惨慘愤憤懒懶扰擾抚撫拦攔拨撥搅攪携攜晒曬横橫浏瀏灿燦烛燭烫燙畅暢皱皺盗盜碍礙祸禍竖豎纲綱缝縫缠纏罢罷舱艙艳豔荐薦萧蕭蜡蠟袭襲诞誕谊誼谣謠谦謙踪蹤辖轄迹跡酿釀"
    "闽閩阀閥韧韌顽頑饶饒驴驢鲸鯨"
)


def _build_s2t(pairs: str) -> dict[str, str]:
    table: dict[str, str] = {}
    for i in range(0, len(pairs), 2):
        simplified, traditional = pairs[i], pairs[i + 1]
        table[simplified] = traditional
    return table


S2T = _build_s2t(_S2T_PAIRS)
_S2T_ORD = {ord(k): v for k, v in S2T.items()}

# Mainland wording -> Taiwan wording, written in Traditional (the text is folded first).
# Only terms that are plainly not Taiwan usage; words Taiwan also uses (數據, 程序, 窗口,
# 菜單, 質量) are left out on purpose.
MAINLAND_TERMS = {
    "軟件": "軟體", "硬件": "硬體", "視頻": "影片", "網絡": "網路", "信息": "資訊", "鼠標": "滑鼠", "打印": "列印",
    "默認": "預設", "服務器": "伺服器", "文件夾": "資料夾", "博客": "部落格", "短信": "簡訊", "出租車": "計程車",
    "屏幕": "螢幕", "內存": "記憶體", "硬盤": "硬碟", "激光": "雷射", "數碼": "數位", "互聯網": "網際網路",
    "寬帶": "寬頻", "公交車": "公車", "酸奶": "優格", "方便麵": "泡麵", "登錄": "登入", "賬號": "帳號",
    "帳號密碼登錄": "帳號密碼登入", "鏈接": "連結", "在線": "線上", "郵箱": "信箱", "音頻": "音訊", "攝像頭": "鏡頭",
    "充電寶": "行動電源", "U盤": "隨身碟", "界面": "介面", "數據庫": "資料庫", "人工智能": "人工智慧",
    "智能手機": "智慧型手機", "二維碼": "QR Code", "塑料": "塑膠", "圓珠筆": "原子筆", "打車": "叫車",
    "移動支付": "行動支付", "筆記本電腦": "筆記型電腦", "複印件": "影本", "質量好": "品質好",
}
_CJK = re.compile(r"[㐀-鿿]")


def fold_script(text: str) -> str:
    """Simplified characters and mainland terms replaced by the Taiwan forms (for matching only)."""
    out = text.translate(_S2T_ORD)
    for mainland, taiwan in MAINLAND_TERMS.items():
        if mainland in out:
            out = out.replace(mainland, taiwan)
    return out


def script_check(text: str) -> dict[str, Any]:
    """Was this written in Traditional Chinese with Taiwan wording? `ok` is None when there is no Chinese at all."""
    simplified = sorted({ch for ch in text if ch in S2T})
    folded = text.translate(_S2T_ORD)
    terms = sorted(t for t in MAINLAND_TERMS if t in folded)
    has_cjk = bool(_CJK.search(text))
    return {"ok": (not simplified and not terms) if has_cjk else None, "simplified": simplified, "mainland_terms": terms}


# ============================================================ normalising model output

_FENCE = re.compile(r"```[A-Za-z0-9_-]*\s*\n?(.*?)```", re.S)
_PREFIX = re.compile(r"^(?:答案|回答|答|標籤|分類|類別|語言|結果|選項|answer|label|category|language|result|option)\s*(?:[:：]|是|為)\s*", re.I)
_QUOTES = "「」『』\"'“”‘’`*()[]【】《》〈〉"
_TRAIL = "。.!！?？,，;；、:： \t\r\n"


def nfkc(text: str) -> str:
    """Full-width digits, letters and ASCII punctuation become half-width; 「」。、 stay as they are."""
    return unicodedata.normalize("NFKC", text)


def unfence(text: str) -> str:
    match = _FENCE.search(text)
    return match.group(1) if match else text


def clean(text: Any, fold: bool = True) -> str:
    """A short answer reduced to what matters: no fences, label prefix, quotes, trailing punctuation or case."""
    out = nfkc(unfence(str(text))).strip()
    for _ in range(3):
        before = out
        out = _PREFIX.sub("", out.strip(_TRAIL)).strip(_QUOTES + _TRAIL)
        if out == before:
            break
    out = re.sub(r"\s+", " ", out).casefold()
    return fold_script(out) if fold else out


def key(text: Any, fold: bool = True) -> str:
    """`clean` with inner spaces removed too, for comparing short answers (100 號 == 100號)."""
    return clean(text, fold).replace(" ", "")


def squash(text: Any, fold: bool = True) -> str:
    """Text for substring checks: width and case folded, all white space removed."""
    out = re.sub(r"\s+", "", nfkc(str(text))).casefold()
    return fold_script(out) if fold else out


def extract_json(text: str) -> Any:
    """The first JSON object in the output: bare, fenced, or with prose around it. None when there is none."""
    candidates = [text.strip()] + [m.group(1).strip() for m in _FENCE.finditer(text)]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    decoder = json.JSONDecoder()
    for source in (text, text.replace("“", '"').replace("”", '"'), re.sub(r",\s*([}\]])", r"\1", text)):
        for match in re.finditer(r"\{", source):
            try:
                value, _ = decoder.raw_decode(source[match.start():])
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    return None


_NUM = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    flat = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", nfkc(text))
    return [float(m.group(0)) for m in _NUM.finditer(flat)]


def parse_number(value: Any) -> float | None:
    """A JSON number, or a string holding exactly one number (１，２００ 元 -> 1200)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        found = _numbers(value)
        if len(found) == 1:
            return found[0]
    return None


_ISO = re.compile(r"^(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})$")
_YMD_ZH = re.compile(r"^(?:西元)?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?$")
_ROC = re.compile(r"^(?:中華)?(?:民國)?\s*(\d{2,3})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?$")


def parse_date(value: Any, roc_ok: bool = False, loose: bool = False) -> str | None:
    """YYYY-MM-DD from an ISO-like string. 2025/3/14 and full-width digits are accepted.

    A 民國 year (114/03/14, 民國114年3月14日) is NOT accepted unless roc_ok: when an
    item asks for ISO, leaving the year unconverted is the mistake being tested.
    `loose` also accepts 2025年3月14日.
    """
    if not isinstance(value, str):
        return None
    text = nfkc(value).strip().strip(_QUOTES)
    match = _ISO.match(text) or (_YMD_ZH.match(text) if loose else None)
    year_add = 0
    if not match and roc_ok:
        match, year_add = _ROC.match(text), 1911
    if not match:
        return None
    year, month, day = int(match.group(1)) + year_add, int(match.group(2)), int(match.group(3))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


# ============================================================ scoring kinds


def _field_ok(spec: Any, got: Any, fold: bool) -> bool:
    if not (isinstance(spec, dict) and "type" in spec):
        if isinstance(spec, bool):
            spec = {"type": "bool", "value": spec}
        elif isinstance(spec, (int, float)):
            spec = {"type": "number", "value": spec}
        elif isinstance(spec, list):
            spec = {"type": "list", "value": spec}
        elif spec is None:
            spec = {"type": "null"}
        else:
            spec = {"type": "string", "value": spec}
    kind = spec["type"]
    if kind == "string":
        wanted = spec.get("values") or [spec["value"]]
        return isinstance(got, (str, int, float)) and not isinstance(got, bool) and key(got, fold) in {key(w, fold) for w in wanted}
    if kind == "number":
        number = parse_number(got)
        return number is not None and abs(number - float(spec["value"])) <= float(spec.get("tol", 0)) + 1e-9
    if kind == "date":
        return parse_date(got, bool(spec.get("roc_ok")), bool(spec.get("loose"))) == spec["value"]
    if kind == "bool":
        if isinstance(got, str) and got.strip().lower() in ("true", "false"):
            got = got.strip().lower() == "true"
        return isinstance(got, bool) and got is bool(spec["value"])
    if kind == "null":
        return got is None or got == "" or got == [] or (isinstance(got, str) and clean(got) in ("null", "none", "無"))
    if kind == "present":
        return got not in (None, "", [], {})
    if kind == "contains":
        return isinstance(got, str) and all(_entry_in(entry, got, fold) for entry in spec["all"])
    if kind == "list":
        if not isinstance(got, list):
            return False

        def element(v: Any) -> Any:
            number = parse_number(v) if not isinstance(v, str) else None
            return number if number is not None else key(v, fold)

        have, want = [element(v) for v in got], [element(v) for v in spec["value"]]
        return have == want if spec.get("ordered") else (sorted(map(str, have)) == sorted(map(str, want)))
    raise ValueError(f"unknown json_fields type {kind!r}")


FIELD_TYPES = ("string", "number", "date", "bool", "null", "present", "contains", "list")


def _lookup(obj: Mapping[str, Any], name: str) -> tuple[bool, Any]:
    if name in obj:
        return True, obj[name]
    low = {str(k).strip().casefold(): v for k, v in obj.items()}
    return (True, low[name.casefold()]) if name.casefold() in low else (False, None)


def _entry_in(entry: Any, text: str, fold: bool) -> bool:
    """One contains entry: a string, a list of alternatives, or {"re": pattern}."""
    if isinstance(entry, dict):
        haystack = nfkc(text)
        return re.search(entry["re"], fold_script(haystack) if fold else haystack, re.I | re.S) is not None
    alternatives = entry if isinstance(entry, list) else [entry]
    haystack = squash(text, fold)
    return any(squash(alt, fold) in haystack for alt in alternatives)


_CITE = re.compile(r"[\[【]\s*(\d{1,2})\s*[\]】]")
_SOURCE_KEYS = ("sources", "source", "citations", "cites", "來源", "出處")


def cited(text: str) -> set[int]:
    """Passage numbers the answer cites: the `sources` field of a JSON answer, else [n] marks in the text."""
    obj = extract_json(text)
    if isinstance(obj, dict):
        for name in _SOURCE_KEYS:
            found, value = _lookup(obj, name)
            if found:
                values = value if isinstance(value, list) else [value]
                out: set[int] = set()
                for v in values:
                    nums = _numbers(str(v)) if not isinstance(v, bool) else []
                    out.update(int(n) for n in nums if n == int(n))
                return out
    return {int(m.group(1)) for m in _CITE.finditer(nfkc(text))}


_ANSWER_MARK = re.compile(r"(?:答案|答|共|總共|合計|answer|total)\s*(?:[:：]|是|為|=|is)?\s*(?:NT\$|\$)?\s*(-?[\d,]+(?:\.\d+)?)", re.I)


def answer_number(text: str) -> float | None:
    """The one number an answer gives.

    A JSON answer's `answer` field wins. Otherwise the number after the last
    答案/Answer mark, else the last number in the text (models often show work
    first). Chinese numerals (三十) are not read.
    """
    obj = extract_json(text)
    if isinstance(obj, dict):
        for name in ("answer", "答案", "result"):
            found, value = _lookup(obj, name)
            if found:
                return parse_number(value)
    flat = nfkc(unfence(text))
    marks = _ANSWER_MARK.findall(flat)
    if marks:
        found_n = _numbers(marks[-1])
        if found_n:
            return found_n[0]
    numbers = _numbers(flat)
    return numbers[-1] if numbers else None


_BULLET = re.compile(r"^\s*(?:[-*+•‧・●○▪◦–—]|\d{1,2}\s*[.、)）]|[(（]\s*\d{1,2}\s*[)）]|[一二三四五六七八九十]{1,2}\s*[、.])\s*(\S.*)$")


def bullet_lines(text: str) -> list[str]:
    return [m.group(1).strip() for m in (_BULLET.match(line) for line in nfkc(unfence(text)).splitlines()) if m]


def visible_len(text: str) -> int:
    """Characters in the answer, not counting white space at the ends."""
    return len(text.strip())


def script_mode(item: Mapping[str, Any]) -> str:
    mode = (item.get("expect") or {}).get("script")
    if mode in SCRIPT_MODES:
        return mode
    return "soft" if item.get("lang") == "zh-TW" else "ignore"


def score(item: Mapping[str, Any], output: str | None) -> dict[str, Any]:
    """Score one output. An item passes only when every hard check passes; sub-scores are kept.

    `script` (Traditional characters, Taiwan wording) is a hard check only for
    items with expect.script == "hard". Otherwise it is recorded as `script_ok`
    and the content checks are run on the text folded to Traditional.
    """
    expect = item["expect"]
    text = output if isinstance(output, str) else ""
    mode = script_mode(item)
    fold = mode != "hard"
    checks: dict[str, bool] = {"not_empty": bool(text.strip())}
    script = script_check(text) if mode != "ignore" else {"ok": None}
    if not checks["not_empty"]:
        for kind in expect:
            if kind != "script":
                checks[kind] = False
        return {"pass": False, "checks": checks, "script_ok": None}

    obj: Any = None
    if "json_fields" in expect or "json_keys" in expect:
        obj = extract_json(text)
    for kind, spec in expect.items():
        if kind == "script":
            continue
        if kind == "exact":
            checks[kind] = key(text, fold) == key(spec, fold)
        elif kind == "one_of":
            checks[kind] = key(text, fold) in {key(option, fold) for option in spec}
        elif kind == "json_fields":
            ok = isinstance(obj, dict)
            if ok:
                for name, field_spec in spec.items():
                    found, got = _lookup(obj, name)
                    absent_ok = isinstance(field_spec, dict) and field_spec.get("type") == "null" or field_spec is None
                    if not (found or absent_ok) or (found and not _field_ok(field_spec, got, fold)):
                        ok = False
                        break
            checks[kind] = ok
        elif kind == "json_keys":
            checks[kind] = isinstance(obj, dict) and sorted(map(str, obj.keys())) == sorted(spec)
        elif kind == "contains_all":
            checks[kind] = all(_entry_in(entry, text, fold) for entry in spec)
        elif kind == "contains_any":
            checks[kind] = any(_entry_in(entry, text, fold) for entry in spec)
        elif kind == "contains_none":
            checks[kind] = not any(_entry_in(entry, text, fold) for entry in spec)
        elif kind == "cites":
            got_cites = cited(text)
            want = set(spec["passages"])
            checks[kind] = (got_cites >= want and bool(got_cites)) if spec.get("mode", "exact") == "superset" else got_cites == want
        elif kind == "number":
            number_spec = spec if isinstance(spec, dict) else {"value": spec}
            number = answer_number(text)
            checks[kind] = number is not None and abs(number - float(number_spec["value"])) <= float(number_spec.get("tol", 0)) + 1e-9
        elif kind == "max_chars":
            checks[kind] = visible_len(text) <= int(spec)
        elif kind == "bullets":
            bullet_spec = spec if isinstance(spec, dict) else {"count": spec}
            lines = bullet_lines(text)
            low = bullet_spec.get("count", bullet_spec.get("min", 1))
            high = bullet_spec.get("count", bullet_spec.get("max", 10 ** 6))
            ok = low <= len(lines) <= high
            if ok and "max_chars_each" in bullet_spec:
                ok = all(len(line) <= int(bullet_spec["max_chars_each"]) for line in lines)
            checks[kind] = ok
        else:
            raise ValueError(f"{item.get('id')}: unknown scoring kind {kind!r}")
    if mode == "hard":
        checks["script"] = script["ok"] is not False
    return {"pass": all(checks.values()), "checks": checks, "script_ok": script["ok"]}


# ============================================================ the item files


def item_problems(item: Any) -> list[str]:
    """What is wrong with one item, as sentences. Empty when it is well formed."""
    if not isinstance(item, dict):
        return ["not a JSON object"]
    out: list[str] = []
    iid = item.get("id")
    if not isinstance(iid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,40}", iid):
        out.append(f"id {iid!r} must be lower-case letters, digits and hyphens")
    unknown = set(item) - ITEM_KEYS
    if unknown:
        out.append(f"unknown keys {sorted(unknown)}")
    if item.get("lang") not in ("zh-TW", "en"):
        out.append("lang must be zh-TW or en")
    tier = item.get("tier")
    if tier not in TIERS:
        out.append(f"tier {tier!r} is not one of {TIERS}")
    elif item.get("task") not in TASKS[tier]:
        out.append(f"task {item.get('task')!r} is not a {tier} task {TASKS[tier]}")
    if item.get("skill") not in SKILLS:
        out.append(f"skill {item.get('skill')!r} is not one of {SKILLS}")
    has_prompt, has_messages = isinstance(item.get("prompt"), str) and bool(item["prompt"].strip()), "messages" in item
    if has_prompt == has_messages:
        out.append("exactly one of prompt and messages is needed")
    if has_messages:
        messages = item["messages"]
        if not (isinstance(messages, list) and messages and all(
                isinstance(m, dict) and m.get("role") in ("system", "user", "assistant") and isinstance(m.get("content"), str) for m in messages)):
            out.append("messages must be a list of {role, content}")
    if not isinstance(item.get("notes"), str):
        out.append("notes must be a string")
    expect = item.get("expect")
    if not isinstance(expect, dict) or not expect:
        return out + ["expect must be a non-empty object"]
    for kind, spec in expect.items():
        if kind not in KINDS:
            out.append(f"unknown scoring kind {kind!r}")
        elif kind == "script" and spec not in SCRIPT_MODES:
            out.append(f"script must be one of {SCRIPT_MODES}")
        elif kind == "json_fields":
            if not isinstance(spec, dict) or not spec:
                out.append("json_fields must be a non-empty object")
            else:
                for name, field_spec in spec.items():
                    if isinstance(field_spec, dict) and field_spec.get("type") not in FIELD_TYPES:
                        out.append(f"json_fields.{name}: unknown type {field_spec.get('type')!r}")
        elif kind in ("contains_all", "contains_any", "contains_none", "one_of", "json_keys") and not (isinstance(spec, list) and spec):
            out.append(f"{kind} must be a non-empty list")
        elif kind == "cites" and not (isinstance(spec, dict) and isinstance(spec.get("passages"), list)
                                      and spec.get("mode", "exact") in ("exact", "superset")):
            out.append("cites must be {passages: [...], mode: exact|superset}")
        elif kind == "max_chars" and not (isinstance(spec, int) and spec > 0):
            out.append("max_chars must be a positive integer")
    if not any(kind in expect for kind in POSITIVE_KINDS):
        out.append(f"expect needs at least one of {POSITIVE_KINDS}, or junk output could pass")
    return out


def item_text(item: Mapping[str, Any]) -> str:
    if "messages" in item:
        return "\n".join(str(m.get("content", "")) for m in item["messages"])
    return str(item.get("prompt", ""))


def item_messages(item: Mapping[str, Any]) -> list[dict[str, str]]:
    if "messages" in item:
        return [{"role": m["role"], "content": m["content"]} for m in item["messages"]]
    return [{"role": "user", "content": item["prompt"]}]


def load_items(directories: Iterable[Path]) -> list[dict[str, Any]]:
    """Every item in `items*.jsonl` under the directories, light before everyday, zh-TW first. A broken line stops the run."""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory in directories:
        for path in sorted(Path(directory).glob("items*.jsonl")):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except ValueError as exc:
                    raise SystemExit(f"{path}:{number}: not JSON ({exc})")
                problems = item_problems(item)
                if problems:
                    raise SystemExit(f"{path}:{number}: " + "; ".join(problems))
                if item["id"] in seen:
                    raise SystemExit(f"{path}:{number}: id {item['id']} is used twice")
                seen.add(item["id"])
                items.append(item)
    items.sort(key=lambda i: (i["lang"] != "zh-TW", TIERS.index(i["tier"])))  # stable: zh-TW light, zh-TW everyday, then the control
    return items


def set_hash(items: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for item in sorted(items, key=lambda i: str(i["id"])):
        digest.update(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()[:16]


def choose_items(items: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    """--tier, --tasks, --no-control, then --sample K --seed S (stratified by set and tier), then --limit N."""
    out = [i for i in items if (not args.tier or i["tier"] == args.tier)]
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
        out = [i for i in out if i["task"] in wanted]
    if args.no_control:
        out = [i for i in out if i["lang"] == "zh-TW"]
    if args.sample and args.sample < len(out):
        rng = random.Random(args.seed)
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in out:
            groups.setdefault((item["lang"], item["tier"]), []).append(item)
        exact = {name: args.sample * len(group) / len(out) for name, group in groups.items()}
        share = {name: max(1, int(exact[name])) for name in groups}  # every set and tier gets at least one item
        for name in sorted(groups, key=lambda g: (int(exact[g]) - exact[g], g)):  # largest remainder first
            if sum(share.values()) >= args.sample:
                break
            if share[name] < len(groups[name]):
                share[name] += 1
        while sum(share.values()) > args.sample:  # a very small K: take back from the largest groups
            share[max(sorted(share), key=lambda g: share[g])] -= 1
        keep: set[str] = set()
        for name in sorted(groups):
            keep.update(i["id"] for i in rng.sample(groups[name], min(share[name], len(groups[name]))))
        out = [i for i in out if i["id"] in keep]
    if args.limit:
        out = out[:args.limit]
    return out


# ============================================================ summaries


def _rate(passed: int, scored: int) -> float | None:
    return round(passed / scored, 4) if scored else None


def _group(results: Mapping[str, Mapping[str, Any]], items: Mapping[str, Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    scored: Counter[str] = Counter()
    passed: Counter[str] = Counter()
    for iid, result in results.items():
        if result.get("state") in ("pass", "fail") and iid in items:
            scored[items[iid][key]] += 1
            passed[items[iid][key]] += result["state"] == "pass"
    return {name: {"scored": scored[name], "passed": passed[name], "rate": _rate(passed[name], scored[name])} for name in sorted(scored)}


def summarise(results: Mapping[str, Mapping[str, Any]], items: Mapping[str, Mapping[str, Any]], planned: int | None = None) -> dict[str, Any]:
    """Pass rates for one target. Only answered items (pass or fail) are in a rate; deferred and errors are counted beside it."""
    out: dict[str, Any] = {}
    for label, lang in (("zh_tw", "zh-TW"), ("en_control", "en")):
        subset = {iid: r for iid, r in results.items() if iid in items and items[iid]["lang"] == lang}
        scored = [r for r in subset.values() if r.get("state") in ("pass", "fail")]
        passed = sum(r["state"] == "pass" for r in scored)
        out[label] = {
            "scored": len(scored), "passed": passed, "rate": _rate(passed, len(scored)),
            "by_tier": _group(subset, items, "tier"), "by_task": _group(subset, items, "task"), "by_skill": _group(subset, items, "skill"),
        }
    zh, en = out["zh_tw"]["rate"], out["en_control"]["rate"]
    out["zh_tw_gap"] = round(en - zh, 4) if zh is not None and en is not None else None
    script = [r["script_ok"] for iid, r in results.items()
              if r.get("state") in ("pass", "fail") and r.get("script_ok") is not None and iid in items and items[iid]["lang"] == "zh-TW"]
    out["script"] = {"checked": len(script), "ok": sum(script), "rate": _rate(sum(script), len(script))}
    states = Counter(r.get("state") for r in results.values())
    out["deferred"], out["errors"] = states["deferred"], states["error"]
    out["not_run"] = max(0, planned - len(results)) if planned is not None else 0
    out["empty_reasoning"] = sum(1 for r in results.values() if r.get("empty_reasoning"))
    latencies = [r["latency_ms"] for r in results.values() if r.get("state") in ("pass", "fail") and isinstance(r.get("latency_ms"), (int, float))]
    out["mean_latency_ms"] = round(sum(latencies) / len(latencies)) if latencies else None
    out["cost_usd"] = round(sum(float(r.get("cost_usd") or 0) for r in results.values()), 6)
    answered: Counter[str] = Counter()
    for r in results.values():
        if r.get("state") in ("pass", "fail") and (r.get("deployment") or r.get("provider")):
            answered[str(r.get("deployment") or r.get("provider"))] += 1
    out["answered_by"] = dict(sorted(answered.items()))
    return out


def _pct(rate: float | None) -> str:
    return "  -  " if rate is None else f"{rate * 100:4.0f}%"


def render_table(report: Mapping[str, Any]) -> str:
    rows = [("target", "kind", "cost", "zh-TW", "light", "every.", "en", "script", "empty", "defer", "error", "ms", "US$")]
    for name, target in report["targets"].items():
        s = target["summary"]
        tiers = s["zh_tw"]["by_tier"]
        rows.append((
            name, target.get("kind", "-"), target.get("status", "-"),
            f"{_pct(s['zh_tw']['rate'])} ({s['zh_tw']['passed']}/{s['zh_tw']['scored']})",
            _pct((tiers.get("light") or {}).get("rate")), _pct((tiers.get("everyday") or {}).get("rate")),
            _pct(s["en_control"]["rate"]), _pct(s["script"]["rate"]),
            str(s["empty_reasoning"]), str(s["deferred"]), str(s["errors"]),
            "-" if s["mean_latency_ms"] is None else str(s["mean_latency_ms"]), f"{s['cost_usd']:.4f}",
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(lines)


MIN_FOR_SUGGEST = 20


def suggest(report: Mapping[str, Any], routing_path: Path | None = None) -> str:
    """Which free target did best per tier on zh-TW, and the lines one might change. Prints only; edits nothing."""
    lines = ["Suggestion from this report (zh-TW items, free targets only). Nothing is edited."]
    current: dict[str, str] = {}
    try:
        import yaml  # PyYAML, already a dependency of the CLI

        loaded = yaml.safe_load((routing_path or ROOT / "config" / "routing.yaml").read_text(encoding="utf-8")) or {}
        current = {name: str(spec.get("alias")) for name, spec in (loaded.get("tasks") or {}).items() if isinstance(spec, dict)}
    except Exception:
        pass
    free = {name: t for name, t in report["targets"].items() if t.get("status") in ("free", "trial")}
    if not free:
        return "\n".join(lines + ["  No free target has results in this report."])
    edits: list[str] = []
    for tier in TIERS:
        ranked = []
        for name, target in free.items():
            cell = (target["summary"]["zh_tw"]["by_tier"].get(tier) or {})
            if cell.get("scored"):
                s = target["summary"]
                ranked.append((-cell["rate"], -(s["script"]["rate"] or 0), s["mean_latency_ms"] or 10 ** 9, name, cell))
        ranked.sort()
        lines.append(f"  {tier}:")
        if not ranked:
            lines.append("    no answered items")
            continue
        for _, _, _, name, cell in ranked:
            few = "" if cell["scored"] >= MIN_FOR_SUGGEST else f"   (only {cell['scored']} items: too few to act on)"
            lines.append(f"    {name:<34} {free[name].get('kind', ''):<10} {_pct(cell['rate'])} ({cell['passed']}/{cell['scored']}){few}")
        aliases = [(row[3], row[4]) for row in ranked if free[row[3]].get("kind") == "alias" and row[3] != "auto"]
        deployments = [(row[3], row[4]) for row in ranked if free[row[3]].get("kind") == "deployment"]
        if aliases:
            best, cell = aliases[0]
            now = current.get(tier)
            if cell["scored"] < MIN_FOR_SUGGEST:
                lines.append(f"    best alias: {best}, but on too few items; run the full set before changing anything")
            elif now == best:
                lines.append(f"    best alias: {best} (already the alias for {tier} in config/routing.yaml)")
            else:
                lines.append(f"    best alias: {best}")
                edits += [f"  {tier}:", f"    alias: {best}" + (f"        # now: {now}" if now else "")]
        if deployments:
            best_dep, cell = deployments[0]
            lines.append(f"    best single model: {best_dep} {_pct(cell['rate'])}. routing.yaml names aliases, not models; to prefer this model, "
                         f"give it a lower `order:` than the others in its alias in config/providers.yaml.")
    if edits:
        lines += ["", "  Lines one might change in config/routing.yaml (check with `muse rules check`):", "  tasks:"] + ["  " + e for e in edits]
    else:
        lines += ["", "  No change to config/routing.yaml follows from this report."]
    lines.append("  A difference of a few points on a set this small is noise; see evals/zh-tw/README.md.")
    return "\n".join(lines)


# ============================================================ replay (no network)


def load_replay(path: Path) -> dict[str, dict[str, str]]:
    """Recorded outputs: JSON lines {"id", "output", "target"?}, or a report written with --keep-outputs."""
    text = path.read_text(encoding="utf-8")
    out: dict[str, dict[str, str]] = {}
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, dict) and isinstance(whole.get("targets"), dict):
        for name, target in whole["targets"].items():
            for iid, result in (target.get("items") or {}).items():
                if isinstance(result, dict) and isinstance(result.get("output"), str):
                    out.setdefault(name, {})[iid] = result["output"]
        return out
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise SystemExit(f"{path}:{number}: not JSON ({exc})")
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise SystemExit(f"{path}:{number}: each line needs an id")
        out.setdefault(str(row.get("target") or "replay"), {})[row["id"]] = "" if row.get("output") is None else str(row["output"])
    return out


def replay(path: Path, items: list[dict[str, Any]], keep_outputs: bool) -> dict[str, Any]:
    by_id = {i["id"]: i for i in items}
    report = new_report(items, proxy=None, auth="none (replay)")
    report["replay"] = path.name
    for name, outputs in load_replay(path).items():
        results: dict[str, Any] = {}
        for iid, output in outputs.items():
            if iid not in by_id:
                continue
            scored = score(by_id[iid], output)
            results[iid] = {"state": "pass" if scored["pass"] else "fail", "checks": scored["checks"], "script_ok": scored["script_ok"]}
            if keep_outputs:
                results[iid]["output"] = output
        report["targets"][name] = {"kind": "replay", "status": "-", "items": results, "summary": summarise(results, by_id, len(items))}
    return report


# ============================================================ the proxy


class Proxy:
    """The few HTTP calls this script makes. The bearer token is held here and never printed or stored."""

    def __init__(self, url: str, bearer: str | None) -> None:
        self.url = url.rstrip("/")
        self._bearer = bearer
        self.requests = 0
        host = urllib.parse.urlsplit(self.url).hostname or ""
        # The proxy is on this machine: never send its traffic through an HTTP(S)_PROXY from the environment.
        handlers = [urllib.request.ProxyHandler({})] if host in ("127.0.0.1", "localhost", "::1") else []
        self._opener = urllib.request.build_opener(*handlers)

    def _open(self, request: urllib.request.Request, timeout: float) -> tuple[int, dict[str, str], bytes]:
        if self._bearer:
            request.add_header("Authorization", f"Bearer {self._bearer}")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
        except urllib.error.HTTPError as err:
            return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()
        except (urllib.error.URLError, OSError, ValueError):
            return 0, {}, b""

    def models(self) -> list[str] | None:
        status, _, raw = self._open(urllib.request.Request(f"{self.url}/v1/models"), 10)
        if status != 200:
            return None
        try:
            return [str(m["id"]) for m in json.loads(raw)["data"]]
        except (ValueError, KeyError, TypeError):
            return None

    def chat(self, body: Mapping[str, Any], headers: Mapping[str, str]) -> tuple[int, dict[str, str], bytes]:
        request = urllib.request.Request(f"{self.url}/v1/chat/completions", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json", **headers}, method="POST")
        self.requests += 1
        return self._open(request, TIMEOUT)


def content_of(raw: bytes) -> str:
    try:
        message = json.loads(raw)["choices"][0]["message"]
        content = message.get("content")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return ""
    if isinstance(content, list):  # content parts
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def local_view(env: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """What each model name can reach, from the catalogue and the keys that are set: tier, providers, output floor.

    This is the same rendering the proxy starts from (scripts/render_config.py),
    so it is the proxy's own picture of free and paid. Empty when it cannot be
    built; every target is then `unknown` and treated like a paid one.
    """
    try:
        import render_config

        from muse import paths

        catalog = render_config.load_catalog(Path(env.get("MUSE_CATALOG", "") or paths.CATALOG_PATH))
        config, _ = render_config.render(catalog, env)
    except (SystemExit, Exception):
        return {}
    alias_meta = catalog.get("aliases") or {}
    view: dict[str, dict[str, Any]] = {}
    for entry in config.get("model_list") or []:
        name, info = str(entry["model_name"]), entry.get("model_info") or {}
        slot = view.setdefault(name, {"kind": "alias" if name in alias_meta else "deployment", "mode": str(info.get("mode", "chat")),
                                      "tiers": set(), "providers": set(), "priced": False, "floor": 0, "unpaid": set()})
        slot["tiers"].add(str(info.get("muse_tier", "unknown")))
        if str(info.get("muse_tier")) not in PAID_TIERS:  # what is left when the proxy's paid gate removes paid and metered tiers
            slot["unpaid"].add("priced" if "muse_price_in" in info else str(info.get("muse_tier", "unknown")))
        slot["providers"].add(str(info.get("provider", "?")))
        slot["priced"] = slot["priced"] or "muse_price_in" in info
        slot["floor"] = max(slot["floor"], int(info.get("muse_min_output_tokens") or 0))
    for name, slot in view.items():  # an alias can fall back to other aliases: count what they reach too
        if slot["kind"] != "alias":
            continue
        reach, todo = {name}, list((alias_meta.get(name) or {}).get("fallbacks") or [])
        while todo:
            other = todo.pop()
            if other in reach or other not in view:
                continue
            reach.add(other)
            todo.extend((alias_meta.get(other) or {}).get("fallbacks") or [])
        slot["reach"] = sorted(reach - {name})
    for name, slot in view.items():
        tiers, providers, priced, floor = set(slot["tiers"]), set(slot["providers"]), slot["priced"], slot["floor"]
        unpaid = set(slot["unpaid"])
        for other in slot.get("reach", []):
            tiers |= view[other]["tiers"]
            unpaid |= view[other]["unpaid"]
            providers |= view[other]["providers"]
            priced = priced or view[other]["priced"]
            floor = max(floor, view[other]["floor"])
        slot["status"] = status_of(tiers, priced)
        # For an app that may not pay, the proxy removes paid and metered deployments before routing.
        slot["status_no_paid"] = status_of(unpaid - {"priced"}, "priced" in unpaid) if unpaid else "paid"
        slot["all_providers"], slot["all_floor"] = sorted(providers), floor
    return view


def status_of(tiers: set[str], priced: bool) -> str:
    """free: only free or local tiers. trial: also a trial allowance. paid: can reach a paid or metered provider."""
    if priced or tiers & PAID_TIERS:
        return "paid"
    if not tiers or tiers - FREE_TIERS - {"trial"}:
        return "unknown"
    return "trial" if "trial" in tiers else "free"


def pick_targets(model_ids: list[str], view: Mapping[str, Mapping[str, Any]], args: argparse.Namespace, no_paid: bool = False) -> list[dict[str, Any]]:
    """Chat aliases the proxy lists; with --deployments also each pinned chat deployment; `auto` only when named.

    `no_paid`: the key in use belongs to an app that may not pay, so the proxy
    removes paid deployments itself and an alias is judged by what is left.
    """
    only = [n.strip() for n in (args.only or "").split(",") if n.strip()]
    listed = list(dict.fromkeys(model_ids))
    targets: list[dict[str, Any]] = []

    def describe(name: str) -> dict[str, Any]:
        slot = view.get(name)
        if not slot:
            return {"name": name, "kind": "unknown", "status": "unknown", "providers": [], "floor": 0}
        return {"name": name, "kind": slot["kind"], "status": slot["status_no_paid" if no_paid else "status"],
                "providers": slot["all_providers"], "floor": slot["all_floor"]}

    for name in listed:
        slot = view.get(name)
        if slot is None:
            if name in only:
                targets.append(describe(name))
            continue
        if slot["mode"] != "chat":
            continue
        if slot["kind"] == "alias" or args.deployments or name in only:
            targets.append(describe(name))
    if "auto" in only:
        chat_aliases = [t for t in map(describe, listed) if t["kind"] == "alias" and view[t["name"]]["mode"] == "chat"]
        statuses = {t["status"] for t in chat_aliases}
        status = next((s for s in ("unknown", "paid", "trial", "free") if s in statuses), "unknown")
        targets.append({"name": "auto", "kind": "alias", "status": status, "providers": sorted({p for t in chat_aliases for p in t["providers"]}),
                        "floor": max([t["floor"] for t in chat_aliases] or [0])})
    if only:
        targets = [t for t in targets if t["name"] in only]
    targets.sort(key=lambda t: (t["kind"] != "alias", t["name"]))
    return targets


def tokens_bound(item: Mapping[str, Any], floor: int) -> int:
    """Generous: two tokens per character of input, plus the output cap (or the model's output floor when larger)."""
    return 2 * len(item_text(item)) + 16 + max(MAX_TOKENS[item["task"]], floor)


def retry_after(headers: Mapping[str, str]) -> float | None:
    try:
        return max(0.0, float(str(headers.get("retry-after", "")).strip()))
    except ValueError:
        return None


def safe(value: Any) -> str | None:
    """A routing header value, kept only when it is a plain identifier."""
    text = str(value or "").strip()
    return text if text and SAFE_HEADER.match(text) else None


def run_item(proxy: Proxy, target: Mapping[str, Any], item: Mapping[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    """One item on one target. A 429 is retried once after its Retry-After when that is within --max-wait; else `deferred`."""
    headers = {"x-muse-privacy": args.privacy}
    if target["name"] == "auto":
        headers["x-muse-task"] = item["tier"]
    if target["status"] in ("paid", "unknown"):
        headers["x-muse-allow-paid"] = "true"
    body = {"model": target["name"], "messages": item_messages(item), "temperature": 0, "max_tokens": MAX_TOKENS[item["task"]]}
    waited = 0.0
    for attempt in (1, 2):
        started = time.time()
        status, response_headers, raw = proxy.chat(body, headers)
        latency = round((time.time() - started) * 1000)
        if status != 429:
            break
        wait = retry_after(response_headers)
        ration = str(response_headers.get("x-muse-ration", ""))
        if attempt == 2 or wait is None or wait > args.max_wait or ration.startswith("refuse"):
            return {"state": "deferred", "http": 429, "retry_after_s": wait, "waited_s": round(waited, 1),
                    "why": safe(ration) or safe(response_headers.get("x-muse-wait")) or "provider"}
        _sleep(wait + 0.5)
        waited += wait + 0.5
    result: dict[str, Any] = {"http": status, "latency_ms": latency}
    for key, header in (("provider", "x-muse-provider"), ("deployment", "x-muse-deployment"), ("min_output", "x-muse-min-output"), ("task", "x-muse-task")):
        value = safe(response_headers.get(header))
        if value:
            result[key] = value
    try:
        cost = float(response_headers.get("x-muse-cost-usd", ""))
        if cost > 0:
            result["cost_usd"] = cost
    except ValueError:
        pass
    if waited:
        result["waited_s"] = round(waited, 1)
    if status != 200:
        result["state"] = "error"
        return result
    output = content_of(raw)
    if str(response_headers.get("x-muse-answer", "")).strip() == "empty-reasoning":
        result["empty_reasoning"] = True
    scored = score(item, output)
    result.update(state="pass" if scored["pass"] else "fail", checks=scored["checks"], script_ok=scored["script_ok"])
    if args.keep_outputs:
        result["output"] = output
    return result


def new_report(items: list[dict[str, Any]], proxy: str | None, auth: str) -> dict[str, Any]:
    try:
        from muse import __version__ as version
    except Exception:
        version = "unknown"
    counts = Counter((i["lang"], i["tier"]) for i in items)
    return {
        "schema": 1, "tool": "scripts/eval_models.py", "muse_version": version,
        "started": datetime.now(timezone.utc).isoformat(timespec="seconds"), "finished": None,
        "proxy": proxy, "auth": auth,
        "set": {"hash": set_hash(items), "items": len(items), "ids": [i["id"] for i in items],
                "counts": {f"{lang}/{tier}": n for (lang, tier), n in sorted(counts.items())}},
        "notes": "No key is stored. Outputs are stored only with --keep-outputs. Scores are rule-based; no judge model.",
        "requests_sent": 0, "targets": {},
    }


def write_report(report: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def find_auth(env: Mapping[str, str]) -> tuple[str | None, str, bool]:
    """(bearer, a label that is safe to print, whether this key's app may NOT pay).

    The eval app token when MUSE_EVAL_APP_TOKEN is set, else the master key. The
    master key is the owner, who may pay; an app token is looked up in the apps
    file (by hash) to see whether its app may.
    """
    token = str(env.get("MUSE_EVAL_APP_TOKEN", "")).strip()
    if token:
        try:
            from muse import apps

            app = apps.find_by_token(token, env)
        except Exception:
            app = None
        if app is None:
            return token, "app token from MUSE_EVAL_APP_TOKEN (app not found in the apps file)", False
        pays = "may pay" if app.allow_paid else "may not pay"
        return token, f"app token from MUSE_EVAL_APP_TOKEN (app '{app.name}', {app.priority}, {pays})", not app.allow_paid
    master = str(env.get("LITELLM_MASTER_KEY", "")).strip()
    if master:
        return master, "master key (LITELLM_MASTER_KEY)", False
    return None, "no key found", False


def merged_env(environ: Mapping[str, str] | None) -> dict[str, str]:
    """Environment, .env and key store merged the way scripts/real_key_check.py does it (muse.keys.export_env)."""
    try:
        from muse import keys

        return dict(keys.export_env(environ))
    except Exception:
        return dict(os.environ if environ is None else environ)


def run(args: argparse.Namespace, env: Mapping[str, str], items: list[dict[str, Any]]) -> int:
    bearer, auth_label, no_paid = find_auth(env)
    proxy = Proxy(args.proxy_url, bearer)
    report_path = Path(args.report)
    by_id = {i["id"]: i for i in items}

    model_ids = proxy.models()  # a listing, not a model call: nothing reaches a provider
    if model_ids is None:
        print(f"Could not list models at {proxy.url}/v1/models ({auth_label}). Run `muse up`, and check the key.", file=sys.stderr)
        return 2
    view = local_view(env)
    targets = pick_targets(model_ids, view, args, no_paid)
    if not targets:
        print("No chat target to evaluate. The proxy lists: " + (", ".join(model_ids) or "nothing"), file=sys.stderr)
        return 2
    included = [t for t in targets if t["status"] in ("free", "trial") or args.allow_paid]
    skipped = [t for t in targets if t not in included]

    report = new_report(items, proxy.url, auth_label)
    if args.resume:
        if not report_path.exists():
            print(f"--resume: there is no report at {report_path}.", file=sys.stderr)
            return 2
        old = json.loads(report_path.read_text(encoding="utf-8"))
        if (old.get("set") or {}).get("hash") != report["set"]["hash"]:
            print("--resume: the report was made with a different set of items (or other --tier/--sample/--limit). "
                  "Use the same options, or start a new report.", file=sys.stderr)
            return 2
        report["started"], report["targets"] = old.get("started", report["started"]), old.get("targets") or {}
        report["requests_sent"] = int(old.get("requests_sent") or 0)

    def todo(target: Mapping[str, Any]) -> list[dict[str, Any]]:
        done = (report["targets"].get(target["name"]) or {}).get("items") or {}
        return [i for i in items if (done.get(i["id"]) or {}).get("state") not in ("pass", "fail")]

    work = {t["name"]: todo(t) for t in included}
    total = sum(len(v) for v in work.values())
    tokens = sum(tokens_bound(i, t["floor"]) for t in included for i in work[t["name"]])
    counts = Counter((i["lang"], i["tier"]) for i in items)
    print("Muse model evaluation: what this will send")
    print(f"  proxy: {proxy.url}")
    print(f"  key:   {auth_label}")
    print(f"  items: {len(items)} (" + ", ".join(f"{lang} {tier} {n}" for (lang, tier), n in sorted(counts.items())) + ")")
    for t in targets:
        reach = ", ".join(t["providers"]) or "not in the local catalogue"
        if t in included:
            n = len(work[t["name"]])
            line = f"at most {n} requests, {sum(tokens_bound(i, t['floor']) for i in work[t['name']])} tokens"
            if args.resume and n < len(items):
                line += f" ({len(items) - n} already answered)"
        else:
            line = "LEFT OUT: it can reach a paid provider (or is unknown); see docs/evals.md, or pass --allow-paid"
        print(f"  {t['name']:<34} {t['kind']:<10} {t['status']:<7} {line}  [{reach}]")
    print(f"  at most {len(included)} targets x {len(items)} items = {total} requests in all, and at most {tokens} tokens"
          f" (2 tokens per input character plus the output cap, a high estimate).")
    print("  A request answered 429 is sent once more after its Retry-After, so at most "
          f"{2 * total} HTTP calls. One at a time, {args.delay:g} s apart, temperature 0.")
    if no_paid:
        print("  This app may not pay: the proxy removes paid providers from an alias before routing, so aliases are judged by their free members.")
    if any(t["status"] == "trial" for t in included):
        print("  trial: this target can use a provider's trial allowance, which may not renew.")
    if any(t["status"] in ("paid", "unknown") for t in included):
        print("  !! --allow-paid: the paid targets above are sent with x-muse-allow-paid: true and CAN COST MONEY (the daily paid cap still applies).")
    else:
        print("  Free targets only: this should cost nothing.")
    print(f"  report: {report_path}" + ("  (with model outputs)" if args.keep_outputs else "  (ids and scores; no model outputs)"))
    if total == 0:
        print("Nothing to send.")
        return finish(report, report_path, by_id, proxy, args, items)
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not a terminal: pass --yes to confirm.", file=sys.stderr)
            return 2
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was sent.")
            return 0
    print()

    for t in included:
        entry = report["targets"].setdefault(t["name"], {"items": {}})
        entry.update(kind=t["kind"], status=t["status"], providers=t["providers"])
        entry.pop("stopped", None)
        results, streak = entry["items"], 0
        pending = work[t["name"]]
        for index, item in enumerate(pending, 1):
            result = run_item(proxy, t, item, args)
            results[item["id"]] = result
            unanswered = result["state"] == "deferred" or (result["state"] == "error" and result.get("http") == 0)
            streak = streak + 1 if unanswered else 0
            if not args.quiet:
                print(f"  {t['name']:<24} {index:>3}/{len(pending)}  {item['id']:<22} {result['state']}")
            if streak >= args.stop_after:
                entry["stopped"] = f"{streak} items in a row deferred or unanswered"
                print(f"  {t['name']}: stopped after {streak} items in a row were deferred or got no answer; --resume continues it later.")
                break
            if index % 10 == 0:
                report["requests_sent"] += proxy.requests
                proxy.requests = 0
                entry["summary"] = summarise(results, by_id, len(items))
                write_report(report, report_path)
            if args.delay > 0 and index < len(pending):
                _sleep(args.delay)
        entry["summary"] = summarise(results, by_id, len(items))
        write_report(report, report_path)
    for t in skipped:
        report.setdefault("left_out", {})[t["name"]] = {"kind": t["kind"], "status": t["status"]}
    return finish(report, report_path, by_id, proxy, args, items)


def finish(report: dict[str, Any], path: Path, by_id: Mapping[str, Mapping[str, Any]], proxy: Proxy | None,
           args: argparse.Namespace, items: list[dict[str, Any]]) -> int:
    for target in report["targets"].values():
        target["summary"] = summarise(target.get("items") or {}, by_id, len(items))
    if proxy is not None:
        report["requests_sent"] += proxy.requests
        proxy.requests = 0
    report["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    write_report(report, path)
    print()
    print(render_table(report))
    print()
    if args.suggest:
        print(suggest(report))
        print()
    print(f"{report['requests_sent']} requests sent. Report: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rank the models a local Muse proxy reaches on the zh-TW test set (rule-based scoring, no judge model).")
    parser.add_argument("--proxy-url", default=None, help="default: MUSE_PROXY_URL or http://127.0.0.1:4000")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--report", metavar="PATH", help="default: $MUSE_HOME/eval-report.json")
    parser.add_argument("--deployments", action="store_true", help="also evaluate each pinned chat deployment, to rank models one by one")
    parser.add_argument("--only", metavar="NAMES", help="comma-separated targets (aliases, deployment ids, or `auto` to route by task tier)")
    parser.add_argument("--allow-paid", action="store_true", help="include targets that can reach a paid provider (sends x-muse-allow-paid: true for those)")
    parser.add_argument("--tier", choices=TIERS, help="only items of this tier")
    parser.add_argument("--tasks", metavar="NAMES", help="comma-separated task categories")
    parser.add_argument("--no-control", action="store_true", help="leave out the English control items")
    parser.add_argument("--sample", type=int, metavar="K", help="a random K items, spread over the sets and tiers")
    parser.add_argument("--seed", type=int, default=0, help="seed for --sample (default 0)")
    parser.add_argument("--limit", type=int, metavar="N", help="only the first N items after the other filters")
    parser.add_argument("--resume", action="store_true", help="continue the report at --report: only items not yet answered are sent")
    parser.add_argument("--keep-outputs", action="store_true", help="store model outputs in the report (default: ids and scores only)")
    parser.add_argument("--suggest", action="store_true", help="print the best free target per tier and the routing.yaml lines one might change")
    parser.add_argument("--replay", metavar="FILE", help="score recorded outputs offline; no network")
    parser.add_argument("--max-wait", type=float, default=DEFAULT_MAX_WAIT, help=f"longest Retry-After to wait for, in seconds (default {DEFAULT_MAX_WAIT:g})")
    parser.add_argument("--stop-after", type=int, default=DEFAULT_STOP_AFTER, help=f"stop a target after this many deferrals in a row (default {DEFAULT_STOP_AFTER})")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"seconds between requests (default {DEFAULT_DELAY:g})")
    parser.add_argument("--privacy", default="public", choices=("public", "standard"), help="x-muse-privacy to send; the prompts are public test text (default public)")
    parser.add_argument("--quiet", action="store_true", help="no line per item")
    parser.add_argument("--items-dir", action="append", metavar="DIR", help="read items from here instead of evals/zh-tw and evals/en-control")
    return parser


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    directories = [Path(d) for d in args.items_dir] if args.items_dir else [ZH_DIR, EN_DIR]
    items = choose_items(load_items(directories), args)
    if not items:
        print("No item matches these options.", file=sys.stderr)
        return 2
    if args.replay:
        report = replay(Path(args.replay), items, args.keep_outputs)
        if not report["targets"]:
            print("The replay file holds no output for these items.", file=sys.stderr)
            return 2
        report["finished"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if args.report:
            write_report(report, Path(args.report))
        print(render_table(report))
        if args.suggest:
            print()
            print(suggest(report))
        return 0
    for extra in (ROOT, ROOT / "scripts"):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    env = merged_env(environ)
    args.proxy_url = args.proxy_url or env.get("MUSE_PROXY_URL", "").strip() or f"http://127.0.0.1:{env.get('MUSE_PORT', '').strip() or '4000'}"
    if not args.report:
        try:
            from muse import paths

            args.report = str(paths.home(env) / "eval-report.json")
        except Exception:
            args.report = "eval-report.json"
    return run(args, env, items)


if __name__ == "__main__":
    raise SystemExit(main())
