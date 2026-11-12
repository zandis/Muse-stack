"""Unit tests for the standalone LiteLLM callback (no LiteLLM required)."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "muse_litellm_callback",
    ROOT / "scripts" / "muse_litellm_callback.py",
)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(mod)


def test_sibling_ids_share_upstream_model():
    model_list = [
        {
            "model_info": {"id": "groq-gpt-oss-20b__fast"},
            "litellm_params": {"model": "groq/openai/gpt-oss-20b"},
        },
        {
            "model_info": {"id": "groq-gpt-oss-20b__smart"},
            "litellm_params": {"model": "groq/openai/gpt-oss-20b"},
        },
        {
            "model_info": {"id": "groq-gpt-oss-20b__cheap"},
            "litellm_params": {"model": "groq/openai/gpt-oss-20b"},
        },
        {
            "model_info": {"id": "gemini-flash__fast"},
            "litellm_params": {"model": "gemini/gemini-3.8-flash"},
        },
    ]
    sibs = set(mod.sibling_ids(model_list, "groq-gpt-oss-20b__fast"))
    assert sibs == {"groq-gpt-oss-20b__smart", "groq-gpt-oss-20b__cheap"}
    assert mod.sibling_ids(model_list, "gemini-flash__fast") == []
    assert mod.sibling_ids(model_list, "unknown") == []


def test_quota_exhausted_text():
    assert mod.is_quota_exhausted_text("insufficient_quota")
    assert mod.is_quota_exhausted_text("You have exceeded your daily quota")
    assert not mod.is_quota_exhausted_text("rate limited, retry after 30")
    assert not mod.is_quota_exhausted_text(None)


def test_callback_swallows_errors_without_litellm():
    # The instance must be importable and must not raise when LiteLLM is absent.
    handler = mod.proxy_handler_instance
    handler.log_failure_event({"exception": RuntimeError("boom")}, None, None, None)
    handler._handle_failure({"litellm_params": {}}, RuntimeError("429"))
