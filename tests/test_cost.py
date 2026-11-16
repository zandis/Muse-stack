"""muse.cost: what a call cost, and when that is unknown."""

from __future__ import annotations

import pytest

from wardmuse import cost


def info(tier="paid", price=None, kind="cloud"):
    out = {"id": "d", "muse_tier": tier, "muse_kind": kind}
    if price is not None:
        out["muse_price_in"], out["muse_price_out"] = price
    return out


@pytest.mark.parametrize("model_info, tin, tout, slo, expected, paid", [
    (info("free"), 11, 7, {"response_cost": 0.5}, 0.0, False),  # free by definition, whatever LiteLLM says
    (info("trial"), 11, 7, None, 0.0, False),
    (info("local", kind="local"), 11, 7, {"response_cost": 3.0}, 0.0, False),
    (info("paid", (20.0, 40.0)), 11, 7, None, (11 * 20 + 7 * 40) / 1e6, True),
    (info("paid", (20.0, 40.0)), 11, 7, {"response_cost": 9.0}, (11 * 20 + 7 * 40) / 1e6, True),  # catalogue wins
    (info("metered", (1.0, 2.0)), 1_000_000, 0, None, 1.0, True),
    (info("paid", (1.0, 2.0)), None, None, None, None, True),  # no usage: unknown
    (info("paid", (1.0, 2.0)), 5, None, None, 5 / 1e6, True),
    (info("paid"), 11, 7, {"response_cost": 0.0021}, 0.0021, True),  # LiteLLM's positive figure
    (info("paid"), 11, 7, {"response_cost": 0.0}, None, True),  # 0.0 for an unmapped model: not trusted
    (info("paid"), 11, 7, {"response_cost": 0.002, "response_cost_failure_debug_info": {"x": 1}}, None, True),
    (info("paid"), 11, 7, {"response_cost": float("nan")}, None, True),
    (info("paid"), 11, 7, {"response_cost": True}, None, True),
    (info("paid"), 11, 7, None, None, True),
    ({"id": "x"}, 11, 7, None, None, True),  # no tier: paid, unknown
])
def test_call_cost(model_info, tin, tout, slo, expected, paid):
    value, is_paid = cost.call_cost(model_info, tin, tout, slo)
    assert is_paid is paid
    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected)


def test_kwargs_fallback_for_litellm_cost():
    assert cost.litellm_cost({}, {"response_cost": 0.01}) == 0.01
    assert cost.litellm_cost(None, None) is None


@pytest.mark.parametrize("bad", [(-1.0, 1.0), ("1", 1.0), (float("inf"), 1.0), (True, 1.0)])
def test_bad_price_is_unknown(bad):
    model_info = {"muse_tier": "paid", "muse_price_in": bad[0], "muse_price_out": bad[1]}
    assert cost.call_cost(model_info, 10, 10)[0] is None
