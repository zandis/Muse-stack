"""The home's own chain: its device key, its SQLite store, and the few operations the CLI needs.

Key custody today: MUSE_HOME/device.key, a 0600 JSON file holding the
Ed25519 seed this home signs with and the reference key for resource
references. It is used only through DeviceKeyFile, which is a
`format.Signer` and a `format.RefKey`; the vault takes over by supplying
another object with the same three members (device, sign, mac). The reference
key is per vault in the design; until the vault exists the home has its own,
so references made here cannot be compared with another device's.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from muse import paths
from muse.records import format as fmt
from muse.records.errors import RecordError
from muse.records.merkle import merkle_root
from muse.records.seal import checkpoint, export, verify_checkpoint
from muse.records.store import SqliteRecordStore

KEY_FILE_TYPE = "muse.device-key"


def records_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "records.sqlite"


def device_key_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / "device.key"


class DeviceKeyFile:
    """The home's signing key and reference key, read from a 0600 file."""

    def __init__(self, seed: bytes, ref_key: bytes) -> None:
        self._signer = fmt.SeedSigner(seed)
        self._ref_key = bytes(ref_key)
        self.device = self._signer.device

    def sign(self, message: bytes) -> bytes:
        return self._signer.sign(message)

    def mac(self, message: bytes) -> bytes:
        return hmac.new(self._ref_key, message, hashlib.sha256).digest()

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> DeviceKeyFile | None:
        """The key in `path`; None when there is no such file. ValueError when the file is not a key file."""
        try:
            raw = Path(path).read_text(encoding="ascii")
        except FileNotFoundError:
            return None
        try:
            data = json.loads(raw)
            if data["type"] != KEY_FILE_TYPE or data["v"] != 1:
                raise ValueError
            seed, ref_key = bytes.fromhex(data["seed"]), bytes.fromhex(data["ref_key"])
            if len(seed) != 32 or len(ref_key) != 32:
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise ValueError("device.key is not a Muse device key file") from None  # never echo its content
        return cls(seed, ref_key)

    @classmethod
    def load_or_create(cls, path: str | os.PathLike[str]) -> DeviceKeyFile:
        """Load the key, creating it on first use. Two processes starting at once end with the same key:
        the file is written under a temporary name and hard-linked into place, which fails if it exists."""
        path = Path(path)
        found = cls.load(path)
        if found is not None:
            return found
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps({"type": KEY_FILE_TYPE, "v": 1, "seed": secrets.token_bytes(32).hex(),
                           "ref_key": secrets.token_bytes(32).hex()})
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(body + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(str(tmp), str(path))
            except FileExistsError:
                pass  # another process got there first: use its key
        finally:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
        found = cls.load(path)
        if found is None:
            raise OSError("could not create the device key file")
        return found


class HomeRecords:
    """The home's chain. `keys` is anything with device, sign and mac (default: the key file)."""

    def __init__(self, environ: Mapping[str, str] | None = None, *, clock: Callable[[], float] = time.time,
                 keys: Any = None, path: str | os.PathLike[str] | None = None) -> None:
        self.environ = environ
        self.clock = clock
        self._keys = keys
        self.store = SqliteRecordStore(path if path is not None else records_path(environ))

    @property
    def keys(self) -> Any:
        if self._keys is None:
            self._keys = DeviceKeyFile.load_or_create(device_key_path(self.environ))
        return self._keys

    def _now_ms(self) -> int:
        return int(self.clock() * 1000)

    def write(self, action: str, summary: str, *, actor: str = "person", outcome: str = "ok", mandate: str | None = None,
              resource: str | None = None, cost: Mapping[str, Any] | None = None, links: Mapping[str, str] | None = None,
              egress: str | None = None) -> dict[str, Any]:
        """Append one record to the home's chain and return it.

        `egress` says where the content of the action went (`none`, `home`, `service:<id>`, `host:<name>`);
        leave it out when the writer does not know."""
        keys = self.keys
        entry: dict[str, Any] = {"actor": actor, "action": action, "outcome": outcome, "summary": summary,
                                 "mandate": mandate, "resource": resource, "cost": cost}
        if links:
            entry["links"] = dict(links)
        if egress is not None:
            entry["egress"] = egress
        now = self._now_ms()

        def make(head: dict[str, Any] | None) -> dict[str, Any]:
            # A clock that was set back must not break the chain: never go below the head's time.
            entry["time"] = now if head is None else max(now, head["time"])
            return fmt.append(head, entry, keys, keys)

        return self.store.append_with(make)

    def records(self) -> list[dict[str, Any]]:
        return self.store.all()

    def device(self) -> str | None:
        """This home's device id, without creating a key."""
        if self._keys is not None:
            return self._keys.device
        found = DeviceKeyFile.load(device_key_path(self.environ))
        return None if found is None else found.device

    def verify(self) -> dict[str, Any]:
        """Check the whole chain and the last checkpoint against it.

        Returns {size, device, checkpoint}; RecordError (kind, index) for the first problem. A chain cut
        back below the last checkpoint is size_mismatch; one rewritten under it is root_mismatch."""
        verifier = fmt.ChainVerifier(self.device())
        for record in self.store.all():
            verifier.push(record)
        cp = self.store.last_checkpoint()
        if cp is not None:
            try:
                verify_checkpoint(cp, verifier.device)
            except RecordError as exc:
                raise RecordError("bad_checkpoint", detail=exc.kind) from None
            if cp["size"] > verifier.size:
                raise RecordError("size_mismatch", detail="records sealed earlier are gone")
            if merkle_root(verifier.hashes[:cp["size"]]) != cp["root"]:
                raise RecordError("root_mismatch", detail="the sealed records have changed")
        return {"size": verifier.size, "device": verifier.device, "checkpoint": cp}

    def seal(self) -> dict[str, Any]:
        """Verify, then sign and keep a checkpoint over every record so far."""
        state = self.verify()
        records = self.store.all()[:state["size"]]
        cp = checkpoint([fmt.record_hash(r) for r in records], self._now_ms(), self.keys)
        self.store.add_checkpoint(cp)
        return cp

    def export(self, keep: Callable[[dict[str, Any]], bool] | None = None, *, redact_summaries: bool = False) -> dict[str, Any]:
        """Seal, then export: everything, or the records `keep` accepts with their inclusion proofs."""
        cp = self.seal()
        records = self.store.all()[:cp["size"]]
        select = None if keep is None else [r["seq"] for r in records if keep(r)]
        return export(records, cp, select, redact_summaries)


def record_pause(environ: Mapping[str, str] | None, paused: bool, clock: Callable[[], float] = time.time,
                 chain: HomeRecords | None = None) -> dict[str, Any]:
    """The record a pause or a resume leaves. One function for `muse pause` / `muse resume` and for the
    home API's pause and resume calls, so both write the same thing. Raises when it cannot be written;
    the caller decides what that means (the pause itself has already happened and must stand).

    `chain`: the home's chain when the caller already holds it; otherwise the one under `environ`."""
    action, summary = ("home.pause", "Paused the queue") if paused else ("home.resume", "Resumed the queue")
    chain = chain if chain is not None else HomeRecords(environ, clock=clock)
    return chain.write(action, summary, actor="person", egress="none")
