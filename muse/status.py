"""Build the status snapshot (muse/types.py::Snapshot) from catalogue, keys and ledger.

Why here and not in the CLI: `muse status`, the dashboard and tests all need
the same answer. Join and enable rules are not re-implemented: the catalogue is
rendered with scripts/render_config.py, exactly as the proxy does at startup.
Never includes key values or prompt/response text.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from muse import paths, quota
from muse import queue as queue_mod
from muse.ledger import Ledger
from muse.types import (
    AliasStatus,
    AppStatus,
    DeploymentStatus,
    Limit,
    Paid,
    Policy,
    ProviderStatus,
    RecentCall,
    Snapshot,
)

_SCRIPTS = paths.REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import render_config  # noqa: E402

WINDOW_SECONDS = {"minute": 60, "day": 86400, "month": 31 * 86400}
PLACEHOLDER = "configured"  # stands in for a key so render() sees it as set; never a real value


def _key_configured(name: str, environ: Mapping[str, str]) -> bool:
    try:
        from muse import keys

        return bool(keys.is_configured(name, environ))
    except Exception:  # keys module missing or keychain broken: fall back to the environment
        return bool(str(environ.get(name, "")).strip())


def _default_privacy(environ: Mapping[str, str]) -> str:
    raw = environ.get("MUSE_DEFAULT_PRIVACY", "").strip() or "standard"
    try:
        from muse import policy

        return policy.normalise_class(raw)
    except Exception:
        return raw if raw in ("public", "standard", "private", "local") else "local"


def _policy(provider: Mapping[str, Any]) -> Policy:
    raw = provider.get("policy") or {}
    return Policy(
        trains_on_inputs=render_config._tri(raw.get("trains_on_inputs")),
        human_review=render_config._tri(raw.get("human_review")),
        region=str(raw.get("region", "unknown")),
        commercial_use=str(raw.get("commercial_use", "unknown")),
        terms_url=str(raw.get("terms_url", "")),
        note=str(raw.get("note", "")),
    )


def _proxy_url(environ: Mapping[str, str]) -> str:
    explicit = environ.get("MUSE_PROXY_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")
    return f"http://127.0.0.1:{environ.get('MUSE_PORT', '').strip() or '4000'}"


def _reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health/liveliness", timeout=2) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _declared_quota(provider: Mapping[str, Any]) -> list[tuple[str, str, int]]:
    """(window, metric, limit) from an optional `quota:` on the provider.

    Accepts a list of {window, metric, limit} or a mapping like
    {requests_per_day: 1000, tokens_per_minute: 6000}.
    """
    raw = provider.get("quota")
    out: list[tuple[str, str, int]] = []
    items: list[tuple[str, str, Any]] = []
    if isinstance(raw, list):
        items = [(str(i.get("window")), str(i.get("metric")), i.get("limit")) for i in raw if isinstance(i, Mapping)]
    elif isinstance(raw, Mapping):
        for key, value in raw.items():
            metric, _, window = str(key).partition("_per_")
            items.append((window, metric, value))
    for window, metric, limit in items:
        if window in WINDOW_SECONDS and metric in ("requests", "tokens"):
            try:
                out.append((window, metric, int(limit)))
            except (TypeError, ValueError):
                continue
    return out


def _window_reset(window: str, provider_id: str, now: float) -> float:
    if window == "minute":
        return now - (now % 60) + 60
    if window == "day":
        return quota.day_start(now, provider_id) + 86400
    return quota.next_month_utc(now)


def _fresh(row: Mapping[str, Any], now: float) -> bool:
    reset_at = row.get("reset_at")
    if reset_at is not None:
        return reset_at > now
    return row.get("seen_at", 0) > now - WINDOW_SECONDS.get(str(row.get("window")), 60)


class _SafeLedger:
    """Ledger reads that never fail the snapshot (missing dir, locked file, ...)."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.ok = True

    def __getattr__(self, name: str) -> Any:
        fn = getattr(self.ledger, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception:
                self.ok = False
                return None

        return call


def build_snapshot(
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
    check_proxy: bool = False,
    ledger: Ledger | None = None,
) -> Snapshot:
    """Everything the CLI and dashboard show, as one JSON-serialisable dict."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    ts = time.time() if now is None else float(now)
    home = paths.home(env)
    db = _SafeLedger(ledger or Ledger(paths.ledger_path(env), clock=lambda: ts))
    default_privacy = _default_privacy(env)

    try:
        catalog = render_config.load_catalog(Path(env.get("MUSE_CATALOG", "") or paths.CATALOG_PATH))
    except (SystemExit, OSError, Exception):
        catalog = {"providers": [], "aliases": {}}

    # Render the way the proxy would, with placeholders for keys held outside the env.
    render_env = dict(env)
    configured: dict[str, bool] = {}
    for provider in catalog["providers"]:
        if render_config.is_local(provider):
            configured[provider["id"]] = bool(str(env.get(str(provider.get("base_env")), "")).strip())
        else:
            name = str(provider["env"])
            configured[provider["id"]] = _key_configured(name, env)
            if configured[provider["id"]] and not str(env.get(name, "")).strip():
                render_env[name] = PLACEHOLDER
    try:
        rendered, enabled_ids = render_config.render(catalog, render_env)
    except (SystemExit, Exception):
        rendered, enabled_ids = {"model_list": []}, []
    enabled = set(enabled_ids)
    local_deployments = render_config.load_local_deployments(env)

    blocks = db.active_blocks(ts) or {}

    def block_for(provider_id: str, upstream: str | None) -> dict[str, Any] | None:
        found = [b for b in (blocks.get(upstream or ""), blocks.get(f"provider:{provider_id}")) if b]
        return max(found, key=lambda b: (b["kind"] == "exhausted", b["until"])) if found else None

    def state_of(block: Mapping[str, Any] | None) -> str:
        if block is None:
            return "ok"
        return "exhausted" if block["kind"] == "exhausted" else "cooling"

    # alias name -> rendered entries; pin id -> aliases it joined
    joined: dict[str, list[str]] = {}
    alias_entries: dict[str, list[dict[str, Any]]] = {}
    for entry in rendered.get("model_list", []):
        name = entry["model_name"]
        if name in catalog["aliases"]:
            alias_entries.setdefault(name, []).append(entry)
            pin = str(entry["model_info"]["id"]).removesuffix(f"__{name}")
            joined.setdefault(pin, []).append(name)

    providers: list[ProviderStatus] = []
    for provider in catalog["providers"]:
        pid = str(provider["id"])
        local = render_config.is_local(provider)
        deployments = list(provider.get("deployments") or [])
        if local:
            deployments += [d for d in local_deployments.get(pid, [])]
        is_on = pid in enabled
        provider_block = blocks.get(f"provider:{pid}")
        dep_rows: list[DeploymentStatus] = []
        upstreams: list[str] = []
        for deployment in deployments:
            dep_id = str(deployment["id"])
            info = render_config._model_info(provider, {"mode": "chat", **deployment}, dep_id)
            upstream = str(info["muse_upstream"])
            upstreams.append(upstream)
            if not configured[pid]:
                state, block = "no_key", None
            elif not is_on:
                state, block = "disabled", None
            else:
                block = block_for(pid, upstream)
                state = state_of(block)
            last = db.last_call(upstream=upstream)
            rate = db.success_rate(since=ts - 86400, upstream=upstream)
            dep_rows.append(
                DeploymentStatus(
                    id=dep_id,
                    model=str(deployment["model"]),
                    mode=str(deployment.get("mode", "chat")),
                    aliases=sorted(joined.get(dep_id, [])) if is_on else [],
                    state=state,  # type: ignore[typeddict-item]
                    blocked_until=quota.iso(block["until"]) if block else None,
                    last_status=last["status"] if last else None,
                    last_used_at=quota.iso(last["ts"]) if last else None,
                    success_rate_24h=rate,
                )
            )

        if not configured[pid]:
            state, block = "no_key", None
        elif not is_on:
            state, block = "disabled", None
        elif provider_block:
            state, block = state_of(provider_block), provider_block
        else:
            dep_blocks = [block_for(pid, u) for u in upstreams]
            if dep_blocks and all(dep_blocks):
                block = min(dep_blocks, key=lambda b: b["until"])  # back as soon as any one returns
                state = "exhausted" if all(b["kind"] == "exhausted" for b in dep_blocks) else "cooling"
            else:
                state, block = "ok", None

        usage = db.usage(provider=pid, now=ts) or {w: {"requests": 0, "tokens": 0} for w in ("minute", "day", "month")}
        providers.append(
            ProviderStatus(
                id=pid,
                kind="local" if local else "cloud",
                tier=str(provider.get("tier", "unknown")),
                key_env=None if local else str(provider["env"]),
                configured=configured[pid],
                enabled=is_on,
                policy=_policy(provider),
                state=state,  # type: ignore[typeddict-item]
                blocked_until=quota.iso(block["until"]) if block else None,
                block_reason=str(block["reason"]) if block else None,
                usage=usage,
                limits=_limits(db, provider, upstreams, usage, ts),
                deployments=dep_rows,
            )
        )

    aliases: list[AliasStatus] = []
    for name, meta in catalog["aliases"].items():
        entries = alias_entries.get(name, [])
        try:
            from muse import policy

            usable = policy.filter_deployments(entries, default_privacy)
        except Exception:
            usable = entries
        open_entries = [
            e for e in usable
            if block_for(str(e["model_info"]["provider"]), str(e["model_info"].get("muse_upstream"))) is None
        ]
        best = sorted(
            open_entries,
            key=lambda e: (
                int(e["litellm_params"].get("order", 0)),
                -int(e["litellm_params"].get("rpm") or 0),
                str(e["model_info"]["id"]),
            ),
        )
        aliases.append(
            AliasStatus(
                name=name,
                mode=str(meta.get("mode", "")),
                available=len(open_entries),
                total=len(entries),
                next=str(best[0]["model_info"]["id"]).removesuffix(f"__{name}") if best else None,
            )
        )

    recent: list[RecentCall] = [
        RecentCall(
            ts=quota.iso(row["ts"]) or "",
            alias=row["alias"],
            deployment=row["deployment_id"],
            provider=row["provider"],
            status=row["status"],
            ok=bool(row["ok"]),
            latency_ms=row["latency_ms"],
            tokens_in=row["tokens_in"],
            tokens_out=row["tokens_out"],
            privacy=row["privacy"],
        )
        for row in (db.recent(50) or [])
    ]
    today = (db.usage(now=ts) or {}).get("day", {"requests": 0, "tokens": 0})
    url = _proxy_url(env)
    return Snapshot(
        generated_at=quota.iso(ts) or "",
        home=str(home),
        proxy={"url": url, "reachable": _reachable(url) if check_proxy else None},
        default_privacy=default_privacy,
        providers=providers,
        aliases=aliases,
        recent=recent,
        totals={"requests_today": int(today["requests"]), "tokens_today": int(today["tokens"])},
        apps=build_apps(env, ts, db),
        paid=build_paid(env, ts, db),
        queue=_queue_stats(env, ts),
        paused=queue_mod.is_paused(env),
    )


def _queue_stats(env: Mapping[str, str], ts: float) -> Any:
    try:
        return queue_mod.stats(env, ts)
    except Exception:  # an unreadable queue file must not take status down
        return {"counts": {s: 0 for s in queue_mod.STATUSES}, "waiting": 0, "next_due_at": None, "oldest_queued_at": None, "hold_until": None}


def build_apps(env: Mapping[str, str], ts: float, db: Any) -> list[AppStatus]:
    """Every app (built-ins first), with today's usage and what was turned away. Counts only."""
    from muse import apps as apps_mod
    from muse.ration import resolve_tz

    tz = resolve_tz(str(env.get("MUSE_TZ", "") or "").strip())
    usage = db.usage_by_app(now=ts, tz=tz) or {}
    counts = db.ration_counts(now=ts, tz=tz) or {}
    deferrals: dict[str | None, dict[str, int]] = {}
    for (app, kind, code), n in counts.items():
        deferrals.setdefault(app, {})[f"{kind}:{code}"] = n
    registered = apps_mod.load_apps(env)
    rows: list[AppStatus] = []
    seen: set[str] = set()
    for app in [apps_mod.OWNER, apps_mod.ANONYMOUS, *[registered[n] for n in sorted(registered)]]:
        seen.add(app.name)
        u = usage.get(app.name) or {}
        rows.append(
            AppStatus(
                name=app.name, builtin=app.builtin, priority=app.priority, daily_requests=app.daily_requests,
                daily_tokens=app.daily_tokens, allow_paid=bool(app.allow_paid), default_privacy=app.default_privacy,
                note=app.note, created_at=app.created_at or None, last_used_at=quota.iso(u["last_ts"]) if u.get("last_ts") else None,
                today={"requests": int(u.get("requests", 0)), "failures": int(u.get("failures", 0)),
                       "tokens": int(u.get("tokens", 0)), "cost_usd": float(u.get("cost_usd", 0.0))},
                deferrals=dict(sorted(deferrals.get(app.name, {}).items())),
            )
        )
    # Apps that made calls today but are no longer registered (removed, or calls from before apps existed).
    for name in sorted(n for n in set(usage) | set(deferrals) if n not in seen):
        u = usage.get(name) or {}
        label = name or "unattributed"
        rows.append(
            AppStatus(
                name=label, builtin=False, priority="normal", daily_requests=None, daily_tokens=None, allow_paid=False,
                default_privacy=None, note="not registered (removed, or called before apps existed)", created_at=None,
                last_used_at=quota.iso(u["last_ts"]) if u.get("last_ts") else None,
                today={"requests": int(u.get("requests", 0)), "failures": int(u.get("failures", 0)),
                       "tokens": int(u.get("tokens", 0)), "cost_usd": float(u.get("cost_usd", 0.0))},
                deferrals=dict(sorted(deferrals.get(name, {}).items())),
            )
        )
    return rows


def build_paid(env: Mapping[str, str], ts: float, db: Any) -> Paid:
    """The paid cap meter: cap, spent today, held by in-flight calls, calls with unknown cost, next reset."""
    from muse import ration

    settings = ration.Settings.from_env(env)
    spend = db.paid_spend(now=ts, tz=settings.tz) or {}
    day_end = ration.local_day_bounds(ts, settings.tz)[1]
    return Paid(
        cap_usd=float(settings.paid_cap_usd), spent_usd=float(spend.get("usd", 0.0)), held_usd=float(spend.get("held_usd", 0.0)),
        calls=int(spend.get("calls", 0)), unknown_cost_calls=int(spend.get("unknown_cost_calls", 0)),
        resets_at=quota.iso(day_end) or "",
    )


def _limits(
    db: _SafeLedger,
    provider: Mapping[str, Any],
    upstreams: list[str],
    usage: Mapping[str, Mapping[str, int]],
    now: float,
) -> list[Limit]:
    """Fresh header-observed limits; else catalogue quota minus ledger usage; else []."""
    pid = str(provider["id"])
    observed: dict[tuple[str, str], dict[str, Any]] = {}
    for scope in [*upstreams, f"provider:{pid}"]:
        for row in db.limits(scope) or []:
            if not _fresh(row, now):
                continue
            key = (row["window"], row["metric"])
            current = observed.get(key)
            # Several upstreams report the same window: show the tightest one.
            if current is None or (row["remaining"] is not None and (current["remaining"] is None or row["remaining"] < current["remaining"])):
                observed[key] = row
    if observed:
        return [
            Limit(
                window=window,  # type: ignore[typeddict-item]
                metric=metric,  # type: ignore[typeddict-item]
                limit=row["limit"],
                remaining=row["remaining"],
                reset_at=quota.iso(row["reset_at"]),
                source="header",
            )
            for (window, metric), row in sorted(observed.items())
        ]
    out: list[Limit] = []
    for window, metric, limit in _declared_quota(provider):
        used = int(usage.get(window, {}).get(metric, 0))
        out.append(
            Limit(
                window=window,  # type: ignore[typeddict-item]
                metric=metric,  # type: ignore[typeddict-item]
                limit=limit,
                remaining=max(0, limit - used),
                reset_at=quota.iso(_window_reset(window, pid, now)),
                source="estimate",
            )
        )
    return out
