"""muse.local: probes against fake runtimes, mapping heuristics, catalogue merge."""

from __future__ import annotations

import json
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import yaml

from muse import local


class FakeRuntime:
    """Loopback HTTP server answering canned JSON per (method, path)."""

    def __init__(self, routes: dict[tuple[str, str], object]):
        self.routes = routes
        outer = self

        class H(BaseHTTPRequestHandler):
            def _serve(self, method: str) -> None:
                request = None
                if method == "POST":
                    request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")
                payload = outer.routes.get((method, self.path))
                if callable(payload):
                    payload = payload(request)
                if payload is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                self._serve("GET")

            def do_POST(self):  # noqa: N802
                self._serve("POST")

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def runtimes():
    made: list[FakeRuntime] = []

    def make(routes):
        r = FakeRuntime(routes)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def dead_env(**extra):
    port = free_port()
    env = {
        "OLLAMA_API_BASE": f"http://127.0.0.1:{port}",
        "LM_STUDIO_API_BASE": f"http://127.0.0.1:{port}/v1",
    }
    env.update(extra)
    return env


OLLAMA_TAGS = {
    "models": [
        {"name": "qwen3:8b", "details": {"family": "qwen3", "parameter_size": "8.2B", "quantization_level": "Q4_K_M"}},
        {"name": "llama3.3:70b", "details": {"family": "llama", "parameter_size": "70.6B", "quantization_level": "Q4_K_M"}},
        {"name": "nomic-embed-text:latest", "details": {"family": "nomic-bert", "parameter_size": "137M"}},
        {"name": "dengcao/Qwen3-Reranker-0.6B:Q8_0", "details": {"parameter_size": "595M"}},
        {"name": "mystery:latest", "details": {}},
        {"name": "llama3.1:8b-instruct-q4", "details": {"parameter_size": "8.0B"}},
    ]
}


def ollama_routes():
    show = lambda caps, ctx: {"capabilities": caps, "model_info": {"general.architecture": "x", "x.context_length": ctx}}  # noqa: E731
    routes = {("GET", "/api/tags"): OLLAMA_TAGS}
    routes[("POST", "/api/show")] = lambda req: show(["embedding"] if "embed" in req["model"] else ["completion"], 40960)
    return routes


def test_ollama_discovery(runtimes):
    rt = runtimes(ollama_routes())
    env = dead_env(OLLAMA_API_BASE=rt.base)
    found = local.discover(env)
    assert list(found) == ["ollama"]
    by_id = {d["id"]: d for d in found["ollama"]}
    assert "ollama-qwen3-8b" in by_id
    q = by_id["ollama-qwen3-8b"]
    assert q["model"] == "ollama_chat/qwen3:8b"
    assert q["mode"] == "chat" and q["aliases"] == ["fast", "cheap"]
    assert q["context_tokens"] == 40960
    assert by_id["ollama-llama3.3-70b"]["aliases"] == ["smart"]
    emb = by_id["ollama-nomic-embed-text"]
    assert emb["mode"] == "embedding" and emb["aliases"] == ["embed"] and emb["model"] == "ollama/nomic-embed-text:latest"
    rr = by_id["ollama-dengcao-qwen3-reranker-0.6b-q8-0"]
    assert rr["mode"] == "rerank" and rr["aliases"] == []
    assert by_id["ollama-mystery"]["aliases"] == ["cheap"]
    assert all(set(d) >= {"id", "model", "mode", "aliases", "notes"} for d in found["ollama"])


def test_ollama_capabilities_override_name(runtimes):
    routes = {("GET", "/api/tags"): {"models": [{"name": "odd-name:1b"}]}, ("POST", "/api/show"): {"capabilities": ["embedding"]}}
    rt = runtimes(routes)
    found = local.discover(dead_env(OLLAMA_API_BASE=rt.base))
    assert found["ollama"][0]["mode"] == "embedding"


def test_long_context_only_when_reported(runtimes):
    routes = {("GET", "/api/tags"): {"models": [{"name": "big:8b"}, {"name": "plain:8b"}]}}
    routes[("POST", "/api/show")] = {"capabilities": ["completion"], "model_info": {"llama.context_length": 262144}}
    rt = runtimes(routes)
    found = local.discover(dead_env(OLLAMA_API_BASE=rt.base))
    assert all("long-context" in d["aliases"] for d in found["ollama"])  # both report 262144
    routes[("POST", "/api/show")] = {"capabilities": ["completion"], "model_info": {"llama.context_length": 8192}}
    found = local.discover(dead_env(OLLAMA_API_BASE=rt.base))
    assert all("long-context" not in d["aliases"] for d in found["ollama"])
    del routes[("POST", "/api/show")]  # no context reported at all
    found = local.discover(dead_env(OLLAMA_API_BASE=rt.base))
    assert all("long-context" not in d["aliases"] and "context_tokens" not in d for d in found["ollama"])


def test_lmstudio_discovery_with_and_without_v0(runtimes):
    models = {"data": [{"id": "qwen/qwen3-8b"}, {"id": "text-embedding-nomic-embed-text-v1.5"}]}
    rt = runtimes({("GET", "/v1/models"): models})
    found = local.discover(dead_env(LM_STUDIO_API_BASE=rt.base + "/v1"))
    by_id = {d["id"]: d for d in found["lmstudio"]}
    assert by_id["lmstudio-qwen-qwen3-8b"]["model"] == "lm_studio/qwen/qwen3-8b"
    assert by_id["lmstudio-text-embedding-nomic-embed-text-v1.5"]["mode"] == "embedding"

    rich = {"data": [{"id": "qwen/qwen3-8b", "type": "llm", "arch": "qwen3", "max_context_length": 262144}]}
    rt2 = runtimes({("GET", "/v1/models"): models, ("GET", "/api/v0/models"): rich})
    found = local.discover(dead_env(LM_STUDIO_API_BASE=rt2.base + "/v1"))
    q = next(d for d in found["lmstudio"] if d["id"] == "lmstudio-qwen-qwen3-8b")
    assert q["context_tokens"] == 262144 and "long-context" in q["aliases"]


def test_generic_openai_only_when_configured(runtimes):
    assert "local_openai" not in local.runtime_bases({}) or local.runtime_bases({})["local_openai"] is None
    rt = runtimes({("GET", "/v1/models"): {"data": [{"id": "gpt-oss-20b.gguf", "meta": {"n_params": 20_900_000_000, "n_ctx_train": 131072}}, {"id": "bge-m3"}]}})
    found = local.discover(dead_env(MUSE_LOCAL_OPENAI_BASE=rt.base + "/v1"))
    by_id = {d["id"]: d for d in found["local_openai"]}
    assert by_id["local-openai-gpt-oss-20b.gguf"]["model"] == "openai/gpt-oss-20b.gguf"
    assert by_id["local-openai-gpt-oss-20b.gguf"]["aliases"] == ["smart"]
    assert by_id["local-openai-bge-m3"]["aliases"] == ["embed"]


def test_unreachable_runtimes_do_not_raise():
    env = dead_env(MUSE_LOCAL_OPENAI_BASE=f"http://127.0.0.1:{free_port()}/v1")
    assert local.discover(env) == {}
    probes = local.probe_runtimes(env)
    assert len(probes) == 3 and not any(p.reachable for p in probes)
    assert all(p.error for p in probes)


def test_garbage_payloads_do_not_raise(runtimes):
    rt = runtimes({("GET", "/api/tags"): ["not", "a", "dict"], ("GET", "/v1/models"): {"data": "nope"}})
    assert local.discover(dead_env(OLLAMA_API_BASE=rt.base, LM_STUDIO_API_BASE=rt.base + "/v1")) == {}


def test_non_loopback_is_not_probed_by_default():
    probes = local.probe_runtimes({"OLLAMA_API_BASE": "http://192.0.2.1:11434", "LM_STUDIO_API_BASE": "http://127.0.0.1:1/v1"})
    ollama = next(p for p in probes if p.provider == "ollama")
    assert not ollama.reachable and "loopback" in ollama.error


def test_host_docker_internal_is_dialled_as_loopback(runtimes):
    rt = runtimes(ollama_routes())
    env = dead_env(OLLAMA_API_BASE=f"http://host.docker.internal:{rt.port}")
    assert "ollama" in local.discover(env)


def test_slug_uniqueness_and_prefix(runtimes):
    routes = {("GET", "/api/tags"): {"models": [{"name": "Qwen3:8B"}, {"name": "qwen3:8b"}, {"name": "qwen3_8b"}, {"name": "qwen3:8b-q4"}]}}
    rt = runtimes(routes)
    ids = [d["id"] for d in local.discover(dead_env(OLLAMA_API_BASE=rt.base))["ollama"]]
    assert len(ids) == len(set(ids)) == 4
    assert all(i.startswith("ollama-") for i in ids)
    assert ids[:3] == ["ollama-qwen3-8b", "ollama-qwen3-8b-2", "ollama-qwen3-8b-3"]


@pytest.mark.parametrize(
    "name,expected",
    [("qwen3:8b", "qwen3-8b"), ("llama3.2:latest", "llama3.2"), ("hf.co/Org/Model-7B-GGUF:Q4_K_M", "hf.co-org-model-7b-gguf-q4-k-m"), ("::", "model")],
)
def test_slugify(name, expected):
    assert local.slugify(name) == expected


@pytest.mark.parametrize(
    "texts,expected",
    [(("8.2B",), 8.2), (("566.7M",), 0.5667), (("qwen3:8b",), 8.0), (("mixtral-8x7b",), 56.0), (("gemma3n:e4b",), 4.0), (("llama3.3:70b",), 70.0), (("mystery",), None), ((None, "phi-3-mini-4k"), None)],
)
def test_parse_params(texts, expected):
    got = local.parse_params_b(*texts)
    assert (got is None) if expected is None else got == pytest.approx(expected)


@pytest.mark.parametrize(
    "name,mode,params,ctx,expected",
    [
        ("embed-x", "embedding", None, None, ["embed"]),
        ("rr", "rerank", 1, None, []),
        ("small-model", "chat", 70, None, ["fast", "cheap"]),
        ("phi-4-mini", "chat", None, None, ["fast", "cheap"]),
        ("gemma-2-9b", "chat", 9.2, None, ["fast", "cheap"]),
        ("qwen3", "chat", 8, None, ["fast", "cheap"]),
        ("qwen3", "chat", 14, None, ["smart"]),
        ("llama", "chat", 70, 131072, ["smart"]),
        ("llama", "chat", 70, 262144, ["smart", "long-context"]),
        ("qwen3", "chat", 8, 1_000_000, ["fast", "cheap", "long-context"]),
        ("unknown", "chat", None, None, ["cheap"]),
        ("unknown", "chat", None, 200_000, ["cheap", "long-context"]),
        ("unknown", "chat", None, 199_999, ["cheap"]),
    ],
)
def test_alias_heuristics(name, mode, params, ctx, expected):
    assert local.suggest_aliases(name, mode, params, ctx) == expected


@pytest.mark.parametrize(
    "name,expected",
    [("bge-m3", "embedding"), ("nomic-embed-text", "embedding"), ("e5-large", "embedding"), ("gte-base", "embedding"), ("mxbai-embed-large", "embedding"), ("bge-reranker-v2-m3", "rerank"), ("qwen3", "chat"), ("gemma3", "chat")],
)
def test_classify_by_name(name, expected):
    assert local.classify_mode(name) == expected


def test_write_catalog_format_perms_and_renderer_compat(tmp_path, monkeypatch):
    found = {"ollama": [{"id": "ollama-a", "model": "ollama_chat/a", "mode": "chat", "aliases": ["fast"], "notes": "n"}]}
    path = tmp_path / "local.yaml"
    local.write_catalog(found, path)
    text = path.read_text()
    assert text.startswith("# Local model catalogue.") and "edit freely" in text
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # the renderer's own loader accepts it
    import importlib.util

    from muse import paths

    spec = importlib.util.spec_from_file_location("rc", paths.REPO_ROOT / "scripts" / "render_config.py")
    rc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc)
    loaded = rc.load_local_deployments({"MUSE_LOCAL_CATALOG": str(path)})
    assert loaded["ollama"][0]["id"] == "ollama-a"


def test_merge_preserves_user_aliases_and_extras(tmp_path):
    path = tmp_path / "local.yaml"
    first = {"ollama": [{"id": "ollama-a", "model": "ollama_chat/a", "mode": "chat", "aliases": ["fast", "cheap"], "notes": "old"}, {"id": "ollama-gone", "model": "ollama_chat/gone", "mode": "chat", "aliases": ["smart"], "notes": ""}]}
    local.write_catalog(first, path)
    data = yaml.safe_load(path.read_text())
    data["ollama"][0]["aliases"] = ["smart", "my-alias"]
    data["ollama"][0]["order"] = 1
    path.write_text(yaml.safe_dump(data))
    second = {"ollama": [{"id": "ollama-a", "model": "ollama_chat/a:new", "mode": "chat", "aliases": ["fast", "cheap"], "context_tokens": 4096, "notes": "new"}, {"id": "ollama-b", "model": "ollama_chat/b", "mode": "chat", "aliases": ["cheap"], "notes": ""}]}
    local.write_catalog(second, path)
    out = yaml.safe_load(path.read_text())
    a = next(d for d in out["ollama"] if d["id"] == "ollama-a")
    assert a["aliases"] == ["smart", "my-alias"] and a["order"] == 1
    assert a["model"] == "ollama_chat/a:new" and a["notes"] == "new" and a["context_tokens"] == 4096
    ids = [d["id"] for d in out["ollama"]]
    assert ids == ["ollama-a", "ollama-b"]  # vanished model dropped, new one added


def test_offline_provider_is_left_untouched(tmp_path):
    path = tmp_path / "local.yaml"
    local.write_catalog({"lmstudio": [{"id": "lmstudio-x", "model": "lm_studio/x", "mode": "chat", "aliases": ["fast"], "notes": ""}]}, path)
    local.write_catalog({"ollama": [{"id": "ollama-a", "model": "ollama_chat/a", "mode": "chat", "aliases": [], "notes": ""}]}, path)
    out = yaml.safe_load(path.read_text())
    assert set(out) == {"lmstudio", "ollama"}
    assert out["lmstudio"][0]["aliases"] == ["fast"]


def test_suggest_env_docker_variant():
    env = local.suggest_env({"ollama": [], "lmstudio": []}, {})
    assert env["OLLAMA_API_BASE"]["host"] == "http://127.0.0.1:11434"
    assert env["OLLAMA_API_BASE"]["docker"] == "http://host.docker.internal:11434"
    assert env["LM_STUDIO_API_BASE"]["docker"] == "http://host.docker.internal:1234/v1"
    assert "container" in env["OLLAMA_API_BASE"]["why"]
    assert "MUSE_LOCAL_OPENAI_BASE" not in env
