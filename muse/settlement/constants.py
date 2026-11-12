"""Networks and assets this phase 0 stub is allowed to name.

Mainnet identifiers are listed only so we can refuse them. There is no
mainnet USDC path, no broadcast, and no facilitator call in this release.

USDC on Base Sepolia is Circle's official testnet token:
https://developers.circle.com/stablecoins/usdc-on-test-networks
(0x036CbD53842c5426634e7929541eC2318f3dCF7e). Confirmed against Circle's
documentation; if Circle rotates the address, update this constant and
``.env.example``.
"""

from __future__ import annotations

# Circle USDC on Base Sepolia (testnet). 6 decimals.
BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
# Circle USDC on Base mainnet — listed only to reject it.
BASE_MAINNET_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

BASE_SEPOLIA_CHAIN_ID = 84532
BASE_MAINNET_CHAIN_ID = 8453
BASE_SEPOLIA_CAIP2 = "eip155:84532"
BASE_MAINNET_CAIP2 = "eip155:8453"

# A clearly-fake default pay-to used by the mock (Ceresus-looking suffix).
# Not a funded wallet. Override with MUSE_X402_PAY_TO for a real testnet address
# if you later wire a real testnet facilitator — which phase 0 will not do.
DEFAULT_PAY_TO = "0x000000000000000000000000000000000000cece"

# Aliases we accept for Base Sepolia, and the canonical CAIP-2 we emit.
_SEPOLIA_ALIASES = {
    "base-sepolia": BASE_SEPOLIA_CAIP2,
    "basesepolia": BASE_SEPOLIA_CAIP2,
    "eip155:84532": BASE_SEPOLIA_CAIP2,
    "84532": BASE_SEPOLIA_CAIP2,
    "sepolia-base": BASE_SEPOLIA_CAIP2,
}

# Anything in this set is a mainnet (or otherwise real-funds) network. Startup
# and the verifier both refuse them.
MAINNET_NETWORKS = frozenset(
    {
        "base",
        "eip155:8453",
        "8453",
        "ethereum",
        "eip155:1",
        "1",
        "mainnet",
        "solana",
        "solana:mainnet",
        "solana:5eykt4usfv8p8njdtrepy1vzqkqzkvdp",
        "polygon",
        "eip155:137",
        "arbitrum",
        "eip155:42161",
    }
)


def default_pay_to() -> str:
    return DEFAULT_PAY_TO


def normalize_network(value: str) -> str:
    raw = (value or "").strip()
    key = raw.lower()
    if key in _SEPOLIA_ALIASES:
        return _SEPOLIA_ALIASES[key]
    return raw


def is_mainnet_network(value: str) -> bool:
    return (value or "").strip().lower() in MAINNET_NETWORKS


def is_allowed_network(value: str) -> bool:
    return normalize_network(value) == BASE_SEPOLIA_CAIP2


def canonicalize_address(value: str) -> str:
    raw = (value or "").strip()
    if not raw.startswith("0x") or len(raw) != 42:
        raise ValueError(f"expected a 20-byte 0x-address, got {raw[:16]!r}…")
    int(raw[2:], 16)  # validate hex
    return "0x" + raw[2:].lower()
