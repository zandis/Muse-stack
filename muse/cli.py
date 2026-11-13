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
    if snap.get("paused"):
        out.append(paint(_fit("*** PAUSED: queue stopped (`muse resume`). The proxy still answers. ***", WIDTH), "yellow+bold"))
    out.extend(_paid_queue_lines(snap, now, paint))
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
    out.extend(paint(_fit(line, WIDTH), "dim") for line in next_helper_lines(snap))  # catalogue v2
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

    out.extend(_apps_block(snap, paint, unicode_ok))

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


def _paid_queue_lines(snap: Mapping[str, Any], now: datetime, paint: Paint) -> list[str]:
    """'Paid today' and 'Queue' lines. Missing keys (an older snapshot) print nothing."""
    out: list[str] = []
    paid = snap.get("paid")
    if isinstance(paid, Mapping):
        cap, spent = float(paid.get("cap_usd") or 0), float(paid.get("spent_usd") or 0)
        text = f"Paid today: ${spent:.2f} of ${cap:.2f}" if cap > 0 else "Paid today: off (cap is $0.00)"
        style = None
        if cap > 0 and spent >= cap:
            text += "  CAP REACHED"
            style = "red"
        elif cap > 0 and spent >= 0.8 * cap:
            style = "yellow"
        reset = countdown(paid.get("resets_at"), now)
        if reset and cap > 0:
            text += f"  (resets in {reset})"
        if paid.get("unknown_cost_calls"):
            n = int(paid["unknown_cost_calls"])
            text += f"  {n} unpriced {'call' if n == 1 else 'calls'}"
        out.append(paint(_fit(text, WIDTH), style))
    queue = snap.get("queue")
    if isinstance(queue, Mapping):
        waiting = int(queue.get("waiting") or 0)
        counts = queue.get("counts") or {}
        if waiting == 0 and not counts.get("running"):
            text = "Queue: empty" + (f" ({counts.get('done', 0)} done)" if counts.get("done") else "")
        else:
            text = f"Queue: {waiting} waiting" + (" (paused)" if snap.get("paused") else "")
            if counts.get("running"):
                text += f", {counts['running']} running"
            due = countdown(queue.get("next_due_at"), now)
            if due:
                text += ", next " + ("due now" if due == "now" else f"in {due}")
            hold = countdown(queue.get("hold_until"), now)
            if hold and hold != "now":
                text += f" (app cap: nothing before {hold})"
        out.append(_fit(text, WIDTH))
    return out


def _apps_block(snap: Mapping[str, Any], paint: Paint, unicode_ok: bool) -> list[str]:
    rows = [a for a in snap.get("apps", []) if isinstance(a, Mapping)]
    if not rows:
        return []
    out = [paint("APPS", "bold")]
    for a in rows:
        today = a.get("today") or {}
        if a.get("name") == "anonymous" and not today.get("requests") and not a.get("deferrals"):
            continue
        used = f"{today.get('requests', 0)} req {short_num(today.get('tokens', 0))} tok"
        caps = []
        for key, val, unit in (("daily_requests", today.get("requests", 0), "req"), ("daily_tokens", today.get("tokens", 0), "tok")):
            cap = a.get(key)
            if cap:
                frac = min(1.0, val / cap)
                caps.append(f"{bar(frac, 5, unicode_ok)} {short_num(val)}/{short_num(cap)} {unit}")
        line = f"  {_fit(str(a.get('name')), 12):<12} {str(a.get('priority', '')):<11} {used:<14}" + ("  " + "  ".join(caps) if caps else "")
        out.append(_fit(line.rstrip(), WIDTH))
        if a.get("deferrals"):
            parts = ", ".join(f"{n}x {ration_words(k)}" for k, n in a["deferrals"].items())
            out.append(paint(_fit(f"    turned away today: {parts}", WIDTH), "yellow"))
    out.append("")
    return out


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
            checks.append(_check("docker", WARN, "Docker not found", "litellm is on PATH: `muse up --native` works." if native else "", "" if native else "Install Docker, or for --native: `pip install --require-hashes -r requirements/proxy.lock`."))
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
    child = {k: v for k, v in env.items() if k != "MUSE_QUEUE_APP_TOKEN"}  # the proxy never needs it
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
        _err("`litellm` is not on PATH. Install it (pip install --require-hashes -r requirements/proxy.lock) or use Docker: muse up")
        return 1
    rc = _load_renderer()
    config, enabled = rc.render(rc.load_catalog(paths.CATALOG_PATH), env)
    cfg_path = paths.home(env) / "litellm-config.yaml"
    rc.write_config(rc.dumps(config, enabled), cfg_path)
    # render_and_start.py hardcodes --host 0.0.0.0; natively we honour MUSE_BIND (default loopback).
    host = env.get("MUSE_BIND", "").strip() or "127.0.0.1"
    port = env.get("MUSE_PORT", "").strip() or "4000"
    child = {k: v for k, v in env.items() if k != "MUSE_QUEUE_APP_TOKEN"}
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
    model = args.model or args.alias or ("auto" if args.task else "fast")
    if args.task:
        headers["x-muse-task"] = args.task  # routing rules: the proxy picks the alias (see `muse rules`)
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
        said = payload.get("error", {}).get("message") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else None
        if isinstance(said, str) and "[muse]" in said:
            _err(said[said.index("[muse]"):][:600])  # the router's reason, with its options=... choices
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
    lower = {str(k).lower(): v for k, v in dict(resp_headers).items()}
    if str(lower.get("x-muse-answer", "")).strip().lower() == "empty-reasoning":
        _err("No visible answer: the model used its whole output allowance thinking. Ask again and allow a longer reply.")
    if args.show_route:
        shown = {k: v for k, v in lower.items()
                 if k in {"x-muse-provider", "x-muse-deployment", "x-muse-min-output", "x-muse-answer"} or k.startswith("x-litellm-")}
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


# ===== BEGIN catalogue v2: `muse connect`, `muse resources` (own block; logic lives in muse/connect.py) =====


def cmd_connect(args: argparse.Namespace) -> int:
    from muse import connect

    return connect.run_connect(
        connect.ConnectIO.system(),
        _env(),
        provider=args.provider,
        extra=args.rest,
        lang=args.lang,
        no_browser=args.no_browser,
        paste_code=args.paste_code,
        test=args.test,
        open_page=True if args.open else None,
        timeout_s=float(args.timeout),
        write_local=not args.no_write,
        as_json=args.json,
    )


def cmd_resources(args: argparse.Namespace) -> int:
    from muse import catalog, connect

    return connect.run_resources(connect.ConnectIO.system(), catalog.normalise_lang(args.lang) if args.lang else catalog.default_lang(_env()), as_json=args.json)


def next_helper_lines(snap: Mapping[str, Any]) -> list[str]:
    """The one "Next helper to add: ..." line for `muse status` (nothing for an older snapshot)."""
    try:
        from muse import catalog, connect

        line = connect.next_helper_line(snap, catalog.default_lang(os.environ))  # render_status stays pure: no key store reads
    except Exception:
        return []
    return [line] if line else []


def register_connect_commands(sub: Any) -> None:
    c = sub.add_parser("connect", help="add a helper step by step (no argument: what is connected and what to add next)")
    c.add_argument("provider", nargs="?", help="a helper name, e.g. openrouter, gemini, groq, or local")
    c.add_argument("rest", nargs="*", help=argparse.SUPPRESS)  # caught so a pasted key is refused, not echoed
    c.add_argument("--lang", metavar="LANG", help="en or zh-TW (default: MUSE_LANG, else from the system locale)")
    c.add_argument("--no-browser", action="store_true", help="print the link instead of opening a browser")
    c.add_argument("--paste-code", action="store_true", help="one-tap flow without a local listener: approve anywhere, paste the code it shows")
    c.add_argument("--test", action="store_true", help="after saving a key, send ONE key-check request that carries no prompt")
    c.add_argument("--open", action="store_true", help="open the provider's key page without asking")
    c.add_argument("--timeout", type=float, default=300.0, metavar="SECONDS", help="how long to wait for the approval (default 300)")
    c.add_argument("--no-write", action="store_true", help="with `local`: show what was found without writing the catalogue")
    c.add_argument("--json", action="store_true", help="with no helper name: print the suggestions as JSON")
    c.set_defaults(func=cmd_connect)
    r = sub.add_parser("resources", help="storage, compute and chain allowances, and your subscriptions (read-only)")
    r.add_argument("--lang", metavar="LANG", help="en or zh-TW")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_resources)


def cmd_catalog_check(args: argparse.Namespace) -> int:
    from muse import catalog_check

    return catalog_check.run(
        pages=args.pages, want_leads=args.leads, max_age_days=args.max_age_days, save=not args.no_save,
        as_json=args.json, fetch=catalog_check.fetch_https, environ=_env(),
    )


def register_catalog_commands(sub: Any) -> None:
    c = sub.add_parser("catalog", help="check the catalogue's dated facts (read-only; never edits it)")
    csub = c.add_subparsers(dest="catalog_command", required=True)
    k = csub.add_parser("check", help="list stale entries; with --pages, say which source pages changed")
    k.add_argument("--max-age-days", type=int, default=30, metavar="N", help="an entry checked more than N days ago is stale (default 30)")
    k.add_argument("--pages", action="store_true", help="fetch each source page once and compare it with the last check")
    k.add_argument("--leads", action="store_true", help="also list names on a public lead list that the catalogue lacks")
    k.add_argument("--no-save", action="store_true", help="with --pages: do not remember the page fingerprints")
    k.add_argument("--json", action="store_true")
    k.set_defaults(func=cmd_catalog_check)


# ===== END catalogue v2 =====


# ===== BEGIN home control API: `muse home serve|token|call` (own block; logic lives in muse/home/) =====


def register_home_commands(sub: Any) -> None:
    from muse.home import cli as home_cli

    home_cli.register(sub)


# ===== END home control API =====


def cmd_version(args: argparse.Namespace) -> int:
    print(f"muse {__version__}")
    return 0


# ------------------------------------------------------------ apps, queue, pause

RATION_WORDS = {
    "defer:yield": "waited for an app in use",
    "defer:reserve": "kept a reserve",
    "defer:blocked": "providers blocked",
    "defer:paid-cap": "paid cap reached",
    "defer:paid-spend-unknown": "paid spend unknown",
    "refuse:app-requests": "hit its request cap",
    "refuse:app-tokens": "hit its token cap",
    "refuse:paid-not-allowed": "paid not allowed",
    "refuse:paid-cap": "paid cap reached",
    "refuse:paid-unknown-cost": "price unknown",
    "refuse:paid-spend-unknown": "paid spend unknown",
}


def ration_words(key: str) -> str:
    """Plain words for a `kind:code` deferral key; an unknown code is shown as it is."""
    return RATION_WORDS.get(key, key.replace(":", " "))


def _opt_int(value: Any) -> str:
    return "-" if value is None else short_num(value)


def _app_rows(snap_apps: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(a) for a in snap_apps]


def _apps_snapshot() -> list[dict[str, Any]]:
    from muse.ledger import Ledger
    from muse.status import _SafeLedger, build_apps

    env = _env()
    now = time.time()
    return _app_rows(build_apps(env, now, _SafeLedger(Ledger(paths.ledger_path(env), clock=lambda: now))))  # type: ignore[arg-type]


def render_apps(rows: list[Mapping[str, Any]], paint: Paint) -> str:
    out = [paint(f"{'APP':<13} {'PRIORITY':<12} {'TODAY':<22} {'CAPS req/tok':<14} PAID", "bold")]
    for a in rows:
        today = a.get("today") or {}
        used = f"{today.get('requests', 0)} req {short_num(today.get('tokens', 0))} tok"
        if today.get("cost_usd"):
            used += f" ${today['cost_usd']:.2f}"
        caps = f"{_opt_int(a.get('daily_requests'))}/{_opt_int(a.get('daily_tokens'))}"
        paid = "yes" if a.get("allow_paid") else "no"
        tag = "  (built in)" if a.get("builtin") else ""
        out.append(f"{_cell(paint, str(a['name']), 13)} {_cell(paint, str(a['priority']), 12)} {_cell(paint, used, 22)} {_cell(paint, caps, 14)} {paid}{tag}".rstrip())
        extra = []
        if a.get("default_privacy"):
            extra.append(f"privacy {a['default_privacy']}")
        if a.get("created_at"):
            extra.append(f"created {str(a['created_at'])[:10]}")
        if a.get("note"):
            extra.append(str(a["note"]))
        if extra:
            out.append(paint(_fit("    " + " · ".join(extra), WIDTH), "dim"))
        if a.get("deferrals"):
            parts = ", ".join(f"{n}x {ration_words(k)}" for k, n in a["deferrals"].items())
            out.append(paint(_fit(f"    turned away today: {parts}", WIDTH), "yellow"))
    return "\n".join(out) + "\n"


def cmd_apps_list(args: argparse.Namespace) -> int:
    rows = _apps_snapshot()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    sys.stdout.write(render_apps(rows, Paint(_color_enabled())))
    return 0


def _token_display_ok(args: argparse.Namespace) -> bool:
    """Rule: a token is printed only when stdout is a terminal or --show-token is given.

    Checked BEFORE the app is created or the token rotated, so a refusal never
    leaves a token that nobody saw. Why: stdout of a non-terminal is usually a log
    file, CI output or a pipe to something that stores it.
    """
    if getattr(args, "show_token", False) or sys.stdout.isatty():
        return True
    _err(
        "Refusing to print a new key: stdout is not a terminal, so it could end up in a log. "
        "Run this in a terminal, or add --show-token if you really want it on stdout. Nothing was created or changed."
    )
    return False


def _base_v1() -> str:
    return _proxy_url() + "/v1"


def _print_token_block(name: str, token: str, extra_env: bool = True) -> None:
    base = _base_v1()
    print()
    print("  COPY THE KEY NOW. It is shown once. Muse keeps only a hash, so it cannot be shown again.")
    print(f"  If you lose it, `muse apps rotate {name}` issues a new one and the old one stops working.")
    print()
    print(f"  Base URL : {base}")
    print(f"  API key  : {token}")
    if extra_env:
        print()
        print("For a client that reads the OpenAI environment variables:")
        print(f"  export OPENAI_BASE_URL={base}")
        print(f"  export OPENAI_API_KEY={token}")
    if base.startswith("http://127.0.0.1") or base.startswith("http://localhost"):
        print()
        print("  127.0.0.1 only works for programs on this machine. From Docker use host.docker.internal.")


def _describe(app: Any) -> str:
    caps = []
    if app.daily_requests is not None:
        caps.append(f"{app.daily_requests} requests/day")
    if app.daily_tokens is not None:
        caps.append(f"{short_num(app.daily_tokens)} tokens/day")
    return f"{app.priority}, {', '.join(caps) if caps else 'no daily caps'}, paid: {'yes' if app.allow_paid else 'no'}"


def _create_app(args: argparse.Namespace, name: str, priority: str, daily_requests: int | None, daily_tokens: int | None,
                allow_paid: bool | None, privacy: str | None, note: str | None, preset: Any = None) -> int:
    from muse import apps, presets

    if not _token_display_ok(args):
        return 2
    try:
        app, token = apps.add_app(name, priority, daily_requests=daily_requests, daily_tokens=daily_tokens,
                                  allow_paid=allow_paid, default_privacy=privacy, note=note, environ=_env())
    except apps.AppError as exc:
        _err(f"Error: {exc}")
        return 1
    if args.json:
        print(json.dumps({"name": app.name, "token": token, "base_url": _base_v1(), "priority": app.priority,
                          "daily_requests": app.daily_requests, "daily_tokens": app.daily_tokens, "allow_paid": app.allow_paid,
                          "model": preset.alias if preset else None}))
        return 0
    print(f"Created app '{app.name}' ({_describe(app)}).")
    if preset:
        print(f"  {preset.why}")
    _print_token_block(app.name, token, extra_env=preset is None)
    if preset:
        print()
        print(presets.hint(preset, _base_v1(), token))
    print()
    print("The proxy picks the new app up on its next request; no restart is needed.")
    return 0


def cmd_apps_add(args: argparse.Namespace) -> int:
    return _create_app(args, args.name, args.priority, args.daily_requests, args.daily_tokens, args.allow_paid, args.privacy, args.note)


def cmd_apps_preset(args: argparse.Namespace) -> int:
    from muse import presets

    preset = presets.get(args.kind)
    name = args.name or args.kind
    return _create_app(args, name, preset.priority, args.daily_requests if args.daily_requests is not None else preset.daily_requests,
                       args.daily_tokens if args.daily_tokens is not None else preset.daily_tokens, args.allow_paid, args.privacy, args.note, preset)


def cmd_apps_set(args: argparse.Namespace) -> int:
    from muse import apps

    changes: dict[str, Any] = {}
    if args.priority:
        changes["priority"] = args.priority
    for flag, field_ in (("daily_requests", "daily_requests"), ("daily_tokens", "daily_tokens")):
        value = getattr(args, flag)
        if value is not None:
            changes[field_] = None if value < 0 else value  # -1 removes the cap
        elif getattr(args, "no_" + flag):
            changes[field_] = None
    if args.allow_paid is not None:
        changes["allow_paid"] = args.allow_paid
    if args.privacy:
        changes["default_privacy"] = None if args.privacy == "none" else args.privacy
    if args.note is not None:
        changes["note"] = args.note or None
    if not changes:
        _err("Nothing to change. Options: --priority, --daily-requests, --daily-tokens, --allow-paid/--no-paid, --privacy, --note.")
        return 2
    try:
        app = apps.update_app(args.name, _env(), **changes)
    except apps.AppError as exc:
        _err(f"Error: {exc}")
        return 1
    print(f"Updated '{app.name}': {_describe(app)}.")
    return 0


def cmd_apps_rm(args: argparse.Namespace) -> int:
    from muse import apps

    try:
        removed = apps.remove_app(args.name, _env())
    except apps.AppError as exc:
        _err(f"Error: {exc}")
        return 1
    if not removed:
        _err(f"No app named '{args.name}'.")
        return 1
    print(f"Removed '{args.name}'. Its key stops working at the next request.")
    return 0


def cmd_apps_rotate(args: argparse.Namespace) -> int:
    from muse import apps

    if not _token_display_ok(args):
        return 2
    try:
        app, token = apps.rotate_token(args.name, _env())
    except apps.AppError as exc:
        _err(f"Error: {exc}")
        return 1
    if args.json:
        print(json.dumps({"name": app.name, "token": token, "base_url": _base_v1()}))
        return 0
    print(f"New key for '{app.name}'. The old key no longer works.")
    _print_token_block(app.name, token, extra_env=False)
    return 0


def cmd_apps(args: argparse.Namespace) -> int:
    return {
        "list": cmd_apps_list, "add": cmd_apps_add, "set": cmd_apps_set, "rm": cmd_apps_rm,
        "rotate": cmd_apps_rotate, "preset": cmd_apps_preset,
    }[args.apps_cmd](args)


# ---- pause / resume


def cmd_pause(args: argparse.Namespace) -> int:
    from muse import queue

    queue.set_paused(_env())
    print("Paused. The queue sends nothing until you run `muse resume`. The proxy keeps answering your apps.")
    _record_home_event("home.pause", "Paused the queue")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from muse import queue

    if queue.clear_paused(_env()):
        print("Resumed. A running `muse queue run` carries on within a few seconds.")
        _record_home_event("home.resume", "Resumed the queue")
    else:
        print("It was not paused.")
    return 0


# ---- queue


def _queue() -> Any:
    from muse import queue

    return queue.Queue(_env())


def _read_prompt(text: str) -> str:
    return sys.stdin.read() if text == "-" else text


def _privacy_for(args: argparse.Namespace) -> str | None:
    return args.privacy or (_env().get("MUSE_DEFAULT_PRIVACY", "").strip().lower() or None)


def _deadline(args: argparse.Namespace) -> float | None:
    from muse import queue
    from muse.ration import resolve_tz

    if not getattr(args, "by", None):
        return None
    return queue.parse_deadline(args.by, time.time(), resolve_tz(_env().get("MUSE_TZ", "").strip()))


def _announce_added(job: Any) -> None:
    due = f", must finish by {datetime.fromtimestamp(job.deadline).strftime('%Y-%m-%d %H:%M')}" if job.deadline else ""
    route = f"task {job.task}" if job.task else f"alias {job.alias}"
    print(f"Queued {job.id} ({job.kind}, {route}{', paid allowed' if job.allow_paid else ''}{due}).")
    print(f"  Result file: {job.out_path}")


def cmd_queue_add(args: argparse.Namespace) -> int:
    from muse import queue

    q = _queue()
    try:
        prompt = _read_prompt(args.prompt)
        if not prompt.strip():
            raise queue.QueueError("the prompt is empty")
        if len(prompt) > queue.MAX_PROMPT_CHARS:
            raise queue.QueueError(f"the prompt is {len(prompt)} characters; the limit is {queue.MAX_PROMPT_CHARS}")
        task, alias = _job_route(args)
        job = q.add(kind="prompt", payload=prompt, alias=alias, privacy=_privacy_for(args), out_path=args.out,
                    deadline=_deadline(args), allow_paid=args.allow_paid, max_attempts=args.max_attempts,
                    max_tokens=args.max_tokens, force=args.overwrite, task=task)
    except queue.QueueError as exc:
        _err(f"Error: {exc}")
        return 1
    _announce_added(job)
    return 0


def cmd_queue_add_file(args: argparse.Namespace) -> int:
    from muse import queue

    q = _queue()
    target = Path(args.path).expanduser().resolve()
    try:
        deadline = _deadline(args)
        if target.is_dir():
            files, skipped = queue.expand_directory(target, args.recursive, args.max_files)
            if not files:
                raise queue.QueueError(f"{target} has no usable text files")
            out_dir = Path(args.out).expanduser().resolve() if args.out else None
            if out_dir is not None:
                out_dir.mkdir(parents=True, exist_ok=True)
            jobs = []
            for f in files:
                out = str(out_dir / (f.name + ".out.txt")) if out_dir else None
                task, alias = _job_route(args)
                jobs.append(q.add(kind="file-summary", payload=str(f), instruction=args.do, alias=alias, privacy=_privacy_for(args),
                                  out_path=out, deadline=deadline, allow_paid=args.allow_paid, max_attempts=args.max_attempts,
                                  max_tokens=args.max_tokens, force=args.overwrite, task=task))
            print(f"Queued {len(jobs)} jobs from {target}.")
            for note in skipped:
                print(f"  skipped: {note}")
            print(f"  Results: {out_dir or paths.queue_out_dir(_env())}")
            return 0
        queue.read_text_file(target)  # fail now, not at run time, for an unreadable or oversized file
        task, alias = _job_route(args)
        job = q.add(kind="file-summary", payload=str(target), instruction=args.do, alias=alias, privacy=_privacy_for(args),
                    out_path=args.out, deadline=deadline, allow_paid=args.allow_paid, max_attempts=args.max_attempts,
                    max_tokens=args.max_tokens, force=args.overwrite, task=task)
    except queue.QueueError as exc:
        _err(f"Error: {exc}")
        return 1
    _announce_added(job)
    return 0


def cmd_queue_add_batch(args: argparse.Namespace) -> int:
    from muse import queue

    q = _queue()
    try:
        text = sys.stdin.read() if args.path == "-" else queue.read_text_file(Path(args.path).expanduser(), limit=queue.MAX_PROMPT_CHARS)
        items = queue.split_batch(text)
        task, alias = _job_route(args)
        job = q.add(kind="batch", payload=json.dumps(items), alias=alias, privacy=_privacy_for(args), out_path=args.out,
                    deadline=_deadline(args), allow_paid=args.allow_paid, max_attempts=args.max_attempts,
                    max_tokens=args.max_tokens, force=args.overwrite, task=task)
    except queue.QueueError as exc:
        _err(f"Error: {exc}")
        return 1
    _announce_added(job)
    print(f"  {len(items)} prompts, run in order, one result file.")
    return 0


def _job_when(job: Any, now: float) -> str:
    if job.status in ("queued", "deferred"):
        left = job.next_attempt_at - now
        return "due now" if left <= 0 else "in " + (countdown(queue_iso(job.next_attempt_at), datetime.fromtimestamp(now, timezone.utc)) or "?")
    return ""


def queue_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_queue_list(args: argparse.Namespace) -> int:
    q = _queue()
    jobs = q.list()
    if args.json:
        print(json.dumps([j.public() for j in jobs], indent=2))
        return 0
    paint = Paint(_color_enabled())
    if not jobs:
        print("The queue is empty. Add a job: muse queue add \"summarise ...\" --alias cheap")
        return 0
    now = time.time()
    print(paint(f"{'ID':<13}{'STATUS':<10}{'KIND':<13}{'ALIAS':<9}{'NEXT':<12}REASON", "bold"))
    style = {"done": "green", "failed": "red", "cancelled": "dim", "deferred": "yellow", "running": "cyan"}
    for j in jobs:
        print(f"{j.id:<13}{paint(f'{j.status:<10}', style.get(j.status))}{j.kind:<13}{_fit(j.alias, 8):<9}{_job_when(j, now):<12}{j.last_reason or ''}".rstrip())
    return 0


def cmd_queue_show(args: argparse.Namespace) -> int:
    from muse import queue

    q = _queue()
    try:
        job = q.find(args.id)
    except queue.QueueError as exc:
        _err(f"Error: {exc}")
        return 1
    if args.output:
        try:
            sys.stdout.write(Path(job.out_path).read_text(encoding="utf-8"))
        except OSError:
            _err(f"There is no result file yet ({job.status}).")
            return 1
        return 0
    data = job.public()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    for key, value in data.items():
        if value not in (None, ""):
            print(f"{key:<16} {value}")
    print("(the prompt and the result are not shown here; `muse queue show ID --output` prints the result file)")
    return 0


def cmd_queue_cancel(args: argparse.Namespace) -> int:
    from muse import queue

    try:
        job = _queue().cancel(args.id)
    except queue.QueueError as exc:
        _err(f"Error: {exc}")
        return 1
    print(f"Cancelled {job.id}.")
    return 0


def cmd_queue_clear(args: argparse.Namespace) -> int:
    from muse import queue

    statuses = ["done", "failed", "cancelled"] if args.finished else ["done"] if args.done else []
    if not statuses:
        _err("Say what to clear: --done (finished jobs) or --finished (done, failed and cancelled).")
        return 2
    try:
        n = _queue().clear(statuses)
    except queue.QueueError as exc:
        _err(f"Error: {exc}")
        return 1
    print(f"Cleared {n} job(s). Result files were left where they are.")
    return 0


def cmd_queue_run(args: argparse.Namespace) -> int:
    from muse import queue

    env = dict(_env())

    def log(line: str) -> None:
        print(f"{datetime.now().strftime('%H:%M:%S')} {line}", file=sys.stderr, flush=True)

    runner = queue.Runner(env, log=log)
    if queue.is_paused(env):
        log("paused (muse resume to continue)")
    try:
        summary = runner.run(once=args.once, max_jobs=args.max_jobs, until_idle=args.until_idle)
    except keys.KeysError as exc:
        _err(f"Error: {exc}")
        return 1
    except queue.apps.AppError as exc:
        _err(f"Error: {exc}")
        return 1
    log(f"stopped ({summary.stopped}): {summary.done} done, {summary.failed} failed, {summary.deferred} deferred this run")
    if summary.stopped == "locked":
        _err("Another `muse queue run` is already running. Only one runner may run at a time.")
        return 1
    return 1 if summary.stopped == "auth" else 0


def cmd_queue(args: argparse.Namespace) -> int:
    return {
        "add": cmd_queue_add, "add-file": cmd_queue_add_file, "add-batch": cmd_queue_add_batch, "list": cmd_queue_list,
        "show": cmd_queue_show, "cancel": cmd_queue_cancel, "clear": cmd_queue_clear, "run": cmd_queue_run,
    }[args.queue_cmd](args)


# ------------------------------------------------------------------- parser


def apps_priorities() -> tuple[str, ...]:
    from muse import apps

    return apps.PRIORITIES


def preset_kinds() -> list[str]:
    from muse import presets

    return list(presets.PRESETS)


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
    a.add_argument("--alias", default=None, help="model alias (default fast; with --task, the task's alias)")
    a.add_argument("--task", metavar="NAME", help="a task from the routing rules (light, everyday, heavy, frontier, ...); see `muse rules`")
    a.add_argument("--privacy", choices=list(_PRIVACY_WORDS))
    a.add_argument("--model", help="a deployment id; overrides --alias")
    a.add_argument("--show-route", action="store_true", help="print which provider answered (stderr)")
    a.set_defaults(func=cmd_ask)

    da = sub.add_parser("dash", help="open the local dashboard")
    da.add_argument("--port", type=int, default=4180)
    da.add_argument("--no-open", action="store_true")
    da.set_defaults(func=cmd_dash)

    register_connect_commands(sub)
    register_catalog_commands(sub)  # catalogue v2 block above
    register_home_commands(sub)  # home control API block above

    e = sub.add_parser("env", help="export lines for an OpenAI-compatible client")
    e.add_argument("--show-master-key", action="store_true", help="print the real key instead of the variable name")
    e.set_defaults(func=cmd_env)

    ap = sub.add_parser("apps", help="applications that use the proxy: keys, priority, daily caps")
    asub = ap.add_subparsers(dest="apps_cmd", required=True)
    al = asub.add_parser("list", help="every app with today's use and what was turned away")
    al.add_argument("--json", action="store_true")

    def app_options(sp: argparse.ArgumentParser, with_priority: bool = True) -> None:
        if with_priority:
            sp.add_argument("--priority", choices=list(apps_priorities()), default=None if sp.prog.endswith(("set", "preset")) else "normal")
        sp.add_argument("--daily-requests", type=int, metavar="N", default=None, help="most successful requests per local day")
        sp.add_argument("--daily-tokens", type=int, metavar="N", default=None, help="most tokens per local day")
        sp.add_argument("--allow-paid", dest="allow_paid", action="store_const", const=True, default=None,
                        help="this app may use paid providers (default: no for background, yes otherwise)")
        sp.add_argument("--no-paid", dest="allow_paid", action="store_const", const=False, help="this app never uses paid providers")
        sp.add_argument("--privacy", choices=list(_PRIVACY_WORDS), default=None, help="default privacy class for this app")
        sp.add_argument("--note", default=None, help="one line to remember what this app is")

    aa = asub.add_parser("add", help="create an app and show its key once")
    aa.add_argument("name")
    app_options(aa)
    aa.add_argument("--show-token", action="store_true", help="print the key even when stdout is not a terminal")
    aa.add_argument("--json", action="store_true", help="print the new key as JSON (same terminal rule)")
    ase = asub.add_parser("set", help="change an app's priority, caps, paid permission or privacy")
    ase.add_argument("name")
    app_options(ase)
    ase.add_argument("--no-daily-requests", action="store_true", help="remove the request cap")
    ase.add_argument("--no-daily-tokens", action="store_true", help="remove the token cap")
    for action in ase._actions:
        if action.dest == "privacy":
            action.choices = [*_PRIVACY_WORDS, "none"]
            action.help = "default privacy class for this app (none removes it)"
    ar = asub.add_parser("rm", help="delete an app; its key stops working")
    ar.add_argument("name")
    aro = asub.add_parser("rotate", help="issue a new key; the old one stops working at once")
    aro.add_argument("name")
    aro.add_argument("--show-token", action="store_true")
    aro.add_argument("--json", action="store_true")
    apr = asub.add_parser("preset", help="create an app with settings for a known tool and print how to configure it")
    apr.add_argument("name", nargs="?", help="app name (default: the tool's name)")
    apr.add_argument("--for", dest="kind", required=True, choices=list(preset_kinds()))
    app_options(apr, with_priority=False)
    apr.add_argument("--show-token", action="store_true")
    apr.add_argument("--json", action="store_true")
    ap.set_defaults(func=cmd_apps)

    qp = sub.add_parser("queue", help="background jobs that run on free allowance when you are not busy")
    qsub = qp.add_subparsers(dest="queue_cmd", required=True)

    def job_options(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--alias", default=None, help="model alias; default: the rules file's task for the queue app")
        sp.add_argument("--task", metavar="NAME", help="a task from the routing rules; the proxy picks the alias (see `muse rules`)")
        sp.add_argument("--privacy", choices=list(_PRIVACY_WORDS))
        sp.add_argument("--out", metavar="PATH", help="where to write the result (default: ~/.muse/queue-out/ID.txt)")
        sp.add_argument("--by", metavar="DEADLINE", help="give up after this: 2h, 90m, 08:30 or 2026-11-13T08:30")
        sp.add_argument("--allow-paid", action="store_true", help="let this job use the paid allowance (still bound by the daily cap)")
        sp.add_argument("--max-attempts", type=int, default=5)
        sp.add_argument("--max-tokens", type=int, default=1024, help="most tokens the answer may use")
        sp.add_argument("--overwrite", action="store_true", help="replace an existing result file")

    qa = qsub.add_parser("add", help='queue a prompt ("-" reads it from stdin, so it stays out of shell history)')
    qa.add_argument("prompt")
    job_options(qa)
    qf = qsub.add_parser("add-file", help="queue a text file, or one job per file in a folder")
    qf.add_argument("path")
    qf.add_argument("--do", required=True, metavar="TASK", help='what to do, e.g. "summarise", "translate to French", "extract the dates"')
    qf.add_argument("--recursive", action="store_true")
    qf.add_argument("--max-files", type=int, default=50, help="refuse a folder with more usable files than this (at most 200)")
    job_options(qf)
    qb = qsub.add_parser("add-batch", help="queue several prompts (separated by a line holding only ---) as one job")
    qb.add_argument("path", help='a text file, or "-" for stdin')
    job_options(qb)
    ql = qsub.add_parser("list", help="every job")
    ql.add_argument("--json", action="store_true")
    qs = qsub.add_parser("show", help="one job's metadata")
    qs.add_argument("id")
    qs.add_argument("--output", action="store_true", help="print the result file")
    qs.add_argument("--json", action="store_true")
    qc = qsub.add_parser("cancel", help="cancel a waiting or running job")
    qc.add_argument("id")
    qcl = qsub.add_parser("clear", help="delete finished jobs from the queue (result files are kept)")
    qcl.add_argument("--done", action="store_true")
    qcl.add_argument("--finished", action="store_true", help="done, failed and cancelled")
    qr = qsub.add_parser("run", help="run the queue (Ctrl-C to stop)")
    qr.add_argument("--once", action="store_true", help="try every job that is due now, then exit")
    qr.add_argument("--max-jobs", type=int, metavar="N", help="stop after N jobs finish")
    qr.add_argument("--until-idle", action="store_true", help="exit when nothing is waiting")
    qp.set_defaults(func=cmd_queue)

    pa = sub.add_parser("pause", help="stop the queue sending anything (the proxy keeps working)")
    pa.set_defaults(func=cmd_pause)
    re_ = sub.add_parser("resume", help="let the queue run again")
    re_.set_defaults(func=cmd_resume)

    v = sub.add_parser("version", help="print the version")
    v.set_defaults(func=cmd_version)
    add_rules_parser(sub)
    register_skills_commands(sub)
    register_records_commands(sub)
    add_permissions_parser(sub)
    register_host_commands(sub)
    return p


# ============================================================ routing rules: `muse rules`, --task
# (rationing work; kept in one block so other command blocks merge cleanly)


def _job_route(args: argparse.Namespace) -> tuple[str | None, str]:
    """(task, alias) for a queued job: --task, else --alias, else the rules file's default for the queue app."""
    from muse import queue, rules

    if args.task:
        return args.task.strip().lower(), args.alias or "auto"
    if args.alias:
        return None, args.alias
    task, _ = rules.load_rules(_env()).default_for(queue.app_for(bool(args.allow_paid)))
    return task, "auto"


def cmd_rules(args: argparse.Namespace) -> int:
    from muse import rules

    env = _env()
    path = rules.rules_path(env)
    if args.action == "check":
        problems = rules.check(path)
        if problems:
            for problem in problems:
                _err(f"problem: {problem}")
            _err("The proxy keeps using the built-in rules until this is fixed (see `muse rules`).")
            return 1
        current = rules.load_rules(env)
        known = _catalogue_aliases()
        unknown = sorted({a for t in current.tasks.values() for a in (t.alias, t.long_input_alias) if a} - known) if known else []
        for alias in unknown:
            _err(f"warning: alias {alias!r} is not in the provider catalogue; requests for it will fail")
        print(f"{path}: ok ({len(current.tasks)} tasks)")
        return 0
    current = rules.load_rules(env)
    if args.json:
        print(json.dumps({**rules.as_dict(current), "path": str(path)}, indent=2, ensure_ascii=False))
        return 0
    for line in rules.explain(current, args.lang):
        print(line)
    print(("規則檔：" if args.lang.startswith("zh") else "Rules file: ") + str(path))
    return 1 if current.problems else 0


def _catalogue_aliases() -> set[str]:
    try:
        data = yaml.safe_load(Path(_env().get("MUSE_CATALOG", "") or paths.CATALOG_PATH).read_text(encoding="utf-8"))
        return set((data or {}).get("aliases") or {})
    except (OSError, yaml.YAMLError, AttributeError):
        return set()


def add_rules_parser(sub: Any) -> None:
    r = sub.add_parser("rules", help="how each kind of task is routed (config/routing.yaml), as sentences")
    r.add_argument("action", nargs="?", choices=["show", "check"], default="show", help="check: validate the rules file")
    r.add_argument("--json", action="store_true")
    r.add_argument("--lang", choices=["en", "zh"], default="en", help="zh: Traditional Chinese")
    r.set_defaults(func=cmd_rules)


# ============================================================ end routing rules


# ===== BEGIN skills: `muse skills list|show|approve|run` (own block; logic lives in muse/skills/cli.py) =====


def register_skills_commands(sub: Any) -> None:
    from muse.skills import cli as skills_cli

    skills_cli.register(sub, _env)


# ===== END skills =====
# ===== BEGIN records: `muse records`, and the record `muse pause` / `muse resume` leave (logic lives in muse/records/) =====


def _record_home_event(action: str, summary: str) -> None:
    """Append a record of something the person did to this home. Never raises: the action itself
    has already happened and must not fail because the record could not be written."""
    try:
        from muse.records import commands

        commands.note_home_event(_env(), action, summary)
    except Exception as exc:  # noqa: BLE001  (a records failure must never stop pause/resume)
        _err(f"Note: no record was written ({type(exc).__name__}).")  # the type only: never the message or a path


def cmd_records(args: argparse.Namespace) -> int:
    try:
        from muse.records import commands
    except ImportError as exc:
        _err(f"Error: records need the `cryptography` package ({type(exc).__name__}).")
        return 1
    try:
        if args.records_command == "verify":
            return commands.verify(_env(), as_json=args.json)
        if args.records_command == "seal":
            return commands.seal(_env())
        if args.records_command == "list":
            return commands.list_records(_env(), args.since, args.action, args.cost_only, args.json)
        if args.records_command == "export":
            return commands.export_file(_env(), args.file, args.since, args.action, args.cost_only, args.redact_summaries)
        return commands.check_file(args.file, args.device, as_json=args.json)
    except ImportError as exc:  # cryptography is imported on first signature
        _err(f"Error: records need the `cryptography` package ({type(exc).__name__}).")
        return 1


def register_records_commands(sub: Any) -> None:
    r = sub.add_parser("records", help="what was done in your name on this computer: list, check, export")
    rsub = r.add_subparsers(dest="records_command", required=True)
    v = rsub.add_parser("verify", help="check every record and the last seal")
    v.add_argument("--json", action="store_true")
    rsub.add_parser("seal", help="sign a checkpoint over every record so far")

    def filters(p: Any) -> None:
        p.add_argument("--since", metavar="WHEN", help="7d, 36h, 2026-11-01 or 2026-11-01T09:00")
        p.add_argument("--action", metavar="NAME", help="one action (files.write) or a family (files)")
        p.add_argument("--cost-only", action="store_true", help="only records that cost money")

    ls = rsub.add_parser("list", help="records, newest first")
    filters(ls)
    ls.add_argument("--json", action="store_true")
    ex = rsub.add_parser("export", help="write a file someone else can check (never overwrites)")
    ex.add_argument("file", metavar="FILE")
    filters(ex)
    ex.add_argument("--redact-summaries", action="store_true", help="leave the summary text out; the file still checks")
    cf = rsub.add_parser("check-file", help="check an export, yours or someone else's")
    cf.add_argument("file", metavar="FILE")
    cf.add_argument("--device", metavar="KEY", help="the device key you expect (64 hex characters)")
    cf.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_records)


# ===== END records =====
# ===== BEGIN permissions: `muse permissions list|show|issue|narrow|check|revoke` (own block; logic lives in muse/permissions/) =====
# Signed permission tokens (docs/spec/interfaces/permission-token.md). This block only
# exercises them by hand: nothing else in Muse enforces these tokens yet.


class _PermUsage(Exception):
    """An argument of `muse permissions` that cannot be read."""


def _perm_time(text: str) -> int:
    """`2026-11-30` or `2026-11-30T12:00:00Z` (no zone means UTC) as seconds since 1970."""
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        raise _PermUsage(f"{text!r} is not a date (use 2026-11-30 or 2026-11-30T12:00:00Z)") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int(when.timestamp())


def _perm_fields(text: str, shapes: dict[int, Any], what: str, example: str) -> Any:
    """Split `a:b:c` and build a value with the function registered for that many parts."""
    parts = text.split(":")
    make = shapes.get(len(parts))
    try:
        if make is None:
            raise ValueError
        return make(*parts)
    except ValueError:
        raise _PermUsage(f"{what} {text!r} is not understood (for example {example})") from None


def _perm_resource(text: str) -> Any:
    from muse import permissions as P

    kind, sep, value = text.partition(":")
    if not sep:
        raise _PermUsage(f"resource {text!r} is not understood (for example folder:/Documents/Work or host:api.example.com)")
    return P.Resource(kind, value)


def _perm_confirm(text: str) -> Any:
    from muse import permissions as P

    return _perm_fields(text, {1: lambda rule: P.Confirm(rule), 4: lambda rule, unit, dec, n: P.Confirm(rule, unit, int(dec), int(n))},
                        "confirmation rule", "each_write, never, or over_amount:TWD:2:500")


def _perm_narrowing(args: argparse.Namespace, now: int) -> Any:
    from muse import permissions as P

    limit = {3: lambda kind, period, n: P.NarrowLimit(kind, "", period, int(n)), 4: lambda kind, unit, period, n: P.NarrowLimit(kind, unit, period, int(n))}
    cap = {2: lambda kind, n: P.Cap(kind, "", int(n)), 3: lambda kind, unit, n: P.Cap(kind, unit, int(n))}
    not_after = _perm_time(args.until) if args.until else (now + 60 * args.minutes if args.minutes else None)
    return P.Narrowing(
        actions=tuple(args.action or ()),
        resources=tuple(_perm_resource(r) for r in args.resource or ()),
        limits=tuple(_perm_fields(l, limit, "limit", "spend:TWD:day:500 or requests:day:10") for l in args.limit or ()),
        caps=tuple(_perm_fields(c, cap, "cap", "spend:TWD:12000") for c in args.cap or ()),
        not_before=_perm_time(args.start) if args.start else None,
        not_after=not_after,
        privacy=args.privacy,
        confirm=_perm_confirm(args.confirm) if args.confirm else None,
        job=args.job, tool=args.tool, card=args.card,
    )


def _perm_print(mandate: Any, lang: str, indent: str = "") -> None:
    from muse import permissions as P

    text = P.describe(mandate, lang)
    print(indent + text.sentence)
    for line in text.lines:
        print(f"{indent}  {line}")


def _perm_state(mandate: Any, revoked: Any, now: int) -> str:
    from muse import permissions as P

    if any(revoked.contains(i) for i in P.revocation_ids(mandate)):
        return "stopped"
    if now > mandate.not_after():
        return "expired"
    return "not yet valid" if now < mandate.not_before() else "active"


def _perm_yes(args: argparse.Namespace, question: str) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        _err("Error: this needs your confirmation; run it in a terminal, or pass --yes")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def cmd_permissions(args: argparse.Namespace) -> int:
    from muse import permissions as P
    from muse.permissions import store as pstore

    try:
        return _cmd_permissions(args, P, pstore)
    except P.PermissionError as exc:
        _err(f"Error: {exc.detail or exc.code} ({exc.code})")
        return 1
    except _PermUsage as exc:
        _err(f"Error: {exc}")
        return 2


def _cmd_permissions(args: argparse.Namespace, P: Any, pstore: Any) -> int:
    keys_ = pstore.FileKeySource(environ=_env())
    store = pstore.PermissionStore(environ=_env())
    now = _perm_time(args.at) if getattr(args, "at", None) else int(time.time())
    lang = getattr(args, "lang", "en")
    cmd = args.permissions_cmd

    if cmd == "issue":
        if not keys_.exists():
            if not args.new_key:
                _err(f"Error: no root key at {keys_.path}. Pass --new-key to create one (keep that file safe: it signs every permission).")
                return 1
            print(f"Created a root key at {keys_.path} (public key {keys_.create()})")
        root = keys_.load()
        limit = {3: lambda kind, period, n: P.Limit(kind, "", 0, period, int(n)),
                 5: lambda kind, unit, dec, period, n: P.Limit(kind, unit, int(dec), period, int(n))}
        start = _perm_time(args.start) if args.start else now
        draft = P.propose_from_fields(P.Draft(
            issuer=P.public_key_text(root), holder=args.holder, actions=tuple(args.action), routine=tuple(args.routine or ()),
            resources=tuple(_perm_resource(r) for r in args.resource),
            limits=tuple(_perm_fields(l, limit, "limit", "spend:TWD:2:day:2000 or requests:day:200") for l in args.limit or ()),
            purpose_text=args.purpose, purpose_code=args.purpose_code, issued_at=now, not_before=start,
            not_after=_perm_time(args.until) if args.until else start + 86400 * args.days,
            confirm=_perm_confirm(args.confirm), privacy=args.privacy, period_zone=args.zone))
        mandate = P.issue(draft, root)  # signed in memory only; nothing is kept unless the person agrees
        _perm_print(mandate, lang)
        if not _perm_yes(args, "Issue this permission?"):
            print("Not issued.")
            return 1
        print(f"Issued {store.save(mandate)} (id {mandate.id})")
        return 0

    if cmd == "list":
        root = keys_.public() if keys_.exists() else None
        revoked = store.revocations(root) if root else P.RevocationList()
        rows = []
        for name in store.names():
            try:
                mandate = store.load(name, root or "")
            except P.PermissionError as exc:
                rows.append({"name": name, "state": f"unreadable ({exc.code})"})
                continue
            rows.append({"name": name, "id": mandate.id, "state": _perm_state(mandate, revoked, now), "holder": mandate.draft.holder,
                         "narrowed": len(mandate.levels), "until": P.format_time(mandate.not_after()), "sentence": P.describe(mandate, lang).sentence})
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        elif not rows:
            print("No permissions yet. Try: muse permissions issue --holder helper:vm --action files.read --resource folder:/Documents/Work --new-key")
        for row in [] if args.json else rows:
            print(f"{row['name']}  [{row['state']}]" + (f"  narrowed {row['narrowed']}x" if row.get("narrowed") else ""))
            if "sentence" in row:
                print(f"  {row['sentence']}")
        return 0

    root = keys_.public()
    name = store.resolve(args.name)
    mandate = store.load(name, root)
    revoked = store.revocations(root)

    if cmd == "show":
        print(f"{name}  [{_perm_state(mandate, revoked, now)}]  id {mandate.id}")
        _perm_print(mandate, lang, "  ")
        print(f"  blocks: {len(mandate.levels) + 1}, {mandate.size()} bytes; revocation ids: " + ", ".join(i[:16] for i in P.revocation_ids(mandate)))
        if args.token:
            print(mandate.to_base64())
        return 0

    if cmd == "narrow":
        child = P.narrow(mandate, _perm_narrowing(args, now))
        _perm_print(child, lang)
        print(f"Saved {store.save(child)} (narrowed from {name}; no key was needed)")
        return 0

    if cmd == "check":
        amount = {2: lambda kind, n: P.Amount(kind, "", int(n)), 3: lambda kind, unit, n: P.Amount(kind, unit, int(n))}
        used = {3: lambda kind, period, n: ((kind, "", period), int(n)), 4: lambda kind, unit, period, n: ((kind, unit, period), int(n))}
        request = P.Request(
            holder=args.holder or mandate.draft.holder, action=args.action, resource=_perm_resource(args.resource),
            privacy=args.privacy or mandate.privacy(),
            amounts=tuple(_perm_fields(a, amount, "amount", "spend:TWD:120") for a in args.amount or ()),
            job=args.job, tool=args.tool, card=args.card)
        usage = dict(_perm_fields(u, used, "usage figure", "spend:TWD:day:0") for u in args.used or ())
        spent = store.spent()
        decision = (P.check_and_record if args.record else P.check)(mandate, request, usage, revoked, spent, now)
        if args.record and decision.allowed:
            store.save_spent(spent)
        print({"allow": "allow", "deny": f"deny: {decision.detail}", "needs_confirmation": f"needs confirmation: {decision.detail}"}[decision.kind])
        return 0 if decision.allowed else 3

    if cmd == "revoke":
        ids = P.revocation_ids(mandate)
        ident = ids[-1] if args.only_this_copy else ids[0]
        scope = "this narrowed copy and anything narrowed from it" if args.only_this_copy and len(ids) > 1 else "this permission and every copy narrowed from it"
        _perm_print(mandate, lang)
        if not _perm_yes(args, f"Stop {scope}?"):
            print("Not stopped.")
            return 1
        revoked.add(ident, now, keys_.load())
        store.save_revocations(revoked)
        print(f"Stopped. The revocation list now has {len(revoked)} entr{'y' if len(revoked) == 1 else 'ies'} ({store.dir / 'revocations.json'}).")
        return 0
    return 2


def add_permissions_parser(sub: Any) -> None:
    p = sub.add_parser("permissions", help="signed permissions: issue, narrow, check, stop (not yet enforced by the router or skills)")
    psub = p.add_subparsers(dest="permissions_cmd", required=True)
    lang = dict(choices=["en", "zh-TW"], default="en", help="zh-TW: Traditional Chinese")

    def add(name: str, helptext: str) -> Any:
        s = psub.add_parser(name, help=helptext)
        s.add_argument("--lang", **lang)
        s.add_argument("--at", metavar="TIME", help="pretend it is this time (for trying things out)")
        s.set_defaults(func=cmd_permissions)
        return s

    ls = add("list", "every stored permission, as a sentence")
    ls.add_argument("--json", action="store_true")
    sh = add("show", "one permission with its limits")
    sh.add_argument("name", help="a stored name, or the start of one")
    sh.add_argument("--token", action="store_true", help="also print the token itself")
    i = add("issue", "write a permission, read it back as a sentence, and sign it once you agree")
    i.add_argument("--holder", required=True, help="who may use it, e.g. helper:vm, app:reader, device:phone-1, clinician:dr-lin")
    i.add_argument("--action", action="append", required=True, help="e.g. files.read, files.write, mail.draft, llm.call, pay.spend (repeat)")
    i.add_argument("--resource", action="append", required=True, help="kind:value, e.g. folder:/Documents/Work, host:api.example.com, calendar:* (repeat)")
    i.add_argument("--limit", action="append", help="spend:TWD:2:day:2000 (unit, decimals, period, minor units) or requests:day:200 (repeat)")
    i.add_argument("--routine", action="append", help="an action that need not be confirmed each time (repeat)")
    i.add_argument("--purpose", default="", help="why, in your own words")
    i.add_argument("--purpose-code", default="", help="a standard purpose code, if one applies")
    i.add_argument("--from", dest="start", metavar="TIME", help="start (default: now)")
    i.add_argument("--until", metavar="TIME", help="end (default: --days after the start)")
    i.add_argument("--days", type=int, default=30, help="how long it lasts when --until is not given (default 30)")
    i.add_argument("--confirm", default="each_write", help="each_write (default), never, or over_amount:TWD:2:500")
    i.add_argument("--privacy", default="private", choices=["local", "private", "standard", "public"], help="least protective class it may send data under (default private)")
    i.add_argument("--zone", default="UTC", help="time zone days and months are counted in (default UTC)")
    i.add_argument("--new-key", action="store_true", help="create the root key if there is none yet")
    i.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    n = add("narrow", "make a narrower copy of a permission (needs no key)")
    n.add_argument("name")
    n.add_argument("--action", action="append")
    n.add_argument("--resource", action="append")
    n.add_argument("--limit", action="append", help="a lower limit: spend:TWD:day:500 or requests:day:10")
    n.add_argument("--cap", action="append", help="the most one request may carry: spend:TWD:12000")
    n.add_argument("--from", dest="start", metavar="TIME")
    n.add_argument("--until", metavar="TIME")
    n.add_argument("--minutes", type=int, help="end this many minutes from now")
    n.add_argument("--privacy", choices=["local", "private", "standard", "public"])
    n.add_argument("--confirm", help="each_write or over_amount:TWD:2:100")
    n.add_argument("--job", help="only for this job id")
    n.add_argument("--tool", help="only for this one tool call (needs --until or --minutes)")
    n.add_argument("--card", help="only for this one approval card (needs --until or --minutes)")
    c = add("check", "would this request be allowed? exit 0 allow, 3 otherwise")
    c.add_argument("name")
    c.add_argument("--action", required=True)
    c.add_argument("--resource", required=True, help="kind:value")
    c.add_argument("--holder", help="who is asking (default: the permission's holder)")
    c.add_argument("--privacy", choices=["local", "private", "standard", "public"], help="class the request is handled under (default: the permission's)")
    c.add_argument("--amount", action="append", help="spend:TWD:120 or requests:1 (repeat)")
    c.add_argument("--used", action="append", help="used so far this period: spend:TWD:day:0 or requests:day:3 (repeat)")
    c.add_argument("--job")
    c.add_argument("--tool")
    c.add_argument("--card")
    c.add_argument("--record", action="store_true", help="if allowed, remember single-use ids so the same approval cannot be used twice")
    r = add("revoke", "stop a permission and every copy narrowed from it")
    r.add_argument("name")
    r.add_argument("--only-this-copy", action="store_true", help="stop only this narrowed copy (and anything narrowed from it)")
    r.add_argument("--yes", action="store_true", help="do not ask for confirmation")


# ===== END permissions =====


# ===== BEGIN host adapter: `muse mcp serve`, `muse hosts list|setup|show|check` (own block; logic lives in muse/mcp/) =====


def register_host_commands(sub: Any) -> None:
    from muse.mcp import cli as host_cli

    host_cli.register(sub, _env, _base_v1)


# ===== END host adapter =====


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
