"""Shared fixtures. Tests never read real provider keys; they pass a fake environ."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from muse.config import Settings, load_settings
from muse.settlement.constants import BASE_SEPOLIA_CAIP2, BASE_SEPOLIA_USDC, default_pay_to


@pytest.fixture
def fake_env(tmp_path) -> dict[str, str]:
    # Isolated from the process environment so a real GROQ_API_KEY on the
    # machine cannot leak into assertions or responses.
    return {
        "LITELLM_MASTER_KEY": "sk-test-master-key-not-real",
        "GEMINI_API_KEY": "gemini-secret-value-should-not-appear",
        "GROQ_API_KEY": "groq-secret-value-should-not-appear",
        "MUSE_X402_NETWORK": BASE_SEPOLIA_CAIP2,
        "MUSE_X402_USDC_ADDRESS": BASE_SEPOLIA_USDC,
        "MUSE_X402_PAY_TO": default_pay_to(),
        "MUSE_X402_FACILITATOR": "mock",
        "MUSE_X402_PRICE_ATOMIC": "10000",
        "MUSE_LEAN_MODE": "mock",
        "MUSE_WORK_LLM": "mock",
        "MUSE_DATA_DIR": str(tmp_path / "data"),
    }


@pytest.fixture
def settings(fake_env) -> Settings:
    return load_settings(fake_env)


@pytest.fixture
def isolated_os_environ(fake_env) -> Iterator[dict[str, str]]:
    """Temporarily replace os.environ for tests that exercise module-level reads."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(fake_env)
    try:
        yield fake_env
    finally:
        os.environ.clear()
        os.environ.update(saved)
