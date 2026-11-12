"""Phase 0 runtime configuration. Loaded from the environment; never from a committed secret file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from muse.settlement.constants import (
    BASE_SEPOLIA_CAIP2,
    BASE_SEPOLIA_USDC,
    default_pay_to,
    is_allowed_network,
    normalize_network,
)

ROOT = Path(__file__).resolve().parents[1]


def _truthy(environ: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """In-process settings for the phase 0 service.

    ``LITELLM_MASTER_KEY`` is accepted as a bearer token when set. It is never
    written to logs. Provider keys (``GROQ_API_KEY``, and the rest of
    ``.env.example``) stay in the environment; this object only records whether
    they are present.
    """

    host: str = "127.0.0.1"
    port: int = 4100
    bind: str = "127.0.0.1"
    master_key: str = ""
    litellm_base_url: str = "http://127.0.0.1:4000"
    default_cooldown_seconds: float = 30.0
    x402_network: str = BASE_SEPOLIA_CAIP2
    x402_usdc: str = BASE_SEPOLIA_USDC
    x402_pay_to: str = field(default_factory=default_pay_to)
    x402_facilitator: str = "mock"
    x402_price_atomic: int = 10_000  # 0.01 USDC, 6 decimals
    x402_max_timeout_seconds: int = 60
    lean_mode: str = "mock"  # "mock" or "real"
    problems_dir: Path = ROOT / "muse" / "useful_work" / "samples"
    formal_conjectures_dir: Path | None = None
    data_dir: Path = ROOT / "data"

    def __post_init__(self) -> None:
        network = normalize_network(self.x402_network)
        object.__setattr__(self, "x402_network", network)
        # Allow-list, case-insensitive. "Mainnet", "EIP155:8453", "base-mainnet"
        # must not start. The denylist approach missed those spellings.
        if not is_allowed_network(network):
            raise ValueError(
                "phase 0 refuses to start unless MUSE_X402_NETWORK is on the "
                "Base Sepolia allow-list (eip155:84532), compared "
                "case-insensitively (mainnet and other networks are refused). "
                f"Got {self.x402_network!r}."
            )
        if self.x402_facilitator.strip().lower() != "mock":
            raise ValueError(
                "phase 0 ships a mock verifier only. MUSE_X402_FACILITATOR must be 'mock'; "
                "a live facilitator would move funds, which this release will not do."
            )
        if self.x402_price_atomic <= 0:
            raise ValueError("MUSE_X402_PRICE_ATOMIC must be a positive integer")
        mode = self.lean_mode.strip().lower()
        if mode not in {"mock", "real"}:
            raise ValueError("MUSE_LEAN_MODE must be 'mock' or 'real'")
        object.__setattr__(self, "lean_mode", mode)


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = environ if environ is not None else os.environ
    price_raw = env.get("MUSE_X402_PRICE_ATOMIC", "").strip() or "10000"
    cooldown_raw = env.get("MUSE_DEFAULT_COOLDOWN_SECONDS", "").strip() or "30"
    port_raw = env.get("MUSE_PHASE0_PORT", "").strip() or "4100"
    formal = env.get("MUSE_FORMAL_CONJECTURES", "").strip()
    problems = env.get("MUSE_PROBLEMS_DIR", "").strip()
    data = env.get("MUSE_DATA_DIR", "").strip()
    return Settings(
        host=env.get("MUSE_PHASE0_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=int(port_raw),
        bind=env.get("MUSE_BIND", "127.0.0.1").strip() or "127.0.0.1",
        master_key=env.get("LITELLM_MASTER_KEY", "").strip(),
        litellm_base_url=env.get("MUSE_BASE_URL", "http://127.0.0.1:4000").strip().rstrip("/"),
        default_cooldown_seconds=float(cooldown_raw),
        x402_network=env.get("MUSE_X402_NETWORK", BASE_SEPOLIA_CAIP2).strip() or BASE_SEPOLIA_CAIP2,
        x402_usdc=env.get("MUSE_X402_USDC_ADDRESS", BASE_SEPOLIA_USDC).strip() or BASE_SEPOLIA_USDC,
        x402_pay_to=env.get("MUSE_X402_PAY_TO", "").strip() or default_pay_to(),
        x402_facilitator=env.get("MUSE_X402_FACILITATOR", "mock").strip() or "mock",
        x402_price_atomic=int(price_raw),
        x402_max_timeout_seconds=int(env.get("MUSE_X402_MAX_TIMEOUT_SECONDS", "60") or "60"),
        lean_mode=env.get("MUSE_LEAN_MODE", "mock").strip() or "mock",
        problems_dir=Path(problems) if problems else ROOT / "muse" / "useful_work" / "samples",
        formal_conjectures_dir=Path(formal) if formal else None,
        data_dir=Path(data) if data else ROOT / "data",
    )
