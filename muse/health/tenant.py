"""Load the issuing organisation's settings from config/tenants/<id>.yaml."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

import yaml

from muse import paths
from muse.health.contracts import Tenant

TENANT_DIR = paths.REPO_ROOT / "config" / "tenants"
PRIVACY = ("public", "standard", "private", "local")


class TenantError(ValueError):
    pass


def load_tenant(tenant_id: str | None = None, environ: Mapping[str, str] | None = None, directory: Path | None = None) -> Tenant:
    env = os.environ if environ is None else environ
    name = (tenant_id or env.get("MUSE_TENANT", "") or "demo-hospital").strip()
    if not name.replace("-", "").replace("_", "").isalnum():
        raise TenantError("tenant id may only contain letters, digits, - and _")
    path = (directory or TENANT_DIR) / f"{name}.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TenantError(f"no tenant file for {name!r}") from exc
    if not isinstance(raw, dict):
        raise TenantError(f"{path.name} must be a mapping")
    allowed = tuple(str(x) for x in raw.get("allowed_privacy") or ("local",))
    default = str(raw.get("default_privacy", "local"))
    for value in (*allowed, default):
        if value not in PRIVACY:
            raise TenantError(f"unknown privacy class {value!r} in {path.name}")
    if default not in allowed:
        raise TenantError("default_privacy must be one of allowed_privacy")
    # Health content may never default to a class that lets training providers in.
    if default in ("public", "standard"):
        raise TenantError("default_privacy for health content must be local or private")
    issuer = str(raw.get("issuer", "hospital"))
    if issuer not in ("hospital", "company"):
        raise TenantError("issuer must be hospital or company")
    features = {str(k): bool(v) for k, v in (raw.get("features") or {}).items()}
    return Tenant(
        id=name,
        display_name=str(raw.get("display_name", name)),
        issuer=issuer,
        locale=str(raw.get("locale", "zh-TW")),
        default_privacy=default,
        allowed_privacy=allowed,
        pilot_ref=str(raw.get("pilot_ref", "")),
        consent_version=str(raw.get("consent_version", "")),
        consent_text=str(raw.get("consent_text", "")).strip(),
        emergency_text=str(raw.get("emergency_text", "")).strip(),
        contact=str(raw.get("contact", "")),
        features=features,
    )
