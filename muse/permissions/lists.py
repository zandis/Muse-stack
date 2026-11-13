"""The revocation list and the spent set (interface document, sections 7 and 8).

Twin of core/src/permissions/lists.rs. Neither touches storage: both are values the
caller loads and saves. A revocation entry is a one-block Biscuit signed by the root
key, so signing and verifying are the library's, not ours.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from muse.permissions.model import PermissionError, is_revocation_id, is_time
from muse.permissions.token import _lib, parse_public_key, parse_rfc3339, public_key_text, rfc3339


def _entry_text(ident: str, at: int, signer: str) -> str:
    return f'schema("muse.revocation", 1);\nrevoked("{ident}", {rfc3339(at)});\nsigner("{signer}");\n'


@dataclass(frozen=True)
class RevocationEntry:
    """One signed statement "this identifier is revoked"."""

    id: str
    at: int
    signer: str
    token: str

    @staticmethod
    def sign(ident: str, at: int, root: Any) -> "RevocationEntry":
        """Sign a new entry with the root key pair."""
        if not is_revocation_id(ident) or not is_time(at):
            raise PermissionError("invalid_revocation", "a revocation needs a 128-hex identifier and a time")
        signer = public_key_text(root)
        source = f'schema("muse.revocation", 1);\nrevoked({{id}}, {rfc3339(at)});\nsigner({{signer}});\n'
        try:
            builder = _lib().BiscuitBuilder()
            builder.add_code(source, {"id": ident, "signer": signer})
            token = builder.build(root.private_key).to_base64()
        except Exception as exc:
            raise PermissionError("library_error", str(exc)) from exc
        return RevocationEntry.verify(token, signer)

    @staticmethod
    def verify(token: str, root: str) -> "RevocationEntry":
        """Verify an entry under the root public key and read it."""

        def bad(why: str) -> PermissionError:
            return PermissionError("invalid_revocation", why)

        key = parse_public_key(root)
        lib = _lib()
        if not isinstance(token, str):
            raise bad("not a token")
        try:
            unverified = lib.UnverifiedBiscuit.from_base64(token)
        except Exception:
            raise bad("not a token") from None
        try:
            biscuit = unverified.verify(key)
        except Exception:
            raise bad("not signed by the root key") from None
        if biscuit.block_count() != 1:
            raise bad("an entry has exactly one block")
        text = biscuit.block_source(0)
        head = 'schema("muse.revocation", 1);\nrevoked("'
        if not text.startswith(head) or '", ' not in text[len(head):]:
            raise bad("not a muse.revocation entry")
        ident, rest = text[len(head):].split('", ', 1)
        if ");\n" not in rest:
            raise bad("not a muse.revocation entry")
        at = parse_rfc3339(rest.split(");\n", 1)[0])
        if at is None:
            raise bad("bad time")
        if not is_revocation_id(ident) or text != _entry_text(ident, at, root):
            raise bad("not in the schema's form, or names another signer")
        return RevocationEntry(ident, at, root, token)


class RevocationList:
    """A set of revocation entries. It only grows; merging two lists is set union."""

    def __init__(self) -> None:
        self._entries: dict[str, RevocationEntry] = {}

    def add(self, ident: str, at: int, root: Any) -> None:
        """Revoke `ident` (a block's revocation identifier) at time `at`, signed by the root key pair."""
        self._insert(RevocationEntry.sign(ident, at, root))

    def add_entry(self, token: str, root: str) -> None:
        """Add an entry received from elsewhere, after verifying it under the root public key."""
        self._insert(RevocationEntry.verify(token, root))

    def _insert(self, entry: RevocationEntry) -> None:
        # For one identifier keep the earlier time, then the smaller text: any order of merges converges.
        old = self._entries.get(entry.id)
        if old is None or (entry.at, entry.token) < (old.at, old.token):
            self._entries[entry.id] = entry

    def merge(self, other: "RevocationList") -> None:
        """Set union with another list (whose entries were verified when it was built)."""
        for entry in other._entries.values():
            self._insert(entry)

    def verify(self, root: str) -> None:
        """Verify every entry again under `root`; raises on the first that fails."""
        for entry in self._entries.values():
            RevocationEntry.verify(entry.token, root)

    def contains(self, ident: str) -> bool:
        return ident in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RevocationList) and self._entries == other._entries

    def entries(self) -> list[RevocationEntry]:
        """Entries sorted by identifier."""
        return [self._entries[key] for key in sorted(self._entries)]

    def to_json(self) -> str:
        """`{"v": 1, "entries": [...]}`, entries sorted by identifier."""
        return json.dumps({"v": 1, "entries": [e.token for e in self.entries()]}, separators=(",", ":"), sort_keys=True)

    @staticmethod
    def from_json(text: str, root: str) -> "RevocationList":
        """Load a list, verifying every entry. One bad entry refuses the whole file."""
        bad = PermissionError("invalid_revocation", "not a revocation list")
        try:
            value = json.loads(text)
        except ValueError:
            raise bad from None
        if not isinstance(value, dict) or type(value.get("v")) is not int or value["v"] != 1 or not isinstance(value.get("entries"), list):
            raise bad
        out = RevocationList()
        for token in value["entries"]:
            if not isinstance(token, str):
                raise bad
            out.add_entry(token, root)
        return out


class SpentSet:
    """Single-use ids already used, each kept until its token has expired.

    Bounded: when it is full of live entries, `insert` fails and the caller must refuse
    the request.
    """

    def __init__(self, capacity: int = 10_000) -> None:
        self.capacity = capacity
        self._entries: dict[str, int] = {}

    def contains(self, key: str, now: int) -> bool:
        """True if `key` was used and its token could still be valid at `now`."""
        until = self._entries.get(key)
        return until is not None and until >= now

    def purge(self, now: int) -> None:
        """Drop entries whose time has passed."""
        self._entries = {k: v for k, v in self._entries.items() if v >= now}

    def has_room(self, extra: int, now: int) -> bool:
        """Whether `extra` more entries fit once expired ones are dropped."""
        return sum(1 for v in self._entries.values() if v >= now) + extra <= self.capacity

    def insert(self, key: str, until: int, now: int) -> None:
        """Remember `key` until `until`."""
        self.purge(now)
        if key not in self._entries and len(self._entries) >= self.capacity:
            raise PermissionError("spent_set_full", "no room to remember another single-use id")
        self._entries[key] = max(self._entries.get(key, until), until)

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[tuple[str, int]]:
        """(key, until) pairs sorted by key, for the caller to store."""
        return sorted(self._entries.items())

    @staticmethod
    def from_entries(entries: list, capacity: int = 10_000) -> "SpentSet":
        out = SpentSet(capacity)
        out._entries = {str(k): int(v) for k, v in entries}
        return out
