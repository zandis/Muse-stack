"""Networks and assets this phase 0 stub is allowed to name.

Startup and the verifier both use an *allow-list* of Base Sepolia
identifiers, compared case-insensitively. Anything else — including
``Mainnet``, ``EIP155:8453``, ``base-mainnet``, ``optimism`` — is
refused. There is no mainnet USDC path, no broadcast, and no
facilitator call in this release.

USDC on Base Sepolia is Circle's official testnet token:
https://developers.circle.com/stablecoins/usdc-on-test-networks
(0x036CbD53842c5426634e7929541eC2318f3dCF7e). Confirmed against Circle's
documentation; if Circle rotates the address, update this constant and
``.env.example``.
"""

from __future__ import annotations

# Circle USDC on Base Sepolia (testnet). 6 decimals.
BASE_SEPOLIA_USDC = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
# Circle USDC on Base mainnet — listed only so docs can name what we refuse.
# Never used as a default and never accepted by the allow-list.
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
# Everything is compared lowercased and stripped of spaces.
_SEPOLIA_ALIASES = {
    "base-sepolia": BASE_SEPOLIA_CAIP2,
    "basesepolia": BASE_SEPOLIA_CAIP2,
    "eip155:84532": BASE_SEPOLIA_CAIP2,
    "84532": BASE_SEPOLIA_CAIP2,
    "sepolia-base": BASE_SEPOLIA_CAIP2,
    "base_sepolia": BASE_SEPOLIA_CAIP2,
}


def default_pay_to() -> str:
    return DEFAULT_PAY_TO


def _fold(value: str) -> str:
    return (value or "").strip().lower().replace(" ", "").replace("_", "-")


def normalize_network(value: str) -> str:
    """Return the canonical CAIP-2 if ``value`` is a Sepolia alias, else the
    folded form (lowercase). Never used as an implicit allow.
    """
    key = _fold(value)
    if key in _SEPOLIA_ALIASES:
        return _SEPOLIA_ALIASES[key]
    return key


def is_allowed_network(value: str) -> bool:
    """Allow-list: only Base Sepolia, case-insensitive."""
    return normalize_network(value) == BASE_SEPOLIA_CAIP2


def is_mainnet_network(value: str) -> bool:
    """True when the value is recognisably *not* the Sepolia allow-list.

    Kept so existing tests and docs can name the refusal; the actual
    guard is ``is_allowed_network``.
    """
    return bool(value) and not is_allowed_network(value)


def canonicalize_address(value: str) -> str:
    raw = (value or "").strip()
    if not raw.startswith("0x") or len(raw) != 42:
        raise ValueError(f"expected a 20-byte 0x-address, got {raw[:16]!r}…")
    int(raw[2:], 16)  # validate hex
    return "0x" + raw[2:].lower()
