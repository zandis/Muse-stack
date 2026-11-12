"""HealthBot + GatewayLLM through a REAL LiteLLM proxy with the muse hook, against fake upstreams.

Catalogue: one cloud provider that trains on inputs (gemini / g-flash) and one
local provider (local_openai / local-model), both in alias "fast". The bot's
tenant allows only "local". Upstream request bodies are captured so the tests
can prove where record text went.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest
import yaml

pytest.importorskip("litellm", reason="integration tests need litellm[proxy] installed")

import render_config  # noqa: E402  (tests/integration/conftest.py puts scripts/ on sys.path)
from fake_upstream import FakeUpstream, Logged  # noqa: E402

from muse.health.bot import HealthBot  # noqa: E402
from muse.health.contracts import Tenant  # noqa: E402
from muse.health.llm import GatewayLLM  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
from health_fakes import FakeMandateBook, FakeReceiptLog, FakeVault  # noqa: E402

_conftest = next(m for name, m in sys.modules.items() if (getattr(m, "__file__", "") or "").endswith(str(Path("integration") / "conftest.py")))
Proxy = _conftest.Proxy

TENANT = Tenant(
    id="e2e", display_name="示範醫院 健康管理助理", issuer="hospital", locale="zh-TW",
    default_privacy="local", allowed_privacy=("local",),
    emergency_text="如果有緊急狀況，請立即撥打 119 或前往急診。",
)
CLOUD_MODELS = {"g-flash"}


class BodyUpstream(FakeUpstream):
    """FakeUpstream that also keeps each request body and can script the answer text per model."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[tuple[str, bytes]] = []
        self.answers: dict[str, Callable[[], str]] = {}

    def bodies_for(self, models: set[str]) -> list[bytes]:
        with self._lock:
            return [body for model, body in self.bodies if model in models]

    def start(self) -> "BodyUpstream":
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:
                return

            def do_GET(self) -> None:
                self._send(200, {"object": "list", "data": []}, {})

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    model = str(json.loads(raw or b"{}").get("model", ""))
                except ValueError:
                    model = ""
                with upstream._lock:
                    behaviour = upstream.behaviours.get(model, upstream.default)
                    upstream.bodies.append((model, raw))
                status, payload, headers = upstream._respond(behaviour, model, self.path)
                if status == 200 and model in upstream.answers and "choices" in payload:
                    payload["choices"][0]["message"]["content"] = upstream.answers[model]()
                with upstream._lock:
                    upstream.log.append(Logged(self.path, model, status, time.time()))
                self._send(status, payload, headers)

            def _send(self, status: int, payload: Any, headers: dict[str, str]) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=lambda: self._server.serve_forever(poll_interval=0.1), daemon=True)
        self._thread.start()
        return self


def health_catalog(upstream: FakeUpstream) -> dict[str, Any]:
    return {
        "aliases": {"fast": {"mode": "chat", "fallbacks": []}},
        "providers": [
            {
                "id": "gemini", "env": "GEMINI_API_KEY", "tier": "free", "api_base": upstream.base("/gemini"),
                "policy": {"trains_on_inputs": True, "human_review": True, "region": "us", "commercial_use": "allowed"},
                "deployments": [{"id": "g-flash", "model": "openai/g-flash", "mode": "chat", "aliases": ["fast"], "order": 1, "rpm": 100}],
            },
            {
                "id": "local_openai", "kind": "local", "base_env": "MUSE_LOCAL_OPENAI_BASE", "tier": "local",
                "policy": {"trains_on_inputs": False, "human_review": False, "region": "local", "commercial_use": "allowed"},
                "deployments": [],
            },
        ],
    }


class HealthProxy(Proxy):
    def __init__(self, workdir: Path, upstream: FakeUpstream, home: Path, *, local: bool = True, extra_env: dict[str, str] | None = None) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        super().__init__(workdir, upstream, home, workers=1, local=local)
        self.env.update(extra_env or {})

    def render(self) -> Path:
        catalog = health_catalog(self.upstream)
        self.catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False))
        config, _ = render_config.render(catalog, self.env)
        path = self.workdir / "litellm.yaml"
        path.write_text(render_config.dumps(config, []))
        return path


@pytest.fixture()
def body_upstream() -> Iterator[BodyUpstream]:
    server = BodyUpstream().start()
    yield server
    server.stop()


@pytest.fixture()
def start_proxy(tmp_path: Path, body_upstream: BodyUpstream, _isolated_muse_home: Path) -> Iterator[Callable[..., HealthProxy]]:
    started: list[HealthProxy] = []

    def factory(name: str, **kw: Any) -> HealthProxy:
        proxy = HealthProxy(tmp_path / name, body_upstream, _isolated_muse_home, **kw).start()
        started.append(proxy)
        return proxy

    yield factory
    for proxy in started:
        proxy.stop()


def stack(tmp_path: Path) -> tuple[Any, Any, Any, str]:
    """Real vault / mandates / receipts if they import and work, else the fakes."""
    marker = f"E2EMARK{uuid.uuid4().hex[:12]}"
    text = f"Lab report. HbA1c 7.2 % (reference 4.0-5.6). Collected 2026-03-04. Specimen note {marker}."
    try:
        from muse.health import crypto
        from muse.health.mandates import LocalMandateBook
        from muse.health.receipts import LocalReceiptLog
        from muse.health.vault import LocalVault

        vault = LocalVault.create(tmp_path / "vault" / "vault.sqlite", "correct horse battery staple", kdf=crypto.KdfParams(n=2**11, r=8, p=1))
        mandates, receipts = LocalMandateBook(vault), LocalReceiptLog(vault)
    except Exception:
        vault, mandates, receipts = FakeVault(), FakeMandateBook(), FakeReceiptLog()
    record = vault.add("lab", "HbA1c", text, "2026-03-04", "Demo Hospital")
    mandates.grant("bot", ("vault.read", "items.write", "draft.letter", "llm.local"), "answer my questions")
    return vault, mandates, receipts, record.id, marker  # type: ignore[return-value]


def bot_for(proxy: HealthProxy, vault: Any, mandates: Any, receipts: Any, *, strict: bool = True) -> HealthBot:
    llm = GatewayLLM(environ={"MUSE_PROXY_URL": proxy.url}, key_env="LITELLM_MASTER_KEY", strict_route=strict, timeout=60)
    clock = lambda: datetime(2026, 11, 12, 1, 0, tzinfo=timezone.utc)  # noqa: E731
    return HealthBot(vault, mandates, receipts, llm, TENANT, clock=clock, alias="fast")


def assert_marker_absent(proxy: HealthProxy, marker: str) -> None:
    time.sleep(0.5)  # success logging is asynchronous
    assert marker not in proxy.log_text(), "record text leaked into proxy stdout/stderr"
    for path in Path(proxy.home).rglob("*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes(), f"record text leaked into {path}"


def test_a_local_question_is_answered_locally_and_cloud_sees_nothing(start_proxy, body_upstream, tmp_path):
    vault, mandates, receipts, rid, marker = stack(tmp_path)
    body_upstream.answers["local-model"] = lambda: f"Your HbA1c was 7.2 % [R:{rid}]."
    proxy = start_proxy("a")
    reply = bot_for(proxy, vault, mandates, receipts).chat("what did my HbA1c report say?")
    assert reply.safety is None, reply.text
    assert reply.text.startswith("Your HbA1c was 7.2 %") and reply.cited_records == (rid,)
    assert reply.route and reply.route.startswith("local_openai/") and reply.route.endswith("(local)")
    assert body_upstream.bodies_for(CLOUD_MODELS) == []
    local_bodies = body_upstream.bodies_for({"local-model"})
    assert len(local_bodies) == 2  # the content-free probe, then the question
    assert marker.encode() not in local_bodies[0] and marker.encode() in local_bodies[1]  # the content really went to the local model
    model_receipt = next(r for r in receipts.list(limit=10) if r.action == "llm.complete")
    assert model_receipt.egress == "local" and model_receipt.privacy == "local" and model_receipt.model.startswith("local_openai/")
    assert_marker_absent(proxy, marker)  # (d)


@pytest.mark.parametrize("variant", ["absent", "down"])
def test_b_no_local_model_means_no_model_and_nothing_sent(start_proxy, body_upstream, tmp_path, variant):
    vault, mandates, receipts, _, marker = stack(tmp_path)
    if variant == "absent":
        proxy = start_proxy("b-absent", local=False)
    else:  # configured, but nothing listens there
        proxy = start_proxy("b-down", extra_env={"MUSE_LOCAL_OPENAI_BASE": "http://127.0.0.1:9/v1"})
    reply = bot_for(proxy, vault, mandates, receipts).chat("我上次的 HbA1c 報告說什麼？")
    assert reply.safety == "no_model", reply.text
    assert reply.text.startswith("本院院內模型目前無法使用，所以我不會把你的紀錄送到其他地方。")
    assert body_upstream.bodies_for(CLOUD_MODELS) == []
    for _, body in body_upstream.bodies:
        assert marker.encode() not in body
    model_receipt = next(r for r in receipts.list(limit=10) if r.action == "llm.complete")
    assert model_receipt.detail.startswith(("refused", "unavailable"))
    assert model_receipt.egress == "none"  # the probe failed; no record text was sent
    assert_marker_absent(proxy, marker)  # (d)


def test_c_without_the_hook_the_client_refuses_to_trust_the_route(start_proxy, body_upstream, tmp_path):
    vault, mandates, receipts, rid, marker = stack(tmp_path)
    leaked = f"CLOUDANSWER{uuid.uuid4().hex[:8]}"
    body_upstream.answers["g-flash"] = lambda: f"{leaked} [R:{rid}]"
    body_upstream.answers["local-model"] = lambda: f"{leaked} [R:{rid}]"
    proxy = start_proxy("c", extra_env={"MUSE_DISABLE_HOOK": "true"})

    # Strict (default): the content-free probe comes back unproven, so no record text is sent at all.
    reply = bot_for(proxy, vault, mandates, receipts).chat("what did my HbA1c report say?")
    assert reply.safety == "no_model" and leaked not in reply.text, reply.text
    for _, body in body_upstream.bodies:
        assert marker.encode() not in body
    model_receipt = next(r for r in receipts.list(limit=10) if r.action == "llm.complete")
    assert model_receipt.egress == "none" and "probe" in model_receipt.detail

    # Non-strict: the plain proxy routes the record to the training cloud provider. The bot's
    # own check still withholds the answer and records the worst case, but the content is gone.
    # This is why strictness (and its probe) is on by default.
    body_upstream.bodies.clear()
    relaxed = bot_for(proxy, vault, mandates, receipts, strict=False).chat("what did my HbA1c report say?")
    assert relaxed.safety == "no_model" and leaked not in relaxed.text
    assert any(marker.encode() in body for body in body_upstream.bodies_for(CLOUD_MODELS))
    model_receipt = next(r for r in receipts.list(limit=10) if r.action == "llm.complete")
    assert model_receipt.egress == "cloud" and "not verified" in model_receipt.detail
    assert marker not in proxy.log_text()
