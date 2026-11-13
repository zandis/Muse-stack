"""Canonical JSON for records: RFC 8785 (JCS), restricted to what a record may hold.

Allowed values: null, true, false, integers in [-(2**53 - 1), 2**53 - 1], strings
(Unicode scalar values only, so no lone surrogates), arrays, and objects with
string keys. Floats are refused: a record holds no fractional number, and for
integers in this range the JCS number form (ECMAScript `Number::toString`) is
plain decimal, so this output is exactly what any RFC 8785 implementation
gives for the same data.

Rules, all from RFC 8785:
- no whitespace; `,` and `:` are the only separators;
- object members sorted by key, comparing keys as UTF-16 code units;
- strings: `\\"` and `\\\\`, then `\\b \\f \\n \\r \\t`, every other character below
  U+0020 as `\\u00xx` (lower-case hex), everything else as itself in UTF-8.

The Rust twin is core/src/records/jcs.rs; core/conformance/records.json holds
the cases both must agree on.
"""

from __future__ import annotations

import json
from typing import Any

from muse.records.errors import RecordError

MAX_INT = 2**53 - 1
MAX_DEPTH = 32

_SHORT = {0x08: "\\b", 0x09: "\\t", 0x0A: "\\n", 0x0C: "\\f", 0x0D: "\\r", 0x22: '\\"', 0x5C: "\\\\"}


def _string(text: str, out: list[str]) -> None:
    out.append('"')
    for ch in text:
        code = ord(ch)
        if code in _SHORT:
            out.append(_SHORT[code])
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise RecordError("malformed", detail="a lone surrogate is not text")
        else:
            out.append(ch)
    out.append('"')


def _utf16_key(key: str) -> bytes:
    try:
        return key.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise RecordError("malformed", detail="a lone surrogate is not text") from exc


def _write(value: Any, out: list[str], depth: int) -> None:
    if depth > MAX_DEPTH:
        raise RecordError("malformed", detail="nested too deeply")
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if not -MAX_INT <= value <= MAX_INT:
            raise RecordError("malformed", detail="integer outside +/-(2**53 - 1)")
        out.append(str(value))
    elif isinstance(value, str):
        _string(value, out)
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out, depth + 1)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise RecordError("malformed", detail="object keys must be text")
        out.append("{")
        for i, key in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            _string(key, out)
            out.append(":")
            _write(value[key], out, depth + 1)
        out.append("}")
    else:
        raise RecordError("malformed", detail=f"{type(value).__name__} is not allowed in a record")


def canonicalize(value: Any) -> bytes:
    """The canonical bytes of `value`. RecordError("malformed") for anything a record may not hold.

    Nesting is limited to MAX_DEPTH containers inside the outermost value."""
    out: list[str] = []
    _write(value, out, 0)
    return "".join(out).encode("utf-8")


# ---------------------------------------------------------------- strict reading


def _no_float(text: str) -> Any:
    raise ValueError("a fractional or exponent number")


def _no_constant(text: str) -> Any:
    raise ValueError("NaN or Infinity")


def _int(text: str) -> int:
    if text == "-0":
        raise ValueError("minus zero")
    if len(text) > 17:  # longer than any integer a record may hold (2**53 - 1 has 16 digits)
        raise ValueError("integer too long")
    return int(text)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("a repeated key")
        out[key] = value
    return out


def _has_surrogate(value: Any) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(ch) <= 0xDFFF for ch in value)
    if isinstance(value, list):
        return any(_has_surrogate(item) for item in value)
    if isinstance(value, dict):
        return any(_has_surrogate(k) or _has_surrogate(v) for k, v in value.items())
    return False


def loads_strict(text: str | bytes) -> Any:
    """Read JSON the way a verifier must: UTF-8 without a byte-order mark, no
    repeated keys, no fractional or exponent numbers, no `-0`, no NaN/Infinity,
    no integer outside +/-(2**53 - 1), no lone surrogate escapes. Anything else
    is RecordError("malformed")."""
    try:
        if isinstance(text, (bytes, bytearray)):
            text = bytes(text).decode("utf-8")
        value = json.loads(text, parse_float=_no_float, parse_int=_int, parse_constant=_no_constant, object_pairs_hook=_pairs)
    except (ValueError, RecursionError) as exc:
        raise RecordError("malformed", detail="not strict JSON") from exc
    if _has_surrogate(value) or not _ints_in_range(value):
        raise RecordError("malformed", detail="a value a record may not hold")
    return value


def _ints_in_range(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return -MAX_INT <= value <= MAX_INT
    if isinstance(value, list):
        return all(_ints_in_range(item) for item in value)
    if isinstance(value, dict):
        return all(_ints_in_range(item) for item in value.values())
    return True
