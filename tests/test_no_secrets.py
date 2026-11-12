"""Sweep source and the running service for leaked secrets and mainnet settlement."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_SOURCE_SNIPPETS = (
    "MUSE_X402_FACILITATOR=https://",
    "broadcast --rpc-url",
    "--private-key",
)

# Mainnet identifiers may appear only as a denylist / documentation of refusal.
MAINNET_USE_AS_DEFAULT = (
    "MUSE_X402_NETWORK=eip155:8453\n",
    "MUSE_X402_NETWORK=base\n",
    "MUSE_X402_NETWORK=ethereum",
)


def test_gitignore_covers_env_and_foundry_broadcast():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in text
    assert "broadcast" in text or "contracts/broadcast" in text


def test_env_example_has_no_secret_values_and_defaults_to_sepolia():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key.endswith(("_API_KEY", "_MASTER_KEY", "_SALT_KEY", "_PASSWORD", "_SECRET", "_TOKEN")):
            assert value == "", f"{key} must be empty in .env.example, got {value!r}"
    assert "eip155:84532" in text or "base-sepolia" in text
    assert "MUSE_X402_FACILITATOR=mock" in text


def test_source_does_not_enable_mainnet_or_broadcast():
    # Narrow, high-signal strings only. The denylist itself mentions mainnet.
    for snippet in FORBIDDEN_SOURCE_SNIPPETS + MAINNET_USE_AS_DEFAULT:
        hits = []
        for path in ROOT.rglob("*"):
            if path.suffix not in {".py", ".md", ".yml", ".yaml", ".toml", ".sol", ".example", ".txt"}:
                continue
            if any(part in {".git", ".venv", "venv", "out", "cache", "broadcast", "tests", "lib"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if snippet in text:
                hits.append(str(path.relative_to(ROOT)))
        assert not hits, f"{snippet!r} found in {hits}"
