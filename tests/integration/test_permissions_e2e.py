"""A permission end to end on the real proxy: issue -> its app key chats -> stop -> the next chat is refused and
nothing reaches the upstream. The proxy needs no permissions library (muse/compiler/gate.py); issuing does."""

from __future__ import annotations

import time

import pytest

from muse import permissions as P
from test_ration_e2e import message, models, start

pytestmark = pytest.mark.skipif(not P.available(), reason="biscuit-python is not installed (pip install -e '.[permissions]')")


def test_a_stopped_permission_stops_its_app_key_at_the_next_request(make_proxy, upstream):
    from muse.compiler.core import Compiler

    proxy = start(make_proxy)
    env = {"MUSE_HOME": str(proxy.home), "MUSE_TZ": "UTC"}
    c = Compiler(env)
    now = int(time.time())
    draft = P.propose_from_fields(P.Draft(
        issuer=c.ensure_root(), holder="app:reader", actions=("llm.call",), resources=(P.Resource("provider", "router"),),
        limits=(P.Limit("requests", "", 0, "day", 50),), issued_at=now, not_before=now - 60, not_after=now + 86400,
        confirm=P.EACH_WRITE, privacy="standard", period_zone="UTC"))
    mandate, applied = c.issue(draft)
    token = applied.app_token
    assert token and applied.targets["router"]["state"] == "active"

    status, headers, body = proxy.chat("solo", key=token)
    assert status == 200, body
    assert headers["x-muse-app"] == applied.app_name
    before = len(models(upstream))
    assert before == 1

    # A stricter class than the permission allows is fine; a looser one is refused before anything is sent.
    status, _, body = proxy.chat("solo", key=token, headers={"x-muse-privacy": "public"})
    assert status == 403 and "permission privacy" in message(body)

    c.revoke(mandate.id)
    status, _, body = proxy.chat("solo", key=token)
    assert status == 403 and "permission stopped" in message(body), body
    assert "was stopped" in message(body) and token not in str(body)
    assert len(models(upstream)) == before  # nothing reached the upstream
    assert token not in proxy.log_text()
