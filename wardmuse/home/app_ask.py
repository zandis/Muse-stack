"""Ask: conversations kept on the home, and one question sent through the router.

Two parts, both used by muse/home/app_api.py.

`ConversationStore` is `MUSE_HOME/conversations.sqlite` (0600, rollback journal,
secure_delete). It holds what the person asked and what was answered, so it is
the most private file the home has after the keys.

    TODO(vault): this file is plain SQLite protected only by its mode. The vault
    (docs/spec/interfaces/vault-format.md) replaces it; nothing else should read it.

Rules it keeps: bounded (MAX_CONVERSATIONS, MAX_TURNS), the text is never logged,
never put in a record, a card or the usage ledger, and a deleted conversation is
erased from the file (secure_delete), not marked.

`send` posts one chat request to the router as the home's own app and returns a
`RouterReply` with the headers the router documents (README, "Routing rules and
pacing"). `classify` turns that reply into the answer object the API returns:
an answer, or the choices of the refusal trailer (`options=wait:<ISO>,handoff,pay:<USD>`),
exactly the ones the router named.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from wardmuse import paths

MAX_CONVERSATIONS = 200
MAX_TURNS = 200  # per conversation; the oldest are dropped
MAX_TEXT_CHARS = 20_000  # one question
MAX_ANSWER_CHARS = 200_000
HISTORY_TURNS = 6  # earlier question/answer pairs sent along with a new question
HISTORY_CHARS = 24_000
PREVIEW_CHARS = 80
REQUEST_TIMEOUT_S = 120.0
DEFAULT_MAX_TOKENS = 2048
CONTINUE_MAX_TOKENS = 8192  # "Continue" after an answer was cut short: more room, on the next tier up

ASK_APP = "home-ask"
ASK_PAID_APP = "home-ask-paid"
TOKEN_VARS = {ASK_APP: "MUSE_HOME_ASK_APP_TOKEN", ASK_PAID_APP: "MUSE_HOME_ASK_PAID_APP_TOKEN"}

STATUSES = ("answered", "choices", "pending", "queued", "handed_off", "over_limit", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  question TEXT NOT NULL,            -- the person's words
  request TEXT NOT NULL,             -- JSON: privacy, task, attachments
  answer TEXT NOT NULL               -- JSON: the answer object (status, text, route, choices ...)
);
CREATE INDEX IF NOT EXISTS turns_conversation ON turns (conversation_id, created_at);
"""


def store_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "conversations.sqlite"


def iso(ts: float | None) -> str | None:
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class ConversationStore:
    """Access to conversations.sqlite. Safe across threads and processes."""

    def __init__(self, environ: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.path = store_path(environ)

    @contextmanager
    def _conn(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600))  # 0600 before sqlite touches it
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        conn = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA secure_delete=ON")
            conn.executescript(SCHEMA)
            if write:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if write and conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            else:
                if write and conn.in_transaction:
                    conn.execute("COMMIT")
        finally:
            conn.close()

    # ---- writing

    def add_turn(self, conversation_id: str | None, question: str, request: Mapping[str, Any], answer: Mapping[str, Any]) -> tuple[str, str]:
        """Keep a question with its first answer. Returns (conversation id, turn id). An unknown conversation id raises KeyError."""
        now = self.clock()
        turn_id = secrets.token_hex(8)
        with self._conn(write=True) as conn:
            if conversation_id is None:
                conversation_id = secrets.token_hex(8)
                conn.execute("INSERT INTO conversations (id, created_at, updated_at) VALUES (?,?,?)", (conversation_id, now, now))
            elif conn.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone() is None:
                raise KeyError(conversation_id)
            conn.execute("INSERT INTO turns (id, conversation_id, created_at, question, request, answer) VALUES (?,?,?,?,?,?)",
                         (turn_id, conversation_id, now, question, json.dumps(dict(request), ensure_ascii=False),
                          json.dumps(dict(answer), ensure_ascii=False)))
            conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id))
            conn.execute("DELETE FROM turns WHERE conversation_id=? AND id NOT IN (SELECT id FROM turns WHERE conversation_id=? "
                         "ORDER BY created_at DESC, rowid DESC LIMIT ?)", (conversation_id, conversation_id, MAX_TURNS))
            old = conn.execute("SELECT id FROM conversations ORDER BY updated_at DESC, rowid DESC LIMIT -1 OFFSET ?",
                               (MAX_CONVERSATIONS,)).fetchall()
            for row in old:
                conn.execute("DELETE FROM turns WHERE conversation_id=?", (row["id"],))
                conn.execute("DELETE FROM conversations WHERE id=?", (row["id"],))
        return conversation_id, turn_id

    def set_answer(self, turn_id: str, answer: Mapping[str, Any]) -> bool:
        with self._conn(write=True) as conn:
            cur = conn.execute("UPDATE turns SET answer=? WHERE id=?", (json.dumps(dict(answer), ensure_ascii=False), turn_id))
            if cur.rowcount == 1:
                conn.execute("UPDATE conversations SET updated_at=? WHERE id=(SELECT conversation_id FROM turns WHERE id=?)",
                             (self.clock(), turn_id))
            return cur.rowcount == 1

    def delete(self, conversation_id: str) -> bool:
        with self._conn(write=True) as conn:
            conn.execute("DELETE FROM turns WHERE conversation_id=?", (conversation_id,))
            return conn.execute("DELETE FROM conversations WHERE id=?", (conversation_id,)).rowcount == 1

    def delete_all(self) -> int:
        with self._conn(write=True) as conn:
            conn.execute("DELETE FROM turns")
            return int(conn.execute("DELETE FROM conversations").rowcount)

    # ---- reading

    @staticmethod
    def _turn(row: sqlite3.Row) -> dict[str, Any]:
        try:
            request, answer = json.loads(row["request"]), json.loads(row["answer"])
        except ValueError:
            request, answer = {}, {"status": "failed", "reason": "failed"}
        return {"id": row["id"], "conversation_id": row["conversation_id"], "created_at": iso(row["created_at"]),
                "question": row["question"], "request": request, "answer": answer}

    def turn(self, turn_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        return None if row is None else self._turn(row)

    def turns(self, conversation_id: str) -> list[dict[str, Any]] | None:
        with self._conn() as conn:
            if conn.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone() is None:
                return None
            rows = conn.execute("SELECT * FROM turns WHERE conversation_id=? ORDER BY created_at, rowid", (conversation_id,)).fetchall()
        return [self._turn(r) for r in rows]

    def list(self) -> list[dict[str, Any]]:
        """Conversations, newest first, each with the first words of its first question (the person's own device only)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT c.id, c.created_at, c.updated_at, (SELECT COUNT(*) FROM turns t WHERE t.conversation_id=c.id) AS n, "
                "(SELECT question FROM turns t WHERE t.conversation_id=c.id ORDER BY t.created_at, t.rowid LIMIT 1) AS first "
                "FROM conversations c ORDER BY c.updated_at DESC, c.rowid DESC").fetchall()
        out = []
        for row in rows:
            first = " ".join(str(row["first"] or "").split())
            out.append({"id": row["id"], "created_at": iso(row["created_at"]), "updated_at": iso(row["updated_at"]), "turns": row["n"],
                        "preview": first[:PREVIEW_CHARS] + ("…" if len(first) > PREVIEW_CHARS else "")})
        return out

    def history(self, conversation_id: str) -> list[dict[str, str]]:
        """Earlier answered turns as chat messages, newest last, bounded in count and size."""
        turns = [t for t in (self.turns(conversation_id) or []) if t["answer"].get("status") == "answered" and t["answer"].get("text")]
        messages: list[dict[str, str]] = []
        size = 0
        for turn in reversed(turns[-HISTORY_TURNS:]):
            size += len(turn["question"]) + len(turn["answer"]["text"])
            if size > HISTORY_CHARS:
                break
            messages[:0] = [{"role": "user", "content": turn["question"]}, {"role": "assistant", "content": turn["answer"]["text"]}]
        return messages


# ---------------------------------------------------------------- the router


@dataclass(slots=True)
class RouterReply:
    status: int  # 0 = the router could not be reached
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


def ensure_app(name: str, environ: Mapping[str, str]) -> str:
    """The token of the home's own app on the router, creating or repairing the app first.

    Same pattern as the queue's and the skill runner's managed apps: `home-ask` is
    `interactive` (a person is waiting) and never paid; `home-ask-paid` is used only for
    the one request the person agreed to pay for. The token is shown nowhere."""
    from wardmuse import apps, keys

    want_paid = name == ASK_PAID_APP
    token = str(environ.get(TOKEN_VARS[name], "") or "").strip()
    app = apps.load_apps(environ).get(name)
    if app is not None and (app.priority != "interactive" or app.allow_paid != want_paid):
        app = apps.update_app(name, environ, priority="interactive", allow_paid=want_paid)
    if app is not None and token and apps.hash_token(token) == app.token_sha256:
        return token
    if app is None:
        _, token = apps.add_app(name, "interactive", allow_paid=want_paid, environ=environ,
                                note="managed by the home: Ask" + (" (a request you agreed to pay for)" if want_paid else ""))
    else:
        _, token = apps.rotate_token(name, environ)
    keys.set_key(TOKEN_VARS[name], token, force=True, environ=environ)
    return token


def send(url: str, token: str, messages: list[dict[str, str]], *, privacy: str | None, task: str | None, allow_paid: bool,
         post: Callable[..., Any] | None = None, timeout: float = REQUEST_TIMEOUT_S, max_tokens: int = DEFAULT_MAX_TOKENS) -> RouterReply:
    """One chat request. Never raises: a network failure is `RouterReply(0)`."""
    from wardmuse import queue

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if privacy:
        headers["x-muse-privacy"] = privacy
    if task:
        headers["x-muse-task"] = task
    if allow_paid:
        headers["x-muse-allow-paid"] = "true"
    body = json.dumps({"model": "auto", "messages": messages, "max_tokens": int(max_tokens)}).encode("utf-8")
    reply = (post or queue._post)(f"{url}/v1/chat/completions", headers, body, timeout)  # noqa: SLF001 - the queue's own HTTP call
    return RouterReply(int(reply.status), {str(k).lower(): str(v) for k, v in dict(reply.headers).items()}, bytes(reply.body or b""))


def _message(reply: RouterReply) -> str:
    try:
        payload = json.loads(reply.body or b"{}")
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        return str(error.get("message", "") if isinstance(error, dict) else error).strip()
    except ValueError:
        return ""


def _seconds(value: str | None) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 < number < 30 * 86400 else None


# Why the router did not answer, as a small fixed set the app has sentences for.
REASONS = ("used_up", "busy", "yielding", "paid_needed", "paid_limit", "app_limit", "private_no_service", "paused",
           "router_unreachable", "refused_key", "router_limit", "failed")
_RATION_REASONS = {
    "paid-cap": "paid_limit", "paid-not-allowed": "paid_needed", "paid-unknown-cost": "paid_needed", "paid-spend-unknown": "busy",
    "app-requests": "app_limit", "app-tokens": "app_limit", "yield-interactive": "yielding", "reserve": "yielding", "pacing": "yielding",
    "handoff-first": "used_up", "blocked": "used_up", "quota": "used_up",
}


def classify(reply: RouterReply, now: float) -> dict[str, Any]:
    """The router's reply as plain data: an answer, the trailer's options, or why it failed.

    Returns one of
      {"kind": "answer", "text", "provider", "deployment", "cost_usd", "cache", "task", "tokens_in", "tokens_out",
       "min_output", "empty_reasoning", "handoff_suggested"}
      {"kind": "options", "reason", "wait_until": ISO | None, "handoff": bool, "pay_usd": float | None}
      {"kind": "failed", "reason"}
    """
    from wardmuse.ration import parse_options

    h = reply.headers
    if reply.status == 200:
        try:
            payload = json.loads(reply.body)
            text = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            return {"kind": "failed", "reason": "failed"}
        text = text if isinstance(text, str) else ""
        try:
            finish = str(payload["choices"][0].get("finish_reason") or "").strip().lower()
        except (AttributeError, KeyError, IndexError, TypeError):
            finish = ""
        usage = payload.get("usage") if isinstance(payload, dict) and isinstance(payload.get("usage"), dict) else {}

        def num(key: str) -> int | None:
            value = usage.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        try:
            cost = float(h["x-muse-cost-usd"]) if h.get("x-muse-cost-usd") else None
        except ValueError:
            cost = None
        empty = h.get("x-muse-answer", "").strip().lower() == "empty-reasoning" or not text.strip()
        return {"kind": "answer", "text": text[:MAX_ANSWER_CHARS], "provider": h.get("x-muse-provider") or None,
                "deployment": h.get("x-muse-deployment") or None, "cost_usd": cost,
                "cache": h.get("x-muse-cache", "").strip().lower() == "hit", "task": h.get("x-muse-task") or None,
                "tokens_in": num("prompt_tokens"), "tokens_out": num("completion_tokens"),
                "min_output": bool(h.get("x-muse-min-output")), "empty_reasoning": empty,
                "handoff_suggested": h.get("x-muse-handoff", "").strip().lower() == "suggest",
                "cut_short": finish == "length"}
    if reply.status == 0:
        return {"kind": "failed", "reason": "router_unreachable"}
    message = _message(reply)
    if reply.status == 403:
        return {"kind": "failed", "reason": "private_no_service" if message.startswith("[muse]") else "failed"}
    if reply.status == 401:
        return {"kind": "failed", "reason": "refused_key" if "x-muse-privacy" in h else "failed"}
    if reply.status == 429:
        options = parse_options(message)
        ration = h.get("x-muse-ration", "").strip().lower()
        kind, _, code = ration.partition(":")
        reason = _RATION_REASONS.get(code or kind, "used_up" if ration else "busy")
        wait = options.get("wait")
        if not wait and "options=" not in message:
            # No trailer (a provider's own limit passed through): the only honest choice is to wait, when a time is known.
            after = _seconds(h.get("retry-after"))
            wait = iso(now + after) if after is not None else None
        pay = options.get("pay")
        return {"kind": "options", "reason": reason, "wait_until": str(wait) if wait else None,
                "handoff": bool(options.get("handoff")), "pay_usd": float(pay) if isinstance(pay, (int, float)) else None}
    return {"kind": "failed", "reason": "busy" if reply.status in (408, 425) or reply.status >= 500 else "failed"}
