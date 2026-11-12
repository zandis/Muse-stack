"""Exact-repeat cache (plan section 5.6): answer an identical request without asking anyone.

Off unless MUSE_CACHE=exact. Why built here and not LiteLLM's own cache
(litellm.caching, 1.108.5): LiteLLM checks its cache inside the client wrapper
*after* routing and after our deployment hook, keys it on the deployment's
upstream model rather than the alias the user asked for (so two deployments
of one alias never share an answer), cannot be told "this request is in a
privacy class that must not be written to disk", and reports a hit to the
success callbacks the same way as a real call, so the ledger would count it
against quotas and caps. It is also switched on through litellm_settings,
which the renderer owns. This module is small and keeps those rules itself:

- Only identical requests: the alias after rule resolution, the messages, and
  every parameter that changes the answer (cache_key). Only temperature 0, or
  temperature unset on a task the rules file declares `deterministic`. Never a
  stream, tools, n > 1 or logprobs (cacheable).
- The privacy class and the app's default class are part of the key, so an
  entry is never served across classes or across apps with different
  defaults; the stored class is checked again on read.
- Classes public and standard may be kept on disk (MUSE_HOME/cache.sqlite,
  0600, secure_delete) so every proxy worker shares them. private and local
  are kept in this process's memory only and never written anywhere: their
  text already sits in that memory while the request runs, a bounded TTL
  copy adds no new place it can leak to, and it dies with the process.
- TTL (MUSE_CACHE_TTL_S, default 3600 s) and a size bound
  (MUSE_CACHE_MAX_ENTRIES per store, default 1000, oldest dropped first).

The hook serves a hit through LiteLLM's `mock_response` (no network call),
marks it `x-muse-cache: hit`, and records it in the ledger as `cached` with
zero cost and zero tokens, so it never counts against quotas or caps.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from muse import paths

DISK_CLASSES = frozenset({"public", "standard"})
# Parameters that change the answer. Anything else (metadata, user, timeouts) does not.
OUTPUT_PARAMS = (
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "seed", "response_format",
    "presence_penalty", "frequency_penalty", "logit_bias", "reasoning_effort", "top_k",
)
NEVER = ("stream", "tools", "functions", "tool_choice", "function_call", "logprobs", "top_logprobs")


@dataclass(frozen=True, slots=True)
class CacheSettings:
    enabled: bool = False
    ttl_s: float = 3600.0
    max_entries: int = 1000

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "CacheSettings":
        env = os.environ if environ is None else environ
        mode = str(env.get("MUSE_CACHE", "") or "").strip().lower()

        def number(name: str, default: float, lo: float, hi: float) -> float:
            try:
                value = float(str(env.get(name, "") or default))
            except ValueError:
                return default
            return value if math.isfinite(value) and lo <= value <= hi else default

        return cls(mode == "exact", number("MUSE_CACHE_TTL_S", 3600.0, 1, 30 * 86400),
                   int(number("MUSE_CACHE_MAX_ENTRIES", 1000, 1, 100000)))


def cacheable(data: Mapping[str, Any], *, deterministic: bool = False) -> tuple[bool, str]:
    """(may be cached, why). Pure. A chat request with plain messages whose answer should not vary."""
    if not isinstance(data.get("messages"), list) or not data.get("messages"):
        return False, "not a chat request"
    for key in NEVER:
        value = data.get(key)
        if value not in (None, False, [], {}):
            return False, f"'{key}' is set"
    n = data.get("n")
    if n not in (None, 1):
        return False, "n > 1"
    temperature = data.get("temperature")
    if temperature is None:
        return (True, "temperature unset on a deterministic task") if deterministic else (False, "temperature is not 0")
    try:
        return (True, "temperature 0") if float(temperature) == 0.0 else (False, "temperature is not 0")
    except (TypeError, ValueError):
        return False, "temperature is not a number"


def cache_key(alias: str, data: Mapping[str, Any], privacy: str, app_default_privacy: str) -> str:
    """SHA-256 over the canonical JSON of what decides the answer and who may see it. Pure."""
    doc = {
        "v": 1,
        "alias": str(alias),
        "messages": data.get("messages"),
        "params": {k: data.get(k) for k in OUTPUT_PARAMS if data.get(k) is not None},
        "privacy": str(privacy),
        "app_privacy": str(app_default_privacy),
    }
    raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def on_disk_allowed(privacy: str) -> bool:
    """Only public and standard requests may be written to disk."""
    return privacy in DISK_CLASSES


class ExactCache:
    """Memory store (every class) plus a disk store (public/standard only)."""

    def __init__(self, settings: CacheSettings, environ: Mapping[str, str] | None = None) -> None:
        self.settings = settings
        self.environ = environ
        self._mem: OrderedDict[str, tuple[float, str, str]] = OrderedDict()  # key -> (expires, privacy, content)
        self._lock = threading.Lock()
        self._path: Path | None = None

    # ---- disk

    def path(self) -> Path:
        if self._path is None:
            self._path = paths.home(self.environ) / "cache.sqlite"
        return self._path

    def _conn(self) -> sqlite3.Connection:
        path = self.path()
        if not path.exists():
            os.close(os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600))
        conn = sqlite3.connect(str(path), timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, ts REAL NOT NULL, expires REAL NOT NULL,"
                     " privacy TEXT NOT NULL, content TEXT NOT NULL)")
        return conn

    # ---- api

    def get(self, key: str, privacy: str, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        with self._lock:
            item = self._mem.get(key)
            if item is not None:
                expires, cls, content = item
                if expires > now and cls == privacy:
                    self._mem.move_to_end(key)
                    return content
                self._mem.pop(key, None)
        if not on_disk_allowed(privacy) or not self.path().exists():
            return None
        conn = self._conn()
        try:
            row = conn.execute("SELECT expires, privacy, content FROM entries WHERE key=?", (key,)).fetchone()
        finally:
            conn.close()
        if row is None or row[0] <= now or row[1] != privacy:
            return None
        return str(row[2])

    def put(self, key: str, privacy: str, content: str, now: float | None = None) -> str:
        """Store an answer; returns where it went: "disk" or "memory"."""
        now = time.time() if now is None else now
        expires = now + self.settings.ttl_s
        if on_disk_allowed(privacy):
            conn = self._conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT INTO entries (key, ts, expires, privacy, content) VALUES (?,?,?,?,?)"
                             " ON CONFLICT(key) DO UPDATE SET ts=excluded.ts, expires=excluded.expires,"
                             " privacy=excluded.privacy, content=excluded.content", (key, now, expires, privacy, content))
                conn.execute("DELETE FROM entries WHERE expires<=?", (now,))
                conn.execute("DELETE FROM entries WHERE key NOT IN (SELECT key FROM entries ORDER BY ts DESC LIMIT ?)",
                             (self.settings.max_entries,))
                conn.execute("COMMIT")
            finally:
                conn.close()
            return "disk"
        with self._lock:
            self._mem[key] = (expires, privacy, content)
            self._mem.move_to_end(key)
            while len(self._mem) > self.settings.max_entries:
                self._mem.popitem(last=False)
        return "memory"


def answer_text(response: Any) -> str | None:
    """The plain assistant text of a finished, single-choice chat response, else None (not cached)."""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or len(choices) != 1:
        return None
    choice = choices[0]
    if getattr(choice, "finish_reason", None) != "stop":
        return None
    message = getattr(choice, "message", None)
    if message is None or getattr(message, "tool_calls", None) or getattr(message, "function_call", None):
        return None
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else None


def mock_response(content: str, model: str) -> dict[str, Any]:
    """A response for LiteLLM's `mock_response` (a dict, so no string is read as a mock error)."""
    return {
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
