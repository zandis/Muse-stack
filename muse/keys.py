"""Where the user's API keys live, and how to read them without leaking them.

Why this module exists: keys must never land in argv, logs, snapshots or
exception text. Only `export_env()` (and the private readers behind it) touch
values; every other function answers with names, booleans and sources.

Lookup order everywhere is: process environment, then the OS keychain (via the
optional `keyring` package), then the dotenv file. The environment wins so a
one-off `GROQ_API_KEY=... muse up` behaves as people expect.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from muse.paths import CATALOG_PATH, REPO_ROOT

SERVICE = "muse-stack"
MASTER_KEY = "LITELLM_MASTER_KEY"

# Keys that appear in .env.example but not in the routed catalogue.
_EXTRA_KEYS: dict[str, tuple[str | None, str]] = {
    MASTER_KEY: (None, ""),
    # The queue runner's own app token (muse/queue.py). Stored here so it lives in the keychain, not in a plain file.
    "MUSE_QUEUE_APP_TOKEN": (None, ""),
    "MUSE_QUEUE_PAID_APP_TOKEN": (None, ""),
    # The skill runner's own app tokens (muse/skills/app.py), kept the same way.
    "MUSE_SKILLS_APP_TOKEN": (None, ""),
    "MUSE_SKILLS_PAID_APP_TOKEN": (None, ""),
    "MUSE_SKILLS_BACKGROUND_APP_TOKEN": (None, ""),
    "DEEPGRAM_API_KEY": ("deepgram", "https://console.deepgram.com/signup"),
    "ELEVENLABS_API_KEY": ("elevenlabs", "https://elevenlabs.io/app/settings/api-keys"),
    "ASSEMBLYAI_API_KEY": ("assemblyai", "https://www.assemblyai.com/app/account"),
}

# LiteLLM 1.108.x refuses published examples; we also call them weak.
WEAK_MASTER_KEYS = frozenset({"sk-1234", "sk-12345", "changeme", "change-me", "password", "secret", "master", "sk-test"})

# Light shape hints. A mismatch warns; it never blocks (providers change formats).
_SHAPES: dict[str, tuple[str, str]] = {
    "GROQ_API_KEY": ("gsk_", "Groq keys start with gsk_"),
    "GEMINI_API_KEY": ("AIza", "Google AI Studio keys start with AIza"),
    "OPENROUTER_API_KEY": ("sk-or-", "OpenRouter keys start with sk-or-"),
    "JINA_API_KEY": ("jina_", "Jina keys start with jina_"),
    "VOYAGE_API_KEY": ("pa-", "Voyage keys start with pa-"),
    "NVIDIA_NIM_API_KEY": ("nvapi-", "NVIDIA keys start with nvapi-"),
    "DEEPSEEK_API_KEY": ("sk-", "DeepSeek keys start with sk-"),
    "SILICONFLOW_API_KEY": ("sk-", "SiliconFlow keys start with sk-"),
}

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class KeysError(Exception):
    """Raised for user-fixable problems. Messages never contain key values."""


def _load_known() -> dict[str, dict[str, str | None]]:
    """name -> {provider, key_url}, from the catalogue plus the extras."""
    known: dict[str, dict[str, str | None]] = {}
    try:
        data = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
        for provider in data.get("providers", []):
            env = provider.get("env")
            if env:
                known[str(env)] = {"provider": str(provider["id"]), "key_url": str(provider.get("key_url", ""))}
            # Catalogue v2: extra settings a provider needs (Cloudflare's account id).
            for extra in provider.get("also_env") or []:
                known.setdefault(str(extra), {"provider": str(provider["id"]), "key_url": str(provider.get("key_url", ""))})
    except (OSError, yaml.YAMLError, AttributeError, KeyError, TypeError):
        pass  # a broken catalogue must not stop `muse keys set`; extras still work
    for name, (provider, url) in _EXTRA_KEYS.items():
        known.setdefault(name, {"provider": provider, "key_url": url})
    return known


KNOWN_KEYS: dict[str, dict[str, str | None]] = _load_known()


# ---------------------------------------------------------------- validation


def validate_name(name: str, force: bool = False) -> str:
    """Return the name or raise. Unknown names need force=True."""
    if not _NAME_RE.match(name or ""):
        raise KeysError("Key names are upper-case letters, digits and underscores, e.g. GROQ_API_KEY.")
    if name not in KNOWN_KEYS and not force:
        raise KeysError(f"{name} is not a key this stack uses. Known: {', '.join(sorted(KNOWN_KEYS))}. Use --force to store it anyway.")
    return name


def clean_value(raw: str) -> str:
    """Strip whitespace, matching quotes and trailing newlines from a pasted value."""
    value = (raw or "").strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    if not value:
        raise KeysError("The value is empty.")
    if "\n" in value or "\r" in value:
        raise KeysError("The value contains a line break. Paste only the key.")
    return value


def shape_hint(name: str, value: str) -> str | None:
    """A warning when the value does not look like this provider's keys. Never includes the value."""
    shape = _SHAPES.get(name)
    if shape and not value.startswith(shape[0]):
        return f"{shape[1]}; this value does not. Saved anyway."
    if name == MASTER_KEY and value.lower() in WEAK_MASTER_KEYS:
        return "That is a well-known weak master key. Use `muse up` to generate a strong one."
    return None


def expected_prefix(name: str) -> str | None:
    """What the provider's keys are known to start with, or None when no shape is known."""
    shape = _SHAPES.get(name)
    return shape[0] if shape else None


def generate_master_key() -> str:
    return "sk-muse-" + secrets.token_urlsafe(32)


def is_weak_master_key(value: str) -> bool:
    return value.strip().lower() in WEAK_MASTER_KEYS or len(value.strip()) < 16


# ------------------------------------------------------------------- dotenv


def env_file_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = env.get("MUSE_ENV_FILE", "").strip()
    return Path(raw).expanduser() if raw else REPO_ROOT / ".env"


_ASSIGN_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _parse_assignment(line: str) -> tuple[str, str] | None:
    """Parse like scripts/render_config.py::load_dotenv: strip, one pair of quotes, no comments."""
    if line.lstrip().startswith("#"):
        return None
    match = _ASSIGN_RE.match(line.rstrip("\r\n"))
    if not match:
        return None
    value = match.group(2).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return match.group(1), value


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def _dotenv_items(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """All assignments in the dotenv file. First occurrence wins, as in the renderer."""
    out: dict[str, str] = {}
    for line in _read_lines(env_file_path(environ)):
        parsed = _parse_assignment(line)
        if parsed and parsed[0] not in out:
            out[parsed[0]] = parsed[1]
    return out


def file_mode_warning(path: Path) -> str | None:
    """Warn when a secrets file can be read by other local users."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    if mode & 0o077:
        return f"{path} is readable by other users (mode {mode:04o}). Run: chmod 600 {path}"
    return None


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write via temp file + os.replace so a crash never leaves a half-written file.

    Refuses to write through a symlink: replacing a link would silently
    redirect secrets (or the file) somewhere the user did not choose.
    """
    if path.is_symlink():
        raise KeysError(f"{path} is a symlink; refusing to write through it. Replace it with a regular file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _format_assignment(name: str, value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./~-]+", value):
        return f"{name}={value}"
    if '"' not in value:
        return f'{name}="{value}"'
    if "'" not in value:
        return f"{name}='{value}'"
    raise KeysError("The value mixes quote characters and cannot be stored in a dotenv file.")


def _dotenv_set(name: str, value: str, environ: Mapping[str, str] | None) -> list[str]:
    path = env_file_path(environ)
    warnings: list[str] = []
    existing = path.exists()
    if existing:
        warning = file_mode_warning(path)
        if warning:
            warnings.append(warning)
    lines = _read_lines(path)
    out: list[str] = []
    replaced = False
    for line in lines:
        parsed = _parse_assignment(line)
        if parsed and parsed[0] == name:
            if replaced:
                continue  # later duplicates are dead: the renderer reads the first
            out.append(_format_assignment(name, value))
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(_format_assignment(name, value))
    atomic_write_text(path, "\n".join(out) + "\n")
    return warnings


def _dotenv_delete(name: str, environ: Mapping[str, str] | None) -> bool:
    path = env_file_path(environ)
    if not path.exists():
        return False
    lines = _read_lines(path)
    out = [line for line in lines if not ((p := _parse_assignment(line)) and p[0] == name)]
    if len(out) == len(lines):
        return False
    atomic_write_text(path, "\n".join(out) + ("\n" if out else ""))
    return True


# ----------------------------------------------------------------- keychain


def _keyring() -> Any | None:
    """The keyring module when importable AND a real backend exists, else None."""
    try:
        import keyring  # type: ignore[import-not-found]
    except Exception:  # ImportError, or a broken optional dependency
        return None
    if keyring is None:
        return None
    try:
        backend = keyring.get_keyring()
        if getattr(backend, "priority", 1) <= 0:  # keyring's fail backend
            return None
    except Exception:
        return None
    return keyring


def keychain_available() -> bool:
    return _keyring() is not None


def _keychain_get(name: str) -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        value = kr.get_password(SERVICE, name)
    except Exception:
        return None
    return value or None


# ---------------------------------------------------------------- public API


def _env_value(name: str, environ: Mapping[str, str] | None) -> str | None:
    env = os.environ if environ is None else environ
    value = env.get(name, "").strip()
    return value or None


def source_of(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """"env" | "keychain" | "dotenv" | None. Never returns the value."""
    if _env_value(name, environ):
        return "env"
    if _keychain_get(name):
        return "keychain"
    if _dotenv_items(environ).get(name, "").strip():
        return "dotenv"
    return None


def is_configured(name: str, environ: Mapping[str, str] | None = None) -> bool:
    return source_of(name, environ) is not None


@dataclass
class SetResult:
    name: str
    backend: str  # "keychain" | "dotenv"
    location: str  # human-readable, no value
    warnings: list[str] = field(default_factory=list)

    def __repr__(self) -> str:  # explicit: nothing secret here, keep it that way
        return f"SetResult(name={self.name!r}, backend={self.backend!r})"


def set_key(
    name: str,
    value: str,
    backend: str = "auto",
    force: bool = False,
    environ: Mapping[str, str] | None = None,
) -> SetResult:
    """Validate and store a key. `backend`: auto | keychain | dotenv."""
    validate_name(name, force)
    cleaned = clean_value(value)
    warnings: list[str] = []
    hint = shape_hint(name, cleaned)
    if hint:
        warnings.append(hint)
    if backend not in {"auto", "keychain", "dotenv"}:
        raise KeysError("backend must be auto, keychain or dotenv.")
    use_keychain = backend == "keychain" or (backend == "auto" and keychain_available())
    if use_keychain:
        kr = _keyring()
        if kr is None:
            raise KeysError("No usable keychain backend. Install `keyring` or use --backend dotenv.")
        try:
            kr.set_password(SERVICE, name, cleaned)
        except Exception as exc:  # never include exc text: some backends echo arguments
            raise KeysError(f"The keychain refused the write ({type(exc).__name__}).") from None
        if _dotenv_items(environ).get(name):
            warnings.append(f"{env_file_path(environ)} also has {name}; the keychain value takes precedence.")
        return SetResult(name, "keychain", f"OS keychain (service {SERVICE})", warnings)
    path = env_file_path(environ)
    warnings.extend(_dotenv_set(name, cleaned, environ))
    return SetResult(name, "dotenv", str(path), warnings)


def delete_key(name: str, force: bool = False, environ: Mapping[str, str] | None = None) -> list[str]:
    """Remove from keychain and dotenv. Returns where it was removed from."""
    validate_name(name, force)
    removed: list[str] = []
    kr = _keyring()
    if kr is not None and _keychain_get(name):
        try:
            kr.delete_password(SERVICE, name)
            removed.append("keychain")
        except Exception:
            raise KeysError("The keychain refused to delete the entry.") from None
    if _dotenv_delete(name, environ):
        removed.append("dotenv")
    return removed


def list_keys(environ: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """One row per known key. No values."""
    rows = []
    for name, meta in KNOWN_KEYS.items():
        source = source_of(name, environ)
        rows.append(
            {
                "name": name,
                "provider": meta["provider"],
                "configured": source is not None,
                "source": source,
                "key_url": meta["key_url"] or None,
            }
        )
    return rows


def export_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Merged environment for launching the proxy. THIS RETURNS SECRETS: never print it.

    dotenv entries are the base, keychain values override them, and the
    process environment overrides both.
    """
    base = dict(os.environ if environ is None else environ)
    merged: dict[str, str] = dict(_dotenv_items(environ))
    for name in KNOWN_KEYS:
        value = _keychain_get(name)
        if value:
            merged[name] = value
    for name, value in base.items():
        if value.strip() or name not in merged:
            merged[name] = value
    return merged


def find_duplicate_values(environ: Mapping[str, str] | None = None) -> list[list[str]]:
    """Groups of key NAMES that hold the same value (a sign of one account used twice)."""
    env = export_env(environ)
    groups: dict[str, list[str]] = {}
    for name in KNOWN_KEYS:
        if name == MASTER_KEY:
            continue
        value = env.get(name, "").strip()
        if value:
            groups.setdefault(value, []).append(name)
    return [names for names in groups.values() if len(names) > 1]


def extra_key_variants(environ: Mapping[str, str] | None = None) -> list[str]:
    """Names like GROQ_API_KEY_2 / GROQ_API_KEYS that hint at several accounts for one provider."""
    names = set(os.environ if environ is None else environ) | set(_dotenv_items(environ))
    pattern = re.compile(r"^(" + "|".join(re.escape(k) for k in KNOWN_KEYS if k != MASTER_KEY) + r")(_?\d+|S)$")
    return sorted(n for n in names if pattern.match(n) and n not in KNOWN_KEYS)
