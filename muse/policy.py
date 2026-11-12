"""Privacy classes: which deployments may receive a request, and when to raise the class.

Why this exists: one person routes prompts through a local gateway to free tiers
(some train on inputs and allow human review), paid APIs and local models. The
class on a request decides which deployments may see it. The detector below only
ever RAISES a class, so it is tuned for precision over recall, validates
checksums where one exists, and never stores or returns matched text.

Classes, least to most restrictive:
  public    any deployment
  standard  any deployment (default; the user accepts provider terms)
  private   muse_trains == "no" and muse_human_review == "no", or a local model
  local     local models only
Missing or malformed model_info fails closed: cloud, trains unknown, review unknown.

Detector limits: only the first MAX_SCAN_CHARS and last TAIL_SCAN_CHARS characters
of a longer text are scanned (a linear-time bound, not a security boundary).
Contact items (email, phone) are flagged only with another finding kind or with
three or more contact items, so a single email address does not flip the class.
Left out on purpose: Taiwan NHI card numbers (12 digits, no public checksum or
distinguishing shape, so it would flag any 12-digit number) and medical record
numbers (format differs per hospital).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PRIVACY_CLASSES: tuple[str, ...] = ("public", "standard", "private", "local")

MAX_SCAN_CHARS = 200_000
TAIL_SCAN_CHARS = 50_000

_ALIASES = {
    "c0": "public",
    "c1": "standard",
    "c2": "standard",
    "c3": "private",
    "c4": "local",
}


class PolicyError(ValueError):
    """Raised for an unknown privacy class; a typo must never fall back silently."""


def normalise_class(value: str | None, default: str = "standard") -> str:
    """Return a canonical class name; None or empty gives `default`."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None or (isinstance(default, str) and not default.strip()):
            raise PolicyError("no privacy class given and no usable default")
        return normalise_class(default, default="standard")
    if not isinstance(value, str):
        raise PolicyError(f"privacy class must be a string, got {type(value).__name__}")
    key = value.strip().lower()
    key = _ALIASES.get(key, key)
    if key not in PRIVACY_CLASSES:
        raise PolicyError(
            f"unknown privacy class {value!r}; expected one of {', '.join(PRIVACY_CLASSES)}"
        )
    return key


def stricter(a: str, b: str) -> str:
    """The more restrictive of two classes."""
    a, b = normalise_class(a), normalise_class(b)
    return a if PRIVACY_CLASSES.index(a) >= PRIVACY_CLASSES.index(b) else b


# --------------------------------------------------------------------------
# Deployment filtering
# --------------------------------------------------------------------------


def _field(model_info: Any, key: str, default: str) -> str:
    """Read a string field; anything missing or not a string falls to `default`."""
    if not isinstance(model_info, Mapping):
        return default
    value = model_info.get(key)
    if not isinstance(value, str):
        return default
    return value.strip().lower() or default


def _facts(model_info: Any) -> tuple[str, str, str]:
    return (
        _field(model_info, "muse_kind", "cloud"),
        _field(model_info, "muse_trains", "unknown"),
        _field(model_info, "muse_human_review", "unknown"),
    )


def allowed(model_info: Mapping[str, Any], privacy: str) -> bool:
    """True if a deployment with this model_info may receive a `privacy` request."""
    cls = normalise_class(privacy)
    kind, trains, review = _facts(model_info)
    if cls in ("public", "standard"):
        return True
    if cls == "local":
        return kind == "local"
    return kind == "local" or (trains == "no" and review == "no")


def filter_deployments(deployments: list[dict], privacy: str) -> list[dict]:
    """Keep deployments allowed for `privacy`, in order, without mutating the input."""
    normalise_class(privacy)  # fail on a bad class even for an empty list
    out: list[dict] = []
    for dep in deployments:
        info = dep.get("model_info") if isinstance(dep, Mapping) else None
        if allowed(info if isinstance(info, Mapping) else {}, privacy):
            out.append(dep)
    return out


def explain(model_info: Mapping[str, Any], privacy: str) -> str:
    """One short sentence saying why a deployment is or is not allowed."""
    cls = normalise_class(privacy)
    kind, trains, review = _facts(model_info)
    name = _field(model_info, "provider", "this deployment")
    ok = allowed(model_info, cls)
    if cls in ("public", "standard"):
        return f"Allowed: {cls} requests may use any deployment."
    if cls == "local":
        if ok:
            return f"Allowed: {name} runs on this machine."
        return f"Blocked: {name} is a cloud provider and local requests stay on this machine."
    if kind == "local":
        return f"Allowed: {name} runs on this machine."
    if ok:
        return f"Allowed: {name} says it does not train on inputs or use human review."
    reasons = []
    if trains != "no":
        reasons.append("may train on inputs" if trains == "yes" else "training on inputs is unverified")
    if review != "no":
        reasons.append("allows human review" if review == "yes" else "human review is unverified")
    return f"Blocked: {name} {' and '.join(reasons)}; private requests need an explicit no for both."


# --------------------------------------------------------------------------
# Sensitive-content detector
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """Where something sensitive sits. Holds offsets only, never the matched text."""

    kind: str
    start: int
    end: int


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Taiwan: letter value as two digits (A=10 ... Z=33, with the official gaps).
_TW_LETTER = dict(zip("ABCDEFGHJKLMNPQRSTUVXYWZIO", range(10, 36)))


def _tw_valid(s: str) -> bool:
    """National ID, new ARC (second char 8/9) and old ARC (second char A-D)."""
    code = _TW_LETTER.get(s[0])
    if code is None:
        return False
    if s[1].isalpha():
        if s[1] not in "ABCD":
            return False
        mid = [_TW_LETTER[s[1]] % 10]
        weights = [1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1]
    else:
        mid = [int(s[1])]
        weights = [1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1]
    nums = [code // 10, code % 10] + mid + [int(c) for c in s[2:]]
    if len(nums) != len(weights):
        return False
    return sum(n * w for n, w in zip(nums, weights)) % 10 == 0


def _sg_valid(s: str) -> bool:
    total = sum(int(d) * w for d, w in zip(s[1:8], (2, 7, 6, 5, 4, 3, 2)))
    if s[0] in "TG":
        total += 4
    table = "JZIHGFEDCBA" if s[0] in "ST" else "XWUTRQPNMLK"
    return table[total % 11] == s[8]


def _ssn_valid(area: str, group: str, serial: str) -> bool:
    if area in ("000", "666") or area[0] == "9":
        return False
    return group != "00" and serial != "0000"


# (prefix test, allowed lengths) per card scheme; the prefix filter removes most
# Luhn coincidences such as epoch-millisecond timestamps (start with 1).
def _card_scheme(d: str) -> bool:
    n = len(d)
    p2, p3, p4 = int(d[:2]), int(d[:3]), int(d[:4])
    if d[0] == "4":
        return n in (13, 16, 19)
    if 51 <= p2 <= 55 or 2221 <= p4 <= 2720:
        return n == 16
    if p2 in (34, 37):
        return n == 15
    if p4 == 6011 or p2 == 65 or 644 <= p3 <= 649 or p2 == 62:
        return 16 <= n <= 19
    if 3528 <= p4 <= 3589:
        return 16 <= n <= 19
    if 300 <= p3 <= 305 or p2 in (36, 38):
        return n == 14
    return False


_IBAN_LEN = {
    "AT": 20, "BE": 16, "BG": 22, "CH": 21, "CY": 28, "CZ": 24, "DE": 22, "DK": 18,
    "EE": 20, "ES": 24, "FI": 18, "FR": 27, "GB": 22, "GR": 27, "HR": 21, "HU": 28,
    "IE": 22, "IL": 23, "IS": 26, "IT": 27, "LI": 21, "LT": 20, "LU": 20, "LV": 21,
    "MT": 31, "NL": 18, "NO": 15, "PL": 28, "PT": 25, "RO": 24, "SA": 24, "SE": 24,
    "SI": 19, "SK": 24, "TR": 26, "AE": 23,
}  # fmt: skip


def _iban_valid(s: str) -> bool:
    if _IBAN_LEN.get(s[:2]) != len(s):
        return False
    rearranged = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


_PLACEHOLDER_PW = {"password", "pass", "passwd", "pwd", "secret", "changeme", "xxxx", "****", "example"}

_RX = {
    "openai_key": re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
    "anthropic_key": re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{20,}"),
    "google_key": re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}"),
    "groq_key": re.compile(r"(?<![A-Za-z0-9_-])gsk_[A-Za-z0-9]{40,}"),
    "github_token": re.compile(
        r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"
    ),
    "aws_key_id": re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    "slack_token": re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}"),
    "stripe_key": re.compile(r"(?<![A-Za-z0-9_])[sr]k_live_[A-Za-z0-9]{16,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----"),
    "jwt": re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
    ),
    "bearer": re.compile(
        r"(?i)authorization[ \t]*:[ \t]*bearer[ \t]+([A-Za-z0-9._~+/=-]{16,})"
    ),
    "conn_string": re.compile(
        r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]{1,20}://[^\s:/@]{1,64}:([^\s@/]{3,128})@[^\s/]{1,255}"
    ),
    "tw_id": re.compile(r"(?<![A-Za-z0-9])[A-Z][12ABCD89]\d{8}(?![A-Za-z0-9])"),
    "sg_nric": re.compile(r"(?<![A-Za-z0-9])[STFG]\d{7}[A-Z](?![A-Za-z0-9])"),
    "us_ssn": re.compile(r"(?<![\w-])(\d{3})([- ])(\d{2})\2(\d{4})(?![\w-])"),
    "card": re.compile(r"(?<![\w-])\d(?:[ -]?\d){12,18}(?![\w-])"),
    "iban": re.compile(
        r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?(?![A-Za-z0-9])"
    ),
    "email": re.compile(
        r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,6}\.[A-Za-z]{2,24}(?![A-Za-z0-9-])"
    ),
    "tw_mobile": re.compile(r"(?<![\w+-])(?:\+886[- ]?9\d{2}|09\d{2})[- ]?\d{3}[- ]?\d{3}(?![\w-])"),
    "phone": re.compile(r"(?<![\w+.-])\+[1-9](?:[ -]?\d){7,14}(?![\w-])"),
}

_EXAMPLE_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "localhost", "test.com")

# Clinical-document keywords: each name counts once; two distinct names flag.
_HEALTH_MARKERS: dict[str, re.Pattern[str]] = {
    "zh_mrn": re.compile(r"病歷號(?:碼)?"),
    "zh_diagnosis": re.compile(r"診斷"),
    "zh_discharge": re.compile(r"出院病摘|出院摘要"),
    "zh_history": re.compile(r"主訴|現病史|過去病史"),
    "en_mrn": re.compile(r"(?i)\bmedical record number\b|\bMRN[ \t]*[:#]"),
    "en_dx": re.compile(r"(?i)\bdiagnosis[ \t]*:|\bdischarge summary\b|\bhistory of present illness\b|\bchief complaint\b"),
    "en_patient_dob": re.compile(
        r"(?is)\bpatient\b.{0,80}?\b(?:date of birth|dob)\b|\b(?:date of birth|dob)\b.{0,80}?\bpatient\b"
    ),
}

_CONTACT_KINDS = frozenset({"email", "tw_mobile", "phone"})
_MIN_CONTACT_ONLY = 3


def _scan(text: str, base: int, out: list[Finding]) -> None:
    """Run every detector over one segment; offsets are shifted by `base`."""

    def add(kind: str, m_start: int, m_end: int) -> None:
        out.append(Finding(kind, base + m_start, base + m_end))

    for kind in ("anthropic_key", "openai_key", "google_key", "groq_key", "github_token",
                 "aws_key_id", "slack_token", "stripe_key"):
        for m in _RX[kind].finditer(text):
            tok = m.group(0)
            if kind == "openai_key" and not (
                any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok[3:])
            ):
                continue  # "sk-learn-style-words": real keys always carry digits
            add(kind, m.start(), m.end())
    for m in _RX["private_key"].finditer(text):
        end = text.find("-----END", m.end())
        close = text.find("-----", end + 8) if end != -1 else -1
        add("private_key", m.start(), close + 5 if close != -1 else m.end())
    for m in _RX["jwt"].finditer(text):
        add("jwt", m.start(), m.end())
    for m in _RX["bearer"].finditer(text):
        tok = m.group(1)
        if any(c.isdigit() for c in tok):
            add("bearer", m.start(), m.end())
    for m in _RX["conn_string"].finditer(text):
        pw = m.group(1)
        if pw.lower() in _PLACEHOLDER_PW or pw[0] in "<${[*" or set(pw) <= set("xX*"):
            continue
        add("conn_string", m.start(), m.end())

    for m in _RX["tw_id"].finditer(text):
        if _tw_valid(m.group(0)):
            add("tw_id", m.start(), m.end())
    for m in _RX["sg_nric"].finditer(text):
        if _sg_valid(m.group(0)):
            add("sg_nric", m.start(), m.end())
    for m in _RX["us_ssn"].finditer(text):
        if _ssn_valid(m.group(1), m.group(3), m.group(4)):
            add("us_ssn", m.start(), m.end())
    for m in _RX["card"].finditer(text):
        digits = re.sub(r"[ -]", "", m.group(0))
        if (13 <= len(digits) <= 19 and len(set(digits)) > 1
                and _card_scheme(digits) and _luhn(digits)):
            add("credit_card", m.start(), m.end())
    for m in _RX["iban"].finditer(text):
        tokens = m.group(0).split(" ")
        for k in range(len(tokens), 0, -1):  # a trailing word may have been swallowed
            cand = "".join(tokens[:k])
            if _iban_valid(cand):
                add("iban", m.start(), m.start() + len(" ".join(tokens[:k])))
                break

    hits = {name for name, rx in _HEALTH_MARKERS.items() if rx.search(text)}
    if len(hits) >= 2:
        first = min(
            (m for name in hits if (m := _HEALTH_MARKERS[name].search(text))),
            key=lambda m: m.start(),
        )
        add("clinical_document", first.start(), first.end())

    for m in _RX["email"].finditer(text):
        domain = m.group(0).rsplit("@", 1)[1].lower()
        if domain.endswith(_EXAMPLE_EMAIL_DOMAINS):
            continue
        add("email", m.start(), m.end())
    for m in _RX["tw_mobile"].finditer(text):
        add("tw_mobile", m.start(), m.end())
    for m in _RX["phone"].finditer(text):
        add("phone", m.start(), m.end())


def detect_sensitive(text: str) -> list[Finding]:
    """Find secrets, checksummed IDs, clinical markers and contact clusters.

    Returns offsets only. Non-string input yields []. Overlaps resolve in
    detector priority order (secrets, IDs, health, contact); contact items are
    dropped unless another kind is present or there are three or more of them.
    """
    if not isinstance(text, str) or not text:
        return []
    raw: list[Finding] = []
    if len(text) <= MAX_SCAN_CHARS + TAIL_SCAN_CHARS:
        _scan(text, 0, raw)
    else:
        _scan(text[:MAX_SCAN_CHARS], 0, raw)
        tail_start = len(text) - TAIL_SCAN_CHARS
        _scan(text[tail_start:], tail_start, raw)

    accepted: list[Finding] = []
    for f in raw:  # raw is already in priority order
        if not any(f.start < a.end and a.start < f.end for a in accepted if a.kind != "clinical_document"):
            accepted.append(f)
    contacts = [f for f in accepted if f.kind in _CONTACT_KINDS]
    others = [f for f in accepted if f.kind not in _CONTACT_KINDS]
    if contacts and not others and len(contacts) < _MIN_CONTACT_ONLY:
        accepted = []
    return sorted(accepted, key=lambda f: (f.start, f.end, f.kind))


def effective_class(
    requested: str | None, default: str, text: str, auto: bool
) -> tuple[str, list[Finding]]:
    """Resolve the class, raising it to at least private when `auto` finds something."""
    resolved = normalise_class(requested, default)
    findings = detect_sensitive(text)
    if auto and findings:
        resolved = stricter(resolved, "private")
    return resolved, findings
