"""Find models running on this machine and write the local catalogue.

Why: the cloud catalogue is fixed, but which Ollama / LM Studio / llama.cpp
models exist differs per machine. scripts/render_config.py reads the file this
module writes ($MUSE_HOME/local.yaml) and adds those models to the aliases.

Endpoint shapes (checked against docs 2026-11; see UNCONFIRMED below):
- Ollama: GET {base}/api/tags -> {"models": [{"name", "details": {"family",
  "families", "parameter_size", "quantization_level"}}]}; POST {base}/api/show
  {"model": name} -> {"details", "model_info": {"<arch>.context_length": int},
  "capabilities": ["completion", ...]}. LiteLLM: `ollama_chat/<name>` for chat
  (docs: "we recommend ollama_chat"), api_base WITHOUT /v1.
- LM Studio: GET {base}/models (OpenAI shape, base ends in /v1). LiteLLM:
  `lm_studio/<id>`, api_base = LM_STUDIO_API_BASE (with /v1), embeddings
  supported through the same prefix per the LiteLLM LM Studio page.
- Generic OpenAI-compatible: GET {base}/models -> {"data": [{"id", ...}]}.
  LiteLLM: `openai/<id>` + api_base.

UNCONFIRMED (written from memory, treated as optional enrichment, never required):
- LM Studio's richer `GET {root}/api/v0/models` (type llm|vlm|embeddings,
  max_context_length, arch, quantization).
- llama.cpp `/v1/models` `data[].meta.{n_params,n_ctx_train}` and vLLM
  `data[].max_model_len`.
- `ollama/<name>` for embeddings: the LiteLLM Ollama page fetched did not
  cover embeddings; this prefix is from LiteLLM's embedding docs as remembered.
- Nobody here serves a rerank endpoint that we could verify: Ollama has no
  rerank API; LM Studio has none; llama.cpp serves /v1/rerank only when
  started with --reranking, and we do not POST to probe that. So rerank models
  are listed (mode: rerank) with NO alias; add `rerank` by hand if yours works.
- The generic `openai/` provider in LiteLLM may insist on an api_key even for
  a keyless local server; scripts/render_config.py sets none for local
  providers. If calls fail with a missing-key error, set OPENAI_API_KEY=none
  in the container environment.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from wardmuse import paths
from wardmuse.keys import atomic_write_text

OLLAMA = "ollama"
LMSTUDIO = "lmstudio"
GENERIC = "local_openai"

DEFAULT_OLLAMA = "http://127.0.0.1:11434"
DEFAULT_LMSTUDIO = "http://127.0.0.1:1234/v1"

BASE_ENV = {OLLAMA: "OLLAMA_API_BASE", LMSTUDIO: "LM_STUDIO_API_BASE", GENERIC: "MUSE_LOCAL_OPENAI_BASE"}
LONG_CONTEXT_TOKENS = 200_000
PROBE_TIMEOUT = 1.5
MAX_SHOW_CALLS = 60

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
_EMBED_RE = re.compile(r"(embed|bge-|bge_|\bbge\b|nomic-embed|\be5\b|e5-|\bgte\b|gte-|minilm|mxbai|snowflake-arctic-embed|jina-embeddings)", re.I)
_RERANK_RE = re.compile(r"(rerank|reranker)", re.I)
_SMALL_NAME_RE = re.compile(r"(mini|small|lite|flash|tiny|nano)", re.I)


@dataclass
class Probe:
    """Result of probing one runtime. `error` is a short reason, never a payload."""

    provider: str
    base: str
    reachable: bool
    error: str | None = None
    models: list[dict[str, Any]] = field(default_factory=list)


# ------------------------------------------------------------------ helpers


def runtime_bases(environ: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """Base URL per runtime as the user configured it (None = not configured, no default)."""
    env = os.environ if environ is None else environ
    ollama = env.get(BASE_ENV[OLLAMA], "").strip() or DEFAULT_OLLAMA
    lms = env.get(BASE_ENV[LMSTUDIO], "").strip() or DEFAULT_LMSTUDIO
    generic = env.get(BASE_ENV[GENERIC], "").strip() or None
    return {OLLAMA: ollama.rstrip("/"), LMSTUDIO: lms.rstrip("/"), GENERIC: generic.rstrip("/") if generic else None}


def _probe_url(base: str, environ: Mapping[str, str]) -> str | None:
    """Map a configured base to what THIS host should dial; None if it is not loopback.

    host.docker.internal is how the container sees the host, so on the host we
    dial loopback instead. Anything else non-loopback needs MUSE_LOCAL_ALLOW_REMOTE=1.
    """
    parsed = urllib.parse.urlsplit(base)
    host = (parsed.hostname or "").lower()
    if host == "host.docker.internal":
        netloc = "127.0.0.1" + (f":{parsed.port}" if parsed.port else "")
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    if host in _LOOPBACK or envget(environ, "MUSE_LOCAL_ALLOW_REMOTE", "").strip().lower() in {"1", "true", "yes"}:
        return base
    return None


def _request_json(url: str, payload: dict[str, Any] | None = None, timeout: float = PROBE_TIMEOUT) -> Any | None:
    """GET (or POST json) and parse; None on any failure. Never raises."""
    try:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback by default
            return json.loads(resp.read(8_000_000).decode("utf-8", "replace"))
    except Exception:  # URLError, timeout, bad JSON, bad scheme ...
        return None


def parse_params_b(*texts: str | None) -> float | None:
    """Parameter count in billions from '8B', '566.7M', 'qwen3-8b', 'mixtral-8x7b'."""
    for text in texts:
        if not text:
            continue
        t = text.strip().lower()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmbt])", t)
        if m:
            return float(m.group(1)) * {"k": 1e-6, "m": 1e-3, "b": 1.0, "t": 1000.0}[m.group(2)]
        m = re.search(r"(?:^|[^a-z0-9.])(\d+)x(\d+(?:\.\d+)?)b(?![a-z])", t)
        if m:
            return float(m.group(1)) * float(m.group(2))
        m = re.search(r"(?:^|[^a-z0-9.])e?(\d+(?:\.\d+)?)b(?![a-z])", t)
        if m:
            return float(m.group(1))
    return None


def slugify(name: str) -> str:
    """'qwen3:8b' -> 'qwen3-8b'; ':latest' is dropped; '/' and other punctuation become '-'."""
    n = name.strip().lower()
    if n.endswith(":latest"):
        n = n[: -len(":latest")]
    n = re.sub(r"[^a-z0-9.]+", "-", n).strip("-.")
    return n or "model"


def classify_mode(name: str, capabilities: list[str] | None = None, runtime_type: str | None = None) -> str:
    """chat | embedding | rerank, from runtime metadata first, then the name."""
    caps = {c.lower() for c in capabilities or []}
    rt = (runtime_type or "").lower()
    if "rerank" in caps or _RERANK_RE.search(name):
        return "rerank"
    if "embedding" in caps or "embeddings" in caps or rt in {"embeddings", "embedding"}:
        return "embedding"
    if caps and ("completion" in caps or "chat" in caps):
        return "chat"  # the runtime says it generates text; trust it over a name match
    if _EMBED_RE.search(name):
        return "embedding"
    return "chat"


def suggest_aliases(name: str, mode: str, params_b: float | None, context_tokens: int | None) -> list[str]:
    """Heuristic aliases. The user can edit them; edits survive re-discovery."""
    if mode == "embedding":
        return ["embed"]
    if mode != "chat":
        return []  # rerank: see UNCONFIRMED in the module docstring
    if _SMALL_NAME_RE.search(name) or (params_b is not None and params_b <= 9.5):
        aliases = ["fast", "cheap"]
    elif params_b is not None:
        aliases = ["smart"]
    else:
        aliases = ["cheap"]
    if context_tokens is not None and context_tokens >= LONG_CONTEXT_TOKENS:
        aliases.append("long-context")
    return aliases


def litellm_model(provider: str, name: str, mode: str) -> str:
    if provider == OLLAMA:
        return f"ollama/{name}" if mode == "embedding" else f"ollama_chat/{name}"
    if provider == LMSTUDIO:
        return f"lm_studio/{name}"
    return f"hosted_vllm/{name}" if mode == "rerank" else f"openai/{name}"


def _deployment(provider: str, name: str, info: dict[str, Any], taken: set[str]) -> dict[str, Any]:
    mode = classify_mode(name, info.get("capabilities"), info.get("type"))
    params = parse_params_b(info.get("parameter_size"), name)
    ctx = info.get("context_tokens")
    prefix = provider.replace("_", "-")
    base_id = f"{prefix}-{slugify(name)}"
    dep_id, n = base_id, 2
    while dep_id in taken:
        dep_id, n = f"{base_id}-{n}", n + 1
    taken.add(dep_id)
    bits = [b for b in (info.get("family"), info.get("parameter_size"), info.get("quantization")) if b]
    notes = "Discovered locally" + (f" ({', '.join(str(b) for b in bits)})" if bits else "") + "."
    if mode == "rerank":
        notes += " No alias: no verified rerank endpoint for this runtime; add 'rerank' by hand if /rerank works."
    dep: dict[str, Any] = {
        "id": dep_id,
        "model": litellm_model(provider, name, mode),
        "mode": mode,
        "aliases": suggest_aliases(name, mode, params, ctx),
    }
    if ctx:
        dep["context_tokens"] = int(ctx)
    dep["notes"] = notes
    return dep


# ------------------------------------------------------------------- probes


def _probe_ollama(base: str, dial: str) -> Probe:
    root = dial[:-3] if dial.endswith("/v1") else dial
    tags = _request_json(f"{root}/api/tags")
    if not isinstance(tags, dict) or not isinstance(tags.get("models"), list):
        return Probe(OLLAMA, base, False, "no response from /api/tags")
    models: list[dict[str, Any]] = []
    for i, entry in enumerate(tags["models"]):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or entry.get("model") or "").strip()
        if not name:
            continue
        details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
        info: dict[str, Any] = {
            "name": name,
            "family": details.get("family"),
            "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "capabilities": entry.get("capabilities"),
        }
        if i < MAX_SHOW_CALLS:
            show = _request_json(f"{root}/api/show", {"model": name}, timeout=3.0)
            if isinstance(show, dict):
                if isinstance(show.get("capabilities"), list):
                    info["capabilities"] = show["capabilities"]
                mi = show.get("model_info")
                if isinstance(mi, dict):
                    for key, val in mi.items():
                        if key.endswith(".context_length") and isinstance(val, int):
                            info["context_tokens"] = val
                            break
                d2 = show.get("details")
                if isinstance(d2, dict):
                    info["family"] = info["family"] or d2.get("family")
                    info["parameter_size"] = info["parameter_size"] or d2.get("parameter_size")
        models.append(info)
    return Probe(OLLAMA, base, True, None, models)


def _openai_models(url_base: str) -> list[dict[str, Any]] | None:
    data = _request_json(f"{url_base}/models")
    if not isinstance(data, dict):
        return None
    items = data.get("data")
    if not isinstance(items, list):
        items = data.get("models")  # some llama.cpp builds
    if not isinstance(items, list):
        return None
    return [m for m in items if isinstance(m, dict)]


def _probe_lmstudio(base: str, dial: str) -> Probe:
    items = _openai_models(dial)
    if items is None:
        return Probe(LMSTUDIO, base, False, "no response from /models")
    root = dial[:-3] if dial.endswith("/v1") else dial
    rich: dict[str, dict[str, Any]] = {}
    v0 = _request_json(f"{root}/api/v0/models")  # UNCONFIRMED enrichment
    if isinstance(v0, dict) and isinstance(v0.get("data"), list):
        rich = {str(m.get("id")): m for m in v0["data"] if isinstance(m, dict)}
    models = []
    for item in items:
        name = str(item.get("id") or "").strip()
        if not name:
            continue
        extra = rich.get(name, {})
        ctx = extra.get("max_context_length")
        models.append(
            {
                "name": name,
                "family": extra.get("arch"),
                "quantization": extra.get("quantization"),
                "type": extra.get("type"),
                "context_tokens": ctx if isinstance(ctx, int) else None,
            }
        )
    return Probe(LMSTUDIO, base, True, None, models)


def _probe_generic(base: str, dial: str) -> Probe:
    items = _openai_models(dial)
    if items is None:
        return Probe(GENERIC, base, False, "no response from /models")
    models = []
    for item in items:
        name = str(item.get("id") or item.get("model") or item.get("name") or "").strip()
        if not name:
            continue
        meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
        ctx = meta.get("n_ctx_train") or item.get("max_model_len")  # UNCONFIRMED shapes
        n_params = meta.get("n_params")
        info: dict[str, Any] = {"name": name, "context_tokens": ctx if isinstance(ctx, int) else None}
        if isinstance(n_params, (int, float)) and n_params > 0:
            info["parameter_size"] = f"{n_params / 1e9:.1f}B"
        models.append(info)
    return Probe(GENERIC, base, True, None, models)


def probe_runtimes(environ: Mapping[str, str] | None = None) -> list[Probe]:
    """Probe every configured runtime. Never raises."""
    env = os.environ if environ is None else environ
    probes: list[Probe] = []
    fns = {OLLAMA: _probe_ollama, LMSTUDIO: _probe_lmstudio, GENERIC: _probe_generic}
    for provider, base in runtime_bases(env).items():
        if not base:
            continue
        dial = _probe_url(base, env)
        if dial is None:
            probes.append(Probe(provider, base, False, "not a loopback address (set MUSE_LOCAL_ALLOW_REMOTE=1 to probe it)"))
            continue
        try:
            probes.append(fns[provider](base, dial))
        except Exception as exc:  # belt and braces: discovery must never take the CLI down
            probes.append(Probe(provider, base, False, type(exc).__name__))
    return probes


def discover(environ: Mapping[str, str] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Deployments per reachable provider id. Unreachable runtimes are omitted."""
    out: dict[str, list[dict[str, Any]]] = {}
    for probe in probe_runtimes(environ):
        if not probe.reachable:
            continue
        taken: set[str] = set()
        out[probe.provider] = [_deployment(probe.provider, m["name"], m, taken) for m in probe.models]
    return out


# ---------------------------------------------------------------- catalogue

HEADER = (
    "# Local model catalogue. Generated by `muse local discover --write`; edit freely.\n"
    "# Read by scripts/render_config.py. Re-running discovery keeps the `aliases`\n"
    "# (and any extra keys such as order/rpm) you set for models that still exist,\n"
    "# updates the rest, and leaves providers that were offline untouched.\n"
    "# YAML comments in this file are not preserved on rewrite.\n"
)


def read_catalog(path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    target = path or paths.local_catalog_path()
    try:
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): [i for i in v if isinstance(i, dict)] for k, v in data.items() if isinstance(v, list)}


def merge_catalog(
    existing: Mapping[str, list[dict[str, Any]]], found: Mapping[str, list[dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """New facts win; the user's `aliases` and unknown extra keys win over generated ones."""
    merged: dict[str, list[dict[str, Any]]] = {k: list(v) for k, v in existing.items()}
    for provider, deployments in found.items():
        old_by_id = {str(d.get("id")): d for d in existing.get(provider, [])}
        rows = []
        for dep in deployments:
            row = dict(dep)
            old = old_by_id.get(str(dep["id"]))
            if old:
                if "aliases" in old:
                    row["aliases"] = old["aliases"]
                for key, val in old.items():
                    row.setdefault(key, val)
            rows.append(row)
        merged[provider] = rows
    return merged


def write_catalog(found: Mapping[str, list[dict[str, Any]]], path: Path | None = None) -> Path:
    """Merge `found` into the catalogue on disk and write it atomically, 0600."""
    target = path or paths.local_catalog_path()
    merged = merge_catalog(read_catalog(target), found)
    body = yaml.safe_dump(merged, sort_keys=False, default_flow_style=False, allow_unicode=True)
    atomic_write_text(target, HEADER + body)
    return target


def suggest_env(found: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, dict[str, str]]:
    """Base-URL variables to set for the runtimes that were found.

    `host` is for `muse up --native`; `docker` is what the container needs,
    because inside it 127.0.0.1 is the container itself, not your machine.
    """
    bases = runtime_bases(environ)
    out: dict[str, dict[str, str]] = {}
    for provider in found:
        base = bases.get(provider)
        if not base:
            continue
        docker = re.sub(r"//(127\.0\.0\.1|localhost|\[::1\])", "//host.docker.internal", base)
        entry = {"host": base, "docker": docker, "why": "Inside Docker, 127.0.0.1 is the container; host.docker.internal reaches your machine."}
        if provider == OLLAMA:
            entry["note"] = "Ollama listens on 127.0.0.1 only by default; for Docker on Linux start it with OLLAMA_HOST=0.0.0.0 (or the docker0 address)."
            entry["host"] = base[:-3] if base.endswith("/v1") else base
            entry["docker"] = entry["docker"][:-3] if entry["docker"].endswith("/v1") else entry["docker"]
        elif provider == LMSTUDIO:
            entry["note"] = "In LM Studio's server settings enable 'Serve on Local Network' so the container can connect."
        out[BASE_ENV[provider]] = entry
    return out
