"""The parts of the home API the app's screens need: Ask, Chores, Services, Settings, For apps.

Kept beside muse/home/api.py, which only lists these routes in its table (`ROUTE_SPECS`
at the end of this file). The same rules hold as there: fixed plain error sentences,
nothing key-shaped in a reply, a record of what was done, the scope checked before the
handler runs.

What a handler acts on. Everything that touches the outside (the router, a service's
key check, the proxy's restart, the supply-chain check, the settings file) goes through
one object, `AppBackend`, found with `backend(home)`. The fake home swaps in a subclass
with canned behaviour (muse/home/app_fake.py), so the fake speaks exactly this API.

Not built here, and said so in the replies:
  * Streaming. `POST /v1/ask` answers when the whole answer is there.
  * Helper memory. `GET/DELETE /v1/memory` answer honestly that nothing is remembered.
  * The recovery key. `GET /v1/about` says `features.recovery: false` until the vault is wired in.
  * A limit raised "for today only" is held by the home for its own requests; the router's
    own cap is a setting it reads when it starts (see muse/home/app_money.py).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from muse.home import app_ask, app_connect, app_money

log = logging.getLogger("muse.home")


class _Api:
    """muse.home.api, looked up when used: that module lists this one's routes, so neither imports the other at the top."""

    def __getattr__(self, name: str) -> Any:
        from muse.home import api as real

        return getattr(real, name)


api = _Api()

LANGS = ("en", "zh-TW")
ASK_TASKS = ("light", "everyday", "heavy", "frontier")
MAX_ATTACHMENTS = 8
SKILL_WAIT_S = 90.0  # how long POST /v1/ask waits for the files skill before answering `pending`
FILES_SKILL = "files-answer"
QUESTION_TITLE = "question_for_later"  # the fixed title of a plain question: never its words
_HEX16 = re.compile(r"[0-9a-f]{16}\Z")
_CHORE_ID = re.compile(r"[0-9a-f]{8}\Z")
# Settings a person changes from the app, and whether the router must start again to see the change.
RESTART_SETTINGS = frozenset({"daily_limit", "default_privacy"})

_NONE = type(None)
_backend_lock = threading.Lock()


def _iso(ts: float | None) -> str | None:
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ================================================================ the backend


class AppBackend:
    """What these handlers do to the world. One per home; the fake home has its own subclass."""

    def __init__(self, home: Any) -> None:
        self.home = home
        self.conversations = app_ask.ConversationStore(home.environ, clock=home.clock)
        self.flows = app_connect.Flows(self._flow_key, clock=home.clock)
        self._lock = threading.Lock()
        self._raised: tuple[str, Decimal] | None = None  # (local day, the limit in dollars for that day only)
        self._chore_threads: dict[str, threading.Thread] = {}
        self._restart_pending = False

    # ---- settings

    def env(self) -> dict[str, str]:
        return dict(self.home.settings_env())

    def write_setting(self, name: str, value: str) -> None:
        """One non-secret setting into the home's settings file (`.env`), the file `Home.settings_env` reads."""
        from muse import keys

        keys.set_key(name, value, backend="dotenv", force=True, environ=self.home.environ)

    # ---- money

    def today(self) -> str:
        from muse.ration import resolve_tz

        zone = resolve_tz(str(self.env().get("MUSE_TZ", "") or "").strip() or None)
        return datetime.fromtimestamp(self.home.clock(), zone).strftime("%Y-%m-%d")

    def limit_today_usd(self) -> Decimal:
        usd, _ = app_money.limit_usd(self.env())
        with self._lock:
            if self._raised is not None and self._raised[0] == self.today():
                return max(usd, self._raised[1])
        return usd

    def raise_today(self, usd: Decimal) -> None:
        with self._lock:
            self._raised = (self.today(), usd)

    def spent_today_usd(self) -> Decimal:
        try:
            paid = self.home.snapshot().get("paid") or {}
        except Exception:
            return Decimal("0")
        return Decimal(str(paid.get("spent_usd", 0) or 0)) + Decimal(str(paid.get("held_usd", 0) or 0))

    # ---- the router

    def router_send(self, messages: list[dict[str, str]], *, privacy: str | None, task: str | None, allow_paid: bool,
                    max_tokens: int = app_ask.DEFAULT_MAX_TOKENS) -> app_ask.RouterReply:
        from muse import keys, queue

        name = app_ask.ASK_PAID_APP if allow_paid else app_ask.ASK_APP
        env = self.env()
        merged = keys.export_env(self.home.environ)  # holds every stored key for a moment: one name is taken, the rest dropped
        env[app_ask.TOKEN_VARS[name]] = str(merged.get(app_ask.TOKEN_VARS[name], "") or "")
        del merged
        try:
            token = app_ask.ensure_app(name, env)
        except Exception as exc:
            log.warning("ask: the home's own app could not be prepared (%s)", type(exc).__name__)
            return app_ask.RouterReply(0)
        reply = app_ask.send(queue.proxy_url(env), token, messages, privacy=privacy, task=task, allow_paid=allow_paid, max_tokens=max_tokens)
        if reply.status == 401 and "x-muse-privacy" not in reply.headers:
            # The router does not know the token (the app list changed under us): issue a new one, once.
            try:
                env[app_ask.TOKEN_VARS[name]] = ""
                token = app_ask.ensure_app(name, env)
            except Exception:
                return reply
            reply = app_ask.send(queue.proxy_url(env), token, messages, privacy=privacy, task=task, allow_paid=allow_paid,
                                 max_tokens=max_tokens)
        return reply

    # ---- chores

    def chore_env(self) -> Mapping[str, str]:
        return self.home.run_env()

    def chore_store(self) -> Any:
        from muse.skills.schedule import ChoreStore

        return ChoreStore(self.home.environ, self.home.clock)

    def run_chore(self, store: Any, run: Any) -> None:
        """Do one claimed run now. Its result is a proposed file, so a "Save a file" card is made (muse/skills/chores.py)."""
        from muse.skills import chores

        chores.run_one(store, run, self.chore_env(), deliver="card", requeue=False)

    def start_chore_run(self, chore_id: str) -> Any:
        store = self.chore_store()
        queued = store.add_manual_run(chore_id)
        claimed = store.claim(store.clock(), f"home-{queued.id}", queued.id)
        if claimed is None:
            raise api.ApiError("conflict", "That chore is already running. Wait for it to finish.")

        def work() -> None:
            try:
                self.run_chore(store, claimed)
            except Exception as exc:
                log.error("chores: a run could not be finished (%s)", type(exc).__name__)
                try:
                    store.settle(claimed.id, status="failed", summary="it could not run", finished_at=store.clock(), runner=None)
                except Exception:
                    pass
            finally:
                self._chore_threads.pop(claimed.id, None)

        thread = threading.Thread(target=work, name=f"chore-run-{claimed.id}", daemon=True)
        self._chore_threads[claimed.id] = thread
        thread.start()
        return claimed

    def wait_chore(self, run_id: str, timeout: float = 30.0) -> None:
        thread = self._chore_threads.get(run_id)
        if thread is not None:
            thread.join(timeout)

    # ---- services

    def test_key(self, provider: Any) -> dict[str, Any]:
        """ONE request that checks the stored key and sends no prompt: the catalogue's own key test (muse/connect.py)."""
        from muse import connect, keys

        conn = provider.connection
        spec = conn.test if conn else None
        if not spec:
            return {"result": "no_test", "status": None}
        key = str(keys.export_env(self.home.environ).get(str(provider.env), "") or "").strip()
        if not key:
            return {"result": "no_key", "status": None}
        auth = spec.get("auth")
        headers = {"Authorization": f"Bearer {key}"} if auth == "bearer" else {str((auth or {}).get("header")): key}
        try:
            status = int(connect._default_http_get(str(spec["url"]), headers, 10.0))  # noqa: SLF001 - verified https, no redirects
        except Exception as exc:  # the kind only: the text could echo a header
            log.warning("services: a key test could not be made (%s)", type(exc).__name__)
            return {"result": "failed", "status": None}
        finally:
            key = ""
            headers = {}
        if 200 <= status < 300:
            return {"result": "ok", "status": status}
        if status in (401, 403):
            return {"result": "refused", "status": status}
        if status == 429:
            return {"result": "limited", "status": status}
        return {"result": "failed", "status": status}

    def remove_key(self, provider: Any) -> list[str]:
        from muse import keys

        return keys.delete_key(str(provider.env), environ=self.home.environ)

    def _flow_key(self, provider: Any, key: str) -> None:
        self.home.save_key(str(provider.env), key)
        self._restart_pending = True
        self.home.record("key.store", f"Stored a key for the service {api._word(provider.id)}", egress="none", actor="person")

    def local_runtime(self, snap: Mapping[str, Any] | None = None) -> bool:
        """Is an AI on this computer found and answering? (For "Use the AI on this computer instead".)"""
        try:
            snap = snap if snap is not None else self.home.snapshot()
        except Exception:
            return False
        return any(p.get("kind") == "local" and p.get("configured") and p.get("state") == "ok" for p in snap.get("providers") or [])

    # ---- the proxy

    def manages_proxy(self) -> tuple[bool, list[str]]:
        """(can this home restart the router itself, the command it would run)."""
        import shutil
        import sys

        from muse import paths

        native = (paths.home(self.home.environ) / "proxy.pid").exists()
        if native:
            return True, [sys.executable, "-m", "muse", "restart", "--native", "--detach"]
        if shutil.which("docker"):
            return True, [sys.executable, "-m", "muse", "restart"]
        return False, []

    def restart_proxy(self) -> dict[str, Any]:
        import os
        import subprocess

        ok, command = self.manages_proxy()
        shown = self.home.restart_command(self.home.proxy_running())
        if not ok:
            raise api.ApiError("conflict", "This home does not manage the router: it was not started by Muse on this computer, "
                                           "and Docker was not found. Restart it yourself where it runs (`muse restart`).")
        try:
            done = subprocess.run(command, capture_output=True, timeout=180, check=False,  # noqa: S603 - a fixed command
                                  env=None if self.home.environ is None else {**os.environ, **dict(self.home.environ)})
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("restart: the router could not be restarted (%s)", type(exc).__name__)
            raise api.ApiError("unavailable", "The home could not restart the router. Run `muse restart` on the home to see why.") from None
        if done.returncode != 0:
            log.warning("restart: `muse restart` ended with code %s", done.returncode)
            raise api.ApiError("unavailable", "The home could not restart the router. Run `muse restart` on the home to see why.")
        self._restart_pending = False
        return {"restarted": True, "command": shown}

    # ---- about, verify

    def verify(self) -> dict[str, Any]:
        """The supply-chain check of scripts/verify_litellm.py, without the network part. Read-only."""
        import importlib.util

        from muse.catalog import REPO_ROOT

        path = Path(REPO_ROOT) / "scripts" / "verify_litellm.py"
        try:
            spec = importlib.util.spec_from_file_location("muse_verify_litellm", path)
            module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            import sys

            sys.modules.setdefault("muse_verify_litellm", module)
            spec.loader.exec_module(module)  # type: ignore[union-attr]
            site_dirs = module.default_site_dirs()
            findings = module.check_package(site_dirs, module.LOCK_FILE, required=False) + module.check_indicators(site_dirs)
            code = module.exit_code(findings)
        except Exception as exc:
            log.warning("verify: the check could not run (%s)", type(exc).__name__)
            raise api.ApiError("unavailable", "The check could not run on this home. Run `python scripts/verify_litellm.py` there.") from None
        result = {0: "ok", 1: "problem", 2: "unknown"}[code]
        return {"result": result, "lines": [{"level": str(f.level), "text": str(f.message)[:500]} for f in findings][:50]}

    def versions(self) -> dict[str, Any]:
        import platform

        from muse import __version__

        out: dict[str, Any] = {"muse": __version__, "api": api.API_VERSION, "python": platform.python_version(), "router": None}
        try:
            import importlib.metadata

            out["router"] = importlib.metadata.version("litellm")
        except Exception:
            pass
        return out


def backend(home: Any) -> AppBackend:
    found = getattr(home, "app", None)
    if found is None:
        with _backend_lock:
            found = getattr(home, "app", None)
            if found is None:
                found = AppBackend(home)
                home.app = found
    return found


# ================================================================ status: money and titles


def _job_title(job: Any) -> dict[str, Any]:
    """A job's title as a key the app has words for. Never the prompt."""
    key = {"prompt": QUESTION_TITLE, "batch": "questions_for_later", "file-summary": "file_summary"}.get(job.kind, QUESTION_TITLE)
    return {"key": key, "params": {}, "text": None}


def activity(home: Any, device: Any) -> dict[str, list[dict[str, Any]]]:
    """What is running and what finished today, with a short human title each (Today's two lists)."""
    from muse.ration import local_day_bounds, resolve_tz

    b = backend(home)
    now = home.clock()
    start, _ = local_day_bounds(now, resolve_tz(str(b.env().get("MUSE_TZ", "") or "").strip() or None))
    running: list[dict[str, Any]] = []
    done: list[dict[str, Any]] = []
    try:
        jobs = home.queue().list(None)
        if not device.sees_all:
            mine = home.book.jobs_of(device.name)
            jobs = [j for j in jobs if j.id in mine]
        for job in jobs:
            item = {"id": job.id, "kind": "job", "title": _job_title(job), "status": job.status}
            if job.status in ("queued", "running", "deferred"):
                running.append({**item, "at": _iso(job.started_at or job.created_at), "progress": None})
            elif job.finished_at is not None and job.finished_at >= start:
                done.append({**item, "at": _iso(job.finished_at), "ok": job.status == "done"})
    except Exception as exc:
        log.warning("status: the jobs could not be listed (%s)", type(exc).__name__)
    if device.sees_all:
        try:
            store = b.chore_store()
            sentences = {c.id: c.sentence for c in store.list()}
            for run in store.runs(limit=100):
                # A chore's title is the sentence the person wrote for it.
                title = {"key": "chore", "params": {}, "text": sentences.get(run.chore_id)}
                item = {"id": run.id, "kind": "chore", "title": title, "status": run.status, "chore_id": run.chore_id}
                if run.status == "running":
                    running.append({**item, "at": _iso(run.started_at or run.created_at), "progress": None})
                elif run.status != "queued" and run.finished_at is not None and run.finished_at >= start:
                    done.append({**item, "at": _iso(run.finished_at), "ok": run.status in ("done", "partial", "waiting_card")})
        except Exception as exc:
            log.warning("status: the chores could not be listed (%s)", type(exc).__name__)
    done.sort(key=lambda d: d["at"] or "", reverse=True)
    return {"running": running[:50], "done_today": done[:50]}


def h_status(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    status, snap = api.h_status(home, req)
    b = backend(home)
    snap["money"] = app_money.money(b.env(), snap.get("paid"))
    snap["activity"] = activity(home, req.device)
    snap["local_runtime"] = b.local_runtime(snap)
    snap["restart_pending"] = bool(b._restart_pending)  # noqa: SLF001
    return status, snap


def h_keys(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """`POST /v1/keys/{provider}` as before, with how the app can restart the router.

    No daily limit is written here: the router's default and the home's are the same constant."""
    status, reply = api.h_keys(home, req)
    b = backend(home)
    b._restart_pending = True  # noqa: SLF001
    managed, _ = b.manages_proxy()
    reply["restart"]["managed"] = managed
    reply["restart"]["next"] = {"method": "POST", "path": f"{api.PREFIX}/home/restart-proxy"} if managed else None
    return status, reply


# ================================================================ Ask


def _service_names(home: Any) -> dict[str, tuple[str, bool]]:
    """Service id -> (name, is it on this computer). The catalogue first; the status snapshot for anything it lacks."""
    out: dict[str, tuple[str, bool]] = {}
    try:
        for entry in home.snapshot().get("providers") or []:
            if entry.get("id") and entry.get("name"):
                out[str(entry["id"])] = (str(entry["name"]), entry.get("kind") == "local")
    except Exception:
        pass
    try:
        out.update({p.id: (p.name, bool(p.local)) for p in home.load_catalogue().providers})
    except Exception:
        pass
    return out


def _refill_at(home: Any, provider: str | None) -> str | None:
    """When the service that answered next refills its daily allowance, if the status snapshot knows."""
    if not provider:
        return None
    try:
        entry = next((p for p in home.snapshot().get("providers") or [] if p.get("id") == provider), None)
    except Exception:
        return None
    times = [str(lim["reset_at"]) for lim in (entry or {}).get("limits") or [] if lim.get("window") == "day" and lim.get("reset_at")]
    return min(times) if times else None


def handoff_apps(home: Any, question: str) -> list[dict[str, Any]]:
    """The person's own apps a question can be opened in: links only, from the subscriptions catalogue."""
    from muse import catalog

    out = []
    try:
        subs = home.load_catalogue().subscriptions
    except Exception:
        return []
    for sub in subs:
        web = catalog.handoff_link(sub, question, "web")
        app = catalog.handoff_link(sub, question, "app") if sub.open.get("app") else None
        if not web and not app:
            continue
        item: dict[str, Any] = {"id": sub.id, "name": sub.name, "url_web": web}
        if app:
            item["url_app"] = app
        out.append(item)
    return out


def next_task(task: Any) -> str | None:
    """The tier above `task` (light -> everyday -> heavy -> frontier), or None at the top. Unknown counts as everyday."""
    order = list(ASK_TASKS)
    at = order.index(task) if task in order else order.index("everyday")
    return order[at + 1] if at + 1 < len(order) else None


def can_continue(task: Any) -> bool:
    """A cut-short answer can be asked again with more room: always one step, even at the top tier (more room only)."""
    return True


def _answer_from(home: Any, got: Mapping[str, Any], question: str, privacy: str | None) -> dict[str, Any]:
    """The answer object of the API from a classified router reply."""
    b = backend(home)
    env = b.env()
    if got["kind"] == "answer":
        if not got["text"].strip():
            # The service spent its room on reasoning and wrote nothing: "Continue" asks again with more room.
            return {"status": "failed", "reason": "empty", "cut_short": True, "can_continue": can_continue(got.get("task")),
                    "details": {"empty_reasoning": True, "deployment": got["deployment"], "task": got.get("task")}}
        names = _service_names(home)
        provider = got["provider"]
        name, local = names.get(provider or "", (provider or "", False))
        cost = app_money.amount(got["cost_usd"] or 0, env)
        paid = Decimal(cost["usd"]) > 0
        refill = None if (local or got["cache"] or paid) else _refill_at(home, provider)
        if got["cache"]:
            line = {"key": "cache", "params": {}}
        elif not provider:
            line = {"key": "unknown", "params": {}}
        elif local:
            line = {"key": "local", "params": {"service": name}}
        elif paid:
            line = {"key": "paid", "params": {"service": name, "cost": cost}}
        elif refill:
            line = {"key": "free_refill", "params": {"service": name, "refill_at": refill}}
        else:
            line = {"key": "free", "params": {"service": name}}
        return {
            "status": "answered", "text": got["text"],
            "route": {"provider": provider, "name": name or None, "local": local, "cost": cost, "cache": got["cache"],
                      "refill_hint": refill, "line": line},
            "details": {"deployment": got["deployment"], "task": got["task"], "tokens_in": got["tokens_in"],
                        "tokens_out": got["tokens_out"], "min_output": got["min_output"],
                        "empty_reasoning": got["empty_reasoning"], "privacy": privacy},
            "handoff": {"apps": handoff_apps(home, question)},
            "cut_short": bool(got.get("cut_short")),
            "can_continue": bool(got.get("cut_short")) and can_continue(got.get("task")),
        }
    if got["kind"] == "options":
        choices: dict[str, Any] = {}  # in the fixed order of the screens: wait, hand off, pay; only the ones the router named
        if got["wait_until"]:
            choices["wait"] = {"until": got["wait_until"]}
        if got["handoff"]:
            apps = handoff_apps(home, question)
            if apps:
                choices["handoff"] = {"apps": apps}
        if got["pay_usd"] is not None:
            choices["pay"] = app_money.amount(got["pay_usd"], env)
        return {"status": "choices", "reason": got["reason"], "choices": choices}
    return {"status": "failed", "reason": got["reason"]}


def _skill_answer(home: Any, run: Mapping[str, Any], question: str, privacy: str | None) -> dict[str, Any]:
    """The answer object from a finished run of the files skill."""
    env = backend(home).env()
    if run["status"] == "running":
        return {"status": "pending", "run_id": run["id"]}
    result = run.get("result") or {}
    output = result.get("output") if isinstance(result.get("output"), dict) else {}
    used = result.get("used") if isinstance(result.get("used"), dict) else {}
    text = str(output.get("answer") or output.get("text") or "")
    if run["status"] not in ("done", "partial") or not text.strip():
        refusal = result.get("refusal") if isinstance(result.get("refusal"), dict) else {}
        return {"status": "failed", "reason": "private_no_service" if refusal.get("kind") == "privacy" else "skill_failed",
                "run_id": run["id"]}
    names = _service_names(home)
    providers = [p for p in (used.get("providers") or {}) if isinstance(p, str)]
    provider = providers[0] if len(providers) == 1 else None
    name, local = names.get(provider or "", (provider or "", False))
    cost = app_money.amount(used.get("cost_usd") or 0, env)
    paid = Decimal(cost["usd"]) > 0
    if not providers:
        line = {"key": "skill_local", "params": {}}
    elif paid:
        line = {"key": "paid", "params": {"service": name or ", ".join(names.get(p, (p,))[0] for p in providers), "cost": cost}}
    elif provider:
        line = {"key": "local" if local else "free", "params": {"service": name}}
    else:
        line = {"key": "free_many", "params": {"count": len(providers)}}
    sources = skill_sources(output)
    check = result.get("check") if isinstance(result.get("check"), dict) else {}
    return {
        "status": "answered", "text": text[: app_ask.MAX_ANSWER_CHARS], "run_id": run["id"],
        "route": {"provider": provider, "name": name or None, "local": local, "cost": cost, "cache": False, "refill_hint": None,
                  "line": line},
        "details": {"deployment": None, "task": None, "tokens_in": None, "tokens_out": used.get("tokens"),
                    "min_output": False, "empty_reasoning": False, "privacy": result.get("privacy") or privacy,
                    "skill": FILES_SKILL, "requests": used.get("requests"), "checked": check.get("status") == "passed",
                    "partial": run["status"] == "partial"},
        "sources": sources[:50],
        "handoff": {"apps": handoff_apps(home, question)},
    }


MAX_QUOTE_CHARS = 400


def skill_sources(output: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The files skill's citations as `file › paragraph` with the quoted words (ux section 4.2).

    `file` is the path as the skill showed it (relative to the allowed folder), `paragraphs` the numbers the
    statements rest on, `quote` the words of the first of those paragraphs, cut to MAX_QUOTE_CHARS. A PDF source adds
    `pages`; when they are one page, `paragraphs` are the paragraphs on that page (`lease.pdf › page 3, paragraph 2`),
    and when they are several, `paragraphs` is empty (`lease.pdf › pages 3, 4`)."""
    passages = {p.get("n"): p for p in output.get("passages") or [] if isinstance(p, dict)}
    out: list[dict[str, Any]] = []
    for c in output.get("citations") or []:
        if not isinstance(c, dict):
            continue
        passage = passages.get(c.get("n")) or {}
        numbers = [n for n in c.get("paragraphs") or [] if isinstance(n, int) and not isinstance(n, bool)]
        quote = ""
        parts = str(passage.get("text") or "").split("\n\n")
        for part, n in zip(parts, passage.get("paragraphs") or []):
            if numbers and n == numbers[0]:
                quote = part
                break
        if not quote and parts:
            quote = parts[0]
        quote = " ".join(quote.split())
        if len(quote) > MAX_QUOTE_CHARS:
            quote = quote[: MAX_QUOTE_CHARS - 1] + "…"
        source = {"n": c.get("n"), "file": str(passage.get("shown") or c.get("path") or "")[:300], "paragraphs": numbers[:20],
                  "quote": quote, "label": str(c.get("label") or "")[:300]}
        pages = [n for n in c.get("pages") or [] if isinstance(n, int) and not isinstance(n, bool)]
        if pages:
            on_page = [n for n in c.get("page_paragraphs") or [] if isinstance(n, int) and not isinstance(n, bool)]
            source.update(pages=pages[:20], paragraphs=on_page[:20] if len(pages) == 1 else [])
        out.append(source)
    return out


def _public_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": turn["id"], "conversation_id": turn["conversation_id"], "created_at": turn["created_at"],
            "question": turn["question"], "privacy": turn["request"].get("privacy"), "task": turn["request"].get("task"),
            "attachments": list(turn["request"].get("attachments") or []), **turn["answer"]}


def _refresh(home: Any, device: Any, turn: dict[str, Any]) -> dict[str, Any]:
    """A turn whose answer was still on its way: look again (a files run that finished, a queued question that was answered)."""
    b = backend(home)
    answer = turn["answer"]
    new: dict[str, Any] | None = None
    if answer.get("status") == "pending" and answer.get("run_id"):
        run = home.skills.get(device, answer["run_id"])
        if run is None:
            new = {"status": "failed", "reason": "skill_failed"}
        elif run["status"] != "running":
            new = _skill_answer(home, run, turn["question"], turn["request"].get("privacy"))
    elif answer.get("status") == "queued" and answer.get("job_id"):
        from muse import queue

        try:
            job = home.queue().get(answer["job_id"])
        except queue.QueueError:
            job = None
        if job is None or job.status in ("failed", "cancelled"):
            new = {"status": "failed", "reason": "failed", "job_id": answer["job_id"]}
        elif job.status == "done":
            try:
                text = Path(job.out_path).read_text(encoding="utf-8").strip()
            except OSError:
                text = ""
            if not text:
                new = {"status": "failed", "reason": "failed", "job_id": job.id}
            else:
                new = _answer_from(home, {"kind": "answer", "text": text, "provider": job.provider, "deployment": job.deployment,
                                          "cost_usd": job.cost_usd, "cache": False, "task": job.task, "tokens_in": job.tokens_in,
                                          "tokens_out": job.tokens_out, "min_output": False, "empty_reasoning": False},
                                   turn["question"], turn["request"].get("privacy"))
                new["job_id"] = job.id
    if new is not None:
        b.conversations.set_answer(turn["id"], new)
        turn = {**turn, "answer": new}
    return turn


def _ask_record(home: Any, req: Any, answer: Mapping[str, Any]) -> bool:
    """The record of an answered question: which service, what it cost. Never a word of the question or the answer."""
    route = answer.get("route") or {}
    provider = route.get("provider")
    egress = "none" if (route.get("local") or route.get("cache") or not provider) else f"service:{provider}"
    if not re.fullmatch(r"(?:none|service:[a-z0-9][a-z0-9._-]{0,63})", egress):
        egress = "none"
    words = "Answered a question" + (f" with the service {api._word(str(provider))}" if provider else "")
    cost = route.get("cost") or {}
    fields: dict[str, Any] = {"egress": egress, **api._who(req.device)}
    if cost.get("usd") and Decimal(cost["usd"]) > 0:
        # The record format counts in minor units (US cents); a sub-cent cost is rounded up so it never reads as nothing.
        from decimal import ROUND_CEILING

        cents = int((Decimal(cost["usd"]) * 100).to_integral_value(rounding=ROUND_CEILING))
        fields["cost"] = {"currency": "USD", "minor": cents}
    return home.record("home.ask", words, **fields) is not None


def h_ask(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    from muse import apps
    from muse.home import folders as folder_list

    body = req.body
    api._fields(body, {"text": str, "conversation_id": (str, _NONE), "privacy": (str, _NONE), "task": (str, _NONE),
                       "attachments": list}, required=("text",))
    text = body["text"].strip()
    if not text or len(text) > app_ask.MAX_TEXT_CHARS or "\x00" in text:
        raise api.ApiError("invalid_request", f"A question is 1 to {app_ask.MAX_TEXT_CHARS} characters.")
    privacy = body.get("privacy") or home.default_privacy() or "standard"
    if privacy not in apps.PRIVACY_CLASSES:
        raise api.ApiError("invalid_request", f"privacy must be one of {', '.join(apps.PRIVACY_CLASSES)}.")
    task = body.get("task") or "everyday"
    if task not in ASK_TASKS:
        raise api.ApiError("invalid_request", f"task must be one of {', '.join(ASK_TASKS)}.")
    names = body.get("attachments") or []
    if len(names) > MAX_ATTACHMENTS or any(not isinstance(n, str) for n in names) or len(set(names)) != len(names):
        raise api.ApiError("invalid_request", f"attachments is a list of at most {MAX_ATTACHMENTS} different folder names.")
    listed = folder_list.load(home.environ)
    if any(n not in listed for n in names):
        raise api.ApiError("invalid_request", "An attachment is not one of the folders the person has allowed on the home.")
    conversation_id = body.get("conversation_id")
    b = backend(home)
    if conversation_id is not None and (not _HEX16.match(conversation_id) or b.conversations.turns(conversation_id) is None):
        raise api.ApiError("not_found", "There is no such conversation.")
    request = {"privacy": privacy, "task": task, "attachments": list(names)}

    if home.is_paused():
        answer: dict[str, Any] = {"status": "failed", "reason": "paused"}
    elif names:
        from muse.home.skills import SkillRefused

        try:
            run = home.skills.start(req.device, FILES_SKILL, {"inputs": {"question": text}, "folders": list(names),
                                                              "privacy": privacy if privacy != "public" else None,
                                                              "task": task if task in ("light", "everyday") else None})
        except SkillRefused as refused:
            answer = {"status": "failed", "reason": "skill_busy" if refused.code == "conflict" else "skill_failed"}
        else:
            home.skills.wait(run["id"], SKILL_WAIT_S)
            answer = _skill_answer(home, home.skills.get(req.device, run["id"]) or run, text, privacy)
    else:
        history = b.conversations.history(conversation_id) if conversation_id else []
        reply = b.router_send([*history, {"role": "user", "content": text}], privacy=privacy, task=task, allow_paid=False)
        answer = _answer_from(home, app_ask.classify(reply, home.clock()), text, privacy)
    try:
        conversation_id, turn_id = b.conversations.add_turn(conversation_id, text, request, answer)
    except KeyError:
        raise api.ApiError("not_found", "There is no such conversation.") from None
    recorded = _ask_record(home, req, answer) if answer["status"] == "answered" and not names else False
    turn = b.conversations.turn(turn_id)
    return 200, {"conversation_id": conversation_id, "answer": _public_turn(turn), "recorded": recorded, "streaming": False}


def _turn_for(home: Any, req: Any) -> dict[str, Any]:
    turn_id = req.params["id"]
    turn = backend(home).conversations.turn(turn_id) if _HEX16.match(turn_id) else None
    if turn is None:
        raise api.ApiError("not_found", "There is no such question.")
    return turn


def h_ask_get(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._query(req)
    turn = _refresh(home, req.device, _turn_for(home, req))
    return 200, {"conversation_id": turn["conversation_id"], "answer": _public_turn(turn)}


def h_ask_choose(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """Take one of the choices the router offered: wait (queue it), hand off (the person's own app), or pay."""
    from muse import queue

    api._fields(req.body, {"choice": str, "app": (str, _NONE), "raise_today": bool}, required=("choice",))
    choice = req.body["choice"]
    turn = _turn_for(home, req)
    answer = turn["answer"]
    if answer.get("status") not in ("choices", "over_limit"):
        raise api.ApiError("conflict", "That question is not waiting for a choice.")
    choices = answer.get("choices") or {}
    if choice not in ("wait", "handoff", "pay") or choice not in choices:
        raise api.ApiError("invalid_request", "That choice was not offered for this question.")
    b = backend(home)
    env = b.env()
    text, request = turn["question"], turn["request"]
    recorded = False
    if choice == "wait":
        try:
            job = home.queue().add(kind="prompt", payload=text, alias="auto", task=request.get("task"), privacy=request.get("privacy"),
                                   max_tokens=app_ask.DEFAULT_MAX_TOKENS)
        except queue.QueueError:
            raise api.ApiError("unavailable", "The home could not queue the question. Nothing was changed.") from None
        try:
            home.book.set_job_owner(job.id, req.device.name)
        except Exception as exc:
            log.warning("ask: the owner of a queued question could not be written (%s)", type(exc).__name__)
        new = {"status": "queued", "job_id": job.id, "until": choices["wait"]["until"], "title": {"key": QUESTION_TITLE, "params": {}}}
        recorded = home.record("job.queue", "Queued a question to be asked later", links={"job": job.id}, egress="none",
                               **api._who(req.device)) is not None
    elif choice == "handoff":
        apps = {a["id"]: a for a in choices["handoff"]["apps"]}
        app_id = req.body.get("app") or next(iter(apps))
        if app_id not in apps:
            raise api.ApiError("invalid_request", "That app was not offered for this question.")
        new = {"status": "handed_off", "app": {"id": app_id, "name": apps[app_id]["name"]}, "choices": choices,
               "reason": answer.get("reason")}
        # What the person then types into their own app is theirs; Muse sent nothing anywhere.
        recorded = home.record("home.handoff", f"Opened a question in the person's own {api._word(apps[app_id]['name'])} app",
                               egress="none", **api._who(req.device)) is not None
    else:
        estimate = Decimal(choices["pay"]["usd"])
        spent, limit = b.spent_today_usd(), b.limit_today_usd()
        if spent + estimate > limit:
            if not req.body.get("raise_today"):
                new = {"status": "over_limit", "reason": answer.get("reason"), "choices": choices,
                       "over_limit": {"over": app_money.amount(spent + estimate - limit, env), "limit": app_money.amount(limit, env),
                                      "spent": app_money.amount(spent, env), "pay": choices["pay"],
                                      "raise_today": {"to": app_money.amount(spent + estimate, env)}}}
                b.conversations.set_answer(turn["id"], new)
                return 200, {"conversation_id": turn["conversation_id"], "answer": _public_turn({**turn, "answer": new}),
                             "recorded": False}
            b.raise_today(spent + estimate)
            home.record("home.limit", "Raised the daily spending limit for today only", egress="none", **api._who(req.device))
        history = b.conversations.history(turn["conversation_id"])
        reply = b.router_send([*history, {"role": "user", "content": text}], privacy=request.get("privacy"),
                              task=request.get("task"), allow_paid=True)
        got = app_ask.classify(reply, home.clock())
        new = _answer_from(home, got, text, request.get("privacy"))
        if new["status"] == "choices" and new.get("reason") == "paid_limit":
            # The router's own cap said no. It is a setting the router reads when it starts; the home cannot lift it for a day.
            new = {"status": "failed", "reason": "router_limit", "choices": {k: v for k, v in new["choices"].items() if k != "pay"}}
        if new["status"] == "answered":
            recorded = _ask_record(home, req, new)
    b.conversations.set_answer(turn["id"], new)
    return 200, {"conversation_id": turn["conversation_id"], "answer": _public_turn({**turn, "answer": new}), "recorded": recorded}


def h_ask_continue(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """Ask a cut-short question again with more room (CONTINUE_MAX_TOKENS) on the next tier up. Never paid."""
    api._no_body(req)
    turn = _turn_for(home, req)
    answer = turn["answer"]
    if not answer.get("cut_short") or not answer.get("can_continue"):
        raise api.ApiError("conflict", "That answer was not cut short, so there is nothing to continue.")
    if home.is_paused():
        raise api.ApiError("conflict", "Everything is paused. Resume, then continue.")
    b = backend(home)
    request = turn["request"]
    asked = (answer.get("details") or {}).get("task") or request.get("task")
    task = next_task(asked) or asked or "everyday"
    history = [m for m in b.conversations.history(turn["conversation_id"])]
    reply = b.router_send([*history, {"role": "user", "content": turn["question"]}], privacy=request.get("privacy"), task=task,
                          allow_paid=False, max_tokens=app_ask.CONTINUE_MAX_TOKENS)
    new = _answer_from(home, app_ask.classify(reply, home.clock()), turn["question"], request.get("privacy"))
    if new.get("cut_short"):
        new["can_continue"] = False  # once: a second cut is said plainly, not offered again
    b.conversations.set_answer(turn["id"], new)
    recorded = _ask_record(home, req, new) if new["status"] == "answered" else False
    return 200, {"conversation_id": turn["conversation_id"], "answer": _public_turn({**turn, "answer": new}), "recorded": recorded}


def h_conversations(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._query(req)
    return 200, {"conversations": backend(home).conversations.list(), "kept": "on this home, in a private file; the vault will replace it",
                 "limits": {"conversations": app_ask.MAX_CONVERSATIONS, "turns": app_ask.MAX_TURNS}}


def h_conversation_get(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._query(req)
    conversation_id = req.params["id"]
    turns = backend(home).conversations.turns(conversation_id) if _HEX16.match(conversation_id) else None
    if turns is None:
        raise api.ApiError("not_found", "There is no such conversation.")
    return 200, {"id": conversation_id, "turns": [_public_turn(_refresh(home, req.device, t)) for t in turns]}


def h_conversation_delete(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._no_body(req)
    conversation_id = req.params["id"]
    if not _HEX16.match(conversation_id) or not backend(home).conversations.delete(conversation_id):
        raise api.ApiError("not_found", "There is no such conversation.")
    return 200, {"deleted": True}


# ================================================================ Chores


def _chore_public(store: Any, chore: Any, env: Mapping[str, str]) -> dict[str, Any]:
    from muse.skills import schedule

    out = chore.public()
    out["lines"] = schedule.describe(chore, env)
    out["title"] = {"key": "chore", "params": {}, "text": chore.sentence}
    out["typical_time"] = store.typical_time(chore.id, chore.tz or None) if chore.spare_only else None
    out["paid"] = float(chore.budget.get("paid_usd") or 0) > 0
    return out


def _chore(home: Any, req: Any) -> tuple[Any, Any]:
    from muse.skills.schedule import ChoreError

    store = backend(home).chore_store()
    chore_id = req.params["id"]
    try:
        if not _CHORE_ID.match(chore_id):
            raise ChoreError("no such chore")
        return store, store.get(chore_id)
    except ChoreError:
        raise api.ApiError("not_found", "There is no such chore.") from None


def h_chores_list(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._query(req)
    b = backend(home)
    store = b.chore_store()
    env = b.env()
    return 200, {"chores": [_chore_public(store, c, env) for c in store.list()], "can_run": True, "paused": bool(home.is_paused())}


def _folder_paths(home: Any, names: Any, *, for_output: bool = False) -> list[str]:
    from muse.home import folders as folder_list

    out = []
    for name in names or []:
        try:
            out.append(str(folder_list.resolve(name, for_output=for_output, environ=home.environ)))
        except folder_list.FolderError:
            raise api.ApiError("invalid_request", "A folder name is not on the person's list on the home, or its folder is gone."
                               if not for_output else "The output folder is not on the person's list as a folder for new files.") from None
    return out


def _understand(home: Any, body: Mapping[str, Any]) -> dict[str, Any]:
    from muse.skills import schedule

    api._fields(body, {"sentence": str, "folders": list, "out": (str, _NONE)}, required=("sentence",))
    sentence = body["sentence"].strip()
    if not sentence or len(sentence) > schedule.MAX_SENTENCE:
        raise api.ApiError("invalid_request", f"A chore is one sentence of at most {schedule.MAX_SENTENCE} characters.")
    if len(body.get("folders") or []) > schedule.MAX_SOURCES or any(not isinstance(n, str) for n in body.get("folders") or []):
        raise api.ApiError("invalid_request", f"folders is a list of at most {schedule.MAX_SOURCES} folder names.")
    env = backend(home).env()
    folders = _folder_paths(home, body.get("folders"))
    out = _folder_paths(home, [body["out"]], for_output=True)[0] if body.get("out") else None
    from muse.skills import chores

    understood = schedule.understand(sentence, folders=folders, out=out, tz_name=chores.tz_name(env), environ=env)
    understood["label"] = {"spare": bool((understood.get("spec") or {}).get("spare_only"))}
    return understood


def h_chore_understand(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """What the home understood from a sentence. Nothing is saved. Rules only: no model is asked."""
    return 200, {"understood": _understand(home, req.body), "saved": False}


def _checked_understood(understood: Any) -> tuple[str, Any, dict[str, Any]]:
    """(sentence, Spec, budget) from an understood object a client sends back. It is checked again: nothing is taken on trust."""
    from muse.skills import schedule

    if not isinstance(understood, dict) or understood.get("ok") is not True or not isinstance(understood.get("spec"), dict):
        raise api.ApiError("invalid_request", "Send the understood object of a sentence that left nothing to ask.")
    sentence = understood.get("sentence")
    if not isinstance(sentence, str) or not sentence.strip() or len(sentence) > schedule.MAX_SENTENCE:
        raise api.ApiError("invalid_request", "The understood object has no sentence.")
    if schedule.spec_problems(understood["spec"]):
        raise api.ApiError("invalid_request", "The understood object does not describe a chore the home can run.")
    spec = schedule.Spec.from_dict(understood["spec"])
    for path in spec.sources:
        if not Path(path).is_dir():
            raise api.ApiError("invalid_request", "A folder this chore would read is not there on the home.")
    own = schedule.default_budget(spec.template)
    budget = dict(own)
    asked = understood.get("budget_values") if isinstance(understood.get("budget_values"), dict) else {}
    for key in ("requests", "tokens", "seconds"):
        value = asked.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= own.get(key, 0):
            budget[key] = value
    budget["paid_usd"] = 0  # a chore made from the app never spends; money needs a permission, which is not built here
    return sentence.strip(), spec, budget


def h_chore_add(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    from muse import apps
    from muse.skills import chores
    from muse.skills.schedule import ChoreError

    api._fields(req.body, {"understood": dict, "privacy": (str, _NONE)}, required=("understood",))
    privacy = req.body.get("privacy")
    if privacy is not None and privacy not in apps.PRIVACY_CLASSES:
        raise api.ApiError("invalid_request", f"privacy must be one of {', '.join(apps.PRIVACY_CLASSES)}.")
    sentence, spec, budget = _checked_understood(req.body["understood"])
    b = backend(home)
    env = b.env()
    store = b.chore_store()
    try:
        chore = store.add(sentence, spec, budget=budget, privacy=privacy, tz=chores.tz_name(env))
    except ChoreError:
        raise api.ApiError("invalid_request", "The chore was not accepted. Say the sentence again.") from None
    recorded = home.record("home.chore", "Added a chore", egress="none", **api._who(req.device)) is not None
    return 201, {"chore": _chore_public(store, chore, env), "recorded": recorded}


def h_chore_update(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    from muse.skills.schedule import ChoreError

    api._fields(req.body, {"enabled": bool, "understood": dict})
    if not req.body:
        raise api.ApiError("invalid_request", "Send `enabled` to pause or resume, or `understood` to change the sentence.")
    store, chore = _chore(home, req)
    env = backend(home).env()
    try:
        if "understood" in req.body:
            sentence, spec, budget = _checked_understood(req.body["understood"])
            chore = store.replace(chore.id, sentence, spec, budget=budget)
        if "enabled" in req.body:
            chore = store.set_enabled(chore.id, req.body["enabled"])
    except ChoreError:
        raise api.ApiError("invalid_request", "The chore was not changed. Say the sentence again.") from None
    recorded = home.record("home.chore", "Changed a chore", egress="none", **api._who(req.device)) is not None
    return 200, {"chore": _chore_public(store, chore, env), "recorded": recorded}


def h_chore_delete(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    from muse.skills import chores

    api._no_body(req)
    store, chore = _chore(home, req)
    for run in store.remove(chore.id):
        try:
            chores._forget(home.environ or {}, run.id)  # noqa: SLF001 - a result that was waiting for its card is erased
        except Exception:
            pass
    recorded = home.record("home.chore", "Deleted a chore and its history", egress="none", **api._who(req.device)) is not None
    return 200, {"deleted": True, "recorded": recorded}


def h_chore_run(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """Run now. Answers at once; the history says how it went, and a result waits as a "Save a file" card."""
    api._no_body(req)
    _, chore = _chore(home, req)
    if home.is_paused():
        raise api.ApiError("conflict", "Everything is paused. Resume, then run the chore.")
    run = backend(home).start_chore_run(chore.id)
    return 202, {"run": run.public(), "title": {"key": "chore", "params": {}, "text": chore.sentence}}


def h_chore_history(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._query(req)
    store, chore = _chore(home, req)
    return 200, {"chore_id": chore.id, "runs": [r.public() for r in store.runs(chore.id, limit=50)]}


# ================================================================ Services


def badge_of(entry: Mapping[str, Any]) -> str:
    """The privacy badge a service earns (ux section 1): local, private (does not train, nobody reads), else standard."""
    if entry.get("kind") == "local":
        return "local"
    policy = entry.get("policy") or {}
    if policy.get("trains_on_inputs") == "no" and policy.get("human_review") == "no":
        return "private"
    return "standard"


def _left(entry: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """(what is left today of the tightest daily limit, when it refills)."""
    best: tuple[float, dict[str, Any]] | None = None
    for lim in entry.get("limits") or []:
        if lim.get("window") != "day" or not lim.get("limit") or lim.get("remaining") is None:
            continue
        share = float(lim["remaining"]) / float(lim["limit"])
        if best is None or share < best[0]:
            best = (share, lim)
    if best is None:
        return None, None
    lim = best[1]
    return ({"remaining": lim["remaining"], "limit": lim["limit"], "unit": lim.get("metric"),
             "estimated": lim.get("source") != "header"}, lim.get("reset_at"))


def _service_state(entry: Mapping[str, Any]) -> str:
    state = entry.get("state")
    reason = str(entry.get("block_reason") or "").lower()
    if state == "exhausted":
        return "key_refused" if ("key" in reason and ("reject" in reason or "refus" in reason or "401" in reason)) else "used_up"
    return {"ok": "ok", "cooling": "busy", "disabled": "off", "unknown": "unknown"}.get(str(state), "unknown")


def h_services(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    from muse import catalog, connect

    api._query(req)
    try:
        snap = home.snapshot()
    except api.ApiError:
        raise
    except Exception as exc:
        log.warning("services: the snapshot could not be built (%s)", type(exc).__name__)
        raise api.ApiError("unavailable", "The home could not read its own status. Run `muse doctor` on the home to see why.",
                           retry_after=5) from None
    cat = home.load_catalogue()
    by_id = {p.id: p for p in cat.providers}
    connected = []
    for entry in snap.get("providers") or []:
        if not entry.get("configured"):
            continue
        provider = by_id.get(entry.get("id"))
        left, refill = _left(entry)
        local = entry.get("kind") == "local"
        connected.append({
            "id": entry.get("id"), "name": entry.get("name"), "badge": badge_of(entry), "local": local,
            "left": left, "unlimited": local, "refill_at": refill, "estimated": bool(left and left["estimated"]),
            "good_for": list(entry.get("good_for") or []), "checked_on": entry.get("checked_on"),
            "state": _service_state(entry), "retry_at": entry.get("blocked_until"),
            "can_test": bool(provider and provider.connection and provider.connection.test and not local),
            "can_disconnect": bool(provider and provider.env and not local),
            "tier": entry.get("tier"),
        })
    ids = home.connected_ids(cat)
    add = []
    for s in catalog.suggest_next(ids, home.env, providers=cat.providers):
        provider = by_id.get(s.provider_id)
        conn = provider.connection if provider else None
        item = s.to_dict()
        add.append({
            "id": item["id"], "name": item["name"], "rank": item["rank"],
            "method": "one_tap" if item["method"] == "oauth_pkce" else "guided",
            "minutes": item["minutes_estimate"], "steps": item["steps"],
            "card": item["card_required"], "phone": item["phone_required"], "gain": item["gain"],
            "key_host": _host(conn.key_url if conn else None), "facts": _facts(provider),
        })
    return 200, {"connected": connected, "add": add, **connect.resources_payload(cat), "local_runtime": backend(home).local_runtime(snap),
                 "restart_pending": bool(backend(home)._restart_pending)}  # noqa: SLF001


def _host(url: Any) -> str | None:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return None
    return parts.hostname if parts.scheme == "https" and parts.hostname else None


def _facts(provider: Any) -> dict[str, Any]:
    """The two plain facts shown before connecting (first run): how much a day for free, and whether it may learn from what is sent."""
    if provider is None:
        return {"free_per_day": None, "may_train": "unknown"}
    day = [a for a in provider.allowance if a.window == "day" and a.unit == "requests"]
    small = min((a.limit for a in day), default=None)
    raw = (provider.policy or {}).get("trains_on_inputs", "unknown")
    trains = {True: "yes", False: "no"}.get(raw, str(raw).strip().lower()) if isinstance(raw, (bool, str)) else "unknown"
    return {"free_per_day": small, "may_train": trains if trains in ("yes", "no", "varies", "unknown") else "unknown"}


def _connectable(home: Any, provider_id: str) -> Any:
    return api._provider(home, provider_id)


def h_service_test(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._no_body(req)
    provider = _connectable(home, req.params["id"])
    if not home.key_configured(str(provider.env)):
        raise api.ApiError("conflict", "That service is not connected on this home.")
    result = backend(home).test_key(provider)
    return 200, {"provider": provider.id, "name": provider.name, **result, "sent_a_prompt": False}


def h_service_remove(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._no_body(req)
    provider = _connectable(home, req.params["id"])
    if not home.key_configured(str(provider.env)):
        raise api.ApiError("not_found", "That service is not connected on this home.")
    b = backend(home)
    try:
        removed = b.remove_key(provider)
    except Exception as exc:
        log.warning("services: a key could not be removed (%s)", type(exc).__name__)
        raise api.ApiError("unavailable", "The home could not remove the key. Nothing was changed.") from None
    b._restart_pending = True  # noqa: SLF001
    record = home.record("key.remove", f"Removed the key for the service {api._word(provider.id)}", egress="none", **api._who(req.device))
    running = home.proxy_running()
    managed, _ = b.manages_proxy()
    return 200, {"provider": provider.id, "removed": bool(removed), "still_set_elsewhere": home.key_configured(str(provider.env)),
                 "restart": {"needed": True, "proxy_running": running, "command": home.restart_command(running), "managed": managed},
                 "recorded": record is not None}


# ---- one-tap connection


def _flow_reply(home: Any, flow: Any) -> dict[str, Any]:
    from muse import catalog

    out = flow.public()
    if flow.status == "connected":
        cat = home.load_catalogue()
        provider = next((p for p in cat.providers if p.id == flow.provider), None)
        head = provider.headline() if provider else None
        running = home.proxy_running()
        managed, _ = backend(home).manages_proxy()
        out["gained"] = {"name": provider.name if provider else flow.provider,
                         "gain": None if head is None else {"window": head.window, "limit": head.limit, "unit": head.unit},
                         "text": {lang: catalog.gain_text(head, lang) for lang in catalog.LANGS}}
        out["restart"] = {"needed": True, "proxy_running": running, "command": home.restart_command(running), "managed": managed}
    return out


def h_connect_start(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """Start the one-tap flow. `same_device: true` says the browser that will open `url` is on the home's own computer."""
    api._fields(req.body, {"same_device": bool})
    provider = _connectable(home, req.params["provider"])
    mode = "loopback" if req.body.get("same_device") else "paste_code"
    try:
        flow = backend(home).flows.start(provider, mode)
    except app_connect.FlowError as err:
        raise api.ApiError(err.code, err.message) from None
    return 201, {**flow.public(), "url": flow.url, "name": provider.name, "facts": _facts(provider),
                 "next": {"method": "GET", "path": f"{api.PREFIX}/connect/flows/{flow.id}"}}


def _flow(home: Any, req: Any) -> Any:
    flow_id = req.params["state_id"]
    flow = backend(home).flows.get(flow_id) if _HEX16.match(flow_id) else None
    if flow is None:
        raise api.ApiError("not_found", "There is no such connection in progress.")
    return flow


def h_connect_flow(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._query(req)
    return 200, _flow_reply(home, _flow(home, req))


def h_connect_code(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """paste_code mode: the code the service showed. It is traded for the key at once and kept nowhere."""
    api._fields(req.body, {"code": str}, required=("code",))
    flow = _flow(home, req)
    try:
        flow = backend(home).flows.give_code(flow.id, lambda pid: _connectable(home, pid), req.body["code"].strip())
    except app_connect.FlowError as err:
        raise api.ApiError(err.code, err.message) from None
    return 200, _flow_reply(home, flow)


def h_connect_cancel(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._no_body(req)
    flow = _flow(home, req)
    backend(home).flows.cancel(flow.id)
    return 200, {"cancelled": True}


def h_restart_proxy(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._no_body(req)
    result = backend(home).restart_proxy()
    record = home.record("home.restart", "Restarted the router so it reads the current keys and settings", egress="none",
                         **api._who(req.device))
    return 200, {**result, "recorded": record is not None}


# ================================================================ For apps


def h_app_presets(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """Ready-made settings for the programs people point at Muse (muse/presets.py), and the address to paste."""
    from muse import presets, queue

    api._query(req)
    base = queue.proxy_url(backend(home).env()) + "/v1"
    out = []
    for preset in presets.PRESETS.values():
        steps = [s.replace("{{", "\0").replace("}}", "\1").replace("{base}", base).replace("{alias}", preset.alias)
                 .replace("\0", "{").replace("\1", "}") for s in preset.steps]
        out.append({
            "kind": preset.kind, "title": preset.title, "priority": preset.priority, "daily_requests": preset.daily_requests,
            "daily_tokens": preset.daily_tokens, "alias": preset.alias, "why": preset.why,
            "steps": steps,  # `{key}` is left in: the app puts the new key there, once, on the person's screen
            "key_placeholder": "{key}", "verified": preset.verified, "verified_on": presets.CHECKED if preset.verified else None,
            "checked_on": presets.CHECKED, "source": preset.source, "caveat": preset.caveat or None,
        })
    return 200, {"endpoint": base, "presets": out}


# ================================================================ Settings, memory, verify, about


def _settings(home: Any) -> dict[str, Any]:
    from muse import catalog

    b = backend(home)
    env = b.env()
    per, day = app_money.rate(env)
    return {
        "daily_limit": app_money.daily_limit(env),
        "default_privacy": home.default_privacy() or "standard",
        "language": catalog.default_lang(env),
        "language_set": bool(str(env.get("MUSE_LANG", "") or "").strip()),
        "currency": app_money.currency(env),
        "rate": f"{per.normalize():f}", "rate_source": app_money.RATE_SOURCE, "rate_date": day,
        "currencies": list(app_money.CURRENCIES), "languages": list(LANGS),
        "privacy_choices": ["standard", "private", "local"],
        "restart_pending": bool(b._restart_pending),  # noqa: SLF001
        "first_run": first_run_state(env),
    }


FIRST_RUN_NAME = "MUSE_FIRST_RUN"
FIRST_RUN_ENDS = ("done", "skipped")


def first_run_state(env: Mapping[str, str]) -> str:
    """`pending` until the first-run flow is finished (`done`) or skipped (`skipped`); it never goes back."""
    value = str(env.get(FIRST_RUN_NAME, "") or "").strip().lower()
    return value if value in FIRST_RUN_ENDS else "pending"


def h_settings_get(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    api._query(req)
    return 200, {"settings": _settings(home)}


def h_settings_patch(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """Change settings. Written to the home's settings file; a provider key is never written here."""
    body = req.body
    api._fields(body, {"daily_limit_local": (int, float, str), "default_privacy": str, "language": str, "currency": str,
                       "usd_rate": (int, float, str), "first_run": str})
    if not body:
        raise api.ApiError("invalid_request", "Send at least one setting to change.")
    b = backend(home)
    env = b.env()
    writes: list[tuple[str, str]] = []
    changed: list[str] = []
    if "currency" in body:
        code = body["currency"].strip().upper()
        if code not in app_money.CURRENCIES:
            raise api.ApiError("invalid_request", f"currency must be one of {', '.join(app_money.CURRENCIES)}.")
        writes.append(("MUSE_CURRENCY", code))
        env["MUSE_CURRENCY"] = code
        if "usd_rate" not in body:
            env.pop("MUSE_USD_RATE", None)
            writes.append(("MUSE_USD_RATE", f"{app_money.DEFAULT_RATES[code].normalize():f}"))
            writes.append(("MUSE_USD_RATE_DATE", app_money.DEFAULT_RATE_DATE))
        changed.append("currency")
    if "usd_rate" in body:
        value = app_money._dec(body["usd_rate"])  # noqa: SLF001
        if value is None or value <= 0 or value > app_money.MAX_RATE:
            raise api.ApiError("invalid_request", "usd_rate is how many units of your currency one US dollar is: a number above 0.")
        text = f"{value.normalize():f}"
        today = datetime.fromtimestamp(home.clock(), timezone.utc).strftime("%Y-%m-%d")
        writes += [("MUSE_USD_RATE", text), ("MUSE_USD_RATE_DATE", today)]
        env["MUSE_USD_RATE"], env["MUSE_USD_RATE_DATE"] = text, today
        changed.append("usd_rate")
    if "daily_limit_local" in body:
        usd = app_money.local_to_usd(body["daily_limit_local"], env)
        if usd is None or usd > Decimal("10000"):
            raise api.ApiError("invalid_request", "daily_limit_local is an amount in your currency, zero or more. Zero means never spend.")
        writes.append((app_money.CAP_NAME, app_money.cap_setting_text(usd)))
        changed.append("daily_limit")
    if "default_privacy" in body:
        if body["default_privacy"] not in ("standard", "private", "local"):
            raise api.ApiError("invalid_request", "default_privacy must be standard, private or local.")
        writes.append(("MUSE_DEFAULT_PRIVACY", body["default_privacy"]))
        changed.append("default_privacy")
    if "language" in body:
        if body["language"] not in LANGS:
            raise api.ApiError("invalid_request", f"language must be one of {', '.join(LANGS)}.")
        writes.append(("MUSE_LANG", body["language"]))
        changed.append("language")
    if "first_run" in body:
        if body["first_run"] not in FIRST_RUN_ENDS:
            raise api.ApiError("invalid_request", "first_run must be done or skipped; it cannot be started again.")
        if first_run_state(env) == "pending":  # finished or skipped once is final: the first value stays
            writes.append((FIRST_RUN_NAME, body["first_run"]))
        changed.append("first_run")
    try:
        for name, value in writes:
            b.write_setting(name, value)
    except Exception as exc:
        log.warning("settings: a setting could not be written (%s)", type(exc).__name__)
        raise api.ApiError("unavailable", "The home could not write its settings file. Some settings may not have changed.") from None
    restart = bool(RESTART_SETTINGS & set(changed))
    if restart:
        b._restart_pending = True  # noqa: SLF001
    running = home.proxy_running()
    managed, _ = b.manages_proxy()
    record = home.record("home.settings", "Changed settings: " + ", ".join(changed).replace("_", " "), egress="none",
                         **api._who(req.device))
    return 200, {"settings": _settings(home), "changed": changed,
                 "restart": {"needed": restart, "proxy_running": running, "command": home.restart_command(running), "managed": managed},
                 "recorded": record is not None}


def h_memory_get(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """What the helper remembers about the person. Helper memory is not built, and this says so."""
    api._query(req)
    b = backend(home)
    return 200, {"built": False, "items": [], "note": "nothing_remembered",
                 "conversations": len(b.conversations.list())}


def h_memory_delete(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """Forget everything: there is no helper memory yet, so this erases the kept conversations, which is all there is."""
    api._no_body(req)
    removed = backend(home).conversations.delete_all()
    record = home.record("home.forget", "Erased the conversations kept on the home", egress="none", **api._who(req.device))
    return 200, {"built": False, "items_removed": 0, "conversations_removed": removed, "recorded": record is not None}


def h_verify(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    """"Check that this installation is genuine": the supply-chain check, read-only, no network."""
    api._no_body(req)
    return 200, backend(home).verify()


def h_about(home: Any, req: Any) -> tuple[int, dict[str, Any]]:
    from muse import catalog, queue

    api._query(req)
    b = backend(home)
    cat = home.load_catalogue()
    return 200, {
        "versions": b.versions(),
        "catalogue": {"checked_on": catalog.newest_checked_on(cat), "services": len(cat.providers)},
        # What the app may show. `recovery` turns on when the vault is wired into the home; until then the app shows
        # the recovery step as coming, never made-up words.
        "features": {"recovery": False, "memory": False, "streaming": False, "wallet": False, "export": False},
        "endpoint": queue.proxy_url(b.env()) + "/v1",
        "local_runtime": b.local_runtime(),
    }


# ================================================================ the routes
#
# (method, path under /v1, scope, handler, extra Route arguments). muse/home/api.py turns these into its Route
# rows; docs/spec/interfaces/home-api.v1.yaml lists the same, and tests/test_home_openapi.py compares the two.

ROUTE_SPECS: tuple[tuple[str, str, str, Any, dict[str, Any]], ...] = (
    ("POST", "/ask", "ask.write", h_ask, {}),
    ("GET", "/ask/{id}", "ask.write", h_ask_get, {}),
    ("POST", "/ask/{id}/choose", "ask.write", h_ask_choose, {}),
    ("POST", "/ask/{id}/continue", "ask.write", h_ask_continue, {}),
    ("GET", "/conversations", "ask.write", h_conversations, {}),
    ("GET", "/conversations/{id}", "ask.write", h_conversation_get, {}),
    ("DELETE", "/conversations/{id}", "ask.write", h_conversation_delete, {}),
    ("GET", "/chores", "chores.read", h_chores_list, {}),
    ("POST", "/chores", "chores.write", h_chore_add, {}),
    ("POST", "/chores/understand", "chores.write", h_chore_understand, {}),
    ("PATCH", "/chores/{id}", "chores.write", h_chore_update, {}),
    ("DELETE", "/chores/{id}", "chores.write", h_chore_delete, {}),
    ("POST", "/chores/{id}/run", "chores.write", h_chore_run, {}),
    ("GET", "/chores/{id}/history", "chores.read", h_chore_history, {}),
    ("GET", "/services", "catalogue.read", h_services, {}),
    ("POST", "/services/{id}/test", "keys.write", h_service_test, {}),
    ("DELETE", "/services/{id}", "keys.write", h_service_remove, {}),
    ("POST", "/connect/{provider}/start", "connect.write", h_connect_start, {}),
    ("GET", "/connect/flows/{state_id}", "connect.write", h_connect_flow, {}),
    ("POST", "/connect/flows/{state_id}/code", "connect.write", h_connect_code, {"takes_key": True}),
    ("DELETE", "/connect/flows/{state_id}", "connect.write", h_connect_cancel, {}),
    ("POST", "/home/restart-proxy", "home.admin", h_restart_proxy, {}),
    ("GET", "/apps/presets", "apps.read", h_app_presets, {}),
    ("GET", "/settings", "settings.read", h_settings_get, {}),
    ("PATCH", "/settings", "settings.write", h_settings_patch, {}),
    ("GET", "/memory", "settings.read", h_memory_get, {}),
    ("DELETE", "/memory", "settings.write", h_memory_delete, {}),
    ("POST", "/verify", "home.admin", h_verify, {}),
    ("GET", "/about", "settings.read", h_about, {}),
)
