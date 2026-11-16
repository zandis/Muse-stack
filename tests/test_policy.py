"""Tests for muse.policy. All identifiers and keys are generated, never real."""

from __future__ import annotations

import copy
import time

import pytest

from wardmuse import policy
from wardmuse.policy import (
    PRIVACY_CLASSES,
    Finding,
    PolicyError,
    allowed,
    detect_sensitive,
    effective_class,
    explain,
    filter_deployments,
    normalise_class,
    request_is_clinical,
    stricter,
)

# ---------------------------------------------------------------- model_info

GEMINI_FREE = {"provider": "gemini", "muse_kind": "cloud", "muse_tier": "free",
               "muse_trains": "yes", "muse_human_review": "yes"}
UNKNOWNS = {"provider": "x", "muse_kind": "cloud", "muse_trains": "unknown", "muse_human_review": "unknown"}
TRAINS_NO_REVIEW_UNKNOWN = {"muse_kind": "cloud", "muse_trains": "no", "muse_human_review": "unknown"}
PAID_NO_NO = {"provider": "paid", "muse_kind": "cloud", "muse_tier": "paid",
              "muse_trains": "no", "muse_human_review": "no"}
LOCAL = {"provider": "ollama", "muse_kind": "local", "muse_trains": "no", "muse_human_review": "no"}
LOCAL_BARE = {"muse_kind": "local"}

# class -> (info -> expected)
MATRIX = [
    (GEMINI_FREE, dict(public=True, standard=True, private=False, local=False)),
    (UNKNOWNS, dict(public=True, standard=True, private=False, local=False)),
    (TRAINS_NO_REVIEW_UNKNOWN, dict(public=True, standard=True, private=False, local=False)),
    (PAID_NO_NO, dict(public=True, standard=True, private=True, local=False)),
    (LOCAL, dict(public=True, standard=True, private=True, local=True)),
    (LOCAL_BARE, dict(public=True, standard=True, private=True, local=True)),
    ({}, dict(public=True, standard=True, private=False, local=False)),
    (None, dict(public=True, standard=True, private=False, local=False)),
    ("garbage", dict(public=True, standard=True, private=False, local=False)),
    (42, dict(public=True, standard=True, private=False, local=False)),
    ({"muse_kind": 1, "muse_trains": None, "muse_human_review": ["no"]},
     dict(public=True, standard=True, private=False, local=False)),
    ({"muse_kind": "cloud", "muse_trains": True, "muse_human_review": False},
     dict(public=True, standard=True, private=False, local=False)),
    ({"muse_trains": " NO ", "muse_human_review": "No"},
     dict(public=True, standard=True, private=True, local=False)),
]


@pytest.mark.parametrize("info,expect", MATRIX, ids=[repr(m[0])[:40] for m in MATRIX])
def test_allowed_matrix(info, expect):
    for cls in PRIVACY_CLASSES:
        assert allowed(info, cls) is expect[cls], cls
        assert isinstance(explain(info, cls), str) and explain(info, cls)


def test_explain_mentions_reason():
    assert "train" in explain(GEMINI_FREE, "private") and "Blocked" in explain(GEMINI_FREE, "private")
    assert "unverified" in explain(UNKNOWNS, "private")
    assert explain(PAID_NO_NO, "private").startswith("Allowed")
    assert "local" in explain(PAID_NO_NO, "local").lower()
    assert explain(LOCAL, "local").startswith("Allowed")


def test_allowed_rejects_unknown_class():
    with pytest.raises(PolicyError):
        allowed(LOCAL, "secret")


# ------------------------------------------------------------ normalise_class


@pytest.mark.parametrize("value,default,expect", [
    (None, "standard", "standard"), ("", "private", "private"), ("   ", "local", "local"),
    ("PUBLIC", "standard", "public"), ("  Private ", "standard", "private"),
    ("c0", "standard", "public"), ("C1", "standard", "standard"), ("c2", "standard", "standard"),
    ("c3", "standard", "private"), ("c4", "standard", "local"), ("local", "standard", "local"),
])
def test_normalise_ok(value, default, expect):
    assert normalise_class(value, default) == expect


@pytest.mark.parametrize("value", ["secret", "c5", "c", "privte", "1", 3, ["private"]])
def test_normalise_errors(value):
    with pytest.raises(PolicyError):
        normalise_class(value)


def test_normalise_bad_default():
    with pytest.raises(PolicyError):
        normalise_class(None, "bogus")
    assert issubclass(PolicyError, ValueError)


def test_stricter():
    assert stricter("public", "private") == "private"
    assert stricter("local", "private") == "local"
    assert stricter("c3", "standard") == "private"
    for a in PRIVACY_CLASSES:
        assert stricter(a, a) == a


# ------------------------------------------------------------------- filtering


def _dep(name, info):
    d = {"model_name": name, "litellm_params": {"model": name}}
    if info is not ...:
        d["model_info"] = info
    return d


def test_filter_order_and_immutability():
    deps = [_dep("a", GEMINI_FREE), _dep("b", PAID_NO_NO), _dep("c", ...), _dep("d", LOCAL),
            _dep("e", "junk"), _dep("f", UNKNOWNS)]
    before = copy.deepcopy(deps)
    out = filter_deployments(deps, "private")
    assert [d["model_name"] for d in out] == ["b", "d"]
    assert out[0] is deps[1]  # same objects, not copies
    assert deps == before
    assert [d["model_name"] for d in filter_deployments(deps, "standard")] == list("abcdef")
    assert [d["model_name"] for d in filter_deployments(deps, "c4")] == ["d"]
    assert filter_deployments([], "local") == []
    with pytest.raises(PolicyError):
        filter_deployments([], "nope")


# ------------------------------------------------------------ effective_class


def test_effective_class_never_lowers():
    secret = "key " + "sk-ant-" + "Ab3" * 12
    assert effective_class(None, "standard", secret, True)[0] == "private"
    assert effective_class("public", "standard", secret, True)[0] == "private"
    assert effective_class("local", "standard", secret, True)[0] == "local"
    assert effective_class("private", "standard", secret, True)[0] == "private"
    assert effective_class("c4", "standard", "hello", True)[0] == "local"
    cls, findings = effective_class(None, "standard", secret, False)
    assert cls == "standard" and findings  # auto off: reported, not raised
    assert effective_class("public", "standard", "hello", True) == ("public", [])
    with pytest.raises(PolicyError):
        effective_class("bogus", "standard", "x", True)


# ---------------------------------------------------------------- generators

TW_LETTER = dict(zip("ABCDEFGHJKLMNPQRSTUVXYWZIO", range(10, 36)))


def tw_id(letter="A", second="1", body="2345678"):
    """Build a Taiwan ID / new ARC (digit second char) with a valid check digit."""
    code = TW_LETTER[letter]
    nums = [code // 10, code % 10, int(second)] + [int(c) for c in body]
    w = [1, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    check = (-sum(n * x for n, x in zip(nums, w))) % 10
    return f"{letter}{second}{body}{check}"


def tw_old_arc(letter="A", second="B", body="234567"):
    code = TW_LETTER[letter]
    nums = [code // 10, code % 10, TW_LETTER[second] % 10] + [int(c) for c in body]
    w = [1, 9, 8, 7, 6, 5, 4, 3, 2, 1]
    nums = nums + [0]
    w = [1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1]
    # body is 7 digits incl. none of check; need 8 digits after the second letter
    total = sum(n * x for n, x in zip(nums[:-1], w[:-1]))
    check = (-total) % 10
    return f"{letter}{second}{body}{check}"


def sg_id(prefix="S", digits="1234567"):
    total = sum(int(d) * w for d, w in zip(digits, (2, 7, 6, 5, 4, 3, 2)))
    if prefix in "TG":
        total += 4
    table = "JZIHGFEDCBA" if prefix in "ST" else "XWUTRQPNMLK"
    return prefix + digits + table[total % 11]


def luhn_number(prefix: str, length: int) -> str:
    body = prefix + "7" * (length - len(prefix) - 1)
    for check in "0123456789":
        cand = body + check
        if policy._luhn(cand):
            return cand
    raise AssertionError


def iban(country="DE", bban="370400440532013000"):
    rearr = bban + country
    num = "".join(str(int(c, 36)) for c in rearr) + "00"
    check = 98 - int(num) % 97
    return f"{country}{check:02d}{bban}"


def kinds(text):
    return [f.kind for f in detect_sensitive(text)]


# ---------------------------------------------------------- true positives

SECRETS = [
    ("anthropic_key", "sk-ant-" + "Ab1_" * 10),
    ("openai_key", "sk-" + "Zx9" * 16),
    ("openai_key", "sk-proj-" + "Qw3_" * 12),
    ("google_key", "AIza" + "Ab3_-" * 7),
    ("groq_key", "gsk_" + "Kj8" * 15),
    ("github_token", "ghp_" + "a1B2" * 9),
    ("github_token", "gho_" + "a1B2" * 9),
    ("github_token", "github_pat_" + "a1B2_" * 12),
    ("aws_key_id", "AKIA" + "B7C9D2E4F6G8H3J5"),
    ("slack_token", "xoxb-" + "1234567890-abcdefghij"),
    ("stripe_key", "sk_live_" + "Ab3dE6gH9jK2mN5p"),
    ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----"),
    ("private_key", "-----BEGIN PRIVATE KEY-----"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9." + "eyJzdWIiOiJ4eHh4eHgifQ." + "abcDEF123_-xyz"),
    ("bearer", "Authorization: Bearer abc123DEF456ghi789"),
    ("conn_string", "postgres://app:s3cr3tPw@db.internal:5432/main"),
]


@pytest.mark.parametrize("kind,secret", SECRETS, ids=[f"{k}-{i}" for i, (k, _) in enumerate(SECRETS)])
def test_secret_true_positive(kind, secret):
    text = f"please debug this: {secret} thanks"
    found = detect_sensitive(text)
    assert [f.kind for f in found] == [kind]
    f = found[0]
    assert text[f.start:f.end].startswith(secret[:6])


def test_anthropic_not_also_openai():
    assert kinds("sk-ant-" + "Ab1_" * 10) == ["anthropic_key"]


def test_private_key_span_covers_block():
    block = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    text = "x " + block + " y"
    f = detect_sensitive(text)[0]
    assert text[f.start:f.end] == block


@pytest.mark.parametrize("make", [
    lambda: tw_id("A", "1"), lambda: tw_id("F", "2", "7654321"), lambda: tw_id("Z", "1", "0000001"),
    lambda: tw_id("B", "8"), lambda: tw_id("H", "9", "1357913"),
])
def test_tw_id_positive(make):
    ident = make()
    assert kinds(f"id {ident}.") == ["tw_id"]


def test_tw_old_arc_positive():
    ident = tw_old_arc("A", "B", "2345670")
    assert kinds(f"arc: {ident}") == ["tw_id"]
    assert kinds(f"arc: {tw_old_arc('K', 'C', '1234560')}") == ["tw_id"]


def test_tw_id_negative_bad_checksum():
    ident = tw_id("A", "1")
    bad = ident[:-1] + str((int(ident[-1]) + 1) % 10)
    assert kinds(f"id {bad}") == []
    assert kinds(f"id {ident.lower()}") == []  # lowercase never flagged
    assert kinds(f"id X{ident}") == []  # embedded in a longer token


@pytest.mark.parametrize("prefix", ["S", "T", "F", "G"])
def test_sg_positive_and_negative(prefix):
    ident = sg_id(prefix, "2468135")
    assert kinds(f"nric {ident}") == ["sg_nric"]
    wrong = ident[:-1] + ("A" if ident[-1] != "A" else "B")
    assert kinds(f"nric {wrong}") == []


def test_ssn():
    assert kinds("ssn 219-09-9998") == ["us_ssn"]
    assert kinds("ssn 219 09 9998") == ["us_ssn"]
    for bad in ["000-12-3456", "666-12-3456", "900-12-3456", "123-00-4567", "123-45-0000",
                "219-09 9998", "219099998"]:
        assert kinds(f"ssn {bad}") == [], bad


@pytest.mark.parametrize("prefix,length", [("4", 16), ("4", 13), ("51", 16), ("37", 15), ("6011", 16), ("3530", 16)])
def test_card_positive(prefix, length):
    n = luhn_number(prefix, length)
    assert kinds(f"card {n}") == ["credit_card"]
    spaced = " ".join(n[i:i + 4] for i in range(0, len(n), 4))
    assert kinds(f"card {spaced}.") == ["credit_card"]
    dashed = "-".join(n[i:i + 4] for i in range(0, len(n), 4))
    assert kinds(f"card {dashed}") == ["credit_card"]


def test_card_negatives():
    n = luhn_number("4", 16)
    bad = n[:-1] + str((int(n[-1]) + 1) % 10)
    assert kinds(f"c {bad}") == []
    assert kinds("c " + "4" * 16) == []  # repeated digit
    assert kinds(f"order ORD-{n}") == []
    assert kinds(f"c {luhn_number('9', 16)}") == []  # no card scheme
    assert kinds("ts 1699999999999") == []  # epoch ms
    assert kinds("isbn 978-0-306-40615-7") == []


@pytest.mark.parametrize("country,bban", [("DE", "370400440532013000"), ("GB", "NWBK60161331926819"),
                                          ("FR", "30006000011234567890189"), ("NL", "ABNA0417164300")])
def test_iban(country, bban):
    ok = iban(country, bban)
    assert kinds(f"pay to {ok} today") == ["iban"]
    spaced = " ".join(ok[i:i + 4] for i in range(0, len(ok), 4))
    assert kinds(f"pay to {spaced} today") == ["iban"]
    bad = ok[:-1] + str((int(ok[-1]) + 1) % 10)
    assert kinds(f"pay to {bad} today") == []


def test_iban_trailing_word_not_swallowed():
    ok = iban()
    spaced = " ".join(ok[i:i + 4] for i in range(0, len(ok), 4))
    text = f"{spaced} ABCD"
    f = detect_sensitive(text)[0]
    assert text[f.start:f.end] == spaced


# ------------------------------------------------------------------ contact


def test_single_contact_not_flagged():
    assert kinds("write to jane.doe@corp.org") == []
    assert kinds("call 0912-345-678") == []
    assert kinds("call +886 912 345 678") == []
    assert kinds("call +1 415 555 0100") == []


def test_three_contacts_flagged():
    text = "a@corp.org, b@corp.org and 0912345678"
    assert sorted(kinds(text)) == ["email", "email", "tw_mobile"]


def test_two_contacts_not_flagged():
    assert kinds("a@corp.org and 0912345678") == []


def test_contact_with_other_kind_flagged():
    text = f"{tw_id()} mail me at jane@corp.org"
    assert sorted(kinds(text)) == ["email", "tw_id"]


def test_contact_variants_with_other_finding():
    anchor = tw_id()
    for phone, kind in [("0912-345-678", "tw_mobile"), ("+886-912-345-678", "tw_mobile"),
                        ("+44 20 7946 0958", "phone"), ("+81312345678", "phone")]:
        assert sorted(kinds(f"{anchor} {phone}")) == sorted([kind, "tw_id"]), phone


def test_example_domains_ignored():
    text = f"{tw_id()} a@example.com b@example.org"
    assert kinds(text) == ["tw_id"]


def test_email_inside_conn_string_not_double_counted():
    assert kinds("mysql://root:hunter22@db.corp.org/app") == ["conn_string"]


def test_conn_string_placeholders_ignored():
    for pw in ["password", "<password>", "${DB_PASS}", "xxxxxxxx", "****"]:
        assert kinds(f"postgres://user:{pw}@localhost/db") == [], pw


# ------------------------------------------------------------------- health


def _info(**fields):
    base = {"provider": "gemini", "muse_kind": "cloud", "muse_tier": "free",
            "muse_trains": "yes", "muse_human_review": "yes", "muse_clinical_use": "forbidden"}
    base.update(fields)
    return base


def test_clinical_request_never_uses_gemini_on_any_class():
    """Gemini is forbidden for clinical use on every tier, including standard and private."""
    gemini_free = _info()
    gemini_paid = _info(muse_tier="paid", muse_trains="no", muse_human_review="no")
    groq = {"provider": "groq", "muse_kind": "cloud", "muse_trains": "unknown",
            "muse_human_review": "unknown", "muse_clinical_use": "unknown"}
    for cls in PRIVACY_CLASSES:
        assert allowed(gemini_free, cls, clinical=True) is False
        assert allowed(gemini_paid, cls, clinical=True) is False
        assert "clinical" in explain(gemini_free, cls, clinical=True)
    assert allowed(gemini_free, "standard", clinical=False) is True
    assert allowed(groq, "standard", clinical=True) is True
    assert allowed(gemini_paid, "private", clinical=False) is True  # paid terms would pass private
    assert allowed(gemini_paid, "private", clinical=True) is False  # the clinical ban still holds
    deps = [
        {"model_name": "smart", "model_info": gemini_free},
        {"model_name": "smart", "model_info": gemini_paid},
        {"model_name": "smart", "model_info": groq},
    ]
    kept = filter_deployments(deps, "standard", clinical=True)
    assert [d["model_info"]["provider"] for d in kept] == ["groq"]
    assert filter_deployments(deps[:2], "standard", clinical=True) == []
    assert filter_deployments(deps[:2], "private", clinical=True) == []


def test_medical_request_phrases_are_clinical_and_a_single_diagnosis_is_not():
    assert request_is_clinical(detect_sensitive("Please give me medical advice about this rash"))
    assert request_is_clinical(detect_sensitive("請給我醫療建議"))
    assert request_is_clinical(detect_sensitive("MRN: 55\nDiagnosis: asthma"))
    assert not request_is_clinical(detect_sensitive("What is the capital of France?"))
    assert not request_is_clinical(detect_sensitive("診斷"))  # one marker is not a clinical document
    assert not request_is_clinical(detect_sensitive("help me diagnose this failing test"))


def test_clinical_needs_two_markers():
    assert kinds("病歷號 123") == []
    assert kinds("這個程式診斷錯誤") == []
    assert kinds("病歷號 123 診斷 高血壓") == ["clinical_document"]
    assert kinds("出院病摘: 患者 主訴 胸痛") == ["clinical_document"]
    assert kinds("Medical Record Number 55 and MRN: 55") == []  # same marker twice
    assert kinds("MRN: 55\nDiagnosis: asthma") == ["clinical_document"]
    assert kinds("Patient John, Date of Birth 1990") == []
    assert kinds("Patient John (Date of Birth: 1990)\nChief complaint: cough") == ["clinical_document"]


# --------------------------------------------------------- true negatives

NEGATIVES = [
    "add a task-list and a disk-image feature",
    "see risk-assessment-for-the-whole-project-2024",
    "pip install scikit-learn-extra-with-long-name",
    "order #1234567890123 shipped",
    "id 550e8400-e29b-41d4-a716-446655440000",
    "commit 3f786850e387550fdab836ed7e6dc881de23001b",
    "version 1.2.3-rc.4+build.20240101 released",
    "ISBN 978-3-16-148410-0",
    "at 2024-05-06T12:34:56.789Z",
    "blob " + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=" * 3,
    "theorem foo : ∀ n : Nat, n + 0 = n := by simp  -- sk-1 task-2",
    "def f(sk): return sk-1 + ask-2",
    "AKIA is a prefix; AKIAshort",
    "Authorization: Bearer <token>",
    "Authorization: Bearer YOUR_TOKEN_HERE_PLEASE",
    "eyJ is how base64 json starts",
    "phone 12345678",
    "tel 02-2345-6789",
    "-----BEGIN CERTIFICATE-----",
    "ghp_short",
    "The patient was seen.",
    "",
]


@pytest.mark.parametrize("text", NEGATIVES, ids=[t[:30] or "empty" for t in NEGATIVES])
def test_true_negatives(text):
    assert detect_sensitive(text) == []


@pytest.mark.parametrize("value", [None, 123, b"sk-ant-xxxx", ["a"], {"a": 1}, 1.5])
def test_non_string_safe(value):
    assert detect_sensitive(value) == []  # type: ignore[arg-type]
    assert effective_class(None, "standard", value, True) == ("standard", [])  # type: ignore[arg-type]


# --------------------------------------------------- no matched text leaks


def test_finding_never_holds_text():
    secret = "sk-ant-" + "Zq7_" * 10
    ident = tw_id()
    text = f"{secret} {ident}"
    findings = detect_sensitive(text)
    assert len(findings) == 2
    for f in findings:
        assert isinstance(f, Finding)
        blob = repr(f) + str(f) + repr(f.__class__.__dataclass_fields__)
        assert secret not in blob and ident not in blob and "Zq7" not in blob
        assert set(vars(f) if hasattr(f, "__dict__") else {}) == set()
        assert {fld for fld in f.__dataclass_fields__} == {"kind", "start", "end"}
        assert isinstance(f.start, int) and isinstance(f.end, int)
    with pytest.raises(Exception):
        findings[0].kind = "x"  # frozen


def test_offsets_point_at_match():
    ident = tw_id()
    text = "前文 " + ident + " 後文"
    f = detect_sensitive(text)[0]
    assert text[f.start:f.end] == ident


# ------------------------------------------------------------ performance

ADVERSARIAL = [
    "sk-" * 350_000,
    "a" * 1_048_576,
    "1 " * 524_288,
    "1" * 1_048_576,
    "-".join(["1234"] * 262_144),
    "a@" * 524_288,
    "a." * 524_288 + "@x",
    "eyJ" + "a" * 1_048_000,
    "AKIA" * 262_144,
    "://" * 349_000,
    "x://a:" * 170_000,
    "DE89 " * 209_715,
    "+" + "1 " * 524_000,
    "patient " * 130_000,
    "A1" * 524_288,
    "\n" * 1_048_576,
    "-----BEGIN PRIVATE KEY-----" * 38_000,
    "Authorization: Bearer " + "a" * 1_040_000,
]


@pytest.mark.parametrize("payload", ADVERSARIAL, ids=[str(i) for i in range(len(ADVERSARIAL))])
def test_redos_one_megabyte(payload):
    # Processor time of this thread, not the wall clock: the bound is on the work the detector does,
    # and the wall clock also counts other test workers and whatever else the machine is running.
    start = time.thread_time()
    detect_sensitive(payload)
    elapsed = time.thread_time() - start
    assert elapsed < 1.0, f"{elapsed:.2f}s of processor time"


def test_scan_cap_head_and_tail():
    secret = "sk-ant-" + "Zq7_" * 10
    pad = "x" * (policy.MAX_SCAN_CHARS + policy.TAIL_SCAN_CHARS)
    middle = pad + " " + secret + " " + pad
    assert detect_sensitive(middle) == []  # beyond both windows: documented limit
    tail = pad + pad + " " + secret
    f = detect_sensitive(tail)
    assert [x.kind for x in f] == ["anthropic_key"]
    assert tail[f[0].start:f[0].end] == secret
    assert detect_sensitive(secret + " " + pad[:1000])[0].start == 0
    head = secret + " " + pad + pad
    assert detect_sensitive(head)[0].start == 0
