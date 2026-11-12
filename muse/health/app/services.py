"""What the web server needs from the rest of the health stack.

The server never imports vault / mandates / receipts / bot / importers itself.
It receives a `Services` container, so tests plug in fakes and production plugs
in the real modules through `build_services` (the one factory function).
"""

from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from muse.health.contracts import BotReply, Mandate, MandateBook, ReceiptLog, Tenant, Vault

APP_VERSION = "0.1.0"


class ServicesUnavailable(RuntimeError):
    """The real health modules are not installed (yet)."""


class NotSupported(Exception):
    """This backend cannot do what was asked (for example change a passphrase)."""


@dataclass(frozen=True)
class ImportedDoc:
    """One record an importer found in a file. Nothing is saved until confirmed."""

    kind: str
    title: str
    text: str
    occurred_on: str | None = None
    source: str = ""


class Bot(Protocol):
    def chat(self, user_text: str, history: list[dict[str, str]]) -> BotReply: ...
    def confirm(self, change_id: str) -> Any: ...
    def reject(self, change_id: str) -> Any: ...


@dataclass
class Runtime:
    """Everything that exists only while one vault is unlocked."""

    vault: Vault
    mandates: MandateBook
    receipts: ReceiptLog
    bot: Bot


@dataclass
class Services:
    tenant: Tenant
    vault_exists: Callable[[], bool]
    create_vault: Callable[[str], Vault]                      # returns an unlocked vault
    open_vault: Callable[[], Vault]                           # returns a locked vault; caller unlocks
    make_runtime: Callable[[Vault], Runtime]
    parse_import: Callable[[str, bytes], list[ImportedDoc]]   # raises ValueError for unreadable files
    grant_default_mandate: Callable[[MandateBook], Mandate]
    change_passphrase: Callable[[Vault, str, str], None]      # raises NotSupported / WrongPassphrase
    destroy_vault: Callable[[Vault], None]                    # gets the verified, unlocked vault
    version: str = APP_VERSION


# --------------------------------------------------------------------------
# Production wiring. Imports are lazy so this file loads before the sibling
# modules exist, and fails with a readable message if they are missing.
# --------------------------------------------------------------------------

def _need(module: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ServicesUnavailable(
            f"the health module {module!r} is not available yet; "
            "the web app cannot run with real data until it is installed"
        ) from exc


def _first(mod: Any, *names: str) -> Any:
    for n in names:
        if hasattr(mod, n):
            return getattr(mod, n)
    raise ServicesUnavailable(f"{mod.__name__} has none of: {', '.join(names)}")


def build_services(tenant: Tenant, vault_path: str | os.PathLike[str], environ: Mapping[str, str] | None = None) -> Services:
    env = os.environ if environ is None else environ
    path = Path(vault_path)
    vault_mod = _need("muse.health.vault")
    mand_mod = _need("muse.health.mandates")
    rcpt_mod = _need("muse.health.receipts")
    imp_mod = _need("muse.health.importers")
    bot_mod = _need("muse.health.bot")
    LocalVault = vault_mod.LocalVault
    LocalMandateBook = mand_mod.LocalMandateBook
    LocalReceiptLog = rcpt_mod.LocalReceiptLog
    HealthBot = bot_mod.HealthBot
    llm = _make_llm(env)

    def vault_exists() -> bool:
        return path.exists()

    def create_vault(passphrase: str) -> Vault:
        return LocalVault.create(path, passphrase)

    def open_vault() -> Vault:
        return LocalVault.open(path)

    def make_runtime(vault: Vault) -> Runtime:
        mandates = LocalMandateBook(vault)
        receipts = LocalReceiptLog(vault)
        bot = HealthBot(vault, mandates, receipts, llm, tenant)
        return Runtime(vault, mandates, receipts, bot)

    def parse_import(filename: str, data: bytes) -> list[ImportedDoc]:
        # importers.ImportFailed is a ValueError, which the server turns into a safe 422.
        return [ImportedDoc(d.kind, d.title, d.text, d.occurred_on, d.source)
                for d in imp_mod.import_file(data, filename, locale=tenant.locale)]

    default_spec = _first(mand_mod, "default_bot_mandate")

    def grant_default(book: MandateBook) -> Mandate:
        spec = default_spec(tenant)
        if isinstance(spec, Mandate):
            return spec
        get = (lambda k, dflt=None: spec.get(k, dflt)) if isinstance(spec, dict) else (lambda k, dflt=None: getattr(spec, k, dflt))
        return book.grant(get("grantee", "bot"), tuple(get("scopes", ())), str(get("purpose", "")),
                          tuple(get("record_kinds", ()) or ()), get("expires_at"))

    def change_passphrase(vault: Vault, old: str, new: str) -> None:
        fn = getattr(vault, "change_passphrase", None)
        if fn is None:
            raise NotSupported("change_passphrase")
        fn(old, new)

    def destroy_vault(vault: Vault) -> None:
        fn = getattr(vault, "destroy", None)      # crypto-shred, zero, unlink
        if fn is not None:
            fn()
        elif path.is_file():
            path.unlink()

    return Services(tenant, vault_exists, create_vault, open_vault, make_runtime, parse_import,
                    grant_default, change_passphrase, destroy_vault)


def _make_llm(env: Mapping[str, str]) -> Any:
    return _need("muse.health.llm").GatewayLLM(environ=env)
