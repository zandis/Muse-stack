"""Map LiteLLM aliases to shared upstream model ids.

The catalog (``config/providers.yaml``) lists each deployment once, with
the aliases it joins. The rendered LiteLLM config then *copies* that
deployment once per alias (``{pin_id}__{alias}``), so LiteLLM's own
cooldown — which is keyed by deployment id — does not automatically
cool the same upstream under a different alias.

This index is the input to the circuit breaker and to the LiteLLM
callback: given an alias, the upstream model strings; given an
upstream, every alias (and every rendered deployment id) that shares it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = ROOT / "config" / "providers.yaml"


@dataclass(frozen=True)
class Deployment:
    provider_id: str
    pin_id: str
    model: str  # upstream, e.g. "groq/openai/gpt-oss-20b"
    aliases: tuple[str, ...]
    join: str


@dataclass
class UpstreamIndex:
    deployments: tuple[Deployment, ...]
    alias_upstreams: dict[str, tuple[str, ...]]
    upstream_aliases: dict[str, tuple[str, ...]]
    # rendered LiteLLM model_list ids: "{pin_id}__{alias}"
    upstream_deployment_ids: dict[str, tuple[str, ...]]

    def upstreams_for(self, alias: str) -> tuple[str, ...]:
        return self.alias_upstreams.get(alias, ())

    def aliases_for(self, upstream: str) -> tuple[str, ...]:
        return self.upstream_aliases.get(upstream, ())

    def sibling_deployment_ids(self, deployment_id: str) -> tuple[str, ...]:
        """Other rendered LiteLLM ids that share this deployment's upstream."""
        upstream = self.upstream_of_deployment_id(deployment_id)
        if not upstream:
            return ()
        return tuple(d for d in self.upstream_deployment_ids.get(upstream, ()) if d != deployment_id)

    def upstream_of_deployment_id(self, deployment_id: str) -> str | None:
        for upstream, ids in self.upstream_deployment_ids.items():
            if deployment_id in ids:
                return upstream
        # A direct (non-alias) name is the pin_id itself.
        for dep in self.deployments:
            if dep.pin_id == deployment_id:
                return dep.model
        return None

    def breaker_key(self, name: str) -> str:
        """Canonical circuit-breaker key for a caller-supplied name.

        If ``name`` is an alias with a single upstream, return that
        upstream. If it is already an upstream model string, return it.
        Otherwise return ``name`` unchanged (a mock alias, a provider id).
        """
        if name in self.alias_upstreams:
            ups = self.alias_upstreams[name]
            if len(ups) == 1:
                return ups[0]
        if name in self.upstream_aliases:
            return name
        for dep in self.deployments:
            if dep.pin_id == name or dep.model == name:
                return dep.model
        return name


def load_upstream_index(catalog: Mapping[str, Any] | Path | None = None) -> UpstreamIndex:
    data: Mapping[str, Any]
    if catalog is None:
        path = DEFAULT_CATALOG
        data = _load_yaml(path)
    elif isinstance(catalog, Path):
        data = _load_yaml(catalog)
    else:
        data = catalog
    deployments: list[Deployment] = []
    alias_map: dict[str, list[str]] = {}
    upstream_aliases: dict[str, list[str]] = {}
    upstream_ids: dict[str, list[str]] = {}
    for provider in data.get("providers") or []:
        provider_id = str(provider.get("id") or "")
        for raw in provider.get("deployments") or []:
            pin_id = str(raw["id"])
            model = str(raw["model"])
            aliases = tuple(str(a) for a in (raw.get("aliases") or []))
            join = str(raw.get("join") or "always")
            deployments.append(
                Deployment(
                    provider_id=provider_id,
                    pin_id=pin_id,
                    model=model,
                    aliases=aliases,
                    join=join,
                )
            )
            for alias in aliases:
                alias_map.setdefault(alias, [])
                if model not in alias_map[alias]:
                    alias_map[alias].append(model)
                upstream_aliases.setdefault(model, [])
                if alias not in upstream_aliases[model]:
                    upstream_aliases[model].append(alias)
                rendered = f"{pin_id}__{alias}"
                upstream_ids.setdefault(model, [])
                if rendered not in upstream_ids[model]:
                    upstream_ids[model].append(rendered)
            # The direct (non-alias) model_list entry uses pin_id.
            upstream_ids.setdefault(model, [])
            if pin_id not in upstream_ids[model]:
                upstream_ids[model].append(pin_id)
    return UpstreamIndex(
        deployments=tuple(deployments),
        alias_upstreams={k: tuple(v) for k, v in alias_map.items()},
        upstream_aliases={k: tuple(v) for k, v in upstream_aliases.items()},
        upstream_deployment_ids={k: tuple(v) for k, v in upstream_ids.items()},
    )


def _load_yaml(path: Path) -> Mapping[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    return data
