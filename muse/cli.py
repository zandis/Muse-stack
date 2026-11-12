"""`muse` command line. Standard library only (PyYAML is used by the modules it imports).

Why a CLI first: the dashboard is optional, but one screen in the terminal
(`muse status`) has to answer "what can I call right now, and what is it
costing me in quota". Exit codes: 0 ok, 1 a problem was found, 2 usage.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

from muse import __version__, keys, local, paths

DEFAULT_PROXY = "http://127.0.0.1:4000"
WIDTH = 80
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_ENV_CACHE: dict[str, str] | None = None


# ------------------------------------------------------------------ plumbing


def _env() -> dict[str, str]:
    """Merged environment (process + keychain + dotenv), cached per command. Holds secrets: never print."""
    global _ENV_CACHE
    if _ENV_CACHE is None:
        _ENV_CACHE = keys.export_env()
    return _ENV_CACHE


def _proxy_url() -> str:
    return (_env().get("MUSE_PROXY_URL", "").strip() or DEFAULT_PROXY).rstrip("/")


def _color_enabled(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    return bool(getattr(stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")


class Paint:
    """ANSI styling that is a no-op when colour is off."""

    CODES = {"red": "31", "green": "32", "yellow": "33", "cyan": "36", "dim": "2", "bold": "1"}

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, style: str | None) -> str:
        if not self.enabled or not style or not text:
            return text
        return "".join(f"\x1b[{self.CODES[s]}m" for s in style.split("+")) + text + "\x1b[0m"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def countdown(value: str | None, now: datetime) -> str | None:
    """'3h 12m', '12m 05s', '45s', 'now'. None when there is no time."""
    when = _parse_iso(value)
    if when is None:
        return None
    secs = int((when - now).total_seconds())
    if secs <= 0:
        return "now"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, sec = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins:02d}m"
    if mins:
        return f"{mins}m {sec:02d}s" if mins < 10 else f"{mins}m"
    return f"{sec}s"


def short_num(n: int | float | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= size:
            val = n / size
            return f"{val:.1f}".rstrip("0").rstrip(".") + unit if val < 100 else f"{val:.0f}{unit}"
    return str(int(n))


def bar(fraction: float | None, width: int = 8, unicode_ok: bool = True) -> str:
    full, empty = ("█", "░") if unicode_ok else ("#", "-")
    if fraction is None:
        return "?" * width
    filled = max(0, min(width, round(fraction * width)))
    return full * filled + empty * (width - filled)


def privacy_badge(provider: Mapping[str, Any]) -> str:
    """local / no-train / trains / unknown, derived from the catalogue policy."""
    if provider.get("kind") == "local":
        return "local"
    trains = (provider.get("policy") or {}).get("trains_on_inputs", "unknown")
    return {"yes": "trains", "no": "no-train"}.get(trains, "unknown")


# ------------------------------------------------------------- status screen

_STATE_LABEL = {"ok": "ok", "cooling": "cooling", "exhausted": "exhausted", "no_key": "no key", "disabled": "disabled", "unknown": "unknown"}
_STATE_STYLE = {"ok": "green", "cooling": "yellow", "exhausted": "red", "no_key": "dim", "disabled": "dim", "unknown": "yellow"}
_BADGE_STYLE = {"local": "cyan", "no-train": "green", "trains": "yellow", "unknown": "dim"}


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _cell(paint: Paint, text: str, width: int, style: str | None = None) -> str:
    return paint(_fit(text, width).ljust(width), style)


def _is_active(p: Mapping[str, Any]) -> bool:
    return bool(p.get("configured")) and p.get("enabled", True)


def _soonest(snap: Mapping[str, Any], now: datetime, alias: str | None = None) -> tuple[str, str] | None:
    """(deployment-or-provider id, ISO time) of the nearest unblock, optionally for one alias."""
    best: tuple[datetime, str, str] | None = None
    for p in snap.get("providers", []):
        if not _is_active(p):
            continue
        for d in p.get("deployments", []):
            if alias and alias not in d.get("aliases", []):
                continue
            when = _parse_iso(d.get("blocked_until") or (p.get("blocked_until") if not alias else None))
            if when and when > now and (best is None or when < best[0]):
                best = (when, d["id"], d.get("blocked_until") or p["blocked_until"])
        if not alias:
            when = _parse_iso(p.get("blocked_until"))
            if when and when > now and (best is None or when < best[0]):
                best = (when, p["id"], p["blocked_until"])
    return (best[1], best[2]) if best else None


def render_status(snap: Mapping[str, Any], *, color: bool = False, now: datetime | None = None, unicode_ok: bool = True) -> str:
    """Pure function: snapshot in, screen out. Every line is at most 80 columns of plain text."""
    now = now or datetime.now(timezone.utc)
    paint = Paint(color)
    out: list[str] = []
    providers = list(snap.get("providers", []))
    active = [p for p in providers if _is_active(p)]
    proxy = snap.get("proxy", {})
    reach = proxy.get("reachable")
    proxy_txt = "not checked" if reach is None else ("up" if reach else "DOWN")
    proxy_style = None if reach is None else ("green" if reach else "red")

    out.append(f"{paint('muse', 'bold')}  proxy {_fit(str(proxy.get('url', '')), 28)} {paint(proxy_txt, proxy_style)}  default privacy: {snap.get('default_privacy', '?')}")
    totals = snap.get("totals", {})
    out.append(paint(f"today: {totals.get('requests_today', 0)} requests, {short_num(totals.get('tokens_today', 0))} tokens", "dim"))
    out.append("")

    if not active:
        out.append(paint("Nothing is configured yet. Two commands to get started:", "yellow"))
        out.append("  muse keys set GEMINI_API_KEY     # free key: muse keys open GEMINI_API_KEY")
        out.append("  muse up                          # start the proxy on 127.0.0.1:4000")
        out.append("  (local models instead? `muse local discover --write`)")
        out.append("")
    else:
        usable = [p for p in active if p.get("state") == "ok"]
        if not usable:
            soon = _soonest(snap, now)
            tail = ""
            if soon:
                tail = f" Soonest: {soon[0]} in {countdown(soon[1], now)}."
            out.append(paint("Everything is blocked right now." + tail, "red+bold"))
            out.append("")

    out.append(paint(f"{'PROVIDER':<13} {'STATE':<9} {'KEY':<3} {'TIER':<7} {'PRIVACY':<8} TODAY", "bold"))
    for p in providers:
        state = p.get("state", "unknown")
        if p.get("kind") == "local":
            key = "-"
            key_style = "dim"
        else:
            key = "✓" if p.get("configured") else "✗"
            key_style = "green" if p.get("configured") else "dim"
        day = (p.get("usage") or {}).get("day", {})
        today = f"{day.get('requests', 0)} req {short_num(day.get('tokens', 0))} tok" if _is_active(p) else ""
        badge = privacy_badge(p)
        out.append(
            " ".join(
                [
                    _cell(paint, p["id"], 13, None if _is_active(p) else "dim"),
                    _cell(paint, "off" if p.get("kind") == "local" and state == "no_key" else _STATE_LABEL.get(state, state), 9, _STATE_STYLE.get(state)),
                    _cell(paint, key, 3, key_style),
                    _cell(paint, str(p.get("tier", "")), 7),
                    _cell(paint, badge, 8, _BADGE_STYLE.get(badge)),
                    _fit(today, WIDTH - 13 - 9 - 3 - 7 - 8 - 5),
                ]
            ).rstrip()
        )
        if not _is_active(p):
            continue
        if p.get("blocked_until") or state in {"cooling", "exhausted"}:
            cd = countdown(p.get("blocked_until"), now)
            reason = p.get("block_reason") or state
            text = f"  blocked: {reason}" + (f", unblocks in {cd}" if cd else "")
            out.append(paint(_fit(text, WIDTH), "yellow" if state == "cooling" else "red"))
        limits = p.get("limits") or []
        if not limits:
            out.append(paint("  limits: — (not reported by the provider or the catalogue)", "dim"))
        for lim in limits:
            out.append(_limit_line(lim, now, paint, unicode_ok))
    if any(lim.get("source") != "header" for p in active for lim in (p.get("limits") or []) if lim.get("remaining") is not None):
        out.append(paint("  ~ = estimate (catalogue limit minus what the ledger counted)", "dim"))
    out.append("")

    aliases = [a for a in snap.get("aliases", []) if a.get("total", 0) > 0]
    out.append(paint("ALIASES", "bold"))
    if not aliases:
        out.append(paint("  (none: no deployments are enabled yet)", "dim"))
    for a in aliases:
        avail, total = a.get("available", 0), a.get("total", 0)
        line = f"  {a['name']:<13} {avail}/{total} available"
        if avail > 0 and a.get("next"):
            line += f"  next: {a['next']}"
            out.append(_fit(line, WIDTH))
        elif avail == 0:
            soon = _soonest(snap, now, a["name"])
            extra = f"  soonest: {soon[0]} in {countdown(soon[1], now)}" if soon else "  none available"
            out.append(paint(_fit(line + extra, WIDTH), "red"))
        else:
            out.append(_fit(line, WIDTH))
    out.append("")

    out.append(paint("RECENT CALLS", "bold"))
    recent = list(snap.get("recent", []))[:5]
    if not recent:
        out.append(paint("  (no calls recorded yet)", "dim"))
    for c in recent:
        when = _parse_iso(c.get("ts"))
        stamp = when.astimezone().strftime("%H:%M:%S") if when else "--:--:--"
        ok = c.get("ok")
        status = str(c.get("status") if c.get("status") is not None else "-")
        lat = f"{c['latency_ms']}ms" if c.get("latency_ms") is not None else "-"
        toks = f"{short_num(c.get('tokens_in'))}>{short_num(c.get('tokens_out'))}"
        line = "  " + " ".join(
            [
                stamp,
                _cell(paint, str(c.get("alias") or "-"), 12),
                _cell(paint, str(c.get("deployment", "")), 24),
                _cell(paint, status, 3, "green" if ok else "red"),
                f"{lat:>7}",
                f"{toks:>9}",
                _fit(str(c.get("privacy", "")), 8),
            ]
        )
        out.append(line.rstrip())
    return "\n".join(out) + "\n"


def _limit_line(lim: Mapping[str, Any], now: datetime, paint: Paint, unicode_ok: bool) -> str:
    metric = "req" if lim.get("metric") == "requests" else "tok"
    head = f"  {str(lim.get('window', '')):<7}{metric} "
    limit, remaining = lim.get("limit"), lim.get("remaining")
    if limit is None and remaining is None:
        return paint(_fit(head + "— limit unknown", WIDTH), "dim")
    approx = "" if lim.get("source") == "header" else "~"
    if limit and remaining is not None:
        frac = max(0.0, min(1.0, remaining / limit))
        style = "red" if frac < 0.1 else ("yellow" if frac < 0.3 else "green")
        body = f"{bar(frac, 8, unicode_ok)} {approx}{short_num(remaining)}/{short_num(limit)} left"
        body = body.replace(bar(frac, 8, unicode_ok), paint(bar(frac, 8, unicode_ok), style), 1)
        plain_len = len(head) + len(strip_ansi(body))
    else:
        body = f"{'?' * 8} {short_num(remaining)} left" if limit is None else f"{'?' * 8} ?/{short_num(limit)}"
        plain_len = len(head) + len(body)
    reset = countdown(lim.get("reset_at"), now)
    tail = f"  resets in {reset}" if reset and reset != "now" else ("  resets now" if reset else "")
    if plain_len + len(tail) > WIDTH:
        tail = tail[: max(0, WIDTH - plain_len)]
    return head + body + tail


def _build_snapshot(check_proxy: bool = False) -> dict[str, Any]:
    from muse.status import build_snapshot  # lazy: owned by another module

    return build_snapshot(environ=_env(), check_proxy=check_proxy)  # type: ignore[no-any-return]


def _run_probes() -> None:
    """Ask providers that have a quota endpoint (OpenRouter) what is left. Best effort."""
    try:
        from muse import probes
        from muse.ledger import Ledger

        probes.run_probes(keys.export_env(_env()), Ledger(environ=_env()))
    except Exception as exc:  # a probe must never break the status screen
        _err(f"Quota probe skipped: {type(exc).__name__}")


def cmd_status(args: argparse.Namespace) -> int:
    def once() -> int:
        if getattr(args, "probe", False):
            _run_probes()
        try:
            snap = _build_snapshot(check_proxy=True)
        except ImportError:
            _err("muse status needs muse/status.py, which is not installed yet.")
            return 1
        except Exception as exc:
            _err(f"Could not build the status snapshot: {type(exc).__name__}: {exc}")
            return 1
        if args.json:
            print(json.dumps(snap, indent=2, sort_keys=False))
        else:
            sys.stdout.write(render_status(snap, color=_color_enabled(), unicode_ok=_unicode_ok()))
        return 0

    if not args.watch:
        return once()
    tty = sys.stdout.isatty()
    try:
        while True:
            global _ENV_CACHE
            _ENV_CACHE = None
            if tty and not args.json:
                sys.stdout.write("\x1b[2J\x1b[H")
            code = once()
            sys.stdout.flush()
            if code:
                return code
            time.sleep(max(1, args.watch))
    except KeyboardInterrupt:
        return 0


def _unicode_ok() -> bool:
    return (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "").startswith("utf")


# --------------------------------------------------------------------- keys


def cmd_keys(args: argparse.Namespace) -> int:
    action = args.keys_cmd
    if action == "list":
        rows = keys.list_keys()
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        paint = Paint(_color_enabled())
        print(paint(f"{'KEY':<24} {'PROVIDER':<12} {'SET':<4} SOURCE", "bold"))
        for r in rows:
            mark = paint("✓", "green") if r["configured"] else paint("✗", "dim")
            print(f"{r['name']:<24} {str(r['provider'] or '-'):<12} {mark}    {r['source'] or '-'}")
        if not keys.keychain_available():
            print(paint("\nOS keychain: unavailable (pip install 'muse-stack[keyring]'); keys are stored in the dotenv file.", "dim"))
        return 0
    name = args.name
    if "=" in name:
        _err("Pass only the key NAME. A KEY=value argument would leak the value into shell history and `ps`.")
        return 2
    try:
        if action == "open":
            keys.validate_name(name, force=args.force)
            url = (keys.KNOWN_KEYS.get(name) or {}).get("key_url")
            if not url:
                _err(f"No key page is known for {name}.")
                return 1
            print(url)
            return 0
        if action == "rm":
            removed = keys.delete_key(name, force=args.force)
            if removed:
                print(f"Removed {name} from: {', '.join(removed)}. (A value exported in your shell is not touched.)")
            else:
                print(f"{name} was not stored in the keychain or the dotenv file.")
            return 0
        # set
        keys.validate_name(name, force=args.force)
        if args.stdin:
            raw = sys.stdin.read()
        else:
            if not sys.stdin.isatty():
                _err("No terminal to prompt on. Pipe the value in with --stdin instead.")
                return 2
            raw = getpass.getpass(f"Paste {name} (input hidden): ")
        result = keys.set_key(name, raw, backend=args.backend, force=args.force)
    except keys.KeysError as exc:
        _err(f"Error: {exc}")
        return 1
    for w in result.warnings:
        _err(f"warning: {w}")
    print(f"Saved {name} to {result.location}.")
    if name == keys.MASTER_KEY:
        print("Restart the proxy for the new master key to take effect: muse restart")
    return 0


# -------------------------------------------------------------------- local


def cmd_local(args: argparse.Namespace) -> int:
    env = _env()
    if args.local_cmd == "list":
        cat = local.read_catalog(paths.local_catalog_path(env))
        if args.json:
            print(json.dumps(cat, indent=2))
            return 0
        if not any(cat.values()):
            print("No local models catalogued. Run: muse local discover --write")
            return 0
        for provider, deps in cat.items():
            print(f"{provider}")
            for d in deps:
                print(f"  {d.get('id', '?'):<34} {d.get('mode', '?'):<9} {','.join(d.get('aliases') or []) or '-'}")
        return 0
    probes = local.probe_runtimes(env)
    found = local.discover(env)
    if args.json:
        print(json.dumps({"runtimes": [{"provider": p.provider, "base": p.base, "reachable": p.reachable, "error": p.error, "models": len(p.models)} for p in probes], "found": found}, indent=2))
    else:
        for p in probes:
            if p.reachable:
                print(f"✓ {p.provider:<13} {p.base}  ({len(p.models)} models)")
            else:
                print(f"✗ {p.provider:<13} {p.base}  not reachable ({p.error})")
        for provider, deps in found.items():
            for d in deps:
                print(f"    {d['id']:<34} {d['mode']:<9} {','.join(d['aliases']) or '-'}")
        if not found:
            print("No local runtime answered. Start Ollama or LM Studio, or set MUSE_LOCAL_OPENAI_BASE.")
    if args.write and found:
        target = local.write_catalog(found, paths.local_catalog_path(env))
        if not args.json:
            print(f"\nWrote {target}. Aliases you edited there are kept on the next discover.")
            for var, entry in local.suggest_env(found, env).items():
                print(f"\n  {var}   host: {entry['host']}   docker: {entry['docker']}")
                print(f"    {entry['why']}")
                if entry.get("note"):
                    print(f"    {entry['note']}")
            print("\nSet those in .env (docker variant for `muse up`), then: muse restart")
    elif args.write and not args.json:
        print("Nothing to write.")
    return 0


# ------------------------------------------------------------------- doctor

OK, FAIL, WARN = "ok", "fail", "warn"
_MARK = {OK: "✓", FAIL: "✗", WARN: "!"}


def _check(cid: str, status: str, title: str, detail: str = "", fix: str = "") -> dict[str, str]:
    return {"id": cid, "status": status, "title": title, "detail": detail, "fix": fix}


def _http(method: str, url: str, headers: Mapping[str, str] | None = None, body: bytes | None = None, timeout: float = 3.0) -> tuple[int, Mapping[str, str], bytes]:
    """Return (status, headers, body); HTTP error statuses are returned, network errors raise OSError."""
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-configured URL
            return resp.status, dict(resp.headers.items()), resp.read(4_000_000)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read(200_000)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise OSError(str(getattr(exc, "reason", exc))) from None


def _load_renderer() -> Any:
    path = paths.REPO_ROOT / "scripts" / "render_config.py"
    spec = importlib.util.spec_from_file_location("muse_render_config", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("muse_render_config", mod)
    spec.loader.exec_module(mod)
    return mod


def _catalog() -> dict[str, Any]:
    data = yaml.safe_load(paths.CATALOG_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def compose_gaps(env: Mapping[str, str], compose_path: Path | None = None) -> list[str]:
    """Variable names the container will NOT receive from `muse up`, though they are configured.

    The container only gets names listed under `environment:` (or present in
    its env_file). Keychain and process-env values are only reachable through
    `environment:`, so each such name needs a bare `NAME:` line there.
    """
    path = compose_path or paths.REPO_ROOT / "docker-compose.yml"
    try:
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        svc = next(iter(compose["services"].values()))
        listed = svc.get("environment") or {}
        names = set(listed.keys() if isinstance(listed, dict) else (str(i).split("=")[0] for i in listed))
    except (OSError, yaml.YAMLError, KeyError, StopIteration, AttributeError, TypeError):
        return []
    wanted: list[str] = []
    try:
        for prov in _catalog().get("providers", []):
            for var in (prov.get("env"), prov.get("base_env")):
                if var and var not in wanted:
                    wanted.append(str(var))
    except (OSError, yaml.YAMLError):
        pass
    default_env_file = keys.env_file_path(env) == paths.REPO_ROOT / ".env"
    gaps = []
    for var in wanted:
        if var in names:
            continue
        src = keys.source_of(var, env) if var in keys.KNOWN_KEYS else ("env" if env.get(var, "").strip() else None)
        if src is None and var not in keys.KNOWN_KEYS:
            src = "dotenv" if keys._dotenv_items(env).get(var) else None  # noqa: SLF001
        if src in ("env", "keychain") or (src == "dotenv" and not default_env_file):
            gaps.append(var)
    return gaps


def run_checks() -> list[dict[str, str]]:
    env = _env()
    checks: list[dict[str, str]] = []

    def guarded(fn: Callable[[], None], cid: str) -> None:
        try:
            fn()
        except Exception as exc:  # a broken check must not hide the others
            checks.append(_check(cid, WARN, f"{cid} check crashed", f"{type(exc).__name__}", "Report this as a bug."))

    def python() -> None:
        ok = sys.version_info >= (3, 11)
        v = ".".join(map(str, sys.version_info[:3]))
        checks.append(_check("python", OK if ok else FAIL, f"Python {v}", "" if ok else "Muse needs 3.11 or newer.", "" if ok else "Install Python 3.11+ and reinstall: pip install -e ."))

    def catalogue() -> None:
        try:
            rc = _load_renderer()
            cat = rc.load_catalog(paths.CATALOG_PATH)
            config, enabled = rc.render(cat, env)
            n = len(config.get("model_list", []))
            checks.append(_check("catalogue", OK, "Catalogue loads and renders", f"{len(cat['providers'])} providers, {len(enabled)} enabled, {n} model entries"))
        except (SystemExit, Exception) as exc:
            checks.append(_check("catalogue", FAIL, "Catalogue does not render", str(exc)[:200], "Run python3 scripts/validate_config.py for details."))

    def master() -> None:
        value = env.get(keys.MASTER_KEY, "").strip()
        if not value:
            checks.append(_check("master_key", FAIL, "No master key set", "The proxy would accept unauthenticated calls.", "Run `muse up` (generates one) or `muse keys set LITELLM_MASTER_KEY`."))
        elif keys.is_weak_master_key(value):
            checks.append(_check("master_key", FAIL, "Master key is weak or a published example", "", "Replace it: delete it with `muse keys rm LITELLM_MASTER_KEY`, then run `muse up`."))
        else:
            checks.append(_check("master_key", OK, f"Master key set ({keys.source_of(keys.MASTER_KEY, env)})"))

    def dotenv_perms() -> None:
        path = keys.env_file_path(env)
        if path.is_symlink():
            checks.append(_check("dotenv", WARN, f"{path} is a symlink", "Muse will not write keys through a symlink.", "Replace it with a regular file."))
        elif not path.exists():
            checks.append(_check("dotenv", OK, "No dotenv file", "Keys come from the keychain or environment."))
        else:
            warning = keys.file_mode_warning(path)
            if warning:
                checks.append(_check("dotenv", WARN, f"{path.name} is readable by other users", "", f"chmod 600 {path}"))
            else:
                checks.append(_check("dotenv", OK, f"{path.name} permissions are private"))

    def providers() -> None:
        rows = [r for r in keys.list_keys(env) if r["provider"] and r["name"] != keys.MASTER_KEY and r["name"] in _routed_names()]
        have = [r["provider"] for r in rows if r["configured"]]
        local_models = any(local.read_catalog(paths.local_catalog_path(env)).values())
        if have:
            checks.append(_check("providers", OK, f"{len(have)} provider(s) configured", ", ".join(have)))
        elif local_models:
            checks.append(_check("providers", OK, "No cloud keys, but local models are catalogued"))
        else:
            checks.append(_check("providers", FAIL, "No provider keys and no local models", "There is nothing to route to.", "muse keys set GEMINI_API_KEY   (free: muse keys open GEMINI_API_KEY)"))

    def accounts() -> None:
        dupes = keys.find_duplicate_values(env)
        extra = keys.extra_key_variants(env)
        msg = "Muse is one person with one account per provider. Several keys for one provider, or one key shared with others, is pooling that providers' terms usually forbid."
        if dupes or extra:
            detail = []
            if dupes:
                detail.append("same value stored under: " + "; ".join(" = ".join(g) for g in dupes))
            if extra:
                detail.append("extra key variables: " + ", ".join(extra))
            checks.append(_check("accounts", WARN, "Possible duplicate or extra accounts", " | ".join(detail) + ". " + msg, "Keep one key per provider; delete the others."))
        else:
            checks.append(_check("accounts", OK, "One account per provider", msg))

    def local_rt() -> None:
        probes = local.probe_runtimes(env)
        up = [p.provider for p in probes if p.reachable]
        configured_down = [p.provider for p in probes if not p.reachable and env.get(local.BASE_ENV[p.provider], "").strip()]
        if configured_down:
            checks.append(_check("local", WARN, "Configured local runtime not reachable", ", ".join(configured_down), "Start it, or unset its *_API_BASE variable."))
        elif up:
            checks.append(_check("local", OK, "Local runtimes reachable", ", ".join(up)))
        else:
            checks.append(_check("local", OK, "No local runtime running", "Optional. Ollama or LM Studio would add private models."))

    def proxy() -> None:
        url = _proxy_url()
        try:
            status, _, _ = _http("GET", f"{url}/health/liveliness", timeout=2.0)
        except OSError:
            checks.append(_check("proxy", WARN, f"Proxy not reachable at {url}", "", "Start it with `muse up`."))
            return
        if status != 200:
            checks.append(_check("proxy", WARN, f"Proxy answered HTTP {status} at {url}", "", "Check `docker compose logs litellm`."))
            return
        try:
            auth_status, _, _ = _http("GET", f"{url}/v1/models", timeout=3.0)
        except OSError:
            auth_status = 0
        if auth_status in (401, 403):
            checks.append(_check("proxy", OK, f"Proxy up at {url}, requires auth"))
        elif auth_status == 200:
            checks.append(_check("proxy", WARN, f"Proxy up at {url} and accepts unauthenticated calls", "No master key is enforced.", "Set a master key and `muse restart`."))
        else:
            checks.append(_check("proxy", OK, f"Proxy up at {url}", f"/v1/models returned HTTP {auth_status}"))

    def ledger() -> None:
        try:
            home = paths.home(env)
            led = paths.ledger_path(env)
            if led.exists():
                ok = os.access(led, os.W_OK)
            else:
                probe = home / f".write-test-{os.getpid()}"
                probe.write_text("x")
                probe.unlink()
                ok = True
        except OSError as exc:
            checks.append(_check("ledger", FAIL, "Cannot write to the state directory", str(exc)[:120], "Check MUSE_HOME permissions."))
            return
        checks.append(_check("ledger", OK if ok else FAIL, "Ledger is writable" if ok else "Ledger is read-only", "" if ok else str(paths.ledger_path(env)), "" if ok else "chmod u+w on it."))

    def docker() -> None:
        exe = shutil.which("docker")
        if not exe:
            native = shutil.which("litellm")
            checks.append(_check("docker", WARN, "Docker not found", "litellm is on PATH: `muse up --native` works." if native else "", "" if native else "Install Docker, or `pip install litellm[proxy]` for --native."))
            return
        try:
            proc = subprocess.run([exe, "compose", "version"], capture_output=True, timeout=8)
            ok = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        checks.append(_check("docker", OK if ok else WARN, "Docker with Compose v2" if ok else "Docker is installed but `docker compose` failed", "", "" if ok else "Install the Compose v2 plugin."))
        gaps = compose_gaps(env)
        if gaps:
            checks.append(_check("compose", WARN, "docker-compose.yml does not forward some configured variables", ", ".join(gaps), "Add each as a bare line under services.litellm.environment (e.g. `GROQ_API_KEY:`)."))

    def bind() -> None:
        bind_addr = env.get("MUSE_BIND", "127.0.0.1").strip() or "127.0.0.1"
        loop = bind_addr in {"127.0.0.1", "localhost", "::1"}
        if loop:
            checks.append(_check("bind", OK, f"Proxy port bound to loopback ({bind_addr})"))
        else:
            checks.append(_check("bind", WARN, f"MUSE_BIND={bind_addr} exposes the proxy beyond this machine", "Free-tier keys and the master key are all that stands between the network and your quota.", "Set MUSE_BIND=127.0.0.1, or put a TLS proxy in front."))

    def unverified() -> None:
        flagged = []
        for prov in _catalog().get("providers", []):
            for dep in prov.get("deployments") or []:
                if str(dep.get("verified", "")).strip().lower() == "partial" or dep.get("deprecated"):
                    flagged.append(str(dep["id"]))
        if flagged:
            checks.append(_check("unverified", WARN, f"{len(flagged)} deployment(s) unverified or deprecated", ", ".join(flagged), "Treat their model ids as unconfirmed; see README 'Not verified'."))
        else:
            checks.append(_check("unverified", OK, "All catalogued deployments are verified"))

    for cid, fn in [("python", python), ("catalogue", catalogue), ("master_key", master), ("dotenv", dotenv_perms), ("providers", providers), ("accounts", accounts), ("local", local_rt), ("proxy", proxy), ("ledger", ledger), ("docker", docker), ("bind", bind), ("unverified", unverified)]:
        guarded(fn, cid)
    return checks


def _routed_names() -> set[str]:
    try:
        return {str(p["env"]) for p in _catalog().get("providers", []) if p.get("env")}
    except (OSError, yaml.YAMLError):
        return set()


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = run_checks()
    failed = any(c["status"] == FAIL for c in checks)
    if args.json:
        print(json.dumps({"ok": not failed, "checks": checks}, indent=2))
        return 1 if failed else 0
    paint = Paint(_color_enabled())
    style = {OK: "green", FAIL: "red", WARN: "yellow"}
    for c in checks:
        print(f"{paint(_MARK[c['status']], style[c['status']])} {c['title']}")
        if c["detail"] and c["status"] != OK:
            print(f"    {c['detail']}")
        elif c["detail"] and c["id"] in {"accounts", "providers"}:
            print(f"    {c['detail']}")
        if c["fix"] and c["status"] != OK:
            print(f"    fix: {c['fix']}")
    n_warn = sum(c["status"] == WARN for c in checks)
    n_fail = sum(c["status"] == FAIL for c in checks)
    print(f"\n{len(checks) - n_warn - n_fail} ok, {n_warn} warning(s), {n_fail} problem(s)")
    return 1 if failed else 0


# --------------------------------------------------------------- up / down


def _pidfile(env: Mapping[str, str]) -> Path:
    return paths.home(env) / "proxy.pid"


def _docker_child_env(env: Mapping[str, str]) -> dict[str, str]:
    """Child env for compose: keys merged in; loopback local bases rewritten for the container."""
    child = dict(env)
    for var in local.BASE_ENV.values():
        val = child.get(var, "").strip()
        if val:
            child[var] = re.sub(r"//(127\.0\.0\.1|localhost|\[::1\])", "//host.docker.internal", val)
    return child


def ensure_master_key(show: bool) -> None:
    global _ENV_CACHE
    if not keys.is_configured(keys.MASTER_KEY):
        key = keys.generate_master_key()
        result = keys.set_key(keys.MASTER_KEY, key)
        _ENV_CACHE = None
        print(f"Generated a master key and stored it in {result.location}.")
        if not show:
            print("It is not printed. Clients read it with `muse env`; reveal it once with `muse up --show-master-key`.")
        for w in result.warnings:
            _err(f"warning: {w}")
    if show:
        print(f"LITELLM_MASTER_KEY={_env().get(keys.MASTER_KEY, '')}")


def _compose_cmd(*extra: str) -> list[str]:
    return ["docker", "compose", *extra]


def _run(cmd: list[str], env: Mapping[str, str]) -> int:
    try:
        return subprocess.run(cmd, cwd=paths.REPO_ROOT, env=dict(env), check=False).returncode
    except FileNotFoundError:
        _err(f"`{cmd[0]}` not found. Install Docker, or use `muse up --native` with litellm on PATH.")
        return 1


def _native_stop(env: Mapping[str, str]) -> bool:
    pf = _pidfile(env)
    try:
        pid = int(pf.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped native proxy (pid {pid}).")
    except ProcessLookupError:
        print("Native proxy was not running.")
    except PermissionError:
        _err(f"Not allowed to stop pid {pid}.")
        return False
    try:
        pf.unlink()
    except OSError:
        pass
    return True


def _up_native(env: dict[str, str], detach: bool) -> int:
    exe = shutil.which("litellm")
    if not exe:
        _err("`litellm` is not on PATH. Install it (pip install 'litellm[proxy]') or use Docker: muse up")
        return 1
    rc = _load_renderer()
    config, enabled = rc.render(rc.load_catalog(paths.CATALOG_PATH), env)
    cfg_path = paths.home(env) / "litellm-config.yaml"
    rc.write_config(rc.dumps(config, enabled), cfg_path)
    # render_and_start.py hardcodes --host 0.0.0.0; natively we honour MUSE_BIND (default loopback).
    host = env.get("MUSE_BIND", "").strip() or "127.0.0.1"
    port = env.get("MUSE_PORT", "").strip() or "4000"
    child = dict(env)
    child["PYTHONPATH"] = os.pathsep.join(filter(None, [str(paths.REPO_ROOT), child.get("PYTHONPATH", "")]))
    child["MUSE_HOME"] = str(paths.home(env))
    cmd = [exe, "--config", str(cfg_path), "--host", host, "--port", port]
    print(f"Starting litellm natively on {host}:{port} with providers: {', '.join(enabled) or '(none)'}")
    if detach:
        log = open(paths.home(env) / "proxy.log", "ab")  # noqa: SIM115 - handed to the child
        proc = subprocess.Popen(cmd, env=child, cwd=paths.REPO_ROOT, stdout=log, stderr=log, start_new_session=True)
        _pidfile(env).write_text(str(proc.pid))
        print(f"Running in the background (pid {proc.pid}); log: {paths.home(env) / 'proxy.log'}")
        return 0
    return subprocess.run(cmd, env=child, cwd=paths.REPO_ROOT, check=False).returncode


def cmd_up(args: argparse.Namespace) -> int:
    ensure_master_key(getattr(args, "show_master_key", False))
    env = _env()
    paths.home(env)  # create it ourselves, 0700, so docker does not create it as root
    if args.native:
        return _up_native(dict(env), args.detach)
    gaps = compose_gaps(env)
    if gaps:
        _err("warning: docker-compose.yml does not forward these configured variables to the container, so they will NOT reach it:")
        _err("    " + ", ".join(gaps))
        _err("  Add under services.litellm.environment, one bare line each (value comes from the host environment):")
        for g in gaps:
            _err(f"      {g}:")
    return _run(_compose_cmd("up", "-d"), _docker_child_env(env))


def cmd_down(args: argparse.Namespace) -> int:
    env = _env()
    stopped = _native_stop(env)
    if stopped:
        # A native proxy was running, so there is no container to stop. Do not
        # make a machine without Docker print a daemon error.
        return 0
    return _run(_compose_cmd("down"), _docker_child_env(env))


def cmd_restart(args: argparse.Namespace) -> int:
    env = _env()
    _native_stop(env)
    if args.native:
        return cmd_up(argparse.Namespace(native=True, detach=args.detach, show_master_key=False))
    ensure_master_key(False)
    return _run(_compose_cmd("up", "-d", "--force-recreate"), _docker_child_env(_env()))


# ---------------------------------------------------------------------- ask

_PRIVACY_WORDS = ("public", "standard", "private", "local")


def cmd_ask(args: argparse.Namespace) -> int:
    env = _env()
    url = _proxy_url()
    master = env.get(keys.MASTER_KEY, "").strip()
    headers = {"Content-Type": "application/json"}
    if master:
        headers["Authorization"] = f"Bearer {master}"
    privacy = args.privacy or env.get("MUSE_DEFAULT_PRIVACY", "").strip()
    if privacy:
        headers["x-muse-privacy"] = privacy
    model = args.model or args.alias
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": args.prompt}]}).encode()
    try:
        status, resp_headers, data = _http("POST", f"{url}/v1/chat/completions", headers, body, timeout=120.0)
    except OSError as exc:
        _err(f"Cannot reach the proxy at {url} ({exc}). Start it with: muse up")
        return 1
    text = data.decode("utf-8", "replace")
    try:
        payload = json.loads(text)
    except ValueError:
        payload = {}
    # The hook stamps x-muse-privacy on every request that got past the proxy's
    # own auth. So a 401 carrying it came from a provider, not from the proxy.
    passed_proxy_auth = any(str(name).lower() == "x-muse-privacy" for name in dict(resp_headers))
    if status == 401 and passed_proxy_auth:
        _err(
            "A provider rejected its API key (HTTP 401), not the proxy. That provider is now paused; "
            "see which one with `muse status`, fix it with `muse keys set <NAME>`, then `muse restart`."
        )
        return 1
    if status == 401 or status == 403 and "auth" in text.lower() and not passed_proxy_auth:
        _err("The proxy rejected the master key (HTTP 401). Check it with `muse doctor`; set it with `muse keys set LITELLM_MASTER_KEY`, then `muse restart`.")
        return 1
    if status == 429:
        msg = f"Every deployment for '{model}' is rate-limited or blocked (HTTP 429)."
        try:
            snap = _build_snapshot()
            now = datetime.now(timezone.utc)
            alias_name = model if any(a.get("name") == model for a in snap.get("aliases", [])) else None
            soon = _soonest(snap, now, alias_name)
            if soon:
                msg += f" Soonest reset: {soon[0]} in {countdown(soon[1], now)}."
        except Exception:
            pass
        _err(msg + " See `muse status`.")
        return 1
    if status >= 400:
        err = payload.get("error", payload) if isinstance(payload, dict) else {}
        message = str(err.get("message", err) if isinstance(err, dict) else err)[:400] or f"HTTP {status}"
        low = message.lower()
        klass = next((w for w in _PRIVACY_WORDS if re.search(rf"\b{w}\b", low)), None)
        if status < 500 and ("privacy" in low or "policy" in low):
            _err(f"Refused by privacy policy (HTTP {status}){f', class {klass!r}' if klass else ''}: {message}")
            _err("No provider allowed for that class could serve it. Try --privacy standard, or add a local model (`muse local discover --write`).")
        else:
            _err(f"The proxy returned HTTP {status}: {message}")
        return 1
    try:
        answer = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        _err("The proxy answered 200 but the body was not a chat completion.")
        return 1
    print(answer if answer is not None else "")
    if args.show_route:
        lower = {k.lower(): v for k, v in resp_headers.items()}
        shown = {k: v for k, v in lower.items() if k in {"x-muse-provider", "x-muse-deployment"} or k.startswith("x-litellm-")}
        _err("route:" if shown else "route: (the proxy sent no routing headers)")
        for k in sorted(shown):
            _err(f"  {k}: {shown[k]}")
    return 0


# ----------------------------------------------------------- dash, env, misc


def cmd_dash(args: argparse.Namespace) -> int:
    try:
        from muse.dashboard.server import serve
    except ImportError:
        _err("The dashboard is not installed yet (muse/dashboard/server.py).")
        return 1
    url = f"http://127.0.0.1:{args.port}"
    print(f"Dashboard on {url} (Ctrl-C to stop)")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        serve(host="127.0.0.1", port=args.port)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        _err(f"Could not start the dashboard on port {args.port}: {exc}")
        return 1
    return 0


# ------------------------------------------------------------------- health


def _health_vault_path(args: argparse.Namespace) -> Path:
    if getattr(args, "vault", None):
        return Path(args.vault).expanduser()
    from muse.health.vault import default_vault_path

    return default_vault_path()


def cmd_health_app(args: argparse.Namespace) -> int:
    """Start the health assistant's web app on loopback."""
    try:
        from muse.health.app.server import serve
        from muse.health.app.services import build_services
        from muse.health.tenant import TenantError, load_tenant
    except ImportError as exc:
        _err(f"The health assistant needs the 'health' extra: pip install -e '.[health]' ({exc.name} is missing).")
        return 1
    try:
        tenant = load_tenant(args.tenant, _env())
    except TenantError as exc:
        _err(f"Tenant settings problem: {exc}")
        return 1
    try:
        services = build_services(tenant, _health_vault_path(args), _env())
    except Exception as exc:
        _err(f"Could not start the health assistant: {type(exc).__name__}: {exc}")
        return 1
    url = f"http://127.0.0.1:{args.port}"
    print(f"{tenant.display_name}: {url} (Ctrl-C to stop)")
    print("Health content is only sent to models in the tenant's privacy class "
          f"({', '.join(tenant.allowed_privacy)}). Without one, the assistant still handles records and reminders.")
    try:
        serve(services, host="127.0.0.1", port=args.port, open_browser=not args.no_open)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        _err(f"Could not start on port {args.port}: {exc}")
        return 1
    return 0


def cmd_health_import(args: argparse.Namespace) -> int:
    """Create a vault from an exported archive, e.g. when moving from the hospital install."""
    try:
        from muse.health.contracts import VaultError, WrongPassphrase
        from muse.health.vault import LocalVault
    except ImportError as exc:
        _err(f"The health assistant needs the 'health' extra: pip install -e '.[health]' ({exc.name} is missing).")
        return 1
    dest = _health_vault_path(args)
    if dest.exists():
        _err(f"A vault already exists at {dest}. Refusing to overwrite it; choose another --vault path.")
        return 1
    # The passphrase is always required: it is what proves no record was
    # removed from the archive on the way here.
    passphrase = sys.stdin.readline().rstrip("\n") if args.stdin else getpass.getpass("Vault passphrase: ")
    try:
        vault = LocalVault.import_archive(args.archive, dest, passphrase=passphrase)
        vault.unlock(passphrase)
        count = len(vault.list(limit=100000))
        vault.lock()
    except WrongPassphrase:
        dest.unlink(missing_ok=True)
        _err("Wrong passphrase, or the archive was altered. Nothing was imported.")
        return 1
    except (VaultError, OSError) as exc:
        dest.unlink(missing_ok=True)
        _err(f"Could not import the archive: {exc}")
        return 1
    print(f"Imported {count} records into {dest}. Open it with: muse health app")
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    url = _proxy_url()
    print("# Point an OpenAI-compatible client at the Muse proxy. Use: eval \"$(muse env)\"")
    print(f"export OPENAI_BASE_URL={url}/v1")
    print(f"export OPENAI_API_BASE={url}/v1  # older SDKs")
    if args.show_master_key:
        print(f"export OPENAI_API_KEY={_env().get(keys.MASTER_KEY, '')}")
    else:
        print(f'export OPENAI_API_KEY="${{{keys.MASTER_KEY}:?export {keys.MASTER_KEY} first}}"  # read from this variable; the key itself is not printed')
        src = keys.source_of(keys.MASTER_KEY, _env())
        if src in ("keychain", "dotenv"):
            print(f"# {keys.MASTER_KEY} is stored in the {src}; `muse env --show-master-key` prints the real export line.")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"muse {__version__}")
    return 0


# ------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="muse", description="Your own AI keys and local models behind one local proxy.")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("status", help="providers, quotas, aliases, recent calls")
    s.add_argument("--json", action="store_true")
    s.add_argument("--watch", type=int, metavar="N", default=0, help="refresh every N seconds")
    s.add_argument("--probe", action="store_true", help="ask providers that offer a quota endpoint (OpenRouter) what is left")
    s.set_defaults(func=cmd_status)

    k = sub.add_parser("keys", help="manage provider API keys (values are never taken from argv)")
    ksub = k.add_subparsers(dest="keys_cmd", required=True)
    kl = ksub.add_parser("list", help="which keys are set, and where")
    kl.add_argument("--json", action="store_true")
    ks = ksub.add_parser("set", help="prompt for a key (hidden) or read it from stdin")
    ks.add_argument("name")
    ks.add_argument("--stdin", action="store_true", help="read the value from stdin")
    ks.add_argument("--force", action="store_true", help="allow a name outside the catalogue")
    ks.add_argument("--backend", choices=["auto", "keychain", "dotenv"], default="auto")
    kr = ksub.add_parser("rm", help="delete a key from the keychain and dotenv file")
    kr.add_argument("name")
    kr.add_argument("--force", action="store_true")
    ko = ksub.add_parser("open", help="print the page where this key is issued")
    ko.add_argument("name")
    ko.add_argument("--force", action="store_true")
    k.set_defaults(func=cmd_keys)

    lo = sub.add_parser("local", help="local models (Ollama, LM Studio, OpenAI-compatible)")
    lsub = lo.add_subparsers(dest="local_cmd", required=True)
    ld = lsub.add_parser("discover", help="probe local runtimes")
    ld.add_argument("--write", action="store_true", help="write the local catalogue")
    ld.add_argument("--json", action="store_true")
    ll = lsub.add_parser("list", help="show the local catalogue")
    ll.add_argument("--json", action="store_true")
    lo.set_defaults(func=cmd_local)

    d = sub.add_parser("doctor", help="check the setup and say how to fix it")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_doctor)

    for name, fn, helptext in (("up", cmd_up, "start the proxy"), ("restart", cmd_restart, "recreate the proxy with current keys")):
        u = sub.add_parser(name, help=helptext)
        u.add_argument("--native", action="store_true", help="run litellm on the host instead of Docker")
        u.add_argument("--detach", action="store_true", help="with --native, run in the background")
        if name == "up":
            u.add_argument("--show-master-key", action="store_true", help="print the master key once")
        u.set_defaults(func=fn)
    dn = sub.add_parser("down", help="stop the proxy")
    dn.set_defaults(func=cmd_down)

    a = sub.add_parser("ask", help="one chat call through the proxy")
    a.add_argument("prompt")
    a.add_argument("--alias", default="fast")
    a.add_argument("--privacy", choices=list(_PRIVACY_WORDS))
    a.add_argument("--model", help="a deployment id; overrides --alias")
    a.add_argument("--show-route", action="store_true", help="print which provider answered (stderr)")
    a.set_defaults(func=cmd_ask)

    da = sub.add_parser("dash", help="open the local dashboard")
    da.add_argument("--port", type=int, default=4180)
    da.add_argument("--no-open", action="store_true")
    da.set_defaults(func=cmd_dash)

    h = sub.add_parser("health", help="the health-management assistant")
    hsub = h.add_subparsers(dest="health_cmd", required=True)
    ha = hsub.add_parser("app", help="start the assistant's web app on 127.0.0.1")
    ha.add_argument("--tenant", help="tenant id in config/tenants (default: MUSE_TENANT or demo-hospital)")
    ha.add_argument("--port", type=int, default=4190)
    ha.add_argument("--vault", help="vault file (default: ~/.muse/health/vault.sqlite)")
    ha.add_argument("--no-open", action="store_true")
    ha.set_defaults(func=cmd_health_app)
    hi = hsub.add_parser("import", help="create a vault from an exported archive")
    hi.add_argument("archive")
    hi.add_argument("--vault", help="where to create the vault (default: ~/.muse/health/vault.sqlite)")
    hi.add_argument("--stdin", action="store_true", help="read the passphrase from stdin")
    hi.set_defaults(func=cmd_health_import)

    e = sub.add_parser("env", help="export lines for an OpenAI-compatible client")
    e.add_argument("--show-master-key", action="store_true", help="print the real key instead of the variable name")
    e.set_defaults(func=cmd_env)

    v = sub.add_parser("version", help="print the version")
    v.set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> int:
    global _ENV_CACHE
    _ENV_CACHE = None
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return 2
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
    except keys.KeysError as exc:
        _err(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
