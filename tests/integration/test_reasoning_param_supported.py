"""With LiteLLM installed, every catalogue entry that sets a reasoning parameter names one LiteLLM passes on.

`scripts/validate_config.py` makes the same check, but skips it where LiteLLM is absent (the keyless
config job installs PyYAML only). This test is where it is guaranteed to run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from muse import catalog as catalog_mod

litellm = pytest.importorskip("litellm")
ROOT = Path(__file__).resolve().parents[2]


def test_every_declared_reasoning_parameter_is_one_litellm_passes_on():
    catalog = yaml.safe_load((ROOT / "config" / "providers.yaml").read_text(encoding="utf-8"))
    declared = [(str(d["id"]), str(d["model"]), catalog_mod.reasoning_for(d))
                for p in catalog["providers"] for d in p.get("deployments") or []]
    declared = [row for row in declared if row[2]]
    assert declared, "no shipped deployment declares reasoning"
    for dep_id, model, want in declared:
        route, _, name = model.partition("/")
        supported = litellm.get_supported_openai_params(model=name, custom_llm_provider=route) or []
        assert want["param"] in supported, f"{dep_id}: LiteLLM drops {want['param']} for {route}/"
