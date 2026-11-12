"""Deterministic guardrails for the health bot. No model, no medical knowledge base.

Why deterministic: the model may be small, local and easy to steer with text
inside a record. Escalating an emergency, declining medical advice and
withholding an unsafe answer must not depend on it.

Three jobs:
- `screen_user_text`: the user's OWN words (never pasted or retrieved record
  text) -> "urgent", "declined_medical_advice" or "ok". Urgent favours recall:
  a needless emergency banner costs a sentence; a missed one can cost a life.
  Declining favours precision: "what does eGFR mean?" must be answered.
- `screen_model_text`: post-filter on a model answer. Any flag means the bot
  withholds the WHOLE answer and shows a fixed fallback (see bot.py for why it
  does not edit the answer instead).
- `wrap_untrusted`: delimit record / pasted text as data for the prompt.

Flags and reasons are category codes only, never matched text, so they can be
put in a receipt or a log.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Crisis resources (Taiwan)
# --------------------------------------------------------------------------
# Checked 2026-11-12 by web search:
# - 1925 安心專線: 衛生福利部, 24 hours, free of charge; replaced 0800-788-995
#   (single number since 2019-12-20). Source: https://mohw.gov.tw/cp-16-48244-1.html
# - 1995 生命線 and 1980 張老師: listed together with 1925 as suicide-prevention
#   resources in 2025 reporting (health.tvbs.com.tw). Their current service
#   hours were NOT confirmed from an official page: shown without hours.
# - 119: fire and ambulance (emergency medical services). 110: police.
# Re-check these before every pilot release; the tenant can override them.
CRISIS_LINES: dict[str, tuple[tuple[str, str], ...]] = {
    "zh-TW": (
        ("119", "緊急救護（有立即危險時）"),
        ("1925", "安心專線，24 小時免付費"),
        ("1995", "生命線"),
        ("1980", "張老師"),
    ),
    "en": (
        ("119", "ambulance, if you are in immediate danger"),
        ("1925", "Taiwan MOHW Mental Health Hotline, 24 hours, free"),
        ("1995", "Lifeline Taiwan"),
        ("1980", "Teacher Chang counselling line"),
    ),
}
CRISIS_NUMBERS: tuple[str, ...] = ("119", "110", "1925", "1995", "1980")


@dataclass(frozen=True)
class SafetyVerdict:
    level: str                       # "ok" | "urgent" | "declined_medical_advice"
    reasons: tuple[str, ...] = ()    # category codes, e.g. ("chest_pain",)

    @property
    def crisis(self) -> bool:
        """Self-harm: the reply adds crisis lines and stays short and warm."""
        return "self_harm" in self.reasons


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_CLAUSE_BREAK = re.compile(r"[,.;:!?，。；：！？\n]|\bbut\b|\bhowever\b|但是|可是|不過|但")

# A negation right before the term in the same clause ("no chest pain",
# "denies chest pain", "沒有胸痛"). Allows up to two words in between
# ("no real chest pain") but not more ("not sure if this is chest pain").
_NEG_EN = re.compile(
    r"\b(?:no|not|without|denies|denied|deny|never|none|free of|negative for|"
    r"don'?t|do not|doesn'?t|does not|didn'?t|did not|haven'?t|have not|hasn'?t|isn'?t|aren'?t|wasn'?t)"
    r"(?:\s+(?:have|has|had|having|any|a|an|real|really|much|feel|feeling|got|get|experience|experiencing))?"
    r"(?:\s+\S+){0,1}\s*$"
)
_NEG_ZH = re.compile(r"(?:沒有|沒|無|不會|未|否認|並無|並沒有|沒什麼)\S{0,2}$")
# Question forms that contain a negation character but are not negations.
_ZH_QUESTION_FORMS = re.compile(r"有沒有|是不是|會不會|是否")


def detect_locale(text: str, default: str = "zh-TW") -> str:
    """zh-TW if the text has CJK, en if it has Latin letters and no CJK, else default."""
    if _CJK.search(text or ""):
        return "zh-TW"
    if re.search(r"[A-Za-z]{2,}", text or ""):
        return "en"
    return default


def _negated(text: str, start: int) -> bool:
    prefix = text[:start]
    breaks = list(_CLAUSE_BREAK.finditer(prefix))
    if breaks:
        prefix = prefix[breaks[-1].end():]
    prefix = prefix[-40:]
    if _NEG_EN.search(prefix.lower()):
        return True
    zh = _ZH_QUESTION_FORMS.sub("？", prefix[-8:])
    return bool(_NEG_ZH.search(zh))


def _rx(*parts: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in parts), re.IGNORECASE)


# --------------------------------------------------------------------------
# Emergencies / red flags
# --------------------------------------------------------------------------
# (category, pattern, negatable). Self-harm is not negatable on purpose: a
# person may play it down ("我沒有想死啦，只是..."); a gentle crisis line is
# the right answer either way.
_URGENT: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("chest_pain", _rx(
        r"\bchest\s+(?:pain|pressure|tightness|hurts?|is\s+tight)",
        r"\bpain\s+in\s+(?:my\s+|the\s+)?chest", r"\bcrushing\b.{0,20}\bchest",
        r"\bheart\s+attack\b",
        r"胸痛", r"胸口(?:很|好|會)?(?:痛|悶|緊|壓)", r"胸悶", r"心臟病發", r"心肌梗塞發作",
    ), True),
    ("breathing", _rx(
        r"\b(?:can'?t|cannot|can\s+not|hard\s+to|trouble|difficulty|struggling\s+to|unable\s+to)\s+(?:catch\s+(?:my|his|her)\s+)?breath(?:e|ing)?\b",
        r"\bshort(?:ness)?\s+of\s+breath\b", r"\b(?:not|stopped)\s+breathing\b",
        r"\bchoking\b", r"\bgasping\b", r"\blips?\s+(?:are|is|turning)\s+blue\b",
        r"呼吸困難", r"喘不過氣", r"(?:不能|無法|沒辦法)呼吸", r"吸不到氣", r"呼吸急促", r"窒息", r"嘴唇發紫",
    ), True),
    ("stroke", _rx(
        r"\bface\s+(?:is\s+)?droop", r"\bdrooping\s+(?:face|mouth)", r"\bslurred\s+speech\b",
        r"\bslurring\b", r"\b(?:weakness|numbness|numb|weak)\s+(?:on|in)\s+one\s+side\b",
        r"\bone\s+side\s+of\s+(?:my|his|her|the)\s+(?:body|face)\b",
        r"\b(?:having|had|is\s+having)\s+a\s+stroke\b", r"\bstroke\s+(?:symptoms|signs)\b",
        r"\bworst\s+headache\b", r"\bsudden(?:ly)?\s+(?:can'?t|cannot)\s+(?:see|speak|talk|move)\b",
        r"中風(?!過|史|病史|紀錄|記錄)", r"嘴歪", r"臉歪", r"口齒不清", r"講話不清楚", r"說話含糊",
        r"(?:單側|半邊|一邊)(?:手腳|身體|臉)?(?:無力|麻|不能動)", r"突然(?:看不見|失明|不能說話)",
        r"(?:劇烈|爆炸性|最嚴重的)頭痛",
    ), True),
    ("bleeding", _rx(
        r"\b(?:severe|heavy|heavily|a\s+lot\s+of|uncontrolled|massive)\s+bleed",
        r"\bbleeding\s+(?:heavily|a\s+lot|badly|won'?t\s+stop|that\s+won'?t\s+stop|nonstop|non-stop)",
        r"\bcan'?t\s+stop\s+(?:the\s+)?bleed", r"\bwon'?t\s+stop\s+bleeding\b",
        r"\b(?:vomiting|throwing\s+up|coughing\s+up|coughing)\s+blood\b", r"\bblack\s+(?:tarry\s+)?stools?\b",
        r"大量出血", r"血流不止", r"止不住(?:血|的血)", r"一直(?:在)?流血", r"流(?:了)?很多血", r"吐血", r"咳血", r"解黑便", r"大便.{0,2}黑",
    ), True),
    ("anaphylaxis", _rx(
        r"\banaphyla", r"\bthroat\s+(?:is\s+)?(?:closing|swelling|swollen|tight)",
        r"\bswollen\s+(?:tongue|throat|lips)", r"\b(?:tongue|lips|face|throat)\s+(?:is\s+|are\s+)?swelling",
        r"\bsevere\s+allergic\b", r"\ballergic\s+reaction\b.{0,40}\b(?:breath|swell|throat)",
        r"\bhives\s+all\s+over\b", r"\bepipen\b",
        r"過敏性休克", r"喉嚨(?:腫|緊|快封|卡住)", r"舌頭腫", r"嘴唇腫", r"嚴重過敏", r"全身(?:起)?(?:蕁麻疹|疹子)",
    ), True),
    ("self_harm", _rx(
        r"\bkill(?:ing)?\s+myself\b", r"\bsuicid", r"\bend\s+(?:my\s+life|it\s+all)\b", r"\bwant(?:s)?\s+to\s+die\b",
        r"\bdon'?t\s+want\s+to\s+(?:live|be\s+alive|wake\s+up)\b", r"\bself[-\s]?harm", r"\bhurt(?:ing)?\s+myself\b",
        r"\bcut(?:ting)?\s+myself\b", r"\bno\s+reason\s+to\s+live\b", r"\bbetter\s+off\s+dead\b",
        r"自殺", r"想死", r"不想活", r"活不下去", r"輕生", r"結束(?:自己的)?生命", r"傷害自己", r"割腕", r"自殘",
        r"了結自己", r"跳樓", r"燒炭", r"不如死了",
    ), False),
    ("overdose", _rx(
        r"\boverdos", r"\btook\s+(?:too\s+many|a\s+lot\s+of|all\s+(?:my|the|of\s+my))\s+(?:pills|tablets|meds|medications?|\w+\s+pills)",
        r"\btoo\s+many\s+(?:pills|tablets)\b", r"\b(?:swallowed|drank|ate)\s+(?:some\s+)?(?:bleach|poison|pesticide|antifreeze)",
        r"\bpoison(?:ed|ing)\b",
        r"藥吃(?:太多|過量)", r"吃(?:了)?太多(?:顆)?藥", r"服藥過量", r"過量服用", r"吞了.{0,8}(?:藥|農藥|漂白水)", r"喝(?:了)?(?:農藥|漂白水)", r"中毒",
    ), True),
    ("unconscious", _rx(
        r"\bpassed\s+out\b", r"\bfainted\b", r"\bunconscious\b", r"\bunresponsive\b", r"\bwon'?t\s+wake\s+up\b",
        r"\bnot\s+waking\s+up\b", r"\bcollapsed\b", r"\bseizure", r"\bseizing\b", r"\bconvulsi",
        r"昏倒", r"暈倒", r"昏迷", r"失去意識", r"叫不醒", r"抽搐", r"癲癇發作", r"意識不清",
    ), True),
    ("pregnancy_bleeding", _rx(
        r"\bpregnan\w*\b.{0,40}\bbleed", r"\bbleed\w*\b.{0,40}\bpregnan", r"\bspotting\b.{0,30}\bpregnan",
        r"懷孕.{0,15}(?:出血|流血)", r"(?:出血|流血).{0,15}懷孕", r"孕期.{0,4}出血", r"孕婦.{0,6}(?:出血|流血)",
    ), True),
    ("infant_fever", _rx(
        r"\b(?:baby|infant|newborn)\b.{0,40}\b(?:fever|temperature\s+of|38(?:\.\d)?\s*°?c?)",
        r"\b(?:fever|temperature)\b.{0,40}\b(?:baby|infant|newborn)\b",
        r"\b\d{1,2}[-\s](?:week|month)[-\s]old\b.{0,40}\bfever",
        r"(?:嬰兒|寶寶|新生兒|小嬰兒|\d+\s*(?:個月|週|周)大).{0,15}(?:發燒|發高燒|燒到|體溫)",
        r"(?:發燒|發高燒).{0,15}(?:嬰兒|寶寶|新生兒)",
    ), True),
    ("emergency_call", _rx(
        r"\bcall\s+(?:an\s+)?ambulance\b", r"\bcall(?:ing)?\s+(?:911|119)\b", r"叫救護車", r"打\s*119",
    ), True),
)


# --------------------------------------------------------------------------
# Medical advice the bot declines
# --------------------------------------------------------------------------
_DRUG_VERB_EN = (
    r"stop|start|quit|skip|switch|change|increase|decrease|reduce|lower|raise|double|halve|cut\s+down|"
    r"come\s+off|go\s+off|get\s+off|taper|keep\s+taking|continue|stop\s+taking|take\s+(?:more|less|extra|double|half)"
)
_DECLINE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("medication_change", _rx(
        rf"\b(?:should|can|could|may|must)\s+i\s+(?:{_DRUG_VERB_EN})\b",
        rf"\bdo\s+i\s+(?:need|have)\s+to\s+(?:{_DRUG_VERB_EN}|take)\b",
        rf"\b(?:if|whether)\s+i\s+(?:can|should|could|may)\s+(?:{_DRUG_VERB_EN})\b",
        r"\bis\s+it\s+(?:ok|okay|safe|fine|alright|bad)\s+(?:to|if\s+i)\s+(?:stop|skip|take|double|mix|combine|drink|miss|quit)",
        r"\bhow\s+(?:much|many)\b.{0,40}\bshould\s+i\s+take\b", r"\bwhat\s+(?:dose|dosage)\s+should\b",
        r"\bshould\s+i\s+(?:be\s+)?tak(?:e|ing)\b", r"\bshould\s+i\s+(?:change|adjust|up|lower)\s+(?:my\s+)?(?:dose|dosage|insulin)",
        r"\bcan\s+i\s+(?:drink|eat|take)\b.{0,40}\b(?:with|while\s+(?:on|taking))\b",
        r"\b(?:what|which)\s+(?:medicine|medication|drug|pill)\s+(?:should|can)\s+i\s+take\b",
        r"(?:可以|能|能不能|可不可以|要不要|該不該|應不應該|應該|需不需要|需要|是否(?:可以|需要|應該)?)(?:先|自己)?(?:停藥|停掉|停吃|停用|不吃|不要吃|少吃|多吃|加量|減量|加藥|換藥|改吃|改用|吃兩倍|吃雙倍|自己調|調整(?:藥|劑量)|增加劑量|減少劑量)",
        r"(?:停|換|加|減)(?:藥|量)(?:嗎|好嗎|可以嗎|比較好)",
        r"(?:應該|要|該)(?:吃|服用)(?:多少|幾顆|幾毫克|幾\s*mg)",
        r"(?:可以|能)(?:跟|和|配).{0,10}(?:一起吃|併用|一起服用)", r"吃藥.{0,6}(?:可以|能)喝酒",
        r"(?:該|應該|要)吃(?:什麼|哪(?:一)?種)藥",
    )),
    ("diagnosis", _rx(
        r"\bdo\s+i\s+have\b", r"\bhave\s+i\s+got\b", r"\bam\s+i\s+(?:diabetic|pre-?diabetic|sick|ill|dying|anemic|anaemic|hypertensive|ok|okay|healthy|at\s+risk)\b",
        r"\bis\s+(?:this|it|that)\s+(?:cancer|a\s+tumou?r|malignant|benign|serious|dangerous|something\s+serious|bad|life[-\s]threatening|fatal)\b",
        r"\b(?:does|do)\s+(?:this|that|it|these|my\b.{0,40})\s+mean\s+(?:that\s+)?i\s+(?:have|am|'ve\s+got|got)\b",
        r"\bwhat'?s\s+wrong\s+with\s+me\b", r"\bwhat\s+is\s+wrong\s+with\s+me\b", r"\bdiagnos(?:e|is)\s+(?:me|my)\b",
        r"\bwhat\s+(?:disease|condition|illness)\s+do\s+i\b",
        r"\bis\s+\d+(?:\.\d+)?\s*\S{0,8}\s+(?:dangerous|bad|serious|worrying|concerning|too\s+high|too\s+low)\b",
        r"\b(?:dangerous|serious|bad|worrying)\s+for\s+me\b", r"\bshould\s+i\s+(?:be\s+)?worr(?:y|ied)\b",
        r"\bis\s+my\b.{0,40}\b(?:dangerous|serious|bad|worrying|concerning|too\s+high|too\s+low)\b",
        r"(?:是不是|是否|會不會|有沒有)(?:得了|罹患|得到|患有)", r"我(?:是不是|是否|會不會)(?:有)?.{0,8}(?:癌|病|症|腫瘤|中風)",
        r"(?:這|這個|它|那)(?:是不是|是|算不算|會不會是|應該是)(?:癌|癌症|腫瘤|惡性|良性|很嚴重|嚴重|危險)",
        r"(?:這|這個|它|那|數字|數值|指數|結果|報告|\d+(?:\.\d+)?)(?:很|會)?(?:嚴重|危險|不好)(?:嗎|不|吧)",
        r"(?:危險|嚴重)嗎", r"我得了什麼", r"幫我診斷", r"診斷(?:一下|我)", r"我(?:怎麼了|生了什麼病)", r"(?:該不該|需不需要|需要|要不要|要)擔心",
        r"(?:代表|表示|意思是)我(?:有|得了|是|罹患)",
    )),
    ("prognosis", _rx(
        r"\bhow\s+long\s+(?:do|will|would)\s+i\s+(?:have|live|survive)\b", r"\blife\s+expectancy\b", r"\bsurvival\s+(?:rate|chance)",
        r"\bwhat\s+are\s+my\s+(?:odds|chances)\b", r"\bchances?\s+(?:of|that)\s+(?:i|my|surviv|recover|dying|cure)", r"\bmy\s+prognosis\b",
        r"\bwill\s+i\s+(?:die|recover|get\s+better|be\s+cured|survive)\b", r"\bis\s+(?:it|this|my\s+\w+)\s+(?:curable|terminal|fatal)\b",
        r"還能活多久", r"存活率", r"活多久", r"會不會死", r"會死嗎", r"治得好嗎", r"(?:能|可以)治好嗎", r"治癒率", r"我的預後", r"好得了嗎",
    )),
    ("treatment_choice", _rx(
        r"\b(?:which|what)\s+(?:treatment|drug|medication|medicine|option|surgery|therapy)\s+(?:should|is\s+better|would\s+you)\b",
        r"\bshould\s+i\s+(?:have|get|do|choose|go\s+with|pick|try|undergo|accept|refuse)\s+(?:the\s+|a\s+)?(?:surgery|chemo|chemotherapy|operation|treatment|radiation|radiotherapy|procedure|biopsy|transplant|stent|injection)",
        r"\bshould\s+i\s+(?:choose|pick|go\s+with)\b.{0,40}\bor\b", r"\bis\b.{0,40}\bbetter\s+than\b.{0,40}\bfor\s+me\b",
        r"\bwhat\s+should\s+i\s+do\s+about\b", r"\bwhat\s+would\s+you\s+(?:recommend|do|suggest)\b", r"\brecommend\s+(?:me\s+)?(?:a\s+)?(?:drug|medication|medicine|treatment)\b",
        r"(?:要不要|該不該|應不應該|應該|要)(?:選|做|接受|開刀|手術|化療|放療|電療)", r"(?:開刀|手術|化療|放療|電療)(?:比較)?好(?:嗎|還是)",
        r"哪(?:一)?(?:種|個)(?:治療|藥|方法|療法|手術)(?:比較好|好|適合我)", r"你(?:建議|推薦)我", r"(?:該|要)怎麼治療",
    )),
)

# Asking for help preparing questions FOR the clinician is not asking the bot
# for advice: "help me ask my doctor whether I should stop metformin" is fine.
_PREP_FRAMING = _rx(
    r"\b(?:ask|tell|asking)\s+(?:my|the)\s+(?:doctor|clinician|physician|nurse|pharmacist|gp|specialist)\b",
    r"\bquestions?\s+(?:for|to)\s+(?:my|the)\s+(?:doctor|clinician|physician|nurse|pharmacist|visit|appointment)\b",
    r"\bprepare\s+(?:some\s+)?questions\b", r"\badd\b.{0,30}\bto\s+my\s+question", r"\bquestion\s+list\b",
    r"^\s*(?:add|note|save)\s+(?:a\s+)?(?:question|concern)\b", r"^\s*(?:記下|新增|加入)(?:一個)?(?:問題|疑問)",
    r"問(?:醫師|醫生|藥師|護理師)", r"要問的問題", r"準備.{0,8}問題", r"問題清單", r"看診(?:時)?(?:要|想)問",
)


def screen_user_text(text: str, locale: str = "zh-TW") -> SafetyVerdict:
    """Classify the user's own words. Do NOT pass pasted or retrieved record text."""
    text = (text or "")[:5000]
    urgent: list[str] = []
    for category, rx, negatable in _URGENT:
        for m in rx.finditer(text):
            if negatable and _negated(text, m.start()):
                continue
            urgent.append(category)
            break
    if urgent:
        return SafetyVerdict("urgent", tuple(dict.fromkeys(urgent)))
    if _PREP_FRAMING.search(text):
        return SafetyVerdict("ok")
    declined = [category for category, rx in _DECLINE if rx.search(text)]
    if declined:
        return SafetyVerdict("declined_medical_advice", tuple(declined))
    return SafetyVerdict("ok")


# --------------------------------------------------------------------------
# Model output filter
# --------------------------------------------------------------------------

_DRUG_OBJ_EN = r"(?:your|the|this|that|all|these)\s+(?:\w+\s+)?(?:medication|medicine|meds|drugs?|pills?|tablets?|dose|dosage|insulin|treatment|prescription)s?"
_DIRECTIVE = _rx(
    rf"\byou\s+(?:should|must|need\s+to|have\s+to|ought\s+to|'d\s+better|had\s+better)\s+(?:not\s+)?(?:{_DRUG_VERB_EN}|take|use|try|begin|discontinue|resume)\b",
    rf"\b(?:i|we)\s+(?:would\s+)?(?:recommend|suggest|advise)\s+(?:that\s+)?(?:you\s+)?(?:{_DRUG_VERB_EN}|take|taking|stopping|starting|increasing|reducing|switching|discontinu)",
    rf"(?:^|[.!\n]\s*)(?:please\s+)?(?:{_DRUG_VERB_EN}|discontinue|take)\s+(?:taking\s+)?{_DRUG_OBJ_EN}",
    r"\btake\s+\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units?|iu|tablets?|pills?|capsules?)\b",
    r"\b(?:it(?:'s|\s+is)\s+(?:safe|fine|ok|okay)\s+to\s+(?:stop|skip|double|increase|reduce|drink))\b",
    r"(?:建議|應該|最好|請|可以先|你可以|您可以)(?:你|您)?(?:先)?(?:停藥|停用|停掉|停止服用|不要吃|不用吃|加量|減量|加藥|換藥|改吃|改用|多吃|少吃|增加劑量|減少劑量|吃兩倍|自行調整)",
    r"(?:你|您)(?:要|需要|應該|得|必須)(?:先)?(?:停藥|停用|停掉|停止服用|加量|減量|換藥|改吃|改用|服用|吃)",
    r"(?:每天|一天)?(?:吃|服用)\s*\d+(?:\.\d+)?\s*(?:mg|毫克|顆|錠|粒|單位)",
)
# Always flagged, framing or not: the classic injected instruction.
_DIRECTIVE_HARD = _rx(
    r"\bstop\s+(?:taking\s+)?all\s+(?:of\s+)?(?:your\s+)?(?:medication|medicine|meds|drugs|pills)",
    r"停(?:止|掉|用)?(?:服用)?所有(?:的)?(?:藥|藥物)", r"所有(?:的)?藥(?:物)?都(?:停|不要吃|不用吃)",
)
# "Ask your clinician whether you should stop..." is a question for the
# clinician, not a directive.
_DIRECTIVE_EXEMPT = re.compile(
    r"\b(?:whether|if|ask|asking|discuss|talk\s+to|check\s+with)\b|是否|要不要|需不需要|可不可以|能不能|問(?:醫師|醫生|藥師)|和(?:醫師|醫生)討論|跟(?:醫師|醫生)討論",
    re.IGNORECASE,
)
_CONDITION_EN = (
    r"(?:type\s*[12]\s+)?diabet\w*|pre-?diabet\w*|cancer|carcinoma|tumou?r|lymphoma|leuk(?:a)?emia|malignan\w*|"
    r"hypertension|hypothyroid\w*|hyperthyroid\w*|an(?:a)?emi\w*|kidney\s+(?:disease|failure)|ckd|liver\s+(?:disease|failure)|"
    r"heart\s+(?:disease|failure)|stroke|infection|pneumonia|hepatitis|cirrhosis|\w+itis|\w+\s+disease|\w+\s+syndrome|\w+\s+disorder"
)
_DIAGNOSIS = _rx(
    rf"\byou\s+(?:have|'ve\s+got|have\s+got|are\s+suffering\s+from|suffer\s+from|definitely\s+have|probably\s+have|likely\s+have|may\s+have|might\s+have|have\s+developed)\s+(?:a\s+|an\s+)?(?:[\w-]+\s+){{0,2}}?(?:{_CONDITION_EN})\b",
    r"\byou\s+are\s+(?:diabetic|pre-?diabetic|anemic|anaemic|hypertensive|dying|terminal|healthy|fine|not\s+sick|cured)\b",
    rf"\b(?:this|it|that|these\s+results?|your\s+results?)\s+(?:is|are|means|indicates?|confirms?|shows?)\s+(?:that\s+)?(?:you\s+have\s+)?(?:{_CONDITION_EN})\b",
    r"\b(?:nothing|no\s+need)\s+to\s+worry\b", r"\bnot\s+(?:serious|dangerous|cancer)\b", r"\bis\s+(?:serious|dangerous|life[-\s]threatening)\b",
    r"(?:你|您)(?:得了|罹患|患有|已經有|確定有|可能有|應該有|有)(?:早期|輕微|嚴重)?(?:糖尿病|高血壓|癌症|腫瘤|中風|心臟病|貧血|腎臟病|肝病|\S{0,6}(?:癌|炎|症|病))",
    r"(?:這|這個|它|結果|報告)(?:是|代表|表示|顯示|確定是)(?:你|您)?(?:有|得了)?(?:糖尿病|癌症|腫瘤|惡性|\S{0,4}(?:癌|症))",
    r"(?:不用|不必|不需要|無須|無需)擔心", r"沒(?:什麼)?(?:大礙|問題的|事的)", r"(?:很|非常)(?:嚴重|危險)", r"不(?:嚴重|危險)",
)
_CITATION = re.compile(r"\[R:[A-Za-z0-9_\-]{1,64}\]")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'）)\]]+")
_PHONE = re.compile(r"(?<![\w.])(?:\+?\d[\d\s\-()]{6,}\d|0\d{1,2}-\d{3,4}-?\d{3,4})(?![\w])")
_DATE = re.compile(r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b|\d{2,4}年\d{1,2}月(?:\d{1,2}日)?|\d{1,2}月\d{1,2}日")
_UNIT_AFTER = re.compile(r"\s*(?:%|mg|mcg|g\b|ml|mmol|mg/dl|iu|units?|毫克|顆|錠|單位|°)", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?。！？\n]+[.!?。！？]?")


def _numbers(text: str) -> set[float]:
    out: set[float] = set()
    for m in re.finditer(r"\d+(?:[.,]\d+)*", text or ""):
        raw = m.group(0).replace(",", "")
        try:
            out.add(float(raw))
        except ValueError:
            continue
    return out


def _norm_date(raw: str) -> str:
    parts = [int(p) for p in re.findall(r"\d+", raw)]
    return "-".join(str(p) for p in parts)


def _strip_allowed_spans(text: str) -> str:
    text = _CITATION.sub(" ", text)
    text = re.sub(r"(?m)^\s*\d{1,2}[.)、]\s", " ", text)  # list numbering
    return text


def screen_model_text(
    text: str,
    locale: str = "zh-TW",
    *,
    sources: Sequence[str] = (),
    allowed_contacts: Iterable[str] = (),
    allowed_hosts: Iterable[str] = (),
) -> list[str]:
    """Flag categories in a model answer: directive, diagnosis, unsupported_number, contact.

    `sources` is every text the model was shown (records, pasted text, the
    user's question). A number in the answer must appear in one of them; dates
    and reference ranges copied from the sources therefore pass, invented
    ones do not. Bare integers up to 12 without a unit are allowed ("2 records").
    """
    text = text or ""
    flags: list[str] = []
    for sentence in _SENTENCE.findall(text):
        if _DIRECTIVE_HARD.search(sentence):
            flags.append("directive")
            break
        m = _DIRECTIVE.search(sentence)
        if m and not _DIRECTIVE_EXEMPT.search(sentence[: m.start()] + " " + m.group(0)):
            flags.append("directive")
            break
    for sentence in _SENTENCE.findall(text):
        m = _DIAGNOSIS.search(sentence)
        if m and not _DIRECTIVE_EXEMPT.search(sentence[: m.start()]):
            flags.append("diagnosis")
            break

    allowed_contact_digits = {re.sub(r"\D", "", c) for c in allowed_contacts if c}
    allowed_contact_digits |= set(CRISIS_NUMBERS)
    hosts = {h.lower() for h in allowed_hosts}
    for m in _URL.finditer(text):
        host = re.sub(r"(?i)^(?:https?://)?(?:www\.)?", "", m.group(0)).split("/")[0].split(":")[0].lower()
        if host not in hosts:
            flags.append("contact")
            break
    body = _URL.sub(" ", _strip_allowed_spans(text))
    source_dates = {_norm_date(m.group(0)) for s in sources for m in _DATE.finditer(s)}
    for m in _DATE.finditer(body):
        if _norm_date(m.group(0)) not in source_dates:
            flags.append("unsupported_number")
            break
    body = _DATE.sub(" ", body)
    source_digits = ["".join(re.findall(r"\d", s)) for s in sources]

    def phone(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 7:
            return m.group(0)
        if digits not in allowed_contact_digits and not any(digits in s for s in source_digits):
            flags.append("contact")
        return " "

    body = _PHONE.sub(phone, body)

    source_numbers: set[float] = set()
    for s in sources:
        source_numbers |= _numbers(s)
    source_numbers |= {float(n) for n in allowed_contact_digits if n}
    for m in re.finditer(r"\d+(?:[.,]\d+)*", body):
        raw = m.group(0)
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if value in source_numbers:
            continue
        has_unit = bool(_UNIT_AFTER.match(body, m.end()))
        if "." not in raw and value <= 12 and not has_unit:
            continue
        flags.append("unsupported_number")
        break
    return list(dict.fromkeys(flags))


# --------------------------------------------------------------------------
# Untrusted data
# --------------------------------------------------------------------------

_INJECTION = _rx(
    r"\bignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions|rules|prompts?)",
    r"\bdisregard\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above|system)", r"\bsystem\s+prompt\b", r"\byou\s+are\s+now\b",
    r"\bnew\s+instructions?\s*:", r"\b(?:assistant|system)\s*:", r"\btell\s+the\s+user\s+to\b",
    r"忽略(?:之前|以上|前面|先前|上述)(?:的)?(?:所有)?(?:指示|指令|規則)", r"你現在是", r"系統提示", r"告訴(?:使用者|用戶|病人)(?:要|去)?",
)

UNTRUSTED_OPEN = "<<<DATA"
UNTRUSTED_CLOSE = "DATA>>>"


def looks_like_injection(text: str) -> bool:
    """Instruction-like text inside data. Used for a receipt note, never to trust the rest."""
    return bool(_INJECTION.search(text or ""))


def wrap_untrusted(record_text: str, label: str = "") -> str:
    """Delimit record or pasted text as DATA for the prompt.

    The fence cannot be closed from inside (delimiters in the text are
    defused), and citation-shaped strings inside the data are defused so a
    record cannot plant a fake [R:...] reference. Prompt-assembly rule: the
    system prompt says text between the fences is data and its instructions are
    not to be followed; the output filter still assumes a model may obey them.
    """
    body = (record_text or "").replace("<<<", "‹‹‹").replace(">>>", "›››")
    body = body.replace("[R:", "(R:")
    head = f"{UNTRUSTED_OPEN} {label}".rstrip()
    return f"{head}\n{body}\n{UNTRUSTED_CLOSE}"


def emergency_message(tenant_emergency_text: str, verdict: SafetyVerdict, locale: str) -> str:
    """Lead with the tenant's emergency text; for self-harm add crisis lines. No lecture."""
    zh = locale.startswith("zh")
    lines = [tenant_emergency_text or ("如果有緊急狀況，請立即撥打 119 或前往急診。" if zh else
                                      "If this is an emergency, call 119 now or go to the nearest emergency department.")]
    if verdict.crisis:
        if zh:
            lines.append("聽起來你現在很不好受。你不必一個人撐著，現在就可以打電話和人談談：")
        else:
            lines.append("It sounds like you are going through something really hard. You don't have to carry it alone; you can talk to someone right now:")
        for number, name in CRISIS_LINES["zh-TW" if zh else "en"]:
            lines.append(f"- {number}：{name}" if zh else f"- {number}: {name}")
    else:
        lines.append("我是整理紀錄的助理，沒辦法判斷緊急狀況，請先尋求即時協助。" if zh else
                     "I organise records and can't assess an emergency; please get help now.")
    lines.append("之後如果需要，我仍可以幫你整理紀錄。" if zh else "Whenever you're ready, I can still help with your records.")
    return "\n".join(lines)
