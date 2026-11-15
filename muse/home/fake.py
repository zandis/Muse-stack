"""The fake home: the same API from canned data, for building apps against.

    python -m muse.home --fake [--tcp PORT] [--state normal|used|paused|cards|empty|error|mismatch]

It needs no proxy, no ledger and no keys. It is the real `Home` and the real
route table pointed at a throw-away folder: the status snapshot is the bundled
example (muse/dashboard/demo_snapshot.json), keys are accepted and thrown away,
and apps, jobs and cards are real stores seeded with demo data. So writes
behave exactly as on a real home (create an app, answer a card twice, cancel a
job) and everything is forgotten when it stops.

States (what the Today screen must handle, docs/spec/02-ux.md section 4.1 and 8):
    normal   services connected, allowance left, one card waiting
    used     every allowance used up; refill times in the snapshot
    paused   "Everything is paused"
    cards    one waiting card of each of the seven kinds, plus answered and expired ones
    empty    nothing connected, no apps, no jobs, no cards
    error    every call answers 503 `unavailable` (the home cannot be reached)
    mismatch as normal, but one record's text was changed after it was written, so the records do not check out

Every state but empty and error also has a history for the Records and Permissions screens (section 4.4, 4.6):
records over several days (the person's own, a helper's, a program's; one that cost money; one whose text was
removed, "redacted"), sealed once; and, when `biscuit-python` is installed, five permissions made by the real
compiler: a folder for the helper, spending (whose spending limit is honestly "not built"), a program's use of the
allowances (the FIRST stop of this one fails at the router entry, so a failed target can be shown; stopping it
again works), one that ended, and one that was stopped two days ago. Plus one request a host bot made that the
person declined. Without the library the fake simply has no permissions, as a real home would not.

Sign in with the fixed token `FAKE_TOKEN`. It only ever works on a fake home.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from muse.home import api
from muse.home.auth import StaticVerifier
from muse.home.cards import CardStore

STATES = ("normal", "used", "paused", "cards", "empty", "error", "mismatch")
DAY = 86400.0
HOST_BOT = "openmausbot-tools"  # the demo host bot (a device with the `host` role)
PROGRAM = "coding-tool"  # the demo program that may use the allowances
SPEND_HOSTS = ("search.example.com", "maps.example.com", "translate.example.com")  # made-up payees: nothing here is a real company
FAKE_TOKEN = "fake-home-token"
FAKE_DEVICE = "fake-device"
DEMO_CARDS_PATH = Path(__file__).resolve().parent / "demo_cards.json"


def fake_verifier() -> StaticVerifier:
    return StaticVerifier({FAKE_DEVICE: FAKE_TOKEN})


def demo_cards() -> list[dict[str, Any]]:
    return list(json.loads(DEMO_CARDS_PATH.read_text(encoding="utf-8"))["cards"])


def _snapshot_for(state: str) -> dict[str, Any]:
    from muse.dashboard.server import demo_snapshot

    snap = demo_snapshot()
    snap["home"] = "(fake home)"
    from muse.home.app_fake import PRIVATE_DEMO_SERVICE

    snap["providers"].append(json.loads(json.dumps(PRIVATE_DEMO_SERVICE)))  # so the Private badge can be shown working
    if state == "used":
        for p in snap["providers"]:
            if not p.get("configured") or p.get("kind") == "local":
                continue
            resets = []
            for limit in p.get("limits") or []:
                if limit.get("remaining") is not None:
                    limit["remaining"] = 0
                if limit.get("reset_at"):
                    resets.append(limit["reset_at"])
            if p.get("state") in ("ok", "cooling", "unknown"):
                p["state"] = "exhausted"
                p["blocked_until"] = max(resets) if resets else p.get("blocked_until")
                p["block_reason"] = "Today's free allowance is used up."
            for d in p.get("deployments") or []:
                if d.get("state") in ("ok", "cooling", "unknown"):
                    d["state"], d["blocked_until"] = "exhausted", p.get("blocked_until")
        for alias in snap["aliases"]:
            alias["available"] = 0
    elif state == "empty":
        for p in snap["providers"]:
            local = p.get("kind") == "local"
            p.update(configured=False, state="unknown" if local else "no_key", blocked_until=None, block_reason=None, limits=[])
            p["usage"] = {w: {"requests": 0, "tokens": 0} for w in ("minute", "day", "month")}
            for d in p.get("deployments") or []:
                d.update(state=p["state"], blocked_until=None, last_status=None, last_used_at=None)
        for alias in snap["aliases"]:
            alias["available"] = 0
        snap["recent"] = []
        snap["totals"] = {"requests_today": 0, "tokens_today": 0}
        snap["apps"] = [a for a in snap["apps"] if a.get("builtin")]
        for a in snap["apps"]:
            a["last_used_at"] = None
            a["today"] = {"requests": 0, "failures": 0, "tokens": 0, "cost_usd": 0.0}
            a["deferrals"] = {}
        snap["paid"].update(spent_usd=0.0, held_usd=0.0, calls=0, unknown_cost_calls=0)
        snap["queue"] = {"counts": {s: 0 for s in snap["queue"]["counts"]}, "waiting": 0, "next_due_at": None,
                         "oldest_queued_at": None, "hold_until": None}
        snap["proxy"]["reachable"] = False
    return snap


def _fake_skill_run(skill: Any, inputs: Any = None, **_kwargs: Any) -> Any:
    """What a skill run "returns" on a fake home: done, a canned answer, nothing spent, nothing proposed."""
    from muse.skills.runner import Result

    name = getattr(skill, "name", str(skill))
    if name == "files-answer":
        return _fake_files_answer(name, str((inputs or {}).get("question") or ""))
    return Result("done", output={"text": f"(fake home: this is where the answer of the skill '{name}' would be)"},
                  notes=["This is a fake home. No model was asked and nothing was read or written."],
                  check={"status": "passed", "reasons": []},
                  used={"requests": 0, "tokens": 0, "cost_usd": 0.0, "seconds": 0.0, "providers": {}}, skill=name, privacy="standard")


DEMO_FILE = "Lease 2026.md"
DEMO_PARAGRAPHS = (
    "# Lease for the flat on Heping East Road",
    "The rent is NT$18,000 a month, paid by the 5th of each month into the landlord's account.",
    "Either side may end the lease with two months' notice in writing.",
    "租金每月新台幣 18,000 元，於每月 5 日前匯入房東帳戶。",
)


def _fake_files_answer(name: str, question: str) -> Any:
    """The files skill on a fake home: one canned answer that cites the demo lease, the way the real skill does."""
    from muse.skills.runner import Result

    zh = any("一" <= ch <= "鿿" for ch in question)
    shown = f"documents/{DEMO_FILE}"
    para = 4 if zh else 2
    answer = ("（假的家）租金每月 18,000 元，5 日前要付 [1]。" if zh else
              "(fake home) The rent is NT$18,000 a month, due by the 5th [1].")
    label = f"{shown} › 第 {para} 段" if zh else f"{shown} › paragraph {para}"
    output = {
        "question": question, "answer": answer, "text": answer, "not_found": False,
        "claims": [{"text": answer, "cites": [1], "sources": [label]}],
        "citations": [{"n": 1, "path": shown, "paragraphs": [para], "label": label}],
        "passages": [{"n": 1, "path": shown, "shown": shown, "paragraph": 1, "paragraph_end": 4, "paragraphs": [1, 2, 3, 4],
                      "text": "\n\n".join(DEMO_PARAGRAPHS), "label": label}],
    }
    return Result("done", output=output, notes=["This is a fake home. No model was asked; the answer is canned."],
                  check={"status": "passed", "reasons": []},
                  used={"requests": 1, "tokens": 640, "cost_usd": 0.0, "seconds": 1.4, "providers": {"groq": 1}}, skill=name,
                  privacy="standard")


class FakeHome(api.Home):
    def __init__(self, state: str = "normal", root: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        if state not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
        self.state = state
        self._own_root = root is None
        self.root = Path(tempfile.mkdtemp(prefix="muse-fake-home-")) if root is None else Path(root)
        # The demo folders the person "allowed" sit outside the home's own folder: a folder inside it may never be given out.
        self.demo_root = Path(tempfile.mkdtemp(prefix="muse-fake-demo-"))
        environ = {"MUSE_HOME": str(self.root), "MUSE_ENV_FILE": str(self.root / "fake.env"), "MUSE_TZ": "UTC"}
        super().__init__(environ, clock=clock)
        self._fake_keys: set[str] = set()
        self._fake_connected: set[str] = set()
        self._fake_removed: set[str] = set()
        self.fail_stop_once: set[str] = set()  # permissions whose next stop fails at the router entry (then works)
        self.demo_mandates: dict[str, str] = {}  # demo name -> permission id, for tests
        from muse.home.app_fake import FakeApp

        self.app = FakeApp(self)  # canned behaviour for the app's routes (muse/home/app_api.py)
        self.skills.runner = _fake_skill_run  # a fake home reaches no router: a run "finishes" with a canned answer
        self._seed()

    def close(self) -> None:
        self.app.flows.close()
        shutil.rmtree(self.demo_root, ignore_errors=True)
        if self._own_root:
            shutil.rmtree(self.root, ignore_errors=True)

    def settings_env(self) -> dict[str, str]:
        # A fake home reads no real .env and no real keychain: only its own throw-away settings file, which
        # `PATCH /v1/settings` writes (a limit, a language). Nothing in it is a key.
        from muse import keys

        return {**keys._dotenv_items(self.environ), **dict(self.environ or {})}  # noqa: SLF001

    def run_env(self) -> dict[str, str]:
        return dict(self.environ or {})

    # ---- canned behaviour

    def check_available(self) -> None:
        if self.state == "error":
            raise api.ApiError("unavailable", "The home cannot be reached right now. (This fake home is in its error state.)", retry_after=5)

    def snapshot(self) -> dict[str, Any]:
        snap = _snapshot_for(self.state)
        for p in snap["providers"]:
            if p["id"] in self._fake_removed:
                p.update(configured=False, state="no_key", blocked_until=None, block_reason=None, limits=[])
            if p["id"] in self._fake_connected:
                p["configured"] = True
                if p.get("state") == "no_key":
                    p["state"] = "ok"
        return snap

    def connected_ids(self, cat: Any) -> set[str]:
        real = {p.id for p in cat.providers if not p.local}
        canned = {p["id"] for p in _snapshot_for(self.state)["providers"] if p.get("configured")}
        return ((canned | self._fake_connected) - self._fake_removed) & real

    def save_key(self, name: str, value: str) -> tuple[str, list[str]]:
        self._fake_keys.add(name)  # the value is dropped here: a fake home stores no key
        cat = self.load_catalogue()
        for p in cat.providers:
            if p.env and all(n in self._fake_keys for n in (p.env, *p.also_env)):
                self._fake_connected.add(p.id)
                self._fake_removed.discard(p.id)
        return "fake", []

    def key_configured(self, name: str) -> bool:
        if name in self._fake_keys:
            return True
        connected = self.connected_ids(self.load_catalogue())  # a canned connected service "has" its key
        return any(p.env == name and p.id in connected for p in self.load_catalogue().providers)

    def forget_service(self, provider: Any) -> None:
        """Disconnect on a fake home: the service is no longer connected for as long as this fake runs."""
        self._fake_keys.discard(str(provider.env))
        self._fake_connected.discard(provider.id)
        self._fake_removed.add(provider.id)

    def proxy_running(self) -> bool:
        return self.state != "empty"

    def compiler(self) -> Any:
        """The real compiler, except that removing the router entry of a permission in `fail_stop_once` fails once."""
        from muse.compiler.core import Compiler

        home = self

        class FakeCompiler(Compiler):
            def _remove_one(self, row: Any, why: str, actor: str) -> None:
                if row["kind"] in ("app", "app_adopted") and row["mandate_id"] in home.fail_stop_once:
                    home.fail_stop_once.discard(row["mandate_id"])
                    raise RuntimeError("fake home: this removal fails once, on purpose")
                super()._remove_one(row, why, actor)

        return FakeCompiler(self.environ, clock=self.clock, record=self.record)

    def restart_command(self, running: bool) -> str:
        return "muse restart" if running else "muse up"

    # ---- seeding

    def _seed(self) -> None:
        if self.state == "paused":
            self.set_paused(True)
        if self.state in ("empty", "error"):
            return
        self._seed_apps()
        self._seed_jobs()
        wanted = demo_cards()
        if self.state != "cards":
            wanted = [c for c in wanted if c["state"] != "pending"] + [c for c in wanted if c["state"] == "pending"][:1]
        seed_cards(self.cards, wanted, self.clock())
        self._seed_history()
        self._seed_app()

    def _seed_app(self) -> None:
        """Demo data for the app's screens: two folders the person "allowed", three chores, one conversation."""
        from muse.home import folders
        from muse.skills import schedule

        feeds, documents = self.demo_root / "feeds", self.demo_root / "documents"
        for folder in (feeds, documents):
            folder.mkdir(parents=True, exist_ok=True)
        (feeds / "example.xml").write_text("<rss><channel><title>Demo feed</title></channel></rss>\n", encoding="utf-8")
        (documents / DEMO_FILE).write_text("\n\n".join(DEMO_PARAGRAPHS) + "\n", encoding="utf-8")
        try:
            folders.add("feeds", feeds, environ=self.environ)
            folders.add("documents", documents, output=True, environ=self.environ)
        except folders.FolderError:
            pass
        store = self.app.chore_store()
        now = self.clock()
        demo = (
            (f"Every Monday 08:00, summarise the feeds in {feeds} into {documents}", True, "done", "3 source(s) summarised, 0 not read; saved"),
            ("Every day at 07:30, write me a daily brief", True, "waiting_card", "nothing to report this time; waiting for your approval"),
            (f"每週五，有多餘額度的時候，整理 {feeds} 裡的訂閱摘要", False, None, None),
        )
        for sentence, enabled, last, summary in demo:
            understood = schedule.understand(sentence, tz_name="Asia/Taipei", environ=self.environ)
            if not understood["ok"]:
                continue
            chore = store.add(understood["sentence"], schedule.Spec.from_dict(understood["spec"]), budget=understood["budget_values"],
                              tz="Asia/Taipei")
            if last:
                run = store.add_manual_run(chore.id, now - 3 * 3600)
                store.settle(run.id, status=last, summary=summary, started_at=now - 3 * 3600, finished_at=now - 3 * 3600 + 42,
                             requests=3, tokens=5200, cost_usd=0.0)
                store.mark_ran(chore.id, last, summary, now - 3 * 3600 + 42)
            if not enabled:
                store.set_enabled(chore.id, False)
        if self.state != "used":
            from muse.home import app_api, app_ask

            question = "What can you do?"
            reply = self.app.router_send([{"role": "user", "content": question}], privacy="standard", task="everyday", allow_paid=False)
            self.app.conversations.add_turn(None, question, {"privacy": "standard", "task": "everyday", "attachments": []},
                                            app_api._answer_from(self, app_ask.classify(reply, now), question, "standard"))  # noqa: SLF001

    def _seed_history(self) -> None:
        """Records over several days, and the permissions they ran under, oldest first (a chain's time never goes back).

        Permissions are made by the real compiler and need `biscuit-python`; records need `cryptography`. Without
        either, that part is simply missing, as it would be on a real home. See the module's docstring for the list."""
        from muse import permissions as P
        from muse.records.home import HomeRecords

        now = self.clock()
        documents = self.demo_root / "documents"
        documents.mkdir(parents=True, exist_ok=True)
        bot = {"actor": "helper", "mandate": f"device:{HOST_BOT}"}
        ids = self.demo_mandates
        can_permit = P.available()

        def at(age: float) -> Callable[[], float]:
            return lambda t=now - age: t

        def write(age: float, action: str, summary: str, **fields: Any) -> dict[str, Any] | None:
            try:
                return HomeRecords(self.environ, clock=at(age)).write(action, summary, **fields)
            except Exception:
                return None  # no `cryptography` here: no records, as on a real home without it

        def issue(age: float, name: str, body: dict[str, Any]) -> None:
            if not can_permit:
                return
            from muse.compiler.core import Compiler
            from muse.home import mandates_api

            try:
                compiler = Compiler(self.environ, clock=at(age))
                fields = mandates_api._fields_of(body, compiler.now(), "Asia/Taipei")  # noqa: SLF001
                mandate, _applied = compiler.issue(mandates_api.draft_from(fields, compiler.ensure_root()), actor="person")
                ids[name] = mandate.id
            except Exception as exc:
                api.log.warning("fake home: the demo permission %s was not made (%s)", name, type(exc).__name__)

        def stop(age: float, name: str) -> None:
            if name in ids:
                from muse.compiler.core import Compiler

                try:
                    Compiler(self.environ, clock=at(age)).revoke(ids[name], actor="person")
                except Exception as exc:
                    api.log.warning("fake home: the demo permission %s was not stopped (%s)", name, type(exc).__name__)

        router = {"kind": "provider", "value": "router"}
        issue(40 * DAY, "ended", {"holder": "helper:home", "actions": ["calendar.read"],
                                  "resources": [{"kind": "calendar", "value": "Family"}], "days": 30})
        write(12 * DAY, "key.store", "Stored a key for the service gemini", egress="none")
        if "ended" in ids:
            from muse.compiler.core import Compiler

            try:
                Compiler(self.environ, clock=at(10 * DAY - 600)).sweep()  # the home's own sweep, just after it ended
            except Exception as exc:
                api.log.warning("fake home: the sweep did not run (%s)", type(exc).__name__)
        issue(10 * DAY - 300, "stopped", {"holder": "app:chat-window", "actions": ["llm.call"], "resources": [router],
                                    "limits": [{"kind": "requests", "period": "day", "amount": 100}], "days": 90})
        issue(6 * DAY, "folder", {"holder": "helper:home", "actions": ["files.read", "llm.call"],
                                  "resources": [{"kind": "folder", "value": str(documents)}, router], "days": 30})
        issue(5 * DAY, "spend", {"holder": "helper:home", "actions": ["pay.spend"],
                                 "resources": [{"kind": "host", "value": h} for h in SPEND_HOSTS],
                                 "limits": [{"kind": "spend", "unit": "TWD", "decimals": 2, "period": "day", "amount": 3000}],
                                 "confirm": {"rule": "over_amount", "unit": "TWD", "decimals": 2, "amount": 1000}, "days": 20})
        issue(4 * DAY, "program", {"holder": f"app:{PROGRAM}", "actions": ["llm.call"], "resources": [router],
                                   "limits": [{"kind": "requests", "period": "day", "amount": 200}], "days": 60})
        if "program" in ids:
            self.fail_stop_once.add(ids["program"])
        write(3 * DAY, "llm.call", f"The program {PROGRAM} used Groq: 12 requests", actor="helper", mandate=ids.get("program"),
              egress="service:groq")
        stop(2 * DAY + 3600, "stopped")
        write(DAY + 7200, "card.create", "Asked whether to save a file", **bot, egress="none")
        write(DAY + 7100, "card.answer", "Approved: save a file (answered on desktop)", egress="none")
        write(DAY + 7000, "host.files.write", "A host bot says it saved a file", **bot)  # its own report: it did not say
        write(DAY + 5400, "skill.run", "Ran the skill files-answer", actor="helper", mandate=ids.get("folder"), egress="service:groq")
        hidden = write(DAY + 5300, "files.write", "Saved the weekly digest to Documents", actor="helper",
                       mandate=ids.get("folder"), egress="none")
        write(DAY + 3600, "llm.call", "Answered a question with a paid service: deepseek", cost={"currency": "USD", "minor": 4},
              egress="service:deepseek")
        changed = write(7200, "llm.call", "A skill used Gemini: 3 requests", **bot, egress="service:gemini")
        write(7190, "skill.run", "Ran the skill files-answer for a host bot", **bot, egress=f"host:{HOST_BOT}")
        write(3600, "home.pause", "Paused the queue", egress="none")
        write(3000, "home.resume", "Resumed the queue", egress="none")
        write(600, "card.answer", "Declined: spend money (answered on desktop)", egress="none", outcome="declined")
        self._seed_declined_request()
        try:
            HomeRecords(self.environ, clock=at(0)).seal()
        except Exception:
            pass
        if hidden is not None:
            self._rewrite_record(hidden["seq"], redact=True)  # the text is gone; the record still checks out
        if changed is not None and self.state == "mismatch":
            self._rewrite_record(changed["seq"], summary="A skill used Gemini: 300 requests")  # no longer what was signed

    def _seed_declined_request(self) -> None:
        """A host bot asked to use the allowances without a daily cap, and the person said no: the "Declined" list."""
        from muse import permissions as P
        from muse.home.auth import Device

        if not P.available():
            return
        bot = Device(HOST_BOT, role="host")
        owner = Device(FAKE_DEVICE, role="owner")

        def call(device: Device, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
            resp = api.dispatch(self, device, method, path, json.dumps(body).encode())
            return resp.payload if resp.status < 300 else {}

        draft = call(bot, "POST", "/v1/mandates/draft", {"holder": f"device:{HOST_BOT}", "actions": ["llm.call", "llm.paid"],
                                                         "resources": [{"kind": "provider", "value": "router"}],
                                                         "days": 90}).get("draft")
        if not draft:
            return
        card = call(bot, "POST", "/v1/mandates", {"draft": draft["id"]}).get("card")
        if card:
            call(owner, "POST", f"/v1/cards/{card['id']}/answer", {"decision": "decline"})

    def _rewrite_record(self, seq: int, *, redact: bool = False, summary: str | None = None) -> None:
        """Change one stored record in place: remove its text (redaction keeps it valid) or change it (it no longer checks)."""
        import sqlite3

        from muse.records import format as fmt
        from muse.records.home import records_path
        from muse.records.jcs import canonicalize

        conn = sqlite3.connect(str(records_path(self.environ)))
        try:
            row = conn.execute("SELECT body FROM records WHERE seq=?", (seq,)).fetchone()
            if row is None:
                return
            record = json.loads(row[0])
            record = fmt.redact(record) if redact else {**record, "summary": summary}
            with conn:
                conn.execute("UPDATE records SET body=? WHERE seq=?", (canonicalize(record).decode("utf-8"), seq))
        finally:
            conn.close()

    def _seed_apps(self) -> None:
        from muse import apps

        for a in _snapshot_for("normal")["apps"]:
            if a.get("builtin"):
                continue
            try:
                apps.add_app(a["name"], a["priority"], daily_requests=a.get("daily_requests"), daily_tokens=a.get("daily_tokens"),
                             allow_paid=a.get("allow_paid"), default_privacy=a.get("default_privacy"), note=a.get("note"),
                             environ=self.environ)
            except apps.AppError:
                continue  # a demo name the registry does not accept is just not seeded

    def _seed_jobs(self) -> None:
        q = self.queue()
        now = self.clock()
        demo = (
            ("queued", "light", {}),
            ("running", "everyday", {"started_at": now - 40, "attempts": 1}),
            ("deferred", "light", {"deferrals": 2, "next_attempt_at": now + 900, "last_reason": "defer:yield-interactive"}),
            ("done", "light", {"started_at": now - 3600, "finished_at": now - 3570, "attempts": 1, "provider": "gemini",
                               "deployment": "gemini-3.1-flash-lite", "tokens_in": 412, "tokens_out": 96, "cost_usd": 0.0}),
        )
        for status, task, changes in demo:
            job = q.add(kind="prompt", payload="(demo job: a fake home holds no real prompt)", alias="auto", task=task)
            changes = {"status": status, **changes}
            if status == "done":
                changes["payload"] = ""
            sets = ", ".join(f"{k}=?" for k in changes)
            with q._conn(write=True) as conn:  # noqa: SLF001 - seeding states the public API reaches only by running jobs
                conn.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*changes.values(), job.id))


def seed_cards(store: CardStore, wanted: list[dict[str, Any]], now: float) -> list[str]:
    """Create demo cards in their stated states, oldest first, using the store's own rules."""
    ids = []
    real_clock = store.clock
    try:
        for spec in sorted(wanted, key=lambda c: -c["age_s"]):
            created = now - spec["age_s"]
            store.clock = lambda t=created: t
            state = spec["state"]
            ttl = None  # the store's own default: 24 hours, 5 minutes for a payment
            if state == "expired":
                ttl = 60.0
            elif state == "pending" and spec["kind"] == "spend":
                ttl = spec["age_s"] + 300.0  # five minutes from when the fake home started
            card_id = store.create(kind=spec["kind"], title=spec["title"], thing=spec["thing"], where=spec["where"],
                                   under=spec["under"], cost=spec["cost"], extra=spec["extra"], job_ref=spec.get("job_ref"), ttl_s=ttl)
            if state in ("approved", "declined"):
                store.clock = lambda t=created + spec.get("answered_after_s", 30): t
                store.answer(card_id, "approve" if state == "approved" else "decline", spec.get("answered_by", "desktop"),
                             bool(spec.get("always", False)))
            ids.append(card_id)
    finally:
        store.clock = real_clock
    store.expire_due()
    return ids
