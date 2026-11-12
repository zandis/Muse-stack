"""Helpers that keep secret values out of logs, responses, and exception text."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Variable names we treat as secret when scanning an environment mapping.
_SECRET_SUFFIXES = ("_API_KEY", "_MASTER_KEY", "_SALT_KEY", "_PASSWORD", "_SECRET", "_TOKEN")
_SECRET_NAMES = {"DATABASE_URL", "AUTHORIZATION", "X-PAYMENT", "PAYMENT-SIGNATURE"}

# A value that looks like a key should never be echoed, even under another name.
_KEYISH = re.compile(r"(?i)(^sk-[A-Za-z0-9_-]{8,}|^gsk_[A-Za-z0-9_-]{8,}|^0x[0-9a-f]{64}$)")


def secret_env_names(environ: Mapping[str, str]) -> set[str]:
    names = set()
    for key in environ:
        upper = key.upper()
        if upper in _SECRET_NAMES or any(upper.endswith(s) for s in _SECRET_SUFFIXES):
            names.add(key)
    return names


def secret_values(environ: Mapping[str, str]) -> list[str]:
    """Return non-empty secret values, longest first, for redaction."""
    values = []
    for name in secret_env_names(environ):
        raw = environ.get(name, "").strip()
        if raw:
            values.append(raw)
    values.sort(key=len, reverse=True)
    return values


def redact(text: str, environ: Mapping[str, str], *, placeholder: str = "***") -> str:
    """Replace known secret values in ``text``. Safe to call on any string."""
    if not text:
        return text
    out = text
    for value in secret_values(environ):
        if len(value) >= 4 and value in out:
            out = out.replace(value, placeholder)
    return out


def configured_flags(environ: Mapping[str, str], names: list[str]) -> dict[str, str]:
    """Report whether each named variable is set, without returning its value."""
    result: dict[str, str] = {}
    for name in names:
        result[name] = "configured" if environ.get(name, "").strip() else "missing"
    return result


def assert_no_secrets(payload: Any, environ: Mapping[str, str]) -> None:
    """Raise ``ValueError`` if a secret value appears in a JSON-serialisable payload."""
    blob = _flatten(payload)
    for value in secret_values(environ):
        if len(value) >= 4 and value in blob:
            raise ValueError("refusing to emit a response that contains a secret value")


def looks_like_secret(value: str) -> bool:
    return bool(_KEYISH.search(value.strip()))


def _flatten(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, (bytes, bytearray)):
        return payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        return " ".join(_flatten(k) + " " + _flatten(v) for k, v in payload.items())
    if isinstance(payload, (list, tuple, set)):
        return " ".join(_flatten(item) for item in payload)
    return str(payload)
