"""Conformance vectors for records: the cases Python and Rust must answer alike.

scripts/export_conformance.py writes `build()` to core/conformance/records.json;
core/tests/records_conformance.rs replays the file against the Rust core, and
tests/test_records_vectors.py replays it against this package.

Vector: {"name", "fn", "input", "expected"}; `expected` is {"ok": value} or
{"err": {"kind", "index"}} (a RecordError). `call(fn, input)` below is the
meaning of each `fn`.

THE KEYS IN THIS FILE ARE TEST-ONLY. `test_only_seed` and `test_only_ref_key`
are fixed public byte strings (0x00..0x1f and so on); nothing real may ever be
signed with them.

Everything is deterministic: no clock, no randomness (the "random" cases use
SHA-256 of a counter).
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from wardmuse.records import format as fmt
from wardmuse.records import jcs, merkle, seal
from wardmuse.records.errors import RecordError

SEED_A = bytes(range(32)).hex()
SEED_B = bytes(range(32, 64)).hex()
REF_KEY = bytes(range(64, 96)).hex()
REF_KEY_2 = bytes(range(96, 128)).hex()
T0 = 1_794_000_000_000  # milliseconds; 2026-11-06T21:20:00Z
L_ORDER = 2**252 + 27742317777372353535851937790883648493


def test_leaf(i: int) -> str:
    """The stand-in record hash number i used by the Merkle vectors."""
    return hashlib.sha256(f"muse records test leaf {i}".encode("ascii")).hexdigest()


def test_leaves(n: int) -> list[str]:
    return [test_leaf(i) for i in range(n)]


# ---------------------------------------------------------------- what each fn means


def call(fn: str, i: dict[str, Any]) -> Any:
    """Run one vector's function on its input. Returns the value, or raises RecordError."""
    b = bytes.fromhex
    if fn == "canonicalize":
        return jcs.canonicalize(i["value"]).decode("utf-8")
    if fn == "loads_strict":
        return jcs.canonicalize(jcs.loads_strict(i["text"])).decode("utf-8")
    if fn == "key_shaped":
        return fmt.key_shaped(i["text"])
    if fn == "check_summary":
        return fmt.check_summary(i["summary"])
    if fn == "resource_ref":
        return fmt.resource_ref(b(i["test_only_ref_key"]), i["resource"])
    if fn == "summary_salt":
        return fmt.summary_salt(b(i["test_only_ref_key"]), i["device"], i["seq"])
    if fn == "summary_hash":
        return fmt.summary_hash(i["salt"], i["summary"])
    if fn == "genesis":
        return fmt.genesis(i["device"])
    if fn == "append":
        return fmt.append(i["head"], i["entry"], b(i["test_only_seed"]), b(i["test_only_ref_key"]))
    if fn == "canonical_bytes":
        fmt.check_record(i["record"])
        return fmt.canonical_bytes(i["record"], i["with_sig"]).decode("utf-8")
    if fn == "hash":
        fmt.check_record(i["record"])
        return fmt.record_hash(i["record"])
    if fn == "verify_record":
        return fmt.verify_record(i["record"], i["device"])
    if fn == "verify_signature":
        return fmt._verify_sig(i["device"], b(i["message"]), i["sig"])
    if fn == "verify_chain":
        return fmt.verify_chain(i["records"], i["device"])
    if fn == "redact":
        fmt.check_record(i["record"])
        return fmt.redact(i["record"])
    if fn == "merkle_root":
        return merkle.merkle_root(test_leaves(i["n"]))
    if fn == "inclusion_proof":
        return merkle.inclusion_proof(test_leaves(i["n"]), i["index"])
    if fn == "verify_inclusion":
        return merkle.verify_inclusion(i["hash"], i["index"], i["size"], i["proof"], i["root"])
    if fn == "consistency_proof":
        return merkle.consistency_proof(test_leaves(i["n"]), i["old_size"])
    if fn == "verify_consistency":
        return merkle.verify_consistency(i["old_size"], i["new_size"], i["old_root"], i["new_root"], i["proof"])
    if fn == "checkpoint":
        return seal.checkpoint(test_leaves(i["n"]), i["time"], b(i["test_only_seed"]))
    if fn == "verify_checkpoint":
        return seal.verify_checkpoint(i["checkpoint"], i["device"])
    if fn == "export":
        return seal.export(i["records"], i["checkpoint"], i["select"], i["redact_summaries"])
    if fn == "export_text":
        return seal.export_text(i["export"])
    if fn == "verify_export":
        return seal.verify_export(i["export"], i["device"])
    if fn == "verify_export_text":
        return seal.verify_export_text(i["text"], i["device"])
    raise KeyError(fn)


FUNCTIONS = (
    "canonicalize", "loads_strict", "key_shaped", "check_summary", "resource_ref", "summary_salt", "summary_hash",
    "genesis", "append", "canonical_bytes", "hash", "verify_record", "verify_signature", "verify_chain", "redact",
    "merkle_root", "inclusion_proof", "verify_inclusion", "consistency_proof", "verify_consistency", "checkpoint",
    "verify_checkpoint", "export", "export_text", "verify_export", "verify_export_text",
)


def outcome(fn: str, inp: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"ok": call(fn, inp)}
    except RecordError as exc:
        return {"err": {"kind": exc.kind, "index": exc.index}}


class _Vectors:
    def __init__(self) -> None:
        self.vectors: list[dict[str, Any]] = []
        self._names: set[str] = set()

    def add(self, fn: str, label: str, inp: dict[str, Any], want: str | None = None) -> dict[str, Any]:
        """Record fn(inp). `want` pins what the author expects: "ok" or an error kind (a check on the vectors themselves)."""
        name = f"{fn}: {label}"
        if name in self._names:
            raise SystemExit(f"duplicate vector name in records: {name}")
        self._names.add(name)
        inp = copy.deepcopy(inp)
        expected = outcome(fn, inp)
        if want is not None:
            got = "ok" if "ok" in expected else expected["err"]["kind"]
            if got != want:
                raise SystemExit(f"records vector {name!r}: expected {want}, Python says {expected}")
        self.vectors.append({"name": name, "fn": fn, "input": inp, "expected": expected})
        return expected


# ---------------------------------------------------------------- building blocks


def _signer(seed_hex: str) -> fmt.SeedSigner:
    return fmt.SeedSigner(bytes.fromhex(seed_hex))


DEVICE_A = _signer(SEED_A).device
DEVICE_B = _signer(SEED_B).device

ENTRIES: list[dict[str, Any]] = [
    {"time": T0, "actor": "person", "action": "mandate.issue", "outcome": "ok", "summary": "Allowed the helper to tidy the Notes folder",
     "resource": "mandate:m-2026-11-001"},
    {"time": T0 + 1_500, "actor": "helper", "action": "files.write", "outcome": "ok", "mandate": "m-2026-11-001",
     "summary": "寫入「週報.md」（繁體中文摘要）", "resource": "file:/Notes/週報.md", "links": {"job": "job-17", "card": "card-3"}},
    {"time": T0 + 2_000, "actor": "helper", "action": "llm.call", "outcome": "ok", "mandate": "m-2026-11-001",
     "summary": "Asked Gemini to summarise 3 notes \U0001f4dd", "cost": {"currency": "TWD", "minor": 30}, "links": {"job": "job-17"}},
    {"time": T0 + 2_000, "actor": "helper", "action": "pay.spend", "outcome": "declined", "mandate": "m-2026-11-001",
     "summary": "Payment of NT$30 declined: over today's limit", "cost": {"currency": "TWD", "minor": 3000},
     "resource": "payee:api.example.com", "links": {"payment_ref": "0x5f2c9a", "job": "job-17"}},
    {"time": T0 + 61_000, "actor": "person", "action": "card.answer", "outcome": "ok", "summary": "Approved: \"send the draft\" \\ café",
     "links": {"card": "card-4"}},
    {"time": T0 + 60_000, "actor": "system", "action": "home.pause", "outcome": "ok", "summary": "Paused the queue"},
    {"time": T0 + 90_000, "actor": "helper", "action": "files.write", "outcome": "failed", "mandate": "m-2026-11-001",
     "summary": "Could not write 한국어 メモ.txt: disk full", "resource": "file:/Notes/メモ.txt"},
    {"time": T0 + 120_000, "actor": "person", "action": "mandate.revoke", "outcome": "ok", "summary": "Stopped the Notes permission",
     "resource": "mandate:m-2026-11-001"},
]


def make_chain(entries: list[dict[str, Any]], seed_hex: str = SEED_A, ref_hex: str = REF_KEY) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    head = None
    for entry in entries:
        head = fmt.append(head, entry, bytes.fromhex(seed_hex), bytes.fromhex(ref_hex))
        out.append(head)
    return out


def resign(record: dict[str, Any], seed_hex: str = SEED_A, **changes: Any) -> dict[str, Any]:
    """A validly signed copy of `record` with some fields changed (what a device signing a second history would make)."""
    changed = {k: v for k, v in record.items() if k != "sig"}
    changed.update(changes)
    return fmt.sign(changed, _signer(seed_hex))


def relink(records: list[dict[str, Any]], start: int, seed_hex: str = SEED_A) -> list[dict[str, Any]]:
    """Re-sign records[start:] so each links to the one before it (after an edit earlier in the chain)."""
    out = [dict(r) for r in records]
    for i in range(start, len(out)):
        prev = fmt.genesis(out[i]["device"]) if i == 0 else fmt.record_hash(out[i - 1])
        out[i] = resign(out[i], seed_hex, prev=prev)
    return out


def flip_hex(text: str, position: int = 0) -> str:
    digit = text[position]
    return text[:position] + ("0" if digit != "0" else "1") + text[position + 1:]


def many_entries(n: int) -> list[dict[str, Any]]:
    """n entries with varied fields, derived from SHA-256 of a counter."""
    actions = ["files.write", "files.read", "llm.call", "pay.spend", "mandate.issue", "mandate.revoke", "card.answer", "home.pause", "home.resume"]
    words = ["Wrote", "讀取", "Asked", "Paid", "已允許", "Stopped", "Answered", "Paused", "Résumé"]
    out = []
    t = T0
    for i in range(n):
        h = hashlib.sha256(f"entry {i}".encode()).digest()
        t += h[0] * 997
        entry: dict[str, Any] = {"time": t, "actor": fmt.ACTORS[h[1] % 3], "action": actions[h[2] % len(actions)],
                                 "outcome": fmt.OUTCOMES[h[3] % 4], "summary": f"{words[h[2] % len(words)]} item {i} \u00e9\u4e2d"}
        if h[4] % 2:
            entry["mandate"] = f"m-{h[5]:03d}"
        if h[6] % 3:
            entry["resource"] = f"file:/docs/{h[7]}/檔案-{i}.txt"
        if h[8] % 4 == 0:
            entry["cost"] = {"currency": ["TWD", "USD", "USDC", "JPY"][h[9] % 4], "minor": int.from_bytes(h[10:14], "big")}
        if h[14] % 3 == 0:
            entry["links"] = {"job": f"job-{h[15]}"}
        out.append(entry)
    return out


# ---------------------------------------------------------------- the areas


def _jcs_vectors(v: _Vectors) -> None:
    add = lambda label, value, want=None: v.add("canonicalize", label, {"value": value}, want)  # noqa: E731
    add("null", None, "ok")
    add("true", True, "ok")
    add("false", False, "ok")
    add("zero", 0, "ok")
    add("empty string", "", "ok")
    add("empty array", [], "ok")
    add("empty object", {}, "ok")
    for n in (1, -1, 10, 255, 2**31 - 1, 2**31, 2**32, 2**53 - 1, -(2**53 - 1), 999_999_999_999_999, 1_000_000_000_000_000,
              1_794_000_000_000):
        add(f"integer {n}", n, "ok")
    for n in (2**53, -(2**53), 2**53 + 1, 2**63 - 1, 2**63, -(2**63), 2**64 - 1, 2**64, -(2**63) - 1, 10**30):
        add(f"integer out of range {n}", n, "malformed")
    for x in (0.5, 1.0, -0.0, 1e21, 1e-7, 0.1, 1.5e300, 9007199254740993.0):
        add(f"float {x!r}", x, "malformed")
    add("float inside", {"a": [1, 2, {"b": 0.25}]}, "malformed")
    add("key order: ascii", {"b": 1, "a": 2, "B": 3, "A": 4, "_": 5, "1": 6, "10": 7, "9": 8, "": 9, " ": 10, "aa": 11}, "ok")
    add("key order: utf-16 code units, not code points",
        {"\U0001f600": 1, "\uffff": 2, "\ue000": 3, "\ud7ff": 4, "\U00010000": 5, "z": 6, "\u00e9": 7, "e\u0301": 8, "\u4e2d": 9, "\uff5e": 10}, "ok")
    add("key order: RFC 8785 section 3.2.3 example",
        {"\u20ac": "Euro Sign", "\r": "Carriage Return", "\ufb33": "Hebrew Letter Dalet With Dagesh", "1": "One",
         "\U0001f600": "Emoji: Grinning Face", "\u0080": "Control", "\u00f6": "Latin Small Letter O With Diaeresis"}, "ok")
    add("key order: prefixes", {"ab": 1, "a": 2, "abc": 3, "a\u0000": 4, "a ": 5}, "ok")
    add("keys needing escapes", {"\"": 1, "\\": 2, "\n": 3, "\u0000": 4, "/": 5, "\u007f": 6}, "ok")
    add("string: quote and backslash", "a \"quoted\" \\ back/slash", "ok")
    add("string: short escapes", "\b\t\n\f\r", "ok")
    add("string: every control character", "".join(chr(c) for c in range(0x20)), "ok")
    add("string: DEL and C1 are literal", "\x7f\x80\x9f", "ok")
    add("string: line separators are literal", "\u2028\u2029\u0085", "ok")
    add("string: zh-TW", "\u7e41\u9ad4\u4e2d\u6587\uff1a\u5df2\u5beb\u5165\u300c\u9031\u5831\u300d", "ok")
    add("string: combining marks stay as given", "e\u0301 vs \u00e9, \u1e69 vs s\u0323\u0307", "ok")
    add("string: emoji and ZWJ", "\U0001f600 \U0001f468\u200d\U0001f469\u200d\U0001f467 \u2764\ufe0f", "ok")
    add("string: astral and specials", "\U00010000 \U0010ffff \ufffd \uffff \ufeff", "ok")
    add("string: text that looks like escapes", "\\u00e9 \\n \\\"", "ok")
    add("string: html-ish is not escaped", "<script>&amp;</script>", "ok")
    add("nested", {"z": [1, [2, [3, {"y": None, "x": [True, False]}]]], "a": {"c": {}, "b": []}}, "ok")
    add("array order is kept", [3, 1, 2, "b", "a", None], "ok")
    deep: Any = 0
    for _ in range(32):
        deep = [deep]
    add("depth 32 is allowed", deep, "ok")
    add("depth 33 is refused", [deep], "malformed")
    add("a record-like object", {"v": 1, "seq": 0, "time": T0, "mandate": None, "actor": "person", "cost": {"minor": 30, "currency": "TWD"}}, "ok")

    text = lambda label, raw, want=None: v.add("loads_strict", label, {"text": raw}, want)  # noqa: E731
    text("plain", '{"b": 1, "a": [true, false, null, "x"]}', "ok")
    text("whitespace everywhere", ' \t\r\n{ "a" :\n[ 1 , 2 ] }\n ', "ok")
    text("escapes", '["\\u00e9\\n\\/\\b\\f\\r\\t\\"\\\\", "\\ud83d\\ude00", "\\u0000"]', "ok")
    text("escaped key equals plain key", '{"\\u0061": 1, "b": 2}', "ok")
    text("repeated key", '{"a": 1, "a": 2}', "malformed")
    text("repeated key through an escape", '{"a": 1, "\\u0061": 2}', "malformed")
    text("repeated key, nested", '{"x": [{"k": 1, "k": 1}]}', "malformed")
    text("fraction", '{"a": 1.0}', "malformed")
    text("exponent", '{"a": 1e3}', "malformed")
    text("capital exponent", '[1E2]', "malformed")
    text("minus zero", '[-0]', "malformed")
    text("minus zero float", '[-0.0]', "malformed")
    text("NaN", '[NaN]', "malformed")
    text("Infinity", '[Infinity]', "malformed")
    text("largest integer", '[9007199254740991, -9007199254740991]', "ok")
    text("integer one past", '[9007199254740992]', "malformed")
    text("negative one past", '[-9007199254740992]', "malformed")
    text("integer above u64", '[18446744073709551616]', "malformed")
    text("40-digit integer", '[1234567890123456789012345678901234567890]', "malformed")
    text("leading zero", '[01]', "malformed")
    text("plus sign", '[+1]', "malformed")
    text("lone high surrogate", '["\\ud83d"]', "malformed")
    text("lone low surrogate", '["\\ude00"]', "malformed")
    text("lone surrogate in a key", '{"\\ud800": 1}', "malformed")
    text("reversed surrogates", '["\\ude00\\ud83d"]', "malformed")
    text("byte-order mark", '\ufeff{"a": 1}', "malformed")
    text("trailing data", '{"a": 1} x', "malformed")
    text("two values", '{"a": 1}{"a": 1}', "malformed")
    text("trailing comma", '[1, 2,]', "malformed")
    text("single quotes", "{'a': 1}", "malformed")
    text("comment", '[1] // x', "malformed")
    text("raw control character in a string", '["a\nb"]', "malformed")
    text("empty", '', "malformed")
    text("only whitespace", '  ', "malformed")
    text("bare word", 'nul', "malformed")
    text("unterminated", '{"a": [1, 2', "malformed")
    text("scalar at the top", '7', "ok")
    text("nesting 40 deep", "[" * 40 + "]" * 40, "malformed")
    text("nesting 200 deep", "[" * 200 + "]" * 200, "malformed")


def _text_vectors(v: _Vectors) -> None:
    ks = lambda label, t, want: v.add("key_shaped", label, {"text": t}, "ok")["ok"] is want or _fail(label)  # noqa: E731
    ks("plain sentence", "Wrote the weekly report to Notes", False)
    ks("zh-TW sentence", "\u5df2\u5c07\u9031\u5831\u5beb\u5165\u300c\u7b46\u8a18\u300d\u8cc7\u6599\u593e", False)
    ks("a path with a date", "Wrote /home/user/Documents/2026-budget-notes-final.md", False)
    ks("a uuid", "Job 123e4567-e89b-12d3-a456-426614174000 finished", False)
    ks("short hex", "commit 9fceb02d0ae598e95dc970b74767f19372d61af8"[:20], False)
    ks("word with the letters sk-", "task-management and risk-assessment-procedures-2026", False)
    ks("31 mixed characters", "a1" * 15 + "a", False)
    ks("32 mixed characters", "a1" * 16, True)
    ks("32 letters only", "a" * 32, False)
    ks("32 digits only", "1" * 32, False)
    ks("sha-256 hex", hashlib.sha256(b"x").hexdigest(), True)
    ks("base64 run", "token dGhpcyBpcyBhIHRlc3Qgb25seSBzZWNyZXQgMTIzNDU2Nzg5MA== end", True)
    ks("hyphens split a long run", "abcd1234-abcd1234-abcd1234-abcd1234-abcd1234", False)
    ks("openai-style", "key sk-test0000000000000000 leaked", True)
    ks("prefix, 19 characters", "sk-0123456789abcdef", False)
    ks("prefix, 20 characters", "sk-0123456789abcdefg", True)
    ks("prefix must start the token", "disk-0123456789abcdefg", False)
    ks("prefix after punctuation", "(sk-0123456789abcdefg)", True)
    ks("github token", "ghp_0123456789abcdefghij", True)
    ks("github fine-grained", "github_pat_0123456789", True)
    ks("slack token", "xoxb-1234-5678-abcdefgh", True)
    ks("aws access key id", "AKIAIOSFODNN7EXAMPLE", True)
    ks("google api key", "AIzaSyTESTONLY0123456789abcdefghijklm", True)
    ks("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2ln", True)
    ks("prefix in lower case does not count", "akiaiosfodnn7example", False)
    ks("pem header", "-----BEGIN PRIVATE KEY-----", True)
    ks("pem header inside text", "pasted -----BEGIN OPENSSH", True)
    ks("four dashes", "----BEGIN", False)
    ks("full-width letters are not token characters", "\uff41\uff11" * 20, False)
    ks("empty", "", False)

    cs = lambda label, s, want: v.add("check_summary", label, {"summary": s}, want)  # noqa: E731
    cs("ascii", "Wrote the weekly report", "ok")
    cs("zh-TW", "\u5df2\u5c07\u300c\u9031\u5831\u300d\u5beb\u5165\u7b46\u8a18\uff0c\u82b1\u8cbb NT$30", "ok")
    cs("nfd becomes nfc", "Cafe\u0301 re\u0301sume\u0301", "ok")
    cs("already nfc", "Caf\u00e9 r\u00e9sum\u00e9", "ok")
    cs("two combining marks reorder", "s\u0307\u0323 and q\u0323\u0307", "ok")
    cs("hangul jamo compose", "\u1112\u1161\u11ab\u1100\u116e\u11a8\u110b\u1165", "ok")
    cs("kana with combining dakuten", "\u304b\u3099\u30cf\u309a", "ok")
    cs("singletons: ohm, angstrom, kelvin", "\u2126 \u212b \u212a", "ok")
    cs("compatibility characters are kept (NFC, not NFKC)", "\ufb01 \u2460 \uff21 \u00b2", "ok")
    cs("composition exclusion stays decomposed", "\u0958 \u095c \u05e9\u05c1", "ok")
    cs("emoji, ZWJ sequence, variation selector, flag", "\U0001f468\u200d\U0001f469\u200d\U0001f467 \u2764\ufe0f \U0001f1f9\U0001f1fc \U0001f44d\U0001f3fd", "ok")
    cs("quotes, backslash, slash", "Said \"hello\" \\ and / then 'bye'", "ok")
    cs("rtl text", "\u0645\u0631\u062d\u0628\u0627 \u05e9\u05dc\u05d5\u05dd", "ok")
    cs("one character", "x", "ok")
    cs("200 characters", "a" * 200, "ok")
    cs("201 characters", "a" * 201, "summary")
    cs("200 astral characters", "\U0001f600" * 200, "ok")
    cs("201 astral characters", "\U0001f600" * 201, "summary")
    cs("300 code points that compose to 150", "e\u0301" * 150, "ok")
    cs("402 code points that compose to 201", "e\u0301" * 201, "summary")
    cs("empty", "", "summary")
    cs("spaces only are allowed", "   ", "ok")
    for code in (0x00, 0x09, 0x0A, 0x0D, 0x1B, 0x1F, 0x7F, 0x80, 0x85, 0x9F, 0x2028, 0x2029, 0x202A, 0x202E, 0x2066, 0x2069):
        cs(f"refused character U+{code:04X}", f"a{chr(code)}b", "summary")
    for code in (0x20, 0xA0, 0xAD, 0x200B, 0x200D, 0x200E, 0x200F, 0x2027, 0x202F, 0x2065, 0x206A, 0xFEFF, 0xFFFD, 0xE000, 0x10FFFF):
        cs(f"allowed character U+{code:04X}", f"a{chr(code)}b", "ok")
    cs("holds an api key", "Used key sk-test0000000000000000 for the call", "summary")
    cs("holds a hash", "Wrote " + hashlib.sha256(b"y").hexdigest(), "summary")
    cs("holds a pem header", "-----BEGIN RSA PRIVATE KEY-----", "summary")
    cs("not text: number", 7, "summary")
    cs("not text: null", None, "summary")
    cs("not text: list", ["a"], "summary")

    for label, key in (("key 1", REF_KEY), ("key 2", REF_KEY_2)):
        for rlabel, res in (("ascii", "file:/Notes/report.md"), ("zh-TW", "file:/\u7b46\u8a18/\u9031\u5831.md"), ("nfd", "file:/Cafe\u0301.txt"),
                            ("nfc", "file:/Caf\u00e9.txt"), ("emoji", "contact:\U0001f469\u200d\U0001f4bb"), ("one character", "x"),
                            ("1024 characters", "r" * 1024), ("control characters are data here", "a\nb\u0000c")):
            v.add("resource_ref", f"{label}, {rlabel}", {"test_only_ref_key": key, "resource": res}, "ok")
    v.add("resource_ref", "empty", {"test_only_ref_key": REF_KEY, "resource": ""}, "malformed")
    v.add("resource_ref", "1025 characters", {"test_only_ref_key": REF_KEY, "resource": "r" * 1025}, "malformed")
    v.add("resource_ref", "not text", {"test_only_ref_key": REF_KEY, "resource": 5}, "malformed")

    for seq in (0, 1, 9, 10, 2**31, 2**53 - 1):
        v.add("summary_salt", f"device A seq {seq}", {"test_only_ref_key": REF_KEY, "device": DEVICE_A, "seq": seq}, "ok")
    v.add("summary_salt", "device B seq 0", {"test_only_ref_key": REF_KEY, "device": DEVICE_B, "seq": 0}, "ok")
    v.add("summary_salt", "other key", {"test_only_ref_key": REF_KEY_2, "device": DEVICE_A, "seq": 0}, "ok")
    v.add("summary_salt", "seq out of range", {"test_only_ref_key": REF_KEY, "device": DEVICE_A, "seq": 2**53}, "malformed")
    v.add("summary_salt", "negative seq", {"test_only_ref_key": REF_KEY, "device": DEVICE_A, "seq": -1}, "malformed")
    v.add("summary_salt", "upper-case device", {"test_only_ref_key": REF_KEY, "device": DEVICE_A.upper(), "seq": 0}, "malformed")

    salt = "00112233445566778899aabbccddeeff"
    for label, s in (("ascii", "Wrote the weekly report"), ("zh-TW", "\u5df2\u5beb\u5165\u9031\u5831"), ("empty text", ""),
                     ("not normalised (hashed as given)", "Cafe\u0301"), ("normalised", "Caf\u00e9"), ("emoji", "\U0001f600")):
        v.add("summary_hash", label, {"salt": salt, "summary": s}, "ok")
    v.add("summary_hash", "another salt", {"salt": "ff" * 16, "summary": "Wrote the weekly report"}, "ok")
    v.add("summary_hash", "short salt", {"salt": "ff" * 15, "summary": "x"}, "malformed")
    v.add("summary_hash", "upper-case salt", {"salt": "FF" * 16, "summary": "x"}, "malformed")

    v.add("genesis", "device A", {"device": DEVICE_A}, "ok")
    v.add("genesis", "device B", {"device": DEVICE_B}, "ok")
    v.add("genesis", "all zero", {"device": "00" * 32}, "ok")
    v.add("genesis", "short", {"device": "00" * 31}, "malformed")
    v.add("genesis", "upper case", {"device": DEVICE_A.upper()}, "malformed")


def _fail(label: str) -> bool:
    raise SystemExit(f"records vector key_shaped {label!r}: not what the author expected")


def _append_vectors(v: _Vectors) -> list[dict[str, Any]]:
    """The worked chain, one append at a time; then entry rules and boundaries. Returns the chain."""
    chain: list[dict[str, Any]] = []
    head = None
    for i, entry in enumerate(ENTRIES):
        got = v.add("append", f"chain A record {i} ({entry['action']})",
                    {"head": head, "entry": entry, "test_only_seed": SEED_A, "test_only_ref_key": REF_KEY}, "ok")
        head = got["ok"]
        chain.append(head)
    base = {"time": T0 + 200_000, "actor": "helper", "action": "files.write", "outcome": "ok", "summary": "Wrote a file"}

    def ap(label: str, entry: dict[str, Any], want: str, head: Any = chain[-1], seed: str = SEED_A, key: str = REF_KEY) -> None:
        v.add("append", label, {"head": head, "entry": entry, "test_only_seed": seed, "test_only_ref_key": key}, want)

    ap("first record of device B", base, "ok", head=None, seed=SEED_B)
    ap("another reference key changes resource and salt", {**base, "resource": "file:/a"}, "ok", key=REF_KEY_2)
    ap("summary is normalised before signing", {**base, "summary": "Cafe\u0301 re\u0301sume\u0301 \u1112\u1161\u11ab"}, "ok")
    ap("resource is normalised before the reference", {**base, "resource": "file:/Cafe\u0301.txt"}, "ok")
    ap("resource already normalised gives the same reference", {**base, "resource": "file:/Caf\u00e9.txt"}, "ok")
    ap("explicit nulls", {**base, "mandate": None, "resource": None, "cost": None}, "ok")
    ap("all four links", {**base, "links": {"job": "j1", "card": "c1", "undo_of": fmt.record_hash(chain[1]), "payment_ref": "0xabc"}}, "ok")
    ap("an undo", {**base, "outcome": "undone", "summary": "Undid: wrote 週報.md", "links": {"undo_of": fmt.record_hash(chain[1])}}, "ok")
    ap("emoji and quotes in the summary", {**base, "summary": "\"Done\" \U0001f44d\U0001f3fd \\o/"}, "ok")
    ap("time equal to the head's", {**base, "time": chain[-1]["time"]}, "ok")
    ap("time earlier, inside the tolerance", {**base, "time": chain[-1]["time"] - fmt.TIME_TOLERANCE_MS}, "ok")
    ap("time earlier, one past the tolerance", {**base, "time": chain[-1]["time"] - fmt.TIME_TOLERANCE_MS - 1}, "time_went_back")
    ap("largest time", {**base, "time": 2**53 - 1}, "ok")
    ap("time too large", {**base, "time": 2**53}, "malformed")
    ap("time zero on a first record", {**base, "time": 0}, "ok", head=None)
    ap("negative time", {**base, "time": -1}, "malformed", head=None)
    ap("time as a float", {**base, "time": 1794000000000.0}, "malformed")
    ap("time as text", {**base, "time": "1794000000000"}, "malformed")
    ap("time as a boolean", {**base, "time": True}, "malformed")
    ap("largest cost", {**base, "cost": {"currency": "USDC", "minor": 2**53 - 1}}, "ok")
    ap("zero cost", {**base, "cost": {"currency": "JPY", "minor": 0}}, "ok")
    ap("cost too large", {**base, "cost": {"currency": "TWD", "minor": 2**53}}, "malformed")
    ap("negative cost", {**base, "cost": {"currency": "TWD", "minor": -1}}, "malformed")
    ap("fractional cost", {**base, "cost": {"currency": "TWD", "minor": 0.5}}, "malformed")
    ap("cost with an extra field", {**base, "cost": {"currency": "TWD", "minor": 1, "exp": 2}}, "malformed")
    ap("cost without currency", {**base, "cost": {"minor": 1}}, "malformed")
    ap("lower-case currency", {**base, "cost": {"currency": "twd", "minor": 1}}, "malformed")
    ap("two-letter currency", {**base, "cost": {"currency": "NT", "minor": 1}}, "malformed")
    ap("cost as a number", {**base, "cost": 30}, "malformed")
    for action in ("home.pause", "a.b", "files.write_text", "pay.spend.refund.partial", "x1.y2"):
        ap(f"action {action}", {**base, "action": action}, "ok")
    for action in ("pause", "Home.pause", "home..pause", ".home", "home.", "home.Pause", "home pause", "1home.pause", "a.b.c.d.e",
                   "home.pa-use", "home.\u6682\u505c", "a" * 33 + ".b", ""):
        ap(f"bad action {action!r}", {**base, "action": action}, "malformed")
    for actor in ("person", "helper", "system"):
        ap(f"actor {actor}", {**base, "actor": actor}, "ok")
    for actor in ("user", "Person", "", None, 1):
        ap(f"bad actor {actor!r}", {**base, "actor": actor}, "malformed")
    for out in fmt.OUTCOMES:
        ap(f"outcome {out}", {**base, "outcome": out}, "ok")
    for out in ("OK", "done", "", None):
        ap(f"bad outcome {out!r}", {**base, "outcome": out}, "malformed")
    for mandate in ("m1", "urn:muse:mandate:2026-11-001", "A.b_c-d:e", "x" * 128):
        ap(f"mandate {mandate[:30]} ({len(mandate)})", {**base, "mandate": mandate}, "ok")
    for mandate in ("", "x" * 129, "has space", "\u6388\u6b0a", "a/b", 7):
        ap(f"bad mandate {str(mandate)[:12]!r} ({len(str(mandate))})", {**base, "mandate": mandate}, "malformed")
    ap("links: empty object", {**base, "links": {}}, "malformed")
    ap("links: unknown key", {**base, "links": {"job": "j", "note": "x"}}, "malformed")
    ap("links: undo_of not a hash", {**base, "links": {"undo_of": "record-3"}}, "malformed")
    ap("links: null value", {**base, "links": {"job": None}}, "malformed")
    ap("links: not an object", {**base, "links": ["job"]}, "malformed")
    ap("links: null", {**base, "links": None}, "malformed")
    ap("summary too long", {**base, "summary": "a" * 201}, "summary")
    ap("summary with a line break", {**base, "summary": "one\ntwo"}, "summary")
    ap("summary holding a key", {**base, "summary": "used ghp_0123456789abcdefghij"}, "summary")
    ap("summary missing", {k: val for k, val in base.items() if k != "summary"}, "malformed")
    ap("unknown entry field", {**base, "seq": 3}, "malformed")
    ap("entry may not carry prev", {**base, "prev": "00" * 32}, "malformed")
    ap("resource empty", {**base, "resource": ""}, "malformed")
    ap("resource not text", {**base, "resource": 5}, "malformed")
    ap("entry not an object", ["x"], "malformed")  # type: ignore[arg-type]
    ap("head of another device", base, "wrong_device", seed=SEED_B)
    ap("head malformed", base, "malformed", head={**chain[-1], "v": 2})
    ap("head not an object", base, "malformed", head="x")
    far = {**chain[-1], "seq": 2**53 - 2, "time": T0}
    ap("head at the second-largest seq", base, "ok", head=far)
    ap("head at the largest seq: the chain is full", base, "malformed", head={**far, "seq": 2**53 - 1})
    ap("redacted head (hash does not depend on the summary)", base, "ok", head=fmt.redact(chain[-1]))
    return chain


def _record_vectors(v: _Vectors, chain: list[dict[str, Any]]) -> None:
    for i, record in enumerate(chain):
        v.add("canonical_bytes", f"chain A record {i}, signed fields", {"record": record, "with_sig": False}, "ok")
        v.add("canonical_bytes", f"chain A record {i}, with sig", {"record": record, "with_sig": True}, "ok")
        v.add("hash", f"chain A record {i}", {"record": record}, "ok")
        v.add("hash", f"chain A record {i}, redacted", {"record": fmt.redact(record)}, "ok")
        v.add("redact", f"chain A record {i}", {"record": record}, "ok")
        v.add("verify_record", f"chain A record {i}", {"record": record, "device": DEVICE_A}, "ok")
    v.add("redact", "already redacted", {"record": fmt.redact(chain[0])}, "ok")
    v.add("canonical_bytes", "malformed record", {"record": {**chain[0], "v": 3}, "with_sig": False}, "malformed")
    v.add("hash", "malformed record", {"record": {"v": 1}}, "malformed")
    v.add("redact", "malformed record", {"record": []}, "malformed")

    r = chain[1]
    vr = lambda label, record, want, device=DEVICE_A: v.add("verify_record", label, {"record": record, "device": device}, want)  # noqa: E731
    vr("no device given", r, "ok", None)
    vr("redacted", fmt.redact(r), "ok")
    vr("checked against device B", r, "wrong_device", DEVICE_B)
    vr("device is checked before the signature", {**r, "sig": flip_hex(r["sig"])}, "wrong_device", DEVICE_B)
    vr("signed by device B", fmt.sign({**{k: x for k, x in r.items() if k != "sig"}, "device": DEVICE_B}, _signer(SEED_B)), "wrong_device")
    vr("device field replaced", {**r, "device": DEVICE_B}, "bad_signature", None)
    vr("sig: first digit changed", {**r, "sig": flip_hex(r["sig"])}, "bad_signature")
    vr("sig: last digit changed", {**r, "sig": flip_hex(r["sig"], 127)}, "bad_signature")
    vr("sig of another record", {**r, "sig": chain[2]["sig"]}, "bad_signature")
    vr("sig all zero", {**r, "sig": "00" * 64}, "bad_signature")
    s_plus_l = (int.from_bytes(bytes.fromhex(r["sig"])[32:], "little") + L_ORDER).to_bytes(32, "little").hex()
    vr("sig with S + L (malleated)", {**r, "sig": r["sig"][:64] + s_plus_l}, "bad_signature")
    for field, value in (("seq", r["seq"] + 1), ("time", r["time"] + 1), ("mandate", None), ("mandate", "m-2026-11-002"), ("actor", "person"),
                         ("action", "files.read"), ("resource", None), ("resource", flip_hex(r["resource"])), ("summary_hash", flip_hex(r["summary_hash"])),
                         ("outcome", "failed"), ("cost", {"currency": "TWD", "minor": 1}), ("prev", flip_hex(r["prev"])),
                         ("links", {"job": "job-18", "card": "card-3"}), ("links", {"job": "job-17"})):
        vr(f"edited {field} -> {json.dumps(value)[:24]}", {**r, field: value}, "bad_signature")
    vr("links removed", {k: x for k, x in r.items() if k != "links"}, "bad_signature")
    vr("links added", {**chain[0], "links": {"job": "j"}}, "bad_signature")
    vr("cost edited", {**chain[2], "cost": {"currency": "TWD", "minor": 31}}, "bad_signature")
    vr("cost currency edited", {**chain[2], "cost": {"currency": "USD", "minor": 30}}, "bad_signature")
    vr("cost removed", {**chain[2], "cost": None}, "bad_signature")
    vr("summary edited", {**r, "summary": r["summary"] + "!"}, "summary_mismatch")
    vr("summary swapped for another record's", {**r, "summary": chain[2]["summary"]}, "summary_mismatch")
    vr("summary salt edited", {**r, "summary_salt": flip_hex(r["summary_salt"])}, "summary_mismatch")
    vr("summary in NFD form", {**chain[4], "summary": chain[4]["summary"].replace("\u00e9", "e\u0301")}, "summary_mismatch")
    vr("summary_hash edited, summary present", {**r, "summary_hash": flip_hex(r["summary_hash"])}, "bad_signature")
    vr("summary_hash edited, redacted", {**fmt.redact(r), "summary_hash": flip_hex(r["summary_hash"])}, "bad_signature")
    vr("summary without salt", {k: x for k, x in r.items() if k != "summary_salt"}, "malformed")
    vr("salt without summary", {k: x for k, x in r.items() if k != "summary"}, "malformed")
    vr("summary too long", {**r, "summary": "a" * 201}, "malformed")
    vr("summary empty", {**r, "summary": ""}, "malformed")
    vr("summary with a control character", {**r, "summary": "a\u0007b"}, "malformed")
    vr("summary null", {**r, "summary": None}, "malformed")
    vr("salt too short", {**r, "summary_salt": r["summary_salt"][:30]}, "malformed")
    for field in fmt.REQUIRED_FIELDS:
        vr(f"missing {field}", {k: x for k, x in r.items() if k != field}, "malformed")
    vr("unknown field", {**r, "note": "x"}, "malformed")
    vr("unknown field: hash", {**r, "hash": fmt.record_hash(r)}, "malformed")
    vr("v is 2", {**r, "v": 2}, "malformed")
    vr("v is 0", {**r, "v": 0}, "malformed")
    vr("v is text", {**r, "v": "1"}, "malformed")
    vr("v is true", {**r, "v": True}, "malformed")
    vr("v is 1.0", {**r, "v": 1.0}, "malformed")
    vr("seq negative", {**r, "seq": -1}, "malformed")
    vr("seq too large", {**r, "seq": 2**53}, "malformed")
    vr("seq a float", {**r, "seq": 1.0}, "malformed")
    vr("seq text", {**r, "seq": "1"}, "malformed")
    vr("time a float", {**r, "time": 1794000001500.5}, "malformed")
    vr("time null", {**r, "time": None}, "malformed")
    vr("device upper case", {**r, "device": r["device"].upper()}, "malformed")
    vr("device short", {**r, "device": r["device"][:62]}, "malformed")
    vr("device not hex", {**r, "device": "g" * 64}, "malformed")
    vr("sig upper case", {**r, "sig": r["sig"].upper()}, "malformed")
    vr("sig short", {**r, "sig": r["sig"][:126]}, "malformed")
    vr("sig base64", {**r, "sig": "A" * 88}, "malformed")
    vr("prev upper case", {**r, "prev": r["prev"].upper()}, "malformed")
    vr("prev null", {**r, "prev": None}, "malformed")
    vr("resource is plain text", {**r, "resource": "file:/Notes/x.md"}, "malformed")
    vr("mandate empty", {**r, "mandate": ""}, "malformed")
    vr("actor unknown", {**r, "actor": "bot"}, "malformed")
    vr("action not dotted", {**r, "action": "write"}, "malformed")
    vr("outcome unknown", {**r, "outcome": "success"}, "malformed")
    vr("cost a number", {**r, "cost": 30}, "malformed")
    vr("cost minor a float", {**r, "cost": {"currency": "TWD", "minor": 30.0}}, "malformed")
    vr("links empty", {**r, "links": {}}, "malformed")
    vr("links null", {**r, "links": None}, "malformed")
    vr("not an object: list", [r], "malformed")
    vr("not an object: null", None, "malformed")
    vr("not an object: text", "record", "malformed")

    # Ed25519 edge cases: both implementations must give the same yes or no.
    digest = fmt.signing_digest(r)
    vs = lambda label, dev, msg, sig: v.add("verify_signature", label, {"device": dev, "message": msg.hex(), "sig": sig}, "ok")  # noqa: E731
    vs("a good signature", r["device"], digest, r["sig"])
    vs("wrong message", r["device"], hashlib.sha256(b"other").digest(), r["sig"])
    vs("wrong key", DEVICE_B, digest, r["sig"])
    vs("S + L", r["device"], digest, r["sig"][:64] + s_plus_l)
    vs("S all ones", r["device"], digest, r["sig"][:64] + "ff" * 32)
    vs("R changed", r["device"], digest, flip_hex(r["sig"], 3))
    vs("empty message", r["device"], b"", _signer(SEED_A).sign(b"").hex())
    vs("long message", r["device"], b"m" * 300, _signer(SEED_A).sign(b"m" * 300).hex())
    identity, order2 = "01" + "00" * 31, "ec" + "ff" * 30 + "7f"
    noncanonical_identity = "ee" + "ff" * 30 + "7f"
    order4, order8 = "00" * 32, "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05"
    for klabel, key in (("identity", identity), ("order 2", order2), ("order 4", order4), ("order 8", order8),
                        ("identity, non-canonical", noncanonical_identity), ("key not on the curve", "02" + "00" * 31),
                        ("identity with the sign bit", "01" + "00" * 30 + "80")):
        for mlabel, msg in (("m1", b"\x01" * 32), ("m2", b"\x02" * 32), ("m3", b"\x03" * 32), ("m4", b"\x04" * 32)):
            for rlabel, rr in (("R identity", identity), ("R = key", key)):
                vs(f"small-order or odd key ({klabel}), {rlabel}, S 0, {mlabel}", key, msg, rr + "00" * 32)


def _chain_vectors(v: _Vectors, chain: list[dict[str, Any]]) -> None:
    n = len(chain)
    vc = lambda label, records, want, device=DEVICE_A: v.add("verify_chain", label, {"records": records, "device": device}, want)  # noqa: E731
    vc("empty", [], "ok")
    vc("empty, no device", [], "ok", None)
    for k in range(1, n + 1):
        vc(f"first {k} of chain A", chain[:k], "ok")
    vc("chain A, device taken from the first record", chain, "ok", None)
    vc("chain A, redacted", [fmt.redact(x) for x in chain], "ok")
    vc("chain A, some redacted", [fmt.redact(x) if i % 2 else x for i, x in enumerate(chain)], "ok")
    vc("chain A checked as device B", chain, "wrong_device", DEVICE_B)
    chain_b = make_chain(ENTRIES[:4], SEED_B)
    vc("chain B", chain_b, "ok", DEVICE_B)
    vc("chain B, no device", chain_b, "ok", None)
    long = make_chain(many_entries(40))
    vc("40 varied records", long, "ok")

    # edited field
    for i in (0, 3, n - 1):
        for field, value in (("time", chain[i]["time"] + 1), ("action", "files.delete"), ("outcome", "undone"), ("actor", "system"),
                             ("mandate", "m-other"), ("summary_hash", flip_hex(chain[i]["summary_hash"], 10)), ("seq", chain[i]["seq"] + 1),
                             ("prev", flip_hex(chain[i]["prev"], 5)), ("cost", {"currency": "TWD", "minor": 1})):
            vc(f"record {i}: edited {field}", chain[:i] + [{**chain[i], field: value}] + chain[i + 1:], "bad_signature")
        vc(f"record {i}: edited summary", chain[:i] + [{**chain[i], "summary": "Nothing happened"}] + chain[i + 1:], "summary_mismatch")
        vc(f"record {i}: bad signature", chain[:i] + [{**chain[i], "sig": flip_hex(chain[i]["sig"], 64)}] + chain[i + 1:], "bad_signature")
        vc(f"record {i}: malformed (extra field)", chain[:i] + [{**chain[i], "x": 1}] + chain[i + 1:], "malformed")
        vc(f"record {i}: not an object", chain[:i] + [None] + chain[i + 1:], "malformed")
    # removed
    vc("first record removed", chain[1:], "missing")
    for i in (1, 4, n - 2):
        vc(f"record {i} removed", chain[:i] + chain[i + 1:], "missing")
    vc("last record removed (not detectable without a checkpoint)", chain[:-1], "ok")
    vc("two records removed", chain[:2] + chain[4:], "missing")
    # reordered
    for i in (0, 2, n - 2):
        swapped = chain[:i] + [chain[i + 1], chain[i]] + chain[i + 2:]
        vc(f"records {i} and {i + 1} swapped", swapped, "missing")
    vc("reversed", chain[::-1], "missing")
    vc("last record moved to the front", [chain[-1]] + chain[:-1], "missing")
    # duplicate and fork
    for i in (0, 3, n - 1):
        vc(f"record {i} twice", chain[:i + 1] + [chain[i]] + chain[i + 1:], "duplicate")
    vc("an early record again at the end", chain + [chain[2]], "duplicate")
    vc("the same record, once redacted", chain[:3] + [fmt.redact(chain[2])] + chain[3:], "duplicate")
    for i in (0, 3, n - 1):
        other = resign(fmt.redact(chain[i]), summary_hash=flip_hex(chain[i]["summary_hash"]))
        vc(f"fork: a second signed record {i} after the first", chain[:i + 1] + [other] + chain[i + 1:], "fork")
        vc(f"fork: a second signed record {i} in place of the first", chain[:i] + [other] + chain[i + 1:], "wrong_prev" if i < n - 1 else "ok")
    vc("fork: a second signed record 2 at the end", chain + [resign(chain[2], time=chain[2]["time"] + 5)], "fork")
    fork_tail = relink(chain[:4] + [resign(chain[4], outcome="failed")] + chain[5:], 5)
    vc("fork: a whole second history from record 4 (valid on its own)", fork_tail, "ok")
    vc("fork: first history, then the second history's record 4", chain + [fork_tail[4]], "fork")
    vc("fork: the two histories spliced", chain[:6] + fork_tail[6:], "wrong_prev")
    # wrong prev
    for i in (0, 1, 5):
        vc(f"record {i} validly signed over another prev", chain[:i] + [resign(chain[i], prev=flip_hex(chain[i]["prev"]))] + chain[i + 1:], "wrong_prev")
    vc("first record uses device B's genesis", [resign(chain[0], prev=fmt.genesis(DEVICE_B))], "wrong_prev")
    vc("first record uses the all-zero prev", [resign(chain[0], prev="00" * 32)], "wrong_prev")
    vc("record 1 links to record 0's signed-fields hash, not its record hash",
       [chain[0], resign(chain[1], prev=hashlib.sha256(fmt.canonical_bytes(chain[0])).hexdigest())], "wrong_prev")
    # wrong device
    spliced_b = fmt.sign({**{k: x for k, x in chain[3].items() if k != "sig"}, "device": DEVICE_B}, _signer(SEED_B))
    vc("record 3 signed by device B", chain[:3] + [spliced_b] + chain[4:], "wrong_device")
    vc("record 3 signed by device B, no device given", chain[:3] + [spliced_b] + chain[4:], "wrong_device", None)
    vc("record 3 signed by device B but naming device A", chain[:3] + [{**chain[3], "sig": spliced_b["sig"]}] + chain[4:], "bad_signature")
    vc("chain B after chain A", chain + chain_b, "wrong_device")
    # time
    tol = fmt.TIME_TOLERANCE_MS
    for i in (1, 4, n - 1):
        back = relink(chain[:i] + [resign(chain[i], time=chain[i - 1]["time"] - tol - 1)] + chain[i + 1:], i)
        vc(f"record {i}: time one past the tolerance before record {i - 1}", back, "time_went_back")
        edge = relink(chain[:i] + [resign(chain[i], time=chain[i - 1]["time"] - tol)] + chain[i + 1:], i)
        vc(f"record {i}: time exactly the tolerance before record {i - 1}", edge[:i + 1], "ok")
    drift = [dict(chain[0])]
    for i in range(1, n):
        drift.append(resign(chain[i], time=drift[-1]["time"] - tol, prev=fmt.record_hash(drift[-1])))
    vc("time: each record the full tolerance before the last (allowed: only neighbours are compared)", drift, "ok")
    zero = relink([resign(chain[0], time=0)] + chain[1:], 1)
    vc("time: first record at 0", zero, "ok")
    big = relink(chain[:n - 1] + [resign(chain[n - 1], time=2**53 - 1)], n - 1)
    vc("time: last record at the largest value", big, "ok")
    # order of checks
    both = chain[:2] + [{**chain[3], "sig": flip_hex(chain[3]["sig"])}] + chain[4:]
    vc("a removed record whose successor is also edited: signature first", both, "bad_signature")
    vc("first problem wins: bad signature at 2 before malformed at 5",
       chain[:2] + [{**chain[2], "sig": flip_hex(chain[2]["sig"])}] + chain[3:5] + [{"v": 1}] + chain[6:], "bad_signature")
    vc("malformed at 1 before bad signature at 4", chain[:1] + [{"v": 1}] + chain[2:4] + [{**chain[4], "sig": flip_hex(chain[4]["sig"])}], "malformed")
    vc("first record malformed, no device", [{"v": 1}] + chain[1:], "malformed", None)


def _merkle_vectors(v: _Vectors) -> None:
    sizes = list(range(0, 71))
    for n in sizes:
        v.add("merkle_root", f"{n} leaves", {"n": n}, "ok")
    for n in (128, 129, 255, 256, 257, 1000):
        v.add("merkle_root", f"{n} leaves", {"n": n}, "ok")
    for n in sizes[1:]:
        indexes = range(n) if n <= 17 else sorted({0, 1, n // 3, n // 2, n - 2, n - 1, (n * 7) % n, 31 % n, 32 % n, 63 % n})
        for i in indexes:
            v.add("inclusion_proof", f"{i} of {n}", {"n": n, "index": i}, "ok")
    for n, i in ((1000, 0), (1000, 511), (1000, 512), (1000, 999), (257, 256), (256, 255)):
        v.add("inclusion_proof", f"{i} of {n}", {"n": n, "index": i}, "ok")
    v.add("inclusion_proof", "index equal to the size", {"n": 5, "index": 5}, "malformed")
    v.add("inclusion_proof", "negative index", {"n": 5, "index": -1}, "malformed")
    v.add("inclusion_proof", "empty tree", {"n": 0, "index": 0}, "malformed")
    for n in sizes:
        olds = range(n + 1) if n <= 17 else sorted({0, 1, 2, n // 3, n // 2, 16 % (n + 1), 32 % (n + 1), 64 % (n + 1), n - 1, n})
        for m in olds:
            v.add("consistency_proof", f"{m} -> {n}", {"n": n, "old_size": m}, "ok")
    for n, m in ((1000, 1), (1000, 512), (1000, 513), (1000, 999), (257, 256), (256, 128)):
        v.add("consistency_proof", f"{m} -> {n}", {"n": n, "old_size": m}, "ok")
    v.add("consistency_proof", "old size above the size", {"n": 5, "old_size": 6}, "malformed")
    v.add("consistency_proof", "negative old size", {"n": 5, "old_size": -1}, "malformed")

    def vi(label: str, n: int, i: int, want: bool | None, **over: Any) -> None:
        leaves = test_leaves(n)
        inp = {"hash": leaves[i] if n else test_leaf(0), "index": i, "size": n, "proof": merkle.inclusion_proof(leaves, i) if n else [],
               "root": merkle.merkle_root(leaves)}
        inp.update(over)
        got = v.add("verify_inclusion", label, inp, "ok")["ok"]
        if want is not None and got is not want:
            raise SystemExit(f"records vector verify_inclusion {label!r}: expected {want}")

    for n, i in ((1, 0), (2, 0), (2, 1), (3, 2), (5, 4), (7, 3), (8, 7), (13, 6), (37, 36), (64, 0), (65, 64), (70, 33)):
        vi(f"good {i} of {n}", n, i, True)
        proof = merkle.inclusion_proof(test_leaves(n), i)
        vi(f"{i} of {n}: another leaf's hash", n, i, False, hash=test_leaf(n + 1))
        vi(f"{i} of {n}: another root", n, i, False, root=merkle.merkle_root(test_leaves(n + 1)))
        vi(f"{i} of {n}: one element too many", n, i, False, proof=proof + [test_leaf(99)])
        # The tree shape, not the root, carries the size: for some (index, size) the same path fits size + 1.
        # The signed checkpoint is what fixes the size. Both implementations must still give the same answer.
        vi(f"{i} of {n}: size one larger", n, i, None, size=n + 1)
        if n > 1:
            vi(f"{i} of {n}: index one off", n, i, False, index=(i + 1) % n)
            vi(f"{i} of {n}: last element dropped", n, i, False, proof=proof[:-1])
            vi(f"{i} of {n}: first element changed", n, i, False, proof=[flip_hex(proof[0])] + proof[1:])
            vi(f"{i} of {n}: proof reversed", n, i, len(proof) == 1, proof=proof[::-1])
    for n in (6, 7, 21):
        for i in range(n):
            for j in range(n):
                if i != j:
                    vi(f"proof for {i} of {n} used at index {j}", n, i, False, index=j)
            vi(f"proof for {i} of {n} with the record hash being the leaf node hash", n, i, False, hash=merkle.leaf_hash(test_leaf(i)))
    vi("index equal to the size", 4, 3, False, index=4)
    vi("negative index", 4, 3, False, index=-1)
    vi("size zero", 0, 0, False)
    vi("an inner node passed off as a record", 4, 0, False, hash=test_leaf(0), index=0, size=2,
       proof=[merkle.inclusion_proof(test_leaves(4), 0)[1]])
    vi("upper-case hex in the proof", 4, 1, False, proof=[h.upper() for h in merkle.inclusion_proof(test_leaves(4), 1)])
    vi("short hash in the proof", 4, 1, False, proof=[merkle.inclusion_proof(test_leaves(4), 1)[0][:62], merkle.inclusion_proof(test_leaves(4), 1)[1]])
    vi("65 proof elements", 4, 1, False, proof=[test_leaf(0)] * 65)
    vi("root not a hash", 4, 1, False, root="root")

    def vcn(label: str, m: int, n: int, want: bool, **over: Any) -> None:
        leaves = test_leaves(n)
        inp = {"old_size": m, "new_size": n, "old_root": merkle.merkle_root(leaves[:m]), "new_root": merkle.merkle_root(leaves),
               "proof": merkle.consistency_proof(leaves, m)}
        inp.update(over)
        got = v.add("verify_consistency", label, inp, "ok")["ok"]
        if got is not want:
            raise SystemExit(f"records vector verify_consistency {label!r}: expected {want}")

    for m, n in ((0, 0), (0, 1), (0, 9), (1, 1), (1, 2), (1, 8), (2, 3), (3, 4), (3, 7), (4, 8), (4, 7), (6, 8), (7, 8), (8, 8), (5, 13),
                 (16, 33), (31, 32), (32, 33), (33, 70), (64, 65), (64, 70), (69, 70), (70, 70)):
        vcn(f"good {m} -> {n}", m, n, True)
        proof = merkle.consistency_proof(test_leaves(n), m)
        vcn(f"{m} -> {n}: another old root", m, n, False, old_root=merkle.merkle_root(test_leaves(m + 71)[1:]))
        vcn(f"{m} -> {n}: another new root (nothing to prove from an empty tree)" if m == 0 else f"{m} -> {n}: another new root", m, n, m == 0 and n > 0, new_root=merkle.merkle_root(test_leaves(n + 72)[2:]))
        vcn(f"{m} -> {n}: one element too many", m, n, False, proof=proof + [test_leaf(99)])
        if proof:
            vcn(f"{m} -> {n}: last element dropped", m, n, False, proof=proof[:-1])
            vcn(f"{m} -> {n}: an element changed", m, n, False, proof=proof[:-1] + [flip_hex(proof[-1])])
        if 0 < m < n:
            vcn(f"{m} -> {n}: sizes swapped", m, n, False, old_size=n, new_size=m)
            vcn(f"{m} -> {n}: old size one smaller", m, n, False, old_size=m - 1)
    changed = test_leaves(8)
    changed[2] = test_leaf(50)
    vcn("history rewritten under the old size", 4, 8, False, new_root=merkle.merkle_root(changed),
        proof=merkle.consistency_proof(changed, 4))
    vcn("negative old size", 3, 8, False, old_size=-1)
    vcn("0 -> 5 with a wrong old root", 0, 5, False, old_root=test_leaf(1))
    vcn("0 -> 5 with a proof element", 0, 5, False, proof=[test_leaf(1)])
    vcn("129 proof elements", 3, 8, False, proof=[test_leaf(0)] * 129)
    vcn("root not a hash", 3, 8, False, old_root="x")


def _seal_vectors(v: _Vectors, chain: list[dict[str, Any]]) -> None:
    n = len(chain)
    for size in (0, 1, 2, 3, 7, 8, 9, 64, 70):
        v.add("checkpoint", f"{size} test leaves, device A", {"n": size, "time": T0 + size, "test_only_seed": SEED_A}, "ok")
    v.add("checkpoint", "device B", {"n": 5, "time": T0, "test_only_seed": SEED_B}, "ok")
    v.add("checkpoint", "time 0", {"n": 5, "time": 0, "test_only_seed": SEED_A}, "ok")
    v.add("checkpoint", "largest time", {"n": 5, "time": 2**53 - 1, "test_only_seed": SEED_A}, "ok")
    v.add("checkpoint", "time too large", {"n": 5, "time": 2**53, "test_only_seed": SEED_A}, "malformed")
    v.add("checkpoint", "negative time", {"n": 5, "time": -1, "test_only_seed": SEED_A}, "malformed")
    v.add("checkpoint", "fractional time", {"n": 5, "time": 1.5, "test_only_seed": SEED_A}, "malformed")

    hashes = [fmt.record_hash(r) for r in chain]
    seed_a = bytes.fromhex(SEED_A)
    cp = seal.checkpoint(hashes, T0 + 300_000, seed_a)
    cp5 = seal.checkpoint(hashes[:5], T0 + 100_000, seed_a)
    cp0 = seal.checkpoint([], T0, seed_a)
    vcp = lambda label, c, want, device=DEVICE_A: v.add("verify_checkpoint", label, {"checkpoint": c, "device": device}, want)  # noqa: E731
    vcp("over chain A", cp, "ok")
    vcp("over the first 5", cp5, "ok")
    vcp("over nothing", cp0, "ok")
    vcp("no device given", cp, "ok", None)
    vcp("checked as device B", cp, "wrong_device", DEVICE_B)
    vcp("size edited", {**cp, "size": n - 1}, "bad_signature")
    vcp("root edited", {**cp, "root": flip_hex(cp["root"])}, "bad_signature")
    vcp("time edited", {**cp, "time": cp["time"] + 1}, "bad_signature")
    vcp("device replaced", {**cp, "device": DEVICE_B}, "bad_signature", None)
    vcp("sig edited", {**cp, "sig": flip_hex(cp["sig"], 70)}, "bad_signature")
    vcp("a record's signature is not a checkpoint signature", {**cp, "sig": chain[0]["sig"]}, "bad_signature")
    vcp("extra field", {**cp, "v": 1}, "malformed")
    vcp("missing time", {k: x for k, x in cp.items() if k != "time"}, "malformed")
    vcp("size a float", {**cp, "size": 8.0}, "malformed")
    vcp("size negative", {**cp, "size": -1}, "malformed")
    vcp("root upper case", {**cp, "root": cp["root"].upper()}, "malformed")
    vcp("not an object", [cp], "malformed")

    def ex(label: str, records: Any, c: Any, select: Any, redact: bool, want: str) -> Any:
        return v.add("export", label, {"records": records, "checkpoint": c, "select": select, "redact_summaries": redact}, want)

    full = ex("everything", chain, cp, None, False, "ok")["ok"]
    full_redacted = ex("everything, summaries redacted", chain, cp, None, True, "ok")["ok"]
    subset = ex("records 1, 2 and 6", chain, cp, [1, 2, 6], False, "ok")["ok"]
    subset_redacted = ex("records 0 and 7, summaries redacted", chain, cp, [0, 7], True, "ok")["ok"]
    one = ex("one record", chain, cp, [3], False, "ok")["ok"]
    none = ex("a filter that matched nothing", chain, cp, [], False, "ok")["ok"]
    every = ex("every record, as a subset", chain, cp, list(range(n)), False, "ok")["ok"]
    older = ex("an earlier checkpoint, with later records present", chain, cp5, None, False, "ok")["ok"]
    older_subset = ex("an earlier checkpoint, subset", chain, cp5, [0, 4], False, "ok")["ok"]
    empty = ex("an empty chain", [], cp0, None, False, "ok")["ok"]
    ex("select beyond the checkpoint", chain, cp5, [4, 5], False, "malformed")
    ex("select not ascending", chain, cp, [2, 1], False, "malformed")
    ex("select repeats", chain, cp, [2, 2], False, "malformed")
    ex("select negative", chain, cp, [-1], False, "malformed")
    ex("fewer records than the checkpoint covers", chain[:5], cp, None, False, "size_mismatch")
    ex("checkpoint over other records", chain, {**cp5, "size": 6}, None, False, "root_mismatch")
    ex("a record edited before export", chain[:2] + [{**chain[2], "sig": flip_hex(chain[2]["sig"])}] + chain[3:], cp, None, False, "root_mismatch")
    ex("malformed checkpoint", chain, {**cp, "size": "8"}, None, False, "malformed")
    ex("malformed record", chain[:2] + [{"v": 1}] + chain[3:], cp, None, False, "malformed")

    for label, e in (("everything", full), ("subset", subset), ("empty chain", empty)):
        v.add("export_text", label, {"export": e}, "ok")

    def ve(label: str, e: Any, want: str, device: Any = None) -> None:
        v.add("verify_export", label, {"export": e, "device": device}, want)

    ve("everything", full, "ok")
    ve("everything, expected device given", full, "ok", DEVICE_A)
    ve("everything, redacted", full_redacted, "ok")
    ve("subset", subset, "ok")
    ve("subset, redacted", subset_redacted, "ok")
    ve("one record", one, "ok")
    ve("no records", none, "ok")
    ve("every record as a subset", every, "ok")
    ve("earlier checkpoint", older, "ok")
    ve("earlier checkpoint, subset", older_subset, "ok")
    ve("empty chain", empty, "ok")
    ve("some records redacted, some not", {**full, "records": [fmt.redact(r) if i in (1, 4) else r for i, r in enumerate(full["records"])]}, "ok")
    ve("expected another device", full, "wrong_device", DEVICE_B)
    ve("subset, expected another device", subset, "wrong_device", DEVICE_B)

    def recs(e: dict[str, Any], i: int, **changes: Any) -> dict[str, Any]:
        records = [dict(r) for r in e["records"]]
        records[i].update(changes)
        return {**e, "records": records}

    # complete exports, tampered
    ve("complete: summary edited", recs(full, 2, summary="Asked Gemini nothing"), "summary_mismatch")
    ve("complete: cost edited", recs(full, 2, cost={"currency": "TWD", "minor": 0}), "bad_signature")
    ve("complete: signature edited", recs(full, 5, sig=flip_hex(full["records"][5]["sig"])), "bad_signature")
    ve("complete: record removed from the middle", {**full, "records": full["records"][:3] + full["records"][4:]}, "missing")
    ve("complete: last record removed", {**full, "records": full["records"][:-1]}, "size_mismatch")
    ve("complete: first record removed", {**full, "records": full["records"][1:]}, "missing")
    ve("complete: records swapped", {**full, "records": [full["records"][1], full["records"][0]] + full["records"][2:]}, "missing")
    ve("complete: a record twice", {**full, "records": full["records"] + [full["records"][-1]]}, "duplicate")
    ve("complete: one record too many for the checkpoint", {**older, "records": chain[:6]}, "size_mismatch")
    ve("complete: checkpoint of another size", {**full, "checkpoint": cp5}, "size_mismatch")
    fork_tail = relink(chain[:7] + [resign(chain[7], outcome="failed")], 8)
    ve("complete: last record replaced by another validly signed one", {**full, "records": fork_tail}, "root_mismatch")
    ve("complete: with a proofs field", {**full, "proofs": [[] for _ in range(n)]}, "malformed")
    ve("complete: marked as a subset", {**full, "complete": False}, "malformed")
    # checkpoint tampered
    ve("checkpoint: size edited", {**full, "checkpoint": {**cp, "size": n - 1}}, "bad_checkpoint")
    ve("checkpoint: root edited", {**full, "checkpoint": {**cp, "root": flip_hex(cp["root"])}}, "bad_checkpoint")
    ve("checkpoint: signature edited", {**subset, "checkpoint": {**cp, "sig": flip_hex(cp["sig"])}}, "bad_checkpoint")
    ve("checkpoint: signed by device B", {**full, "checkpoint": seal.checkpoint(hashes, T0, bytes.fromhex(SEED_B))}, "bad_checkpoint")
    ve("checkpoint: malformed", {**full, "checkpoint": {**cp, "size": "8"}}, "malformed")
    ve("device field names device B", {**full, "device": DEVICE_B}, "bad_checkpoint")
    chain_b = make_chain(ENTRIES[:3], SEED_B)
    cp_b = seal.checkpoint([fmt.record_hash(r) for r in chain_b], T0, bytes.fromhex(SEED_B))
    ve("a whole export by device B is valid on its own", seal.export(chain_b, cp_b), "ok")
    ve("a whole export by device B, when device A was expected", seal.export(chain_b, cp_b), "wrong_device", DEVICE_A)
    ve("device A's export holding device B's records", {**full, "records": chain_b + full["records"][3:]}, "wrong_device")
    # subsets, tampered
    ve("subset: summary edited", recs(subset, 1, summary="x"), "summary_mismatch")
    ve("subset: signature edited", recs(subset, 2, sig=flip_hex(subset["records"][2]["sig"])), "bad_signature")
    ve("subset: record swapped for one not selected", {**subset, "records": [subset["records"][0], chain[3], subset["records"][2]]}, "bad_proof")
    ve("subset: proofs swapped", {**subset, "proofs": [subset["proofs"][2], subset["proofs"][1], subset["proofs"][0]]}, "bad_proof")
    ve("subset: a proof element edited", {**subset, "proofs": [subset["proofs"][0], [flip_hex(subset["proofs"][1][0])] + subset["proofs"][1][1:], subset["proofs"][2]]}, "bad_proof")
    ve("subset: a proof truncated", {**subset, "proofs": [subset["proofs"][0][:-1]] + subset["proofs"][1:]}, "bad_proof")
    ve("subset: records out of order", {**subset, "records": subset["records"][::-1], "proofs": subset["proofs"][::-1]}, "malformed")
    ve("subset: a record twice", {**one, "records": one["records"] * 2, "proofs": one["proofs"] * 2}, "malformed")
    ve("subset: fewer proofs than records", {**subset, "proofs": subset["proofs"][:2]}, "malformed")
    ve("subset: without proofs", {k: x for k, x in subset.items() if k != "proofs"}, "malformed")
    ve("subset: marked complete", {**subset, "complete": True}, "malformed")
    ve("subset: proof not a list", {**one, "proofs": ["x"]}, "malformed")
    ve("subset: record beyond the checkpoint", {**older_subset, "records": [older_subset["records"][0], chain[6]]}, "bad_proof")
    ve("subset: another validly signed record at a sealed seq", {**one, "records": [resign(chain[3], outcome="ok")]}, "bad_proof")
    ve("subset: record by device B", {**one, "records": [chain_b[2]]}, "wrong_device")
    other2 = resign(chain[2], prev=flip_hex(chain[2]["prev"]))
    forged = chain[:2] + [other2] + chain[3:]
    forged_hashes = [fmt.record_hash(r) for r in forged]
    forged_cp = seal.checkpoint(forged_hashes, T0, seed_a)
    forged_export = {"type": seal.EXPORT_TYPE, "format": 1, "spec": seal.SPEC, "device": DEVICE_A, "checkpoint": forged_cp, "complete": False,
                     "records": [forged[1], forged[2]], "proofs": [merkle.inclusion_proof(forged_hashes, 1), merkle.inclusion_proof(forged_hashes, 2)]}
    ve("subset: neighbours that do not link (the device sealed a broken chain)", forged_export, "wrong_prev")
    # shape
    ve("unknown top-level field", {**full, "note": "x"}, "malformed")
    ve("wrong type", {**full, "type": "muse.receipts"}, "malformed")
    ve("format 2", {**full, "format": 2}, "malformed")
    ve("format as text", {**full, "format": "1"}, "malformed")
    ve("format 1.0", {**full, "format": 1.0}, "malformed")
    ve("another spec", {**full, "spec": "record-format/2"}, "malformed")
    ve("missing device", {k: x for k, x in full.items() if k != "device"}, "malformed")
    ve("device upper case", {**full, "device": DEVICE_A.upper()}, "malformed")
    ve("complete not a boolean", {**full, "complete": 1}, "malformed")
    ve("records not a list", {**full, "records": {"0": full["records"][0]}}, "malformed")
    ve("a malformed record anywhere is reported for the file", recs(full, 6, v=2), "malformed")
    ve("malformed record wins over an earlier bad signature", recs(recs(full, 1, sig=flip_hex(full["records"][1]["sig"])), 6, v=2), "malformed")
    ve("not an object", [full], "malformed")
    ve("null", None, "malformed")

    def vt(label: str, text: str, want: str, device: Any = None) -> None:
        v.add("verify_export_text", label, {"text": text, "device": device}, want)

    canonical = seal.export_text(subset)
    vt("canonical text", canonical, "ok")
    vt("canonical text of everything", seal.export_text(full), "ok")
    vt("pretty-printed", json.dumps(subset, indent=2, ensure_ascii=False), "ok")
    vt("ascii-escaped", json.dumps(full, ensure_ascii=True), "ok")
    vt("keys in another order", json.dumps(dict(reversed(list(full.items()))), ensure_ascii=False), "ok")
    vt("expected device", canonical, "ok", DEVICE_A)
    vt("expected another device", canonical, "wrong_device", DEVICE_B)
    vt("a repeated key", canonical.replace('"complete":false,', '"complete":false,"complete":false,'), "malformed")
    vt("a repeated key inside a record", canonical.replace('"actor":"helper",', '"actor":"helper","actor":"helper",', 1), "malformed")
    vt("time written with an exponent", canonical.replace(f'"time":{chain[1]["time"]}', '"time":1.7940000015e12'), "malformed")
    vt("seq written as 1.0", canonical.replace('"seq":1,', '"seq":1.0,'), "malformed")
    vt("format written as 1.0", canonical.replace('"format":1,', '"format":1.0,'), "malformed")
    vt("a byte-order mark", "\ufeff" + canonical, "malformed")
    vt("trailing text", canonical + "x", "malformed")
    vt("truncated", canonical[:-20], "malformed")
    vt("empty", "", "malformed")
    vt("summary changed by one letter", canonical.replace("Gemini", "Gemino"), "summary_mismatch")
    vt("summary with an escaped lone surrogate", canonical.replace("Asked Gemini", "Asked \\ud83d Gemini"), "malformed")
    vt("summary written with escapes is the same summary", canonical.replace("Asked Gemini", "\\u0041sked Gemini"), "ok")
    vt("upper-case hex in a signature", canonical.replace(subset["records"][0]["sig"], subset["records"][0]["sig"].upper()), "malformed")
    vt("not JSON", "muse records export", "malformed")


EGRESS_ENTRIES: list[dict[str, Any]] = [
    {"time": T0, "actor": "person", "action": "home.pause", "outcome": "ok", "summary": "Paused the queue", "egress": "none"},
    {"time": T0 + 1_000, "actor": "helper", "action": "llm.call", "outcome": "ok", "summary": "Asked Gemini to summarise 3 notes",
     "cost": {"currency": "TWD", "minor": 30}, "links": {"job": "job-17"}, "egress": "service:gemini"},
    {"time": T0 + 2_000, "actor": "helper", "action": "llm.call", "outcome": "ok", "summary": "Answered by the model on the home",
     "egress": "home"},
    {"time": T0 + 3_000, "actor": "helper", "action": "skill.run", "outcome": "ok", "mandate": "device:openmausbot-tools",
     "summary": "Ran the skill files-answer for a host bot", "egress": "host:openmausbot-tools"},
    {"time": T0 + 4_000, "actor": "person", "action": "card.answer", "outcome": "declined", "summary": "Declined: save a file",
     "links": {"card": "card-9"}},  # no egress: the record does not say
]


def _egress_vectors(v: _Vectors, chain: list[dict[str, Any]]) -> None:
    """`egress`: an optional signed member. Its values, its bytes, and what editing it does to a signature."""
    base = {"time": T0 + 200_000, "actor": "helper", "action": "files.write", "outcome": "ok", "summary": "Wrote a file"}

    def ap(label: str, entry: dict[str, Any], want: str) -> None:
        v.add("append", label, {"head": chain[-1], "entry": entry, "test_only_seed": SEED_A, "test_only_ref_key": REF_KEY}, want)

    for value in ("none", "home", "service:gemini", "service:nvidia_nim", "service:api.example.com", "service:0", "host:openmausbot-tools",
                  "host:a.b_c-d", "service:" + "a" * 64, "host:" + "9" * 64):
        ap(f"egress {value[:32]} ({len(value)})", {**base, "egress": value}, "ok")
    ap("egress with links, cost, mandate and a resource",
       {**base, "egress": "service:gemini", "links": {"job": "j1"}, "cost": {"currency": "USD", "minor": 2}, "mandate": "m1",
        "resource": "file:/a"}, "ok")
    for value in ("", "None", "NONE", "Home", "none ", " none", "service", "service:", "host:", "service:Gemini", "service:-x",
                  "service:.x", "service:_x", "service:a b", "service:a:b", "service:a/b", "host:主機", "cloud", "device:phone",
                  "home:desk", "none:x", "service:" + "a" * 65, "service:gemini\n", None, 5, True, ["none"], {"to": "home"}):
        shown = json.dumps(value, ensure_ascii=True)
        ap(f"bad egress {shown[:24]} ({len(shown)})", {**base, "egress": value}, "malformed")
    ap("a bad egress is reported before a bad summary", {**base, "egress": "cloud", "summary": "a" * 201}, "malformed")
    ap("a good egress does not hide a bad summary", {**base, "egress": "home", "summary": "a" * 201}, "summary")

    records = make_chain(EGRESS_ENTRIES)
    for i, record in enumerate(records):
        v.add("canonical_bytes", f"egress chain record {i}, signed fields", {"record": record, "with_sig": False}, "ok")
        v.add("canonical_bytes", f"egress chain record {i}, with sig", {"record": record, "with_sig": True}, "ok")
        v.add("hash", f"egress chain record {i}", {"record": record}, "ok")
        v.add("redact", f"egress chain record {i}", {"record": record}, "ok")
        v.add("verify_record", f"egress chain record {i}", {"record": record, "device": DEVICE_A}, "ok")
    v.add("verify_chain", "the egress chain", {"records": records, "device": DEVICE_A}, "ok")
    v.add("verify_chain", "the egress chain, redacted", {"records": [fmt.redact(r) for r in records], "device": DEVICE_A}, "ok")

    r = records[1]
    vr = lambda label, record, want: v.add("verify_record", label, {"record": record, "device": DEVICE_A}, want)  # noqa: E731
    for value in ("none", "home", "service:groq", "host:gemini"):
        vr(f"egress edited -> {value}", {**r, "egress": value}, "bad_signature")
    vr("egress removed", {k: x for k, x in r.items() if k != "egress"}, "bad_signature")
    vr("egress added to a record signed without one", {**records[4], "egress": "none"}, "bad_signature")
    vr("egress added to a record of chain A", {**chain[2], "egress": "service:gemini"}, "bad_signature")
    for value in ("", "cloud", "service:", "Service:gemini", "service:" + "a" * 65, None, 0, ["home"]):
        shown = json.dumps(value, ensure_ascii=True)
        vr(f"egress malformed {shown[:24]} ({len(shown)})", {**r, "egress": value}, "malformed")
    v.add("verify_chain", "egress edited in the middle of a chain",
          {"records": records[:2] + [{**records[2], "egress": "none"}] + records[3:], "device": DEVICE_A}, "bad_signature")

    cp = seal.checkpoint([fmt.record_hash(x) for x in records], T0 + 10_000, bytes.fromhex(SEED_A))
    full = v.add("export", "the egress chain", {"records": records, "checkpoint": cp, "select": None, "redact_summaries": False}, "ok")["ok"]
    subset = v.add("export", "the egress chain: the records that left the devices",
                   {"records": records, "checkpoint": cp, "select": [i for i, x in enumerate(records) if fmt.left_devices(x)],
                    "redact_summaries": True}, "ok")["ok"]
    v.add("verify_export", "the egress chain", {"export": full, "device": DEVICE_A}, "ok")
    v.add("verify_export", "the egress chain: the records that left the devices", {"export": subset, "device": None}, "ok")
    edited = copy.deepcopy(subset)
    edited["records"][0]["egress"] = "none"
    v.add("verify_export", "egress edited in a subset export", {"export": edited, "device": None}, "bad_signature")


def build() -> list[dict[str, Any]]:
    """Every vector, in a fixed order."""
    v = _Vectors()
    _jcs_vectors(v)
    _text_vectors(v)
    chain = _append_vectors(v)
    _record_vectors(v, chain)
    _chain_vectors(v, chain)
    _merkle_vectors(v)
    _seal_vectors(v, chain)
    _egress_vectors(v, chain)  # added with the `egress` member; kept last so every earlier vector is byte for byte as it was
    missing = set(FUNCTIONS) - {x["fn"] for x in v.vectors}
    if missing:
        raise SystemExit(f"records: no vectors for {sorted(missing)}")
    return v.vectors


def render(vectors: list[dict[str, Any]] | None = None) -> str:
    """The text of core/conformance/records.json (same layout as the other areas)."""
    lines = [json.dumps(x, ensure_ascii=True, allow_nan=False, separators=(", ", ": ")) for x in (build() if vectors is None else vectors)]
    return "[\n" + ",\n".join(lines) + "\n]\n"
