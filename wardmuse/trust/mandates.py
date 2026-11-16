"""LocalMandateBook: what the user allowed the bot (or anyone else) to do.

Stored in the vault file's `mandates` table. Policy fields (grantee, scopes,
kinds, times) are plaintext so they can be audited; the purpose text is
encrypted. Every row carries an Ed25519 signature over canonical JSON of all
other fields, made with the vault key. Revocation re-signs the row with
revoked_at set, so the signed object always states whether it is revoked.

A signed "book head" (digest over every row's id and signature) lives in the
meta table and is re-signed on every grant/revoke. If a row is deleted, or an
old signed copy of a row is put back (e.g. to undo a revocation), the head no
longer matches and check() denies everything until the user looks at it.
Residual: an attacker who rolls back the whole file to an older consistent
state (rows and head together) is not detected here; anchoring the head
elsewhere is the mitigation (see receipts.py).

check() fails closed: unknown scope, unknown egress, locked vault, a broken
book head -> denied. A row whose signature does not verify is ignored and
listed by problems(), never honoured.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from typing import Any

from wardmuse.trust import crypto
from wardmuse.trust.contracts import SCOPES, Action, Decision, Mandate, VaultError
from wardmuse.trust.vault import (
    RECORD_KINDS,
    LocalVault,
    check_id,
    has_control_chars,
    iso_utc,
    new_id,
    parse_iso_utc,
)

MANDATE_DOMAIN = "mandate"
HEAD_DOMAIN = "mandate-head"
EGRESS_SCOPE = {"none": None, "local": "llm.local", "private": "llm.private", "cloud": "llm.cloud"}
MAX_PURPOSE_CHARS = 500
_CONTENT_SCOPES = ("vault.read", "vault.write", "llm.local", "llm.private", "llm.cloud")
_GRANTEE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

# Explicit field list for the canonical form. Order does not matter (keys are
# sorted) but the set does: adding a field is a format change.
_FIELDS = ("id", "grantee", "scopes", "purpose", "record_kinds", "created_at", "expires_at", "revoked_at")


def canonical_mandate(mandate: Mandate) -> bytes:
    """Sorted keys, no whitespace, UTF-8; scopes and kinds as JSON arrays."""
    obj: dict[str, Any] = {"domain": "muse-health/mandate/v1"}
    for name in _FIELDS:
        value = getattr(mandate, name)
        obj[name] = list(value) if isinstance(value, tuple) else value
    return crypto.canonical_json(obj)


def verify(mandate: Mandate, public_key: bytes) -> bool:
    """Pure: does `mandate.signature` verify under `public_key`? Usable by a third party."""
    try:
        sig = crypto.b64d(mandate.signature)
    except VaultError:
        return False
    return crypto.verify_signature(public_key, MANDATE_DOMAIN, canonical_mandate(mandate), sig)


def _head_digest(pairs: list[tuple[str, str]]) -> str:
    return crypto.sha256_hex(crypto.canonical_json(sorted(pairs)))


class LocalMandateBook:
    def __init__(self, vault: LocalVault, clock: Callable[[], float] | None = None) -> None:
        self.vault = vault
        self.clock = clock or vault.clock
        self._problems: list[str] = []

    # ------------------------------------------------------------ helpers

    def _row_to_mandate(self, row: Any) -> Mandate:
        purpose = self.vault.decrypt_field("mandates", row["id"], "purpose", row["purpose"]).decode("utf-8")
        return Mandate(
            id=row["id"], grantee=row["grantee"], scopes=tuple(json.loads(row["scopes"])), purpose=purpose,
            record_kinds=tuple(json.loads(row["record_kinds"])), created_at=row["created_at"],
            expires_at=row["expires_at"], revoked_at=row["revoked_at"], signature=row["signature"],
        )

    def _sign(self, m: Mandate) -> str:
        return crypto.b64e(self.vault.sign_with_domain(MANDATE_DOMAIN, canonical_mandate(m)))

    def _write_head(self, conn: Any) -> None:
        pairs = [(r["id"], r["signature"]) for r in conn.execute("SELECT id, signature FROM mandates")]
        digest = _head_digest(pairs)
        sig = self.vault.sign_with_domain(HEAD_DOMAIN, digest.encode("ascii"))
        value = json.dumps({"digest": digest, "count": len(pairs), "signature": crypto.b64e(sig)})
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('mandate_head', ?)", (value,))

    def _load(self) -> tuple[list[Mandate], bool]:
        """(verified mandates, head_ok). Records problems for problems()."""
        self._problems = []
        with self.vault._read() as conn:
            rows = conn.execute("SELECT * FROM mandates ORDER BY created_at, id").fetchall()
            head_row = conn.execute("SELECT value FROM meta WHERE key='mandate_head'").fetchone()
        pk = self.vault.public_key()
        head_ok = True
        pairs = [(r["id"], r["signature"]) for r in rows]
        if head_row is None:
            if rows:
                head_ok = False
                self._problems.append("mandate book head is missing")
        else:
            try:
                head = json.loads(head_row["value"])
                digest = str(head["digest"])
                sig_ok = crypto.verify_signature(pk, HEAD_DOMAIN, digest.encode("ascii"), crypto.b64d(head["signature"]))
            except (ValueError, KeyError, TypeError, VaultError):
                sig_ok, digest = False, ""
            if not sig_ok or not crypto.ct_equal(digest, _head_digest(pairs)):
                head_ok = False
                self._problems.append("mandate book head does not match its rows (a row was removed or rolled back)")
        out = []
        for row in rows:
            try:
                m = self._row_to_mandate(row)
            except (VaultError, ValueError, UnicodeDecodeError):
                self._problems.append(f"mandate {row['id']}: purpose could not be decrypted; ignored")
                continue
            if not verify(m, pk):
                self._problems.append(f"mandate {row['id']}: signature does not verify; ignored")
                continue
            out.append(m)
        return out, head_ok

    # ------------------------------------------------------------ MandateBook

    def grant(self, grantee: str, scopes: tuple[str, ...], purpose: str, record_kinds: tuple[str, ...] = (),
              expires_at: str | None = None, *, allow_cloud: bool = False) -> Mandate:
        if not isinstance(grantee, str) or not _GRANTEE_RE.match(grantee):
            raise VaultError("grantee must be 1-64 letters, digits or . _ : -")
        scopes = tuple(scopes)
        if not scopes:
            raise VaultError("a mandate needs at least one scope")
        for s in scopes:
            if s not in SCOPES:
                raise VaultError(f"unknown scope {s!r}")
        if "llm.cloud" in scopes and not allow_cloud:
            raise VaultError("llm.cloud can only be granted when the issuer allows cloud models")
        kinds = tuple(record_kinds)
        for k in kinds:
            if k not in RECORD_KINDS:
                raise VaultError(f"unknown record kind {k!r}")
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > MAX_PURPOSE_CHARS or has_control_chars(purpose, allow_newlines=True):
            raise VaultError(f"purpose must be 1-{MAX_PURPOSE_CHARS} characters")
        now = self.clock()
        if expires_at is not None and parse_iso_utc(expires_at) <= now:
            raise VaultError("expires_at must be in the future")
        m = Mandate(
            id=new_id(), grantee=grantee, scopes=tuple(sorted(set(scopes))), purpose=purpose,
            record_kinds=tuple(sorted(set(kinds))), created_at=iso_utc(now), expires_at=expires_at,
            revoked_at=None, signature="",
        )
        m = _replace_sig(m, self._sign(m))
        with self.vault._write() as conn:
            ct = self.vault.encrypt_field(conn, "mandates", m.id, "purpose", purpose.encode("utf-8"))
            conn.execute(
                "INSERT INTO mandates (id, grantee, scopes, record_kinds, created_at, expires_at, revoked_at, purpose, signature)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (m.id, m.grantee, json.dumps(list(m.scopes)), json.dumps(list(m.record_kinds)), m.created_at,
                 m.expires_at, None, ct, m.signature),
            )
            self._write_head(conn)
        return m

    def revoke(self, mandate_id: str) -> None:
        """Immediate. Re-signs the mandate with revoked_at; already revoked = no-op."""
        check_id(mandate_id)
        self.vault._require_keys()
        with self.vault._write() as conn:
            row = conn.execute("SELECT * FROM mandates WHERE id=?", (mandate_id,)).fetchone()
            if row is None:
                raise VaultError("no such mandate")
            m = self._row_to_mandate(row)
            if not verify(m, self.vault.public_key()):
                raise VaultError("this mandate failed verification; it is already ignored")
            if m.revoked_at is not None:
                return
            revoked = dataclasses.replace(m, revoked_at=iso_utc(self.clock()), signature="")
            revoked = _replace_sig(revoked, self._sign(revoked))
            conn.execute("UPDATE mandates SET revoked_at=?, signature=? WHERE id=?", (revoked.revoked_at, revoked.signature, mandate_id))
            self._write_head(conn)

    def all(self) -> list[Mandate]:
        """Every verified mandate, revoked ones included (oldest first). Needs the
        vault unlocked (purpose is encrypted). Rows that fail are in problems()."""
        self.vault._require_keys()
        return self._load()[0]

    def problems(self) -> list[str]:
        """Integrity problems seen by the last all()/check(); safe to show (ids only)."""
        return list(self._problems)

    def check(self, grantee: str, action: Action) -> Decision:
        if action.scope not in SCOPES:
            return Decision(False, None, f"Unknown permission {action.scope!r}; refused.")
        if action.egress not in EGRESS_SCOPE:
            return Decision(False, None, f"Unknown destination {action.egress!r}; refused.")
        for k in action.record_kinds:
            if k not in RECORD_KINDS:
                return Decision(False, None, f"Unknown record kind {k!r}; refused.")
        if not self.vault.is_unlocked():
            return Decision(False, None, "The vault is locked.")
        try:
            mandates, head_ok = self._load()
        except VaultError:
            return Decision(False, None, "The permission list could not be read; refused.")
        if not head_ok:
            return Decision(False, None, "The permission list failed its integrity check; nothing is allowed until you review it.")
        now = self.clock()
        needed = {action.scope}
        egress_scope = EGRESS_SCOPE[action.egress]
        if egress_scope:
            needed.add(egress_scope)
        active = []
        for m in mandates:
            if m.grantee != grantee or m.revoked_at is not None:
                continue
            try:
                if m.expires_at is not None and parse_iso_utc(m.expires_at) <= now:
                    continue
                if parse_iso_utc(m.created_at) > now + 300:
                    continue   # dated in the future: do not trust it
            except VaultError:
                continue
            active.append(m)
        if not active:
            return Decision(False, None, f"No active permission has been granted to {grantee}.")
        reason = f"No active permission allows {action.scope}."
        for m in sorted(active, key=lambda m: m.created_at, reverse=True):
            if action.scope not in m.scopes:
                continue
            if egress_scope and egress_scope not in m.scopes:
                reason = f"No active permission allows sending record content to a {action.egress} model."
                continue
            if m.record_kinds and not action.record_kinds and action.scope in _CONTENT_SCOPES:
                # Unspecified kinds against a kind-limited mandate: fail closed.
                reason = "This permission covers only some record kinds and the request did not say which."
                continue
            if m.record_kinds and not set(action.record_kinds) <= set(m.record_kinds):
                reason = f"No active permission covers these record kinds: {', '.join(sorted(set(action.record_kinds) - set(m.record_kinds)))}."
                continue
            return Decision(True, m.id, f"Allowed by your permission of {m.created_at[:10]}.")
        return Decision(False, None, reason)


def _replace_sig(m: Mandate, signature: str) -> Mandate:
    return dataclasses.replace(m, signature=signature)
