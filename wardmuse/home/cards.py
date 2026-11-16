"""Cards: the pending-approval store.

A card is one question the home asks the person before it does something:
save a file, send a message, spend, and so on (docs/spec/02-ux.md section 5).
Every card has the same shape so it can be read at a glance; the JSON form is
pinned by `docs/spec/interfaces/card.schema.json`.

Cards live in their own SQLite file, `MUSE_HOME/cards.sqlite` (0600, rollback
journal, busy timeout), apart from the ledger (which never holds content) and
the queue (which holds prompts). Every method opens its own short transaction,
so several processes (the API server, the helper, a CLI) can use it at once.
Answering is a compare-and-set inside `BEGIN IMMEDIATE`: of two devices that
answer the same card at the same moment, exactly one wins.

Rules:
  * A card is `pending` until answered (`approved` / `declined`) or until its
    time runs out (`expired`). Default life: 24 hours; 5 minutes for `spend`.
  * Answering twice is an error. An expired card cannot be approved.
  * Text is length-bounded and stored exactly as given. It is never treated as
    markup here; a client must render it as text (textContent), never as HTML.
  * Nothing in a card may look like a key or token (the catalogue's detector).
  * `cost` is always present: an amount and currency, or `{"nothing": true}`.

For other modules on the home:

    store = CardStore()
    card_id = store.create(kind="save_file", title="Save a file", thing="notes.md",
                           where="Documents/Muse", under="Weekly digest chore")
    status = store.wait(card_id, timeout=300)     # "approved" | "declined" | "expired" | "pending"
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from wardmuse import paths

KINDS = ("save_file", "message", "spend", "grant_permission", "oneoff_credit", "send_private", "install_skill")
STATUSES = ("pending", "approved", "declined", "expired")
DECISIONS = {"approve": "approved", "decline": "declined"}

DEFAULT_TTL_S = 24 * 3600.0
SPEND_TTL_S = 5 * 60.0
MAX_TTL_S = 30 * 24 * 3600.0

MAX_TITLE = 80
MAX_THING = 200
MAX_WHERE = 200
MAX_UNDER = 200
MAX_REF = 128
MAX_LINE = 200  # one entry of a list in `extra`
MAX_PREVIEW = 500
MAX_LIST = 12
MAX_PENDING = 500  # a runaway job cannot bury the person in questions

AMOUNT_RE = re.compile(r"^(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,8})?$")
CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9]{2,7}$")
REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ID_RE = re.compile(r"^[0-9a-f]{16}$")
ISO_RE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  thing TEXT NOT NULL,
  place TEXT NOT NULL,              -- the card's "where"
  under_text TEXT NOT NULL,
  mandate_id TEXT,
  cost TEXT NOT NULL,               -- JSON object
  extra TEXT NOT NULL,              -- JSON object, validated per kind
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  status TEXT NOT NULL,
  answered_at REAL,
  answered_by TEXT,
  always INTEGER NOT NULL DEFAULT 0,
  job_ref TEXT
);
CREATE INDEX IF NOT EXISTS cards_status ON cards (status, expires_at);
"""


# Added after the first release: a title the app can show in the person's language (the home's own cards only).
_ADDED_COLUMNS = (("title_key", "TEXT"), ("params", "TEXT"))
TITLE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_PARAMS = 8
MAX_PARAM_CHARS = 200


def _migrate(conn: sqlite3.Connection) -> None:
    have = {row["name"] for row in conn.execute("PRAGMA table_info(cards)")}
    for name, kind in _ADDED_COLUMNS:
        if name not in have:
            try:
                conn.execute(f"ALTER TABLE cards ADD COLUMN {name} {kind}")
            except sqlite3.OperationalError:
                pass  # another process added it between the look and the change


def validate_title_key(title_key: Any, params: Any) -> tuple[str | None, dict[str, Any]]:
    """`title_key` names a sentence the app has in every language; `params` are the few plain values it fills in.

    Only the home's own cards carry them (a chore's, a skill's). A card from a host bot keeps its free text."""
    if title_key is None:
        if params:
            raise CardError("params need a title_key")
        return None, {}
    if not isinstance(title_key, str) or not TITLE_KEY_RE.fullmatch(title_key):
        raise CardError("title_key is 1-40 lower-case letters, digits and '_'")
    if params is None:
        return title_key, {}
    if not isinstance(params, Mapping) or len(params) > MAX_PARAMS:
        raise CardError(f"params is an object of at most {MAX_PARAMS} values")
    out: dict[str, Any] = {}
    for name, value in params.items():
        if not isinstance(name, str) or not TITLE_KEY_RE.fullmatch(name):
            raise CardError("a params name is lower-case letters, digits and '_'")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise CardError("a params value is text or a number")
        if isinstance(value, str) and (len(value) > MAX_PARAM_CHARS or any(ord(c) < 32 for c in value)):
            raise CardError(f"a params value is one line of at most {MAX_PARAM_CHARS} characters")
        out[name] = value
    return title_key, out


class CardError(ValueError):
    """A card that is not valid, or a request the store refuses. The message is safe to show."""

    code = "invalid_card"


class CardNotFound(CardError):
    code = "not_found"


class CardAlreadyAnswered(CardError):
    code = "card_already_answered"


class CardExpired(CardError):
    code = "card_expired"


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cards_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "cards.sqlite"


# ---------------------------------------------------------------- validation


def _text(value: Any, what: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise CardError(f"{what} must be text")
    if required and not value.strip():
        raise CardError(f"{what} cannot be empty")
    if len(value) > limit:
        raise CardError(f"{what} is longer than {limit} characters")
    if "\x00" in value:
        raise CardError(f"{what} contains a NUL character")
    return value


def _lines(value: Any, what: str, *, at_least: int = 0) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST or len(value) < at_least:
        need = f"{at_least} to {MAX_LIST}" if at_least else f"at most {MAX_LIST}"
        raise CardError(f"{what} must be a list of {need} lines")
    return [_text(v, f"{what} line", MAX_LINE) for v in value]


def _money(value: Any, what: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"amount", "currency"}:
        raise CardError(f"{what} must have exactly an amount and a currency")
    amount, currency = value["amount"], value["currency"]
    if not isinstance(amount, str) or not AMOUNT_RE.fullmatch(amount):
        raise CardError(f"{what} amount must be a decimal string such as \"30\" or \"0.25\"")
    if not isinstance(currency, str) or not CURRENCY_RE.fullmatch(currency):
        raise CardError(f"{what} currency must be a code such as TWD or USD")
    return {"amount": amount, "currency": currency}


def validate_cost(value: Any) -> dict[str, Any]:
    """`{"nothing": true}` or `{"amount": "30", "currency": "TWD"}`. Never absent."""
    if isinstance(value, Mapping) and set(value) == {"nothing"}:
        if value["nothing"] is not True:
            raise CardError("cost.nothing must be true; give an amount and currency otherwise")
        return {"nothing": True}
    return _money(value, "cost")


def _only(extra: Mapping[str, Any], kind: str, required: Sequence[str], optional: Sequence[str] = ()) -> None:
    unknown = set(extra) - set(required) - set(optional)
    if unknown:
        raise CardError(f"a {kind} card does not take {', '.join(sorted(map(str, unknown)))} in extra")
    missing = [k for k in required if k not in extra]
    if missing:
        raise CardError(f"a {kind} card needs {', '.join(missing)} in extra")


def validate_extra(kind: str, extra: Any) -> dict[str, Any]:
    """The kind-specific line of a card (ux section 5). Unknown fields are refused."""
    if extra is None:
        extra = {}
    if not isinstance(extra, Mapping):
        raise CardError("extra must be an object")
    out: dict[str, Any] = {}
    if kind == "save_file":
        _only(extra, kind, (), ("size_bytes",))
        if "size_bytes" in extra:
            size = extra["size_bytes"]
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise CardError("size_bytes must be a whole number")
            out["size_bytes"] = size
    elif kind == "message":
        _only(extra, kind, ("preview",), ("to", "draft"))
        out["preview"] = _text(extra["preview"], "preview", MAX_PREVIEW)
        if "to" in extra:
            out["to"] = _text(extra["to"], "to", MAX_LINE)
        if "draft" in extra:
            if not isinstance(extra["draft"], bool):
                raise CardError("draft must be true or false")
            out["draft"] = extra["draft"]
    elif kind == "spend":
        _only(extra, kind, ("payee", "limit_left"))
        out["payee"] = _text(extra["payee"], "payee", MAX_LINE)
        out["limit_left"] = _money(extra["limit_left"], "limit_left")
    elif kind == "grant_permission":
        _only(extra, kind, ("sentence", "limits", "ends_at"))
        out["sentence"] = _text(extra["sentence"], "sentence", MAX_PREVIEW)
        out["limits"] = _lines(extra["limits"], "limits")
        ends = extra["ends_at"]
        if ends is not None and not (isinstance(ends, str) and ISO_RE.fullmatch(ends)):
            raise CardError("ends_at must be null or a time such as 2026-12-31T00:00:00Z")
        out["ends_at"] = ends
    elif kind == "oneoff_credit":
        _only(extra, kind, ("service",), ("credit_left",))
        out["service"] = _text(extra["service"], "service", MAX_LINE)
        if "credit_left" in extra:
            out["credit_left"] = _text(extra["credit_left"], "credit_left", MAX_LINE)
    elif kind == "send_private":
        _only(extra, kind, ("badge", "services"))
        out["badge"] = _text(extra["badge"], "badge", MAX_LINE)
        out["services"] = _lines(extra["services"], "services", at_least=1)
    elif kind == "install_skill":
        _only(extra, kind, ("asks_for", "author"), ("sha256",))
        out["asks_for"] = _lines(extra["asks_for"], "asks_for", at_least=1)
        out["author"] = _text(extra["author"], "author", MAX_LINE)
        if "sha256" in extra:
            if not isinstance(extra["sha256"], str) or not SHA256_RE.fullmatch(extra["sha256"]):
                raise CardError("sha256 must be 64 lower-case hex characters")
            out["sha256"] = extra["sha256"]
    else:
        raise CardError(f"kind must be one of {', '.join(KINDS)}")
    return out


def _refuse_secrets(fields: Mapping[str, Any]) -> None:
    from wardmuse.catalog import find_secretlike  # the same detector the catalogue is checked with

    found = find_secretlike(dict(fields))
    if found:
        where = sorted({p.split(".")[0].split("[")[0].split(" ")[0] for p in found})
        raise CardError(f"{', '.join(where)} looks like a key or token; a card must never carry one")


def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "thing": row["thing"],
        "where": row["place"],
        "under": {"text": row["under_text"], "mandate_id": row["mandate_id"]},
        "cost": json.loads(row["cost"]),
        "extra": json.loads(row["extra"]),
        "created_at": iso(row["created_at"]),
        "expires_at": iso(row["expires_at"]),
        "status": row["status"],
        "answered_at": iso(row["answered_at"]),
        "answered_by": row["answered_by"],
        "always": bool(row["always"]),
        "job_ref": row["job_ref"],
        "title_key": row["title_key"] if "title_key" in row.keys() else None,
        "params": json.loads(row["params"]) if "params" in row.keys() and row["params"] else {},
    }


# ---------------------------------------------------------------- the store


class CardStore:
    """Access to cards.sqlite. Safe across threads and processes."""

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        path: Path | None = None,
    ) -> None:
        self.clock = clock
        self.path = Path(path) if path is not None else cards_path(environ)

    @contextmanager
    def _conn(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if not self.path.exists():
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)  # 0600 before sqlite touches it
            os.close(fd)
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
            _migrate(conn)
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

    @staticmethod
    def _expire(conn: sqlite3.Connection, now: float) -> int:
        cur = conn.execute("UPDATE cards SET status='expired' WHERE status='pending' AND expires_at<=?", (now,))
        return cur.rowcount

    # ---- writing

    def create(
        self,
        *,
        kind: str,
        title: str,
        thing: str,
        where: str = "",
        under: str | Mapping[str, Any] = "",
        cost: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
        mandate_id: str | None = None,
        job_ref: str | None = None,
        ttl_s: float | None = None,
        title_key: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> str:
        """Add a pending card and return its id. `cost=None` means it costs nothing.

        `title_key` and `params` are for the home's own cards: the app shows the sentence it has for the key, in the
        person's language, and falls back to `title` / `under` when it does not know the key."""
        if kind not in KINDS:
            raise CardError(f"kind must be one of {', '.join(KINDS)}")
        if isinstance(under, Mapping):
            if set(under) - {"text", "mandate_id"}:
                raise CardError("under takes only text and mandate_id")
            mandate_id = under.get("mandate_id", mandate_id)
            under = under.get("text", "")
        title = _text(title, "title", MAX_TITLE)
        thing = _text(thing, "thing", MAX_THING)
        where = _text(where, "where", MAX_WHERE, required=False)
        under_text = _text(under, "under", MAX_UNDER, required=False)
        for ref, what in ((mandate_id, "mandate_id"), (job_ref, "job_ref")):
            if ref is not None and not (isinstance(ref, str) and REF_RE.fullmatch(ref)):
                raise CardError(f"{what} must be 1-{MAX_REF} letters, digits, '.', '_', ':' or '-'")
        cost_obj = validate_cost({"nothing": True} if cost is None else cost)
        if kind == "spend" and "amount" not in cost_obj:
            raise CardError("a spend card needs an amount and a currency")
        extra_obj = validate_extra(kind, extra)
        title_key, params_obj = validate_title_key(title_key, params)
        if ttl_s is None:
            ttl_s = SPEND_TTL_S if kind == "spend" else DEFAULT_TTL_S
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float)) or not 0 < ttl_s <= MAX_TTL_S:
            raise CardError("a card must expire between a moment and 30 days from now")
        _refuse_secrets({
            "title": title, "thing": thing, "where": where, "under": under_text, "mandate_id": mandate_id,
            "job_ref": job_ref, "cost": cost_obj, "extra": extra_obj, "params": params_obj,
        })
        now = self.clock()
        card_id = secrets.token_hex(8)
        with self._conn(write=True) as conn:
            self._expire(conn, now)
            pending = conn.execute("SELECT COUNT(*) AS n FROM cards WHERE status='pending'").fetchone()["n"]
            if pending >= MAX_PENDING:
                raise CardError(f"there are already {MAX_PENDING} cards waiting for an answer")
            conn.execute(
                "INSERT INTO cards (id, kind, title, thing, place, under_text, mandate_id, cost, extra, created_at, expires_at,"
                " status, job_ref, title_key, params) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'pending', ?,?,?)",
                (card_id, kind, title, thing, where, under_text, mandate_id, json.dumps(cost_obj, ensure_ascii=False),
                 json.dumps(extra_obj, ensure_ascii=False), now, now + float(ttl_s), job_ref, title_key,
                 json.dumps(params_obj, ensure_ascii=False) if title_key else None),
            )
        return card_id

    def answer(self, card_id: str, decision: str, device: str, always: bool = False) -> dict[str, Any]:
        """Approve or decline a pending card. Exactly one answer ever succeeds."""
        if decision not in DECISIONS:
            raise CardError("decision must be approve or decline")
        if not isinstance(always, bool):
            raise CardError("always must be true or false")
        if always and decision != "approve":
            raise CardError("always can only be set when approving")
        device = _text(device, "device", 64)
        if not ID_RE.fullmatch(card_id or ""):
            raise CardNotFound("there is no such card")
        now = self.clock()
        expired = False
        with self._conn(write=True) as conn:
            row = conn.execute("SELECT status, expires_at FROM cards WHERE id=?", (card_id,)).fetchone()
            if row is None:
                raise CardNotFound("there is no such card")
            if row["status"] == "pending" and row["expires_at"] <= now:
                self._expire(conn, now)  # committed below, then reported
                expired = True
            elif row["status"] == "expired":
                expired = True
            elif row["status"] != "pending":
                raise CardAlreadyAnswered(f"this card was already {row['status']}")
            else:
                cur = conn.execute(
                    "UPDATE cards SET status=?, answered_at=?, answered_by=?, always=? WHERE id=? AND status='pending'",
                    (DECISIONS[decision], now, device, int(always), card_id),
                )
                if cur.rowcount != 1:  # cannot happen inside BEGIN IMMEDIATE; never report a win we did not get
                    raise CardAlreadyAnswered("this card was already answered")
        if expired:
            raise CardExpired("this card expired before it was answered")
        return self.get(card_id)

    def expire_due(self, now: float | None = None) -> int:
        """Mark every pending card whose time has run out as expired. Returns how many."""
        with self._conn(write=True) as conn:
            return self._expire(conn, self.clock() if now is None else now)

    # ---- reading

    def get(self, card_id: str) -> dict[str, Any]:
        if not ID_RE.fullmatch(card_id or ""):
            raise CardNotFound("there is no such card")
        with self._conn(write=True) as conn:
            self._expire(conn, self.clock())
            row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if row is None:
            raise CardNotFound("there is no such card")
        return _to_dict(row)

    def list(self, status: str | Sequence[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Cards, newest first. `status` filters to one status or several."""
        wanted = [status] if isinstance(status, str) else list(status or [])
        bad = [s for s in wanted if s not in STATUSES]
        if bad:
            raise CardError(f"status must be one of {', '.join(STATUSES)}")
        sql, args = "SELECT * FROM cards", []
        if wanted:
            sql += f" WHERE status IN ({','.join('?' * len(wanted))})"
            args += wanted
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        with self._conn(write=True) as conn:
            self._expire(conn, self.clock())
            rows = conn.execute(sql, (*args, max(1, min(int(limit), 1000)))).fetchall()
        return [_to_dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        with self._conn(write=True) as conn:
            self._expire(conn, self.clock())
            out = {s: 0 for s in STATUSES}
            for row in conn.execute("SELECT status, COUNT(*) AS n FROM cards GROUP BY status"):
                out[row["status"]] = int(row["n"])
        return out

    def wait(self, card_id: str, timeout: float, poll_s: float = 0.25, sleep: Callable[[float], None] = time.sleep) -> str:
        """Block until the card is answered or expires, or `timeout` seconds pass.

        Returns the card's status; still "pending" means the timeout came first.
        It only polls the local database: nothing is sent anywhere while waiting.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            status = self.get(card_id)["status"]
            if status != "pending":
                return status
            left = deadline - time.monotonic()
            if left <= 0:
                return status
            sleep(min(poll_s, left))
