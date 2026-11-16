#!/usr/bin/env python3
"""Check a running Muse proxy against the owner's REAL provider keys, with a hard cap on what it spends.

Run it on your own machine, after `muse up`. It talks to the local proxy only.
Keys are read by the proxy; this script reads them only to learn which providers
are configured and never copies one anywhere.

What it sends (at most, per provider, and in total) is printed before anything is
called, and you are asked to confirm unless you pass --yes. Every call is tiny:
a prompt of a few words and max_tokens=16.

What it writes: `$MUSE_HOME/real-key-report.json`. The report holds NO key, NO
prompt and NO response text. It holds names, numbers, status codes, the `x-muse-*`
headers, rate-limit header names and parsed values, and (with --probe-limits) the
SHAPE of an error body with every piece of free text replaced by its length.
tests/integration/test_real_key_check.py runs this script against the fake-upstream
harness with canary values and fails if any of them appear in the report.

Exit code: 0 when no check failed, 1 when one did, 2 on a usage problem.
"""

from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from wardmuse.envutil import envget, envhas

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import render_config  # noqa: E402

from wardmuse import __version__, apps, keys, paths, quota  # noqa: E402

PROMPT = "Reply with the single word: ok"  # fixed, so the report can be proven free of it
MAX_OUT_TOKENS = 16
TOKENS_PER_CALL = 48  # generous upper bound: ~12 in, 16 out, rounding
TIMEOUT = 60.0
DEFAULT_BURST = 25
MAX_BURST = 60
SAFE_VALUE = re.compile(r"^[0-9A-Za-z.:+\-]{1,40}$")
KEEP_STR_KEYS = {"code", "status", "type", "@type", "quotaMetric", "quotaId", "reason", "domain", "param"}
SAFE_ENUM = re.compile(r"^[A-Za-z0-9_.@/\-]{1,120}$")
SAFE_MUSE_HEADER = re.compile(r"^[A-Za-z0-9_.@/:,;>\-]{1,120}$")  # ; and > are in x-muse-min-output: raised:16->256
KEYWORDS = ("per day", "per minute", "per month", "per hour", "try again in", "quota", "rate limit", "trial", "free-models-per-day",
            "free-models-per-min", "daily", "monthly", "resource_exhausted", "tokens per", "requests per", "retry")


class Fail(Exception):
    pass


# ---------------------------------------------------------------- redaction


def shape(value: Any, key: str = "", depth: int = 0) -> Any:
    """The structure of a JSON error body with free text removed.

    Strings become "<text:LENGTH>" unless the key is a known enumeration field
    holding a plain identifier (quotaId, code, status...). Numbers, booleans and
    null are kept; long lists are cut to 3 items so the shape stays readable.
    """
    if depth > 8:
        return "<deep>"
    if isinstance(value, dict):
        return {str(k)[:60]: shape(v, str(k), depth + 1) for k, v in list(value.items())[:30]}
    if isinstance(value, list):
        return [shape(v, key, depth + 1) for v in value[:3]] + ([f"<+{len(value) - 3} more>"] if len(value) > 3 else [])
    if isinstance(value, str):
        if key in KEEP_STR_KEYS and SAFE_ENUM.match(value):
            return value
        return f"<text:{len(value)}>"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return "<other>"


def keywords_in(text: str) -> list[str]:
    """Which parser-relevant phrases the error text contains (from a fixed list, not copied text)."""
    low = text.lower()
    return [k for k in KEYWORDS if k in low]


def retry_hint(text: str) -> str | None:
    match = re.search(r"try again in ([0-9hms.]+)", text.lower())
    return match.group(1).rstrip(".") if match else None


def safe_headers(headers: Mapping[str, str]) -> dict[str, Any]:
    """x-muse-*, rate-limit and retry-after headers: name always, value only when it is plainly a number or time."""
    out: dict[str, Any] = {}
    for name, value in sorted(headers.items()):
        low = name.lower()
        if low.startswith("x-muse-") or "ratelimit" in low or low in ("retry-after", "x-ratelimit-reset"):
            out[low] = value if SAFE_VALUE.match(str(value)) or low.startswith("x-muse-") and SAFE_MUSE_HEADER.match(str(value)) else "<redacted>"
    return out


def all_header_names(headers: Mapping[str, str]) -> list[str]:
    return sorted(k.lower() for k in headers)


# ---------------------------------------------------------------- http


class Client:
    def __init__(self, url: str, master: str | None) -> None:
        self.url = url.rstrip("/")
        self.master = master
        self.requests = 0
        self.lock = threading.Lock()

    def call(self, path: str, body: dict[str, Any], token: str | None = "", headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        bearer = self.master if token == "" else token
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        if bearer:
            hdrs["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(f"{self.url}{path}", data=json.dumps(body).encode(), headers=hdrs, method="POST")
        with self.lock:
            self.requests += 1
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
        except urllib.error.HTTPError as err:
            return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()
        except (urllib.error.URLError, OSError, ValueError):
            return 0, {}, b""

    def alive(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/health/liveliness", timeout=5) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError, ValueError):
            return False


def request_for(mode: str, model: str) -> tuple[str, dict[str, Any]]:
    if mode == "embedding":
        return "/v1/embeddings", {"model": model, "input": "ok"}
    if mode == "rerank":
        return "/v1/rerank", {"model": model, "query": "ok", "documents": ["a", "b"], "top_n": 1}
    return "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": PROMPT}], "max_tokens": MAX_OUT_TOKENS}


def answer_state(mode: str, raw: bytes) -> str:
    """What a 200 carried: `answer`, `cut_short`, or `empty`. Never keeps the answer.

    `cut_short` is a chat reply with no visible text because the model spent the
    few allowed output tokens on reasoning (gpt-oss and other reasoning models
    do this at max_tokens=16). The provider accepted the key and ran the model,
    which is all this script checks, so it counts as a pass with a note.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return "empty"
    if not isinstance(data, dict):
        return "empty"
    if mode == "embedding":
        return "answer" if data.get("data") else "empty"
    if mode == "rerank":
        return "answer" if data.get("results") else "empty"
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError, AttributeError):
        return "empty"
    if not isinstance(message, dict):
        return "empty"
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return "answer"
    reasoning = any(isinstance(message.get(k), str) and message[k].strip() for k in ("reasoning_content", "reasoning"))
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    details = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    spent = details.get("reasoning_tokens")
    if choice.get("finish_reason") == "length" or reasoning or (isinstance(spent, int) and spent > 0):
        return "cut_short"
    return "empty"


def body_ok(mode: str, raw: bytes) -> bool:
    """Did a 200 show that the provider ran the request? (A reply cut short by the token cap counts.)"""
    return answer_state(mode, raw) in ("answer", "cut_short")


CUT_SHORT_NOTE = f"no visible text: the model used all {MAX_OUT_TOKENS} output tokens on reasoning; the key and route work"


def floor_note(headers: Mapping[str, str]) -> str:
    """`; ` plus what the output floor did, when the response says x-muse-min-output; else nothing."""
    value = str(headers.get("x-muse-min-output") or "").strip()
    if not value:
        return ""
    return f"; output floor: max_tokens {value[len('raised:'):] if value.startswith('raised:') else value} for this model (x-muse-min-output)"


def provider_busy(status: int, headers: Mapping[str, str], raw: bytes) -> bool:
    """A 429 that came from the provider, not from Muse. The key was not rejected; the free tier is busy."""
    return status == 429 and "x-muse-ration" not in headers and b"[muse]" not in raw


def error_summary(status: int, headers: Mapping[str, str], raw: bytes) -> dict[str, Any]:
    text = raw.decode("utf-8", "replace")
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        parsed = None
    return {
        "status": status, "headers": safe_headers(headers), "header_names": all_header_names(headers),
        "body_shape": shape(parsed) if parsed is not None else f"<non-json:{len(text)}>",
        "keywords": keywords_in(text), "retry_hint": retry_hint(text), "muse_refusal": "[muse]" in text,
    }


def rate_limited_now(env: Mapping[str, str], provider: Mapping[str, Any], status: int, headers: Mapping[str, str], raw: bytes) -> bool:
    """The provider answered 429 to this very call.

    Either its 429 came straight back, or the proxy's one retry was answered by
    Muse's "blocked by a provider quota" message. In the second case the ledger
    holds a block set seconds ago, and its cause must be a rate limit: a block
    for a bad key or missing credit stays a failure.
    """
    if status != 429 or "x-muse-ration" in headers:
        return False
    if b"[muse]" not in raw:
        return True
    time.sleep(0.4)  # the ledger write happens just after the response
    block = block_decision(env, provider["upstream"], provider["id"], time.time())
    return bool(block) and block["cause"] == "rate-limit" and block["set_ago_s"] <= 30


# ---------------------------------------------------------------- plan


def build_plan(env: Mapping[str, str]) -> dict[str, Any]:
    """Which providers and aliases will be called, and the worst case for each."""
    catalog = render_config.load_catalog(Path(envget(env, "MUSE_CATALOG", "") or paths.CATALOG_PATH))
    local_deps = render_config.load_local_deployments(env)
    providers: list[dict[str, Any]] = []
    for provider in catalog["providers"]:
        pid = str(provider["id"])
        local = render_config.is_local(provider)
        if local:
            configured = bool(str(env.get(str(provider.get("base_env")), "")).strip())
            deployments = list(local_deps.get(pid, []))
        else:
            configured = keys.is_configured(str(provider["env"]), env)
            deployments = list(provider.get("deployments") or [])
        if not configured or not deployments:
            continue
        deployments = [d for d in deployments if d.get("mode", "chat") in ("chat", "embedding", "rerank")]
        if not deployments:
            continue
        dep = deployments[0]
        info = render_config._model_info(provider, {"mode": "chat", **dep}, str(dep["id"]))
        providers.append({
            "id": pid, "kind": "local" if local else "cloud", "tier": str(provider.get("tier", "unknown")),
            "deployment": str(dep["id"]), "mode": str(dep.get("mode", "chat")), "upstream": str(info.get("muse_upstream", "")),
            "deployments": [{"id": str(d["id"]), "aliases": list(d.get("aliases") or []), "price": bool(d.get("price"))} for d in deployments],
        })
    aliases = []
    for name, meta in catalog["aliases"].items():
        members = sorted({p["id"] for p in providers for d in p["deployments"] if name in d["aliases"]})
        if members:
            aliases.append({"name": name, "mode": str(meta.get("mode", "chat")), "providers": members})
    return {"providers": providers, "aliases": aliases}


def worst_case(plan: Mapping[str, Any], extra_first: int, burst: int, probe_provider: str | None, paid_probe: bool) -> dict[str, int]:
    """Upper bound of requests that can reach each provider (an alias call may land on any member)."""
    out = {p["id"]: 1 for p in plan["providers"]}
    for alias in plan["aliases"]:
        for member in alias["providers"]:
            out[member] += 1
    if plan["providers"]:
        out[plan["providers"][0]["id"]] += extra_first  # interactive app call + the call a background app should not make
    if probe_provider in out:
        out[probe_provider] += burst
    if paid_probe:
        for p in plan["providers"]:
            if p["tier"] in ("paid", "metered"):
                out[p["id"]] += 1
    return out


# ---------------------------------------------------------------- the checks


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.data: dict[str, Any] = {"providers": [], "aliases": [], "probes": []}

    def add(self, cid: str, title: str, result: str, detail: str = "") -> None:
        self.checks.append({"id": cid, "title": title, "result": result, "detail": detail})
        mark = {"pass": "✓", "fail": "✗", "skip": "-", "info": "·"}[result]
        print(f"  {mark} {title}" + (f"  ({detail})" if detail else ""))


def observed_limits(env: Mapping[str, str], upstream: str, provider: str, now: float) -> list[dict[str, Any]]:
    try:
        from wardmuse.ledger import Ledger

        ledger = Ledger(environ=env)
        rows = [*ledger.limits(upstream), *ledger.limits(f"provider:{provider}")]
    except Exception:
        return []
    return [
        {"window": r["window"], "metric": r["metric"], "limit": r["limit"], "remaining": r["remaining"],
         "resets_in_s": None if r["reset_at"] is None else round(r["reset_at"] - now)}
        for r in rows
    ]


def block_decision(env: Mapping[str, str], upstream: str, provider: str, now: float) -> dict[str, Any] | None:
    """What Muse decided after a 429, from its own ledger: kind and how long, never provider text."""
    try:
        from wardmuse.ledger import Ledger

        blocks = Ledger(environ=env).active_blocks(now)
    except Exception:
        return None
    hit = blocks.get(upstream) or blocks.get(f"provider:{provider}")
    if not hit:
        return None
    reason = str(hit.get("reason", "")).lower()  # Muse's own wording, never the provider's text
    return {"scope": "upstream" if upstream in blocks else "provider", "kind": hit["kind"], "blocked_for_s": round(hit["until"] - now),
            "set_ago_s": round(now - float(hit.get("set_at", now))),
            "cause": "key" if ("key" in reason or "payment" in reason) else "rate-limit"}


def run(args: argparse.Namespace, env: dict[str, str]) -> int:
    master = env.get(keys.MASTER_KEY, "").strip() or None
    client = Client(args.proxy_url, master)
    plan = build_plan(env)
    report = Report()
    probe_provider = args.provider if args.probe_limits else None
    if args.probe_limits and probe_provider not in {p["id"] for p in plan["providers"]}:
        print("--probe-limits needs --provider ID naming a configured provider. Configured: "
              + (", ".join(p["id"] for p in plan["providers"]) or "none"), file=sys.stderr)
        return 2
    burst = min(max(args.burst, 2), MAX_BURST)
    if not plan["providers"]:
        print("No provider is configured. Set a key (muse keys set GEMINI_API_KEY), run `muse up`, then run this again.", file=sys.stderr)
        return 2

    bound = worst_case(plan, 2, burst if probe_provider else 0, probe_provider, args.expect_paid_cap_zero)
    total = sum(bound.values())
    print("Muse real-key check: what this will send")
    print(f"  proxy: {client.url}")
    print(f"  at most {total} requests in all, each about {TOKENS_PER_CALL} tokens or fewer (max_tokens={MAX_OUT_TOKENS}): at most {total * TOKENS_PER_CALL} tokens.")
    for p in plan["providers"]:
        print(f"  {p['id']:<14} at most {bound[p['id']]:>2} requests, {bound[p['id']] * TOKENS_PER_CALL} tokens  ({p['tier']}; pinned call to {p['deployment']})")
    print("  Free tiers: this should cost nothing. A paid provider is billed only if it is called AND the daily paid cap allows it.")
    if args.probe_limits:
        print()
        print("  !! --probe-limits WILL CONSUME QUOTA. It sends up to "
              f"{burst} rapid requests to {probe_provider} until the provider answers HTTP 429.")
        print("  !! That can exhaust a free tier for the rest of the day (or month, for trial keys).")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not a terminal: pass --yes to confirm.", file=sys.stderr)
            return 2
        if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was sent.")
            return 0
    print()

    # 1. proxy
    if not client.alive():
        report.add("proxy", "Proxy reachable", "fail", f"nothing answered at {client.url}; run `muse up`")
        return finish(report, env, args, client, plan)
    report.add("proxy", "Proxy reachable", "pass")

    # 2. one pinned call per provider
    for p in plan["providers"]:
        path, body = request_for(p["mode"], p["deployment"])
        t0 = time.time()
        status, headers, raw = client.call(path, body)
        entry: dict[str, Any] = {
            "id": p["id"], "tier": p["tier"], "kind": p["kind"], "deployment": p["deployment"], "mode": p["mode"], "status": status,
            "latency_ms": round((time.time() - t0) * 1000), "answer_ok": status == 200 and body_ok(p["mode"], raw),
            "answer_state": answer_state(p["mode"], raw) if status == 200 else None,
            "headers": safe_headers(headers),
        }
        if status == 200:
            time.sleep(0.4)  # the ledger write happens just after the response
            entry["observed_limits"] = observed_limits(env, p["upstream"], p["id"], time.time())
            names = sorted(k for k in headers if "ratelimit" in k)
            entry["ratelimit_header_names"] = names
        else:
            entry["error"] = error_summary(status, headers, raw)
        report.data["providers"].append(entry)
        ok = status == 200 and entry["answer_ok"]
        served = headers.get("x-muse-provider")
        if ok and served in (None, p["id"]):
            report.add(f"provider:{p['id']}", f"{p['id']}: one tiny call to {p['deployment']}", "pass",
                       f"{entry['latency_ms']} ms" + (f"; {CUT_SHORT_NOTE}" if entry["answer_state"] == "cut_short" else "")
                       + floor_note(headers))
        elif rate_limited_now(env, p, status, headers, raw):
            # Not a failure of the key or of Muse: a shared free tier turned the call away. Record what Muse decided.
            entry["busy"] = True
            entry["block"] = block_decision(env, p["upstream"], p["id"], time.time())
            report.add(f"provider:{p['id']}", f"{p['id']}: one tiny call to {p['deployment']}", "info",
                       "HTTP 429 from the provider: rate-limited right now, the key was not rejected. Run the check again later.")
        else:
            report.add(f"provider:{p['id']}", f"{p['id']}: one tiny call to {p['deployment']}", "fail", f"HTTP {status}" + (f", served by {served}" if served else ""))
        if ok:
            if entry["ratelimit_header_names"]:
                report.add(f"headers:{p['id']}", f"{p['id']}: rate-limit headers", "info", ", ".join(entry["ratelimit_header_names"]))
            else:
                report.add(f"headers:{p['id']}", f"{p['id']}: rate-limit headers", "info", "none returned; limits stay unknown until a 429")
            if "x-muse-app" not in headers:
                report.add(f"muse-headers:{p['id']}", f"{p['id']}: x-muse-* response headers", "fail", "x-muse-app missing: is the hook loaded?")

    # 3. one call per alias
    for alias in plan["aliases"]:
        path, body = request_for(alias["mode"], alias["name"])
        status, headers, raw = client.call(path, body)
        ok = status == 200 and body_ok(alias["mode"], raw)
        state = answer_state(alias["mode"], raw) if status == 200 else None
        entry = {"name": alias["name"], "mode": alias["mode"], "status": status, "answer_ok": ok, "answer_state": state,
                 "headers": safe_headers(headers)}
        if status != 200:
            entry["error"] = error_summary(status, headers, raw)
        report.data["aliases"].append(entry)
        if ok:
            report.add(f"alias:{alias['name']}", f"alias {alias['name']}: one tiny call", "pass",
                       f"served by {headers.get('x-muse-deployment', '?')}" + (f"; {CUT_SHORT_NOTE}" if state == "cut_short" else "")
                       + floor_note(headers))
        elif provider_busy(status, headers, raw):
            entry["busy"] = True
            report.add(f"alias:{alias['name']}", f"alias {alias['name']}: one tiny call", "info",
                       "HTTP 429 from the providers behind it: rate-limited right now. Run the check again later.")
        else:
            report.add(f"alias:{alias['name']}", f"alias {alias['name']}: one tiny call", "fail",
                       f"HTTP {status}" if status != 200 else "HTTP 200 with an empty reply")

    # 4. privacy local with no local model
    has_local = any(p["kind"] == "local" for p in plan["providers"])
    if has_local:
        report.add("privacy-local", "privacy `local` is refused when no local model exists", "skip", "a local model is configured")
    else:
        chat = next((a for a in plan["aliases"] if a["mode"] == "chat"), None)
        if chat is None:
            report.add("privacy-local", "privacy `local` is refused when no local model exists", "skip", "no chat alias")
        else:
            path, body = request_for("chat", chat["name"])
            status, headers, raw = client.call(path, body, headers={"x-muse-privacy": "local"})
            refused = status == 403 and b"[muse]" in raw
            report.add("privacy-local", "privacy `local` is refused when no local model exists", "pass" if refused else "fail",
                       "HTTP 403, nothing sent upstream" if refused else f"HTTP {status}: it must be 403 with a [muse] message")

    # 5 and 6. an app token works; background is deferred right after interactive
    first = plan["providers"][0]
    cloud_first = next((p for p in plan["providers"] if p["kind"] == "cloud" and p["tier"] not in ("paid", "metered")), first)
    path, body = request_for(cloud_first["mode"], cloud_first["deployment"])
    created: list[str] = []
    try:
        for name in ("rkc-int", "rkc-bg"):
            apps.remove_app(name, env)
        _, tok_int = apps.add_app("rkc-int", "interactive", allow_paid=False, environ=env, note="real_key_check (temporary)")
        created.append("rkc-int")
        _, tok_bg = apps.add_app("rkc-bg", "background", allow_paid=False, environ=env, note="real_key_check (temporary)")
        created.append("rkc-bg")
        time.sleep(0.2)
        status, headers, raw = client.call(path, body, token=tok_int)
        who = headers.get("x-muse-app")
        report.add("app-token", "an app token works", "pass" if status == 200 and who == "rkc-int" else "fail",
                   f"HTTP {status}, x-muse-app={who}")
        time.sleep(0.5)  # let the ledger see the interactive call
        status, headers, raw = client.call(path, body, token=tok_bg)
        ration = headers.get("x-muse-ration", "")
        deferred = status == 429 and ration.startswith("defer:")
        report.add("background-yield", "a background app is deferred right after an interactive call", "pass" if deferred else "fail",
                   f"HTTP 429, {ration}, Retry-After {headers.get('retry-after', '?')}" if deferred
                   else f"HTTP {status}; expected 429 defer:yield (is MUSE_ACTIVE_WINDOW_S set to 0?)")
        report.data["background_yield"] = {"status": status, "ration": ration, "retry_after": headers.get("retry-after")}
    finally:
        for name in created:
            apps.remove_app(name, env)

    # 7. paid cap at zero
    paid = [(p, d) for p in plan["providers"] if p["tier"] in ("paid", "metered") for d in p["deployments"]]
    if not args.expect_paid_cap_zero:
        report.add("paid-cap-zero", "paid cap at 0 never reaches a paid provider", "skip",
                   "restart the proxy with MUSE_DAILY_PAID_CAP_USD=0 and run again with --expect-paid-cap-zero")
    elif not paid:
        report.add("paid-cap-zero", "paid cap at 0 never reaches a paid provider", "skip", "no paid provider has a key")
    else:
        leaked = []
        for p, d in paid:
            mode = p["mode"]
            path, body = request_for(mode, d["id"])
            status, headers, raw = client.call(path, body)
            if status == 200:
                leaked.append(d["id"])
        report.add("paid-cap-zero", "paid cap at 0 never reaches a paid provider", "fail" if leaked else "pass",
                   "a paid call SUCCEEDED: " + ", ".join(leaked) + ". Is the proxy really running with cap 0?" if leaked
                   else f"{len(paid)} paid deployment(s) refused")

    # 8. optional limit probe
    if probe_provider:
        probe_limits(args, env, client, plan, report, probe_provider, burst)
    return finish(report, env, args, client, plan)


def probe_limits(args: argparse.Namespace, env: Mapping[str, str], client: Client, plan: Mapping[str, Any], report: Report, pid: str, burst: int) -> None:
    p = next(x for x in plan["providers"] if x["id"] == pid)
    path, body = request_for(p["mode"], p["deployment"])
    print(f"  probing {pid}: up to {burst} rapid requests until HTTP 429 ...")
    results: list[tuple[int, dict[str, str], bytes]] = []
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            with client.lock:
                if len(results) >= burst:
                    return
                results.append((-1, {}, b""))
                slot = len(results) - 1
            outcome = client.call(path, body)
            results[slot] = outcome
            if outcome[0] == 429 and "x-muse-ration" not in outcome[1]:
                stop.set()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT * 2)
    real = [r for r in results if r[0] != -1]
    first = next((r for r in real if r[0] == 429 and "x-muse-ration" not in r[1]), None)
    entry: dict[str, Any] = {"provider": pid, "deployment": p["deployment"], "sent": len(real), "statuses": sorted({r[0] for r in real})}
    if first is None:
        entry["observed_429"] = False
        report.add("probe-limits", f"{pid}: observe a real 429", "info", f"no 429 in {len(real)} requests; the limit is higher than the burst")
    else:
        time.sleep(0.5)
        entry["observed_429"] = True
        entry["raw"] = error_summary(first[0], first[1], first[2])
        entry["muse_decision"] = block_decision(env, p["upstream"], pid, time.time())
        decided = entry["muse_decision"]
        report.add("probe-limits", f"{pid}: observe a real 429", "info",
                   f"after {len(real)} requests; Muse blocked it as {decided['kind']} for {decided['blocked_for_s']} s" if decided
                   else f"after {len(real)} requests; Muse did not block it (see the report)")
    report.data["probes"].append(entry)


def finish(report: Report, env: Mapping[str, str], args: argparse.Namespace, client: Client, plan: Mapping[str, Any]) -> int:
    failed = [c for c in report.checks if c["result"] == "fail"]
    out = {
        "schema": 1, "generated_at": quota.iso(time.time()), "muse_version": __version__, "proxy_requests_sent": client.requests,
        "checks": report.checks, "passed": not failed, **report.data,
    }
    path = Path(args.report) if args.report else paths.home(env) / "real-key-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(out, handle, indent=2)
        handle.write("\n")
    print()
    counts = {kind: sum(1 for c in report.checks if c["result"] == kind) for kind in ("pass", "info", "skip")}
    print(f"{counts['pass']} passed, {len(failed)} failed, {counts['info']} notes, {counts['skip']} skipped"
          + f"; {client.requests} requests sent.")
    print(f"Report (no keys, prompts or responses): {path}")
    return 1 if failed else 0


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Small, capped real-provider checks against a running local Muse proxy.")
    parser.add_argument("--proxy-url", default=None, help="default: MUSE_PROXY_URL or http://127.0.0.1:4000")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--report", metavar="PATH", help="default: $MUSE_HOME/real-key-report.json")
    parser.add_argument("--probe-limits", action="store_true", help="WILL CONSUME QUOTA: burst one provider until it answers 429")
    parser.add_argument("--provider", help="with --probe-limits: the provider id to burst")
    parser.add_argument("--burst", type=int, default=DEFAULT_BURST, help=f"with --probe-limits: most requests (default {DEFAULT_BURST}, at most {MAX_BURST})")
    parser.add_argument("--expect-paid-cap-zero", action="store_true",
                        help="you restarted the proxy with MUSE_DAILY_PAID_CAP_USD=0: check no paid provider answers")
    args = parser.parse_args(argv)
    env = dict(keys.export_env(environ))
    args.proxy_url = args.proxy_url or envget(env, "MUSE_PROXY_URL", "").strip() or f"http://127.0.0.1:{envget(env, 'MUSE_PORT', '').strip() or '4000'}"
    return run(args, env)


if __name__ == "__main__":
    raise SystemExit(main())
