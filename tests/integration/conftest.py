"""Real LiteLLM proxy + fake upstream harness. Skipped when litellm is not installed."""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

pytest.importorskip("litellm", reason="integration tests need litellm[proxy] installed")

from fake_upstream import FakeUpstream  # noqa: E402
from ration_catalog import MASTER_KEY, RATION_KEYS  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import render_config  # noqa: E402

LITELLM = shutil.which("litellm") or str(Path(sys.executable).with_name("litellm"))

KEYS = {"GEMINI_API_KEY": "fake-g", "GROQ_API_KEY": "fake-q", "PRIVCLOUD_API_KEY": "fake-p"}
_MASTER = object()  # chat(): "send the master key"


def build_catalog(upstream: FakeUpstream) -> dict[str, Any]:
    """Small catalogue in the real schema. Upstream model names double as fake-server scripts."""
    no_no = {"trains_on_inputs": False, "human_review": False, "region": "us", "commercial_use": "allowed"}
    return {
        "aliases": {
            "fast": {"mode": "chat", "fallbacks": ["cheap"]},
            "cheap": {"mode": "chat", "fallbacks": ["fast"]},
        },
        "providers": [
            {
                "id": "gemini", "env": "GEMINI_API_KEY", "tier": "free", "api_base": upstream.base("/gemini"),
                "policy": {"trains_on_inputs": True, "human_review": True, "region": "us", "commercial_use": "allowed"},
                "deployments": [
                    {"id": "g-flash", "model": "openai/g-flash", "mode": "chat", "aliases": ["fast", "cheap"], "order": 1, "rpm": 10},
                ],
            },
            {
                "id": "groq", "env": "GROQ_API_KEY", "tier": "free", "api_base": upstream.base("/groq"),
                "policy": {"trains_on_inputs": "unknown", "human_review": "unknown", "region": "us", "commercial_use": "allowed"},
                "deployments": [
                    {"id": "q-small", "model": "openai/q-small", "mode": "chat", "aliases": ["fast", "cheap"], "order": 2, "rpm": 10},
                ],
            },
            {
                "id": "privcloud", "env": "PRIVCLOUD_API_KEY", "tier": "paid", "api_base": upstream.base("/priv"),
                "policy": no_no,
                "quota": {"requests_per_day": 100},
                "deployments": [
                    # Priced, so the paid gate lets the owner use it automatically under the default cap.
                    {"id": "p-model", "model": "openai/p-model", "mode": "chat", "aliases": ["cheap"], "order": 3, "rpm": 10,
                     "price": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
                ],
            },
            {
                "id": "local_openai", "kind": "local", "base_env": "MUSE_LOCAL_OPENAI_BASE", "tier": "local",
                "policy": {"trains_on_inputs": False, "human_review": False, "region": "local", "commercial_use": "allowed"},
                "deployments": [],
            },
        ],
    }


class Proxy:
    """One `litellm` subprocess with the muse hook, against the fake upstream."""

    def __init__(
        self,
        workdir: Path,
        upstream: FakeUpstream,
        home: Path,
        workers: int = 1,
        local: bool = True,
        master_key: str | None = MASTER_KEY,
        catalog: Any = None,
        env: dict[str, str] | None = None,
        local_aliases: tuple[str, ...] = ("fast",),
        post_render: Any = None,
    ) -> None:
        self.workdir = workdir
        self.upstream = upstream
        self.home = home
        self.workers = workers
        self.local = local
        self.master_key = master_key
        self.catalog_builder = catalog or build_catalog
        self.extra_env = env or {}
        self.local_aliases = local_aliases
        self.post_render = post_render  # callable(config dict) -> None, e.g. to add model_info the renderer does not emit yet
        self.proc: subprocess.Popen[bytes] | None = None
        self.port = 0
        self.catalog_path = workdir / "providers.yaml"
        self.env = self._env()

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY") and k != "LITELLM_MASTER_KEY"
               and not k.startswith(("MUSE_DAILY", "MUSE_ALLOW_PAID", "MUSE_RESERVE", "MUSE_ACTIVE", "MUSE_EXPIRING", "MUSE_TZ",
                                     "MUSE_PACE", "MUSE_CACHE", "MUSE_ROUTING", "MUSE_COOLDOWN", "MUSE_DISABLE_HOOK"))}
        env.update(KEYS)
        env.update(RATION_KEYS)
        if self.master_key:
            env["LITELLM_MASTER_KEY"] = self.master_key
        env.update(
            MUSE_HOME=str(self.home),
            PYTHONPATH=os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep),
            MUSE_DEFAULT_PRIVACY="standard",
            MUSE_CATALOG=str(self.catalog_path),
            LITELLM_LOG="ERROR",
            NO_PROXY="127.0.0.1,localhost",
            no_proxy="127.0.0.1,localhost",
        )
        if self.local:
            env["MUSE_LOCAL_OPENAI_BASE"] = self.upstream.base("/local")
            local_catalog = self.workdir / "local.yaml"
            local_catalog.write_text(
                yaml.safe_dump({"local_openai": [{"id": "local-model", "model": "openai/local-model", "mode": "chat", "aliases": list(self.local_aliases)}]})
            )
            env["MUSE_LOCAL_CATALOG"] = str(local_catalog)
            # No OPENAI_API_KEY here on purpose: the renderer's placeholder key
            # must be enough for a local OpenAI-compatible server.
            env.pop("OPENAI_API_KEY", None)
        env.update(self.extra_env)
        return env

    def render(self) -> Path:
        catalog = self.catalog_builder(self.upstream)
        self.catalog_path.write_text(yaml.safe_dump(catalog, sort_keys=False))
        config, _ = render_config.render(catalog, self.env)
        if envget(self.env, "MUSE_DISABLE_HOOK", "").strip().lower() in {"1", "true", "yes", "on"}:
            assert "callbacks" not in config["litellm_settings"]  # the plain proxy, for LiteLLM-only behaviour
        else:
            assert config["litellm_settings"]["callbacks"] == ["wardmuse.hook.handler"]
        if self.post_render is not None:
            self.post_render(config)
        path = self.workdir / "litellm.yaml"
        path.write_text(render_config.dumps(config, []))
        return path

    def start(self, timeout: float = 120) -> "Proxy":
        config = self.render()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        log = open(self.workdir / f"proxy-{self.port}.log", "wb")
        self.proc = subprocess.Popen(
            [LITELLM, "--config", str(config), "--host", "127.0.0.1", "--port", str(self.port), "--num_workers", str(self.workers)],
            env=self.env, cwd=str(self.workdir), stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"proxy exited: {(self.workdir / f'proxy-{self.port}.log').read_text()[-3000:]}")
            try:
                with urllib.request.urlopen(f"{self.url}/health/liveliness", timeout=2):
                    # Readiness also needs the router; a model call path check is cheap.
                    with urllib.request.urlopen(self._request("/v1/models"), timeout=5):
                        return self
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        raise RuntimeError("proxy did not become ready")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def log_text(self) -> str:
        return (self.workdir / f"proxy-{self.port}.log").read_text(errors="replace")

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            os.killpg(self.proc.pid, 15)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, 9)
            self.proc.wait()
        self.proc = None

    def restart(self) -> "Proxy":
        self.stop()
        return self.start()

    def _request(self, path: str, data: bytes | None = None, headers: dict[str, str] | None = None, key: Any = _MASTER) -> urllib.request.Request:
        token = self.master_key if key is _MASTER else key
        auth = {"authorization": f"Bearer {token}"} if token else {}
        return urllib.request.Request(f"{self.url}{path}", data=data, headers={**auth, **(headers or {})})

    def get(self, path: str, key: Any = _MASTER) -> tuple[int, Any]:
        try:
            with urllib.request.urlopen(self._request(path, key=key), timeout=20) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, raw.decode(errors="replace")

    def chat(
        self,
        model: str,
        text: str = "hello",
        headers: dict[str, str] | None = None,
        timeout: float = 60,
        key: Any = _MASTER,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, str], Any]:
        """POST a chat completion. `key`: default the master key; an app token; None for no auth."""
        payload: dict[str, Any] = {"model": model, "messages": [{"role": "user", "content": text}]}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        payload.update(extra or {})
        request = self._request("/v1/chat/completions", json.dumps(payload).encode(), {"content-type": "application/json", **(headers or {})}, key)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, {k.lower(): v for k, v in response.headers.items()}, json.loads(response.read())
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                payload: Any = json.loads(raw)
            except ValueError:
                payload = raw.decode(errors="replace")
            return err.code, {k.lower(): v for k, v in err.headers.items()}, payload


@pytest.fixture()
def upstream() -> Iterator[FakeUpstream]:
    server = FakeUpstream().start()
    yield server
    server.stop()


@pytest.fixture()
def make_proxy(tmp_path: Path, upstream: FakeUpstream, _isolated_muse_home: Path) -> Iterator[Any]:
    started: list[Proxy] = []

    def factory(workers: int = 1, local: bool = True, **options: Any) -> Proxy:
        proxy = Proxy(tmp_path, upstream, _isolated_muse_home, workers=workers, local=local, **options).start()
        started.append(proxy)
        return proxy

    yield factory
    for proxy in started:
        proxy.stop()
