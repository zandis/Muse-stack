"""Shared shapes for the health bot. One source of truth for the vault, the
mandate book, the receipt log, the bot and the web app.

Rules that hold everywhere:
- Record text exists in plaintext only in memory while the vault is unlocked.
- A receipt never contains record text, a prompt or a model answer. It refers
  to records by content hash.
- Times are ISO-8601 UTC strings ending in "Z". Dates are YYYY-MM-DD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

RecordKind = Literal[
    "lab", "report", "imaging_report", "pathology_report", "medication_list",
    "visit", "note", "document", "measurement",
]
ItemKind = Literal["medication", "appointment", "reminder", "question_list"]

# Where content went. "none": nothing left the process. "local": a model the
# tenant runs on its own machines (or the user's). "private": a provider whose
# terms were checked for no training and no human review. "cloud": anything else.
Egress = Literal["none", "local", "private", "cloud"]

# What a mandate can allow. Kept small on purpose.
SCOPES = (
    "vault.read",        # read records to answer a question
    "vault.write",       # add or delete records on the user's instruction
    "items.write",       # add or change medications, appointments, reminders
    "llm.local",         # send record content to a local / on-premises model
    "llm.private",       # send record content to a contractually private provider
    "llm.cloud",         # send record content to any other provider (off by default)
    "draft.letter",      # draft (never send) a records request or a question list
    "exchange.view",     # add-on: see research invitations
    "exchange.grant",    # add-on: grant a research licence
)


class VaultError(Exception):
    """Base class. Messages never include record text or key material."""


class VaultLocked(VaultError):
    pass


class WrongPassphrase(VaultError):
    pass


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    title: str
    occurred_on: str | None
    source: str            # free text: "National Taiwan University Hospital", "My Health Bank export"
    added_at: str
    size: int              # characters of text
    content_sha256: str    # hash of the plaintext; what receipts refer to


@dataclass(frozen=True)
class Item:
    id: str
    kind: str              # ItemKind
    data: dict[str, Any]   # e.g. {"name": "...", "dose": "...", "schedule": "..."}
    created_at: str
    updated_at: str


class Vault(Protocol):
    """Encrypted store. See muse/trust/vault.py."""

    def is_unlocked(self) -> bool: ...
    def unlock(self, passphrase: str) -> None: ...
    def lock(self) -> None: ...
    def add(self, kind: str, title: str, text: str, occurred_on: str | None = None, source: str = "") -> Record: ...
    def get(self, record_id: str) -> tuple[Record, str]: ...
    def list(self, kind: str | None = None, since: str | None = None, until: str | None = None, limit: int = 200) -> list[Record]: ...
    def search(self, query: str, limit: int = 8) -> list[tuple[Record, str]]: ...   # (record, snippet)
    def delete(self, record_id: str) -> None: ...
    def add_item(self, kind: str, data: dict[str, Any]) -> Item: ...
    def list_items(self, kind: str | None = None) -> list[Item]: ...
    def update_item(self, item_id: str, data: dict[str, Any]) -> Item: ...
    def delete_item(self, item_id: str) -> None: ...
    def sign(self, message: bytes) -> bytes: ...          # Ed25519, key lives inside the vault
    def public_key(self) -> bytes: ...                    # raw 32 bytes; readable while locked
    def export_archive(self, dest: str) -> None: ...      # still encrypted; portable to another install


@dataclass(frozen=True)
class Mandate:
    id: str
    grantee: str                 # "bot", or a tenant / steward id for the add-on
    scopes: tuple[str, ...]
    purpose: str                 # shown to the user, e.g. "Answer my questions about my records"
    record_kinds: tuple[str, ...]  # empty = all kinds
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    signature: str               # base64 Ed25519 over the canonical JSON of the other fields


@dataclass(frozen=True)
class Action:
    scope: str
    record_kinds: tuple[str, ...] = ()
    egress: str = "none"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    mandate_id: str | None
    reason: str                  # one plain sentence, safe to show the user


class MandateBook(Protocol):
    def grant(self, grantee: str, scopes: tuple[str, ...], purpose: str, record_kinds: tuple[str, ...] = (), expires_at: str | None = None) -> Mandate: ...
    def revoke(self, mandate_id: str) -> None: ...
    def all(self) -> list[Mandate]: ...
    def check(self, grantee: str, action: Action) -> Decision: ...


@dataclass(frozen=True)
class Receipt:
    seq: int
    ts: str
    actor: str                   # "user" | "bot" | "system"
    action: str                  # e.g. "vault.read", "llm.complete", "mandate.grant", "items.write"
    mandate_id: str | None
    record_refs: tuple[str, ...]  # content hashes
    egress: str                  # Egress
    model: str | None            # "provider/deployment" as reported by the gateway
    privacy: str | None          # privacy class the gateway enforced
    detail: str                  # short, never content: "3 records", "refused: no local model"
    prev_hash: str
    hash: str
    signature: str


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    count: int
    bad_seq: int | None
    reason: str


class ReceiptLog(Protocol):
    def append(self, actor: str, action: str, *, mandate_id: str | None = None, record_refs: tuple[str, ...] = (), egress: str = "none", model: str | None = None, privacy: str | None = None, detail: str = "") -> Receipt: ...
    def list(self, limit: int = 50, before_seq: int | None = None) -> list[Receipt]: ...
    def verify(self) -> VerifyResult: ...


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider: str | None
    deployment: str | None
    privacy: str | None
    tokens_in: int | None = None
    tokens_out: int | None = None


class LLMRefused(Exception):
    """The gateway refused on policy (no allowed model for this privacy class)."""


class LLMUnavailable(Exception):
    """The gateway or every allowed model is unreachable or rate-limited."""


class LLM(Protocol):
    def complete(self, messages: list[dict[str, str]], *, privacy: str, alias: str = "smart", max_tokens: int = 800) -> LLMResult: ...


@dataclass(frozen=True)
class ProposedChange:
    """A write the bot wants to make. Nothing is written until the user confirms."""

    id: str
    kind: str                    # "add_item" | "update_item" | "delete_item" | "add_record"
    summary: str                 # shown to the user
    payload: dict[str, Any]


@dataclass(frozen=True)
class BotReply:
    text: str
    cited_records: tuple[str, ...] = ()       # record ids the answer drew on
    proposed: tuple[ProposedChange, ...] = ()
    safety: str | None = None                 # None | "urgent" | "declined_medical_advice" | "no_model"
    receipts: tuple[int, ...] = ()            # receipt seq numbers produced by this turn
    route: str | None = None                  # "provider/deployment (privacy)" for display
