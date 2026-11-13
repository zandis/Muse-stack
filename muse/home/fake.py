"""The fake home: the same API from canned data, for building apps against.

    python -m muse.home --fake [--tcp PORT] [--state normal|used|paused|cards|empty|error]

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

STATES = ("normal", "used", "paused", "cards", "empty", "error")
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
    if state == "used":
        soonest = None
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
            if p.get("blocked_until"):
                soonest = min(soonest, p["blocked_until"]) if soonest else p["blocked_until"]
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


class FakeHome(api.Home):
    def __init__(self, state: str = "normal", root: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        if state not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
        self.state = state
        self._own_root = root is None
        self.root = Path(tempfile.mkdtemp(prefix="muse-fake-home-")) if root is None else Path(root)
        environ = {"MUSE_HOME": str(self.root), "MUSE_ENV_FILE": str(self.root / "fake.env"), "MUSE_TZ": "UTC"}
        super().__init__(environ, clock=clock)
        self._fake_keys: set[str] = set()
        self._fake_connected: set[str] = set()
        self._seed()

    def close(self) -> None:
        if self._own_root:
            shutil.rmtree(self.root, ignore_errors=True)

    # ---- canned behaviour

    def check_available(self) -> None:
        if self.state == "error":
            raise api.ApiError("unavailable", "The home cannot be reached right now. (This fake home is in its error state.)", retry_after=5)

    def snapshot(self) -> dict[str, Any]:
        snap = _snapshot_for(self.state)
        for p in snap["providers"]:
            if p["id"] in self._fake_connected:
                p["configured"] = True
                if p.get("state") == "no_key":
                    p["state"] = "ok"
        return snap

    def connected_ids(self, cat: Any) -> set[str]:
        real = {p.id for p in cat.providers if not p.local}
        canned = {p["id"] for p in _snapshot_for(self.state)["providers"] if p.get("configured")}
        return (canned | self._fake_connected) & real

    def save_key(self, name: str, value: str) -> tuple[str, list[str]]:
        self._fake_keys.add(name)  # the value is dropped here: a fake home stores no key
        cat = self.load_catalogue()
        for p in cat.providers:
            if p.env and all(n in self._fake_keys for n in (p.env, *p.also_env)):
                self._fake_connected.add(p.id)
        return "fake", []

    def key_configured(self, name: str) -> bool:
        return name in self._fake_keys

    def proxy_running(self) -> bool:
        return self.state != "empty"

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
