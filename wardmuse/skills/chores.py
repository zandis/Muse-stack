"""`muse chores`: add a chore as a sentence, and run what is due.

    muse chores add "Every Monday 08:00, summarise the feeds in ~/feeds into ~/Documents/Muse"
    muse chores list | show <id> | run-now <id> | pause <id> | resume <id> | edit <id> "<sentence>" | rm <id>
    muse chores tick          # what a timer calls each minute

There is no daemon. `muse chores tick` is one short pass, meant to be called
every minute by a systemd user timer, cron, or (later) the app:

  1. cards answered since the last pass are settled: an approved result is
     written (as a new file, never over an existing one), a declined or expired
     one is dropped;
  2. every chore whose time has come gets exactly one queued run
     (`ChoreStore.tick`, safe when two timers fire at once);
  3. queued runs whose time has come are run, unless Muse is paused.

A run is the `recurring-chore` skill under the chore's budget and privacy
class. Its result is a proposed file. Run by `tick`, nobody is at a terminal,
so a "Save a file" card is created and the result waits in
MUSE_HOME/chores-pending/ (owner-only) until the card is answered.

Allowance. A chore that runs "when there is allowance to spare" is sent as
the background app `skills-background`, so the router's existing rules
(muse/ration.py: yield, reserve, pacing, and the waiver near a reset) decide
when it may go. When the router holds a run back it says when to return; the
run is queued again for that time and is not counted as a failure. A run that
is still waiting when the chore's next occurrence comes round is skipped.

Summaries stored here are counts and status words. File contents, prompts and
answers never reach the database, a card, a log line or an exception.
"""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from wardmuse import paths
from wardmuse.ration import local_day_bounds, resolve_tz
from wardmuse.skills import runner, schedule
from wardmuse.skills.schedule import ChoreError, ChoreStore, Run, Spec
from wardmuse.skills.workspace import Workspace

MAX_RUNS_PER_TICK = 5
DEFAULT_RETRY_S = 600.0
MAX_RETRY_S = 6 * 3600.0
UNREACHABLE_RETRY_S = 300.0
MAX_ATTEMPTS = 3
UNDERSTAND_BUDGET = {"requests": 1, "tokens": 4000, "seconds": 60}

_W: dict[str, dict[str, str]] = {
    "en": {
        "counts": "{done} source(s) summarised, {missed} not read",
        "nothing": "nothing to report this time",
        "waiting": "waiting for your approval",
        "written": "saved",
        "declined": "not approved",
        "expired": "not approved in time",
        "exists": "not saved: a file with that name already exists",
        "unwritable": "not saved: the output folder could not be written",
        "no_card": "the result could not be shown for approval",
        "check": "Couldn't finish: {reason}",
        "held": "waiting for allowance ({reason})",
        "unreachable": "the router could not be reached",
        "missing": "a folder this chore reads is no longer there",
        "failed": "it could not run",
        "card_title": "Save a file",
        "card_under": "{label} chore",
        "b_requests": "Requests",
        "b_jobs": "Background jobs",
        "b_chores": "Chores",
        "b_cards": "Cards",
        "b_req": "{n} request(s) answered, {f} failed",
        "b_cache": "{n} answered from Muse's own cache",
        "b_spent": "US${usd} spent",
        "b_free": "nothing spent",
        "b_none": "nothing recorded",
        "b_jobs_line": "{done} finished, {failed} failed, {waiting} waiting now",
        "b_chores_line": "{ok} run(s) produced a result, {bad} did not, {skipped} skipped",
        "b_cards_line": "{approved} approved, {declined} declined, {expired} expired, {pending} waiting now",
    },
    "zh-TW": {
        "counts": "已摘要 {done} 個來源，{missed} 個未讀取",
        "nothing": "這次沒有需要回報的內容",
        "waiting": "等待你同意",
        "written": "已存檔",
        "declined": "未同意",
        "expired": "逾時未同意",
        "exists": "沒有存檔：已有同名的檔案",
        "unwritable": "沒有存檔：無法寫入輸出資料夾",
        "no_card": "無法送出確認卡",
        "check": "無法完成：{reason}",
        "held": "等待額度（{reason}）",
        "unreachable": "連不上路由器",
        "missing": "這件工作要讀的資料夾已經不在了",
        "failed": "無法執行",
        "card_title": "儲存檔案",
        "card_under": "{label}例行工作",
        "b_requests": "請求",
        "b_jobs": "背景工作",
        "b_chores": "例行工作",
        "b_cards": "確認卡",
        "b_req": "回答了 {n} 次請求，{f} 次失敗",
        "b_cache": "{n} 次由 Muse 自己的快取回答",
        "b_spent": "花費 US${usd}",
        "b_free": "沒有花費",
        "b_none": "沒有紀錄",
        "b_jobs_line": "完成 {done} 件，失敗 {failed} 件，目前等待中 {waiting} 件",
        "b_chores_line": "{ok} 次執行有結果，{bad} 次沒有，{skipped} 次略過",
        "b_cards_line": "同意 {approved} 張，拒絕 {declined} 張，逾時 {expired} 張，目前等待中 {pending} 張",
    },
}


def w(key: str, lang: str, **fmt: Any) -> str:
    text = (_W.get(lang) or _W["en"]).get(key) or _W["en"][key]
    return text.format(**fmt) if fmt else text


def tz_name(env: Mapping[str, str]) -> str:
    return str(envget(env, "MUSE_TZ", "") or "").strip()


# ================================================================ the daily brief's facts


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    return conn


def brief_facts(env: Mapping[str, str], day_after: str, lang: str = "en") -> dict[str, Any]:
    """Counts of what this home did on the day before `day_after` (YYYY-MM-DD, local). Numbers only.

    {"day", "sections": [{"name", "lines", "ok"}]}. A part that cannot be read has ok False and no lines; a part
    with nothing recorded says so. Read-only: no database is created by asking.
    """
    zone = resolve_tz(tz_name(env) or None)
    day = date.fromisoformat(day_after) - timedelta(days=1)
    noon = datetime(day.year, day.month, day.day, 12, tzinfo=zone).timestamp()
    start, end = local_day_bounds(noon, zone)
    sections: list[dict[str, Any]] = []

    def section(key: str, build: Callable[[], list[str]]) -> None:
        try:
            sections.append({"name": w(key, lang), "lines": build() or [w("b_none", lang)], "ok": True})
        except Exception:  # a locked or damaged file: this part is unknown, the rest still stands
            sections.append({"name": w(key, lang), "lines": [], "ok": False})

    def requests() -> list[str]:
        path = paths.ledger_path(env)
        if not path.exists():
            return []
        from wardmuse.ledger import Ledger

        usage = Ledger(path).usage_by_app(now=noon, tz=zone)
        total = sum(row["requests"] for row in usage.values())
        failed = sum(row["failures"] for row in usage.values())
        cached = sum(row["cached_calls"] for row in usage.values())
        cost = sum(row["cost_usd"] for row in usage.values())
        if not (total or failed or cached):
            return []
        lines = [w("b_req", lang, n=total, f=failed)]
        if cached:
            lines.append(w("b_cache", lang, n=cached))
        lines.append(w("b_spent", lang, usd=f"{cost:.2f}") if cost > 0 else w("b_free", lang))
        return lines

    def jobs() -> list[str]:
        path = paths.queue_path(env)
        if not path.exists():
            return []
        conn = _ro(path)
        try:
            counts = {row["status"]: int(row["n"]) for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs WHERE finished_at>=? AND finished_at<? GROUP BY status", (start, end))}
            waiting = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','deferred')").fetchone()["n"]
        finally:
            conn.close()
        if not counts and not waiting:
            return []
        return [w("b_jobs_line", lang, done=counts.get("done", 0), failed=counts.get("failed", 0), waiting=int(waiting))]

    def chores() -> list[str]:
        path = schedule.chores_path(env)
        if not path.exists():
            return []
        conn = _ro(path)
        try:
            counts = {row["status"]: int(row["n"]) for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM runs WHERE created_at>=? AND created_at<? GROUP BY status", (start, end))}
        finally:
            conn.close()
        if not counts:
            return []
        ok = sum(counts.get(k, 0) for k in schedule.RUN_OK)
        skipped = counts.get("skipped", 0)
        return [w("b_chores_line", lang, ok=ok, bad=counts.get("failed", 0) + counts.get("needs_choice", 0), skipped=skipped)]

    def cards() -> list[str]:
        from wardmuse.home.cards import cards_path

        path = cards_path(env)
        if not path.exists():
            return []
        conn = _ro(path)
        try:
            counts = {row["status"]: int(row["n"]) for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM cards WHERE created_at>=? AND created_at<? GROUP BY status", (start, end))}
            pending = conn.execute("SELECT COUNT(*) AS n FROM cards WHERE status='pending'").fetchone()["n"]
        finally:
            conn.close()
        if not counts and not pending:
            return []
        return [w("b_cards_line", lang, approved=counts.get("approved", 0), declined=counts.get("declined", 0),
                  expired=counts.get("expired", 0), pending=int(pending))]

    section("b_requests", requests)
    section("b_jobs", jobs)
    section("b_chores", chores)
    section("b_cards", cards)
    return {"day": day.isoformat(), "sections": sections}


# ================================================================ running one run


def _pending(env: Mapping[str, str], run_id: str, draft: bool = False) -> Path:
    return schedule.pending_dir(env) / (f"{run_id}.draft.md" if draft else f"{run_id}.md")


def _save_private(path: Path, data: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def _forget(env: Mapping[str, str], run_id: str) -> None:
    for draft in (False, True):
        try:
            _pending(env, run_id, draft).unlink()
        except OSError:
            pass


def _shown_folder(path: str) -> str:
    home = str(Path.home())
    text = "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path
    return text if len(text) <= 190 else "…" + text[-189:]


def _counts(output: Any, lang: str) -> str:
    stats = output.get("stats") if isinstance(output, dict) else None
    if not isinstance(stats, dict) or not (stats.get("given") or stats.get("not_read")):
        return w("nothing", lang)
    return w("counts", lang, done=int(stats.get("summarised") or 0), missed=int(stats.get("not_read") or 0))


def _write_file(out_path: str, data: bytes) -> tuple[str | None, str]:
    """Write `data` as a NEW file at `out_path`, or beside it with -2, -3... when that name was taken meanwhile.

    Returns (the path written, "") or (None, "exists" | "unwritable"). Uses the same checked write as every skill.
    """
    target = Path(out_path)
    try:
        workspace = Workspace((), target.parent, may_write=True)
        names = [target.name] + [f"{target.stem}-{n}{target.suffix}" for n in range(2, 10)]
        for name in names:
            write = workspace.propose_write(name, data)
            if workspace.apply(write):
                return write.path, ""
            if "already exists" not in write.refused:
                return None, "unwritable"
    except Exception:
        return None, "unwritable"
    return None, "exists"


def _finish(store: ChoreStore, run: Run, status: str, summary: str, *, count: bool = True, **changes: Any) -> Run:
    now = store.clock()
    store.settle(run.id, status=status, summary=summary, finished_at=now, runner=None, **changes)
    if count:
        store.mark_ran(run.chore_id, status, summary, now)
    else:
        store.note_result(run.chore_id, status, summary)
    return store.get_run(run.id)


def settle_card(store: ChoreStore, env: Mapping[str, str], run: Run) -> Run:
    """Look at the card of a run that is waiting for an answer and act on it. A card still pending changes nothing."""
    from wardmuse.home.cards import CardError, CardStore

    chore_lang = "en"
    try:
        chore_lang = store.get(run.chore_id).spec.lang
    except ChoreError:
        pass
    try:
        status = CardStore(env, clock=store.clock).get(str(run.card_id))["status"]
    except CardError:
        status = "expired"
    if status == "pending":
        return run
    if status != "approved":
        _forget(env, run.id)
        word = "declined" if status == "declined" else "expired"
        if store.settle(run.id, expect="waiting_card", status=word, summary=w(word, chore_lang), finished_at=store.clock()):
            store.note_result(run.chore_id, word, w(word, chore_lang))
        return store.get_run(run.id)
    if not store.settle(run.id, expect="waiting_card", status="running", runner=f"settle-{os.getpid()}"):
        return store.get_run(run.id)  # another process is writing it
    try:
        data = _pending(env, run.id).read_bytes()
    except OSError:
        return _finish(store, run, "failed", w("unwritable", chore_lang), count=False)
    written, why = _write_file(str(run.out_path), data)
    _forget(env, run.id)
    if written is None:
        return _finish(store, run, "failed", w(why, chore_lang), count=False)
    before = (run.summary or "").split(";")[0].strip()
    return _finish(store, run, "done", (before + "; " if before else "") + w("written", chore_lang), count=False, out_path=written)


def settle_cards(store: ChoreStore, env: Mapping[str, str]) -> list[Run]:
    """Act on every answered card. Returns the runs whose state changed."""
    changed = []
    for run in store.runs(statuses=["waiting_card"], limit=500):
        after = settle_card(store, env, run)
        if after.status != "waiting_card":
            changed.append(after)
    return changed


def run_one(
    store: ChoreStore,
    run: Run,
    env: Mapping[str, str],
    *,
    deliver: str = "card",
    confirm: Callable[[str, int], bool] | None = None,
    wait_card: float = 0.0,
    requeue: bool = True,
    post: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Run:
    """Do one claimed run and record how it went.

    deliver   what to do with the proposed file: "write" (the person already agreed), "ask" (`confirm(path, size)`
              answers; a "no" leaves a card), or "card" (create a Save-a-file card and leave the result pending).
    requeue   when the router holds the run back, queue it again for the time the router named (scheduled runs).
    """
    chore = store.get(run.chore_id)
    spec: Spec = chore.spec
    lang = spec.lang
    now = store.clock()
    if any(not Path(src).is_dir() for src in spec.sources):
        return _finish(store, run, "failed", w("missing", lang))
    inputs = dict(chore.inputs)
    inputs.update(date=schedule.local_date(now, chore.tz or None), urls=json.dumps(list(spec.urls)), chore=chore.id, run_id=run.id,
                  last_ok_run=store.last_ok_run(chore.id))
    out_dir = spec.dest or str(schedule.default_out_dir(env))
    extra: dict[str, Any] = {"post": post} if post is not None else {}
    result = runner.run(chore.skill, inputs, folders=list(spec.sources), out_dir=out_dir, budget=chore.budget, privacy=chore.privacy,
                        environ=env, background=chore.spare_only, **extra)
    used = {"requests": int(result.used.get("requests") or 0), "tokens": int(result.used.get("tokens") or 0),
            "cost_usd": float(result.used.get("cost_usd") or 0.0)}
    held = [e for e in result.events if e.get("kind") == "deferred"]
    unreachable = any(e.get("kind") == "call" and e.get("status") == 0 for e in result.events)

    if result.status == "needs_choice" and held:
        reason = str(held[-1].get("reason") or "rate limit")[:40]
        if requeue:
            wait = held[-1].get("retry_after")
            wait = min(MAX_RETRY_S, max(60.0, float(wait))) if isinstance(wait, (int, float)) else DEFAULT_RETRY_S
            store.settle(run.id, status="queued", runner=None, not_before=now + wait, deferrals=run.deferrals + 1,
                         summary=w("held", lang, reason=reason), **used)
            return store.get_run(run.id)
        return _finish(store, run, "needs_choice", w("held", lang, reason=reason), **used)
    if result.status == "failed" and unreachable and requeue and run.attempts + 1 < MAX_ATTEMPTS:
        store.settle(run.id, status="queued", runner=None, not_before=now + UNREACHABLE_RETRY_S * (run.attempts + 1),
                     attempts=run.attempts + 1, summary=w("unreachable", lang))
        return store.get_run(run.id)
    if result.status == "failed":
        return _finish(store, run, "failed", w("unreachable" if unreachable else "failed", lang), **used)
    if result.status == "needs_choice":  # its own check failed twice: keep what it got as a draft, say why in counts
        text = result.output.get("text") if isinstance(result.output, dict) else None
        if isinstance(text, str) and text:
            _save_private(_pending(env, run.id, draft=True), text.encode("utf-8"))
        reasons = result.check.get("reasons") or ["the result did not pass its check"]
        return _finish(store, run, "needs_choice", w("check", lang, reason=str(reasons[0])), **used)

    counts = _counts(result.output, lang)
    if not result.writes:
        return _finish(store, run, result.status, counts, **used)
    write = result.writes[0]
    agreed = deliver == "write" or (deliver == "ask" and confirm is not None and confirm(write.path, len(write.data)))
    if agreed:
        written, why = _write_file(write.path, write.data)
        if written is None:
            return _finish(store, run, "failed", f"{counts}; {w(why, lang)}", **used)
        return _finish(store, run, result.status, f"{counts}; {w('written', lang)}", out_path=written, **used)

    from wardmuse.home.cards import CardError, CardStore

    label = result.output.get("label") if isinstance(result.output, dict) else None
    try:
        _save_private(_pending(env, run.id), write.data)
        cards = CardStore(env, clock=store.clock)
        card_id = cards.create(kind="save_file", title=w("card_title", lang), thing=Path(write.path).name[:200],
                               where=_shown_folder(str(Path(write.path).parent)), under=w("card_under", lang, label=label or "Muse"),
                               cost=None, extra={"size_bytes": len(write.data)}, job_ref=f"chore:{chore.id}:{run.id}",
                               title_key="save_file_chore", params={"label": (label or "Muse")[:200]})
    except (CardError, OSError):
        _forget(env, run.id)
        return _finish(store, run, "failed", f"{counts}; {w('no_card', lang)}", **used)
    after = _finish(store, run, "waiting_card", f"{counts}; {w('waiting', lang)}", card_id=card_id, out_path=write.path, **used)
    if wait_card > 0:
        cards.wait(card_id, wait_card, sleep=sleep)
        after = settle_card(store, env, after)
    return after


def tick(
    env: Mapping[str, str],
    *,
    now: float | None = None,
    run: bool = True,
    wait_card: float = 0.0,
    max_runs: int = MAX_RUNS_PER_TICK,
    clock: Callable[[], float] = time.time,
    post: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """One pass: settle answered cards, queue what is due, run what is queued. Safe to call from two timers at once."""
    from wardmuse import queue

    store = ChoreStore(env, clock)
    ts = clock() if now is None else now
    settled = settle_cards(store, env)
    queued = store.tick(ts)
    ran: list[Run] = []
    paused = queue.is_paused(env)
    if run and not paused:
        me = f"{os.getpid()}-{secrets.token_hex(3)}"
        while len(ran) < max_runs:
            claimed = store.claim(ts, me)
            if claimed is None:
                break
            ran.append(run_one(store, claimed, env, deliver="card", wait_card=wait_card, post=post))
    return {"settled": [r.public() for r in settled], "queued": [r.public() for r in queued], "ran": [r.public() for r in ran],
            "paused": paused}


# ================================================================ the command line


def _say(text: str = "") -> None:
    print(text, file=sys.stderr)


def _local(ts: float | None, zone_name: str) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, resolve_tz(zone_name or None)).strftime("%Y-%m-%d %H:%M")


def _next_line(store: ChoreStore, chore: Any) -> str:
    if not chore.enabled:
        return "paused"
    if chore.spec.spare_only:
        usual = store.typical_time(chore.id, chore.tz or None)
        return "Runs when there is allowance to spare" + (f" (usually around {usual})" if usual else "") + \
               f"; next from {_local(chore.next_run, chore.tz)}"
    return f"next run {_local(chore.next_run, chore.tz)}"


def _understand(args: argparse.Namespace, env: Mapping[str, str], sentence: str) -> dict[str, Any]:
    """Rules first; one model call through the skill only when the rules cannot tell how often or what."""
    zone = tz_name(env)
    folders = list(args.folder or [])
    understood = schedule.understand(sentence, folders=folders, out=args.out, tz_name=zone, environ=env)
    if not understood["ok"] and not args.no_model and schedule.wants_proposal(schedule.parse_sentence(sentence)):
        real = [f for f in folders if Path(f).expanduser().is_dir()]
        result = runner.run(schedule.SKILL, {"mode": "understand", "sentence": sentence, "out": args.out or ""}, folders=real,
                            budget=UNDERSTAND_BUDGET, privacy=getattr(args, "privacy", None), environ=env)
        if result.status in ("done", "partial") and isinstance(result.output, dict) and result.check.get("status") == "passed":
            understood = result.output["understood"]
        elif result.status == "failed" and result.notes:
            _say(f"Note: Muse could not ask a model to read the sentence ({result.notes[0][:160]})")
    requests = getattr(args, "requests", None)
    if requests is not None and understood.get("budget_values"):
        understood["budget_values"] = {**understood["budget_values"], "requests": int(requests)}
        understood["budget"] = schedule.budget_text(understood["budget_values"], understood["lang"])
    return understood


def _confirmed(args: argparse.Namespace, question: str) -> bool | None:
    """True/False for the person's answer; None when nobody could be asked."""
    if args.yes:
        return True
    if not sys.stdin.isatty():
        return None
    try:
        return input(question).strip().lower() in ("y", "yes", "是", "好")
    except EOFError:
        return False


def cmd_add(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    requests = getattr(args, "requests", None)
    if requests is not None and requests < 1:
        _say("Error: --requests must be 1 or more.")
        return 2
    understood = _understand(args, env, args.sentence)
    if args.json and not understood["ok"]:
        print(json.dumps({"added": False, "understood": understood}, indent=2, ensure_ascii=False))
        return 4
    if not args.json:
        print(schedule.understood_text(understood))
    if not understood["ok"]:
        _say("Not added: answer the question(s) above by saying the sentence again with what is missing.")
        return 4
    if not args.json:
        answer = _confirmed(args, "Add this chore? [y/N] ")
        if answer is None:
            _say("Not added: confirm on a terminal, or pass --yes after reading the lines above.")
            return 1
        if not answer:
            print("Not added.")
            return 1
    elif not args.yes:
        print(json.dumps({"added": False, "understood": understood}, indent=2, ensure_ascii=False))
        return 1
    store = ChoreStore(env)
    try:
        chore = store.add(understood["sentence"], Spec.from_dict(understood["spec"]), budget=understood["budget_values"],
                          privacy=args.privacy, tz=tz_name(env))
    except ChoreError as exc:
        _say(f"Error: {exc}")
        return 1
    if args.json:
        print(json.dumps({"added": True, "chore": chore.public(), "understood": understood}, indent=2, ensure_ascii=False))
    else:
        print(f"Added chore {chore.id}. {_next_line(store, chore).capitalize()}.")
        _say("A timer must call `muse chores tick` for it to run by itself (see docs/chores.md).")
    return 0


def cmd_edit(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    store = ChoreStore(env)
    try:
        chore = store.find(args.id)
    except ChoreError as exc:
        _say(f"Error: {exc}")
        return 1
    understood = _understand(args, env, args.sentence)
    print(schedule.understood_text(understood))
    if not understood["ok"]:
        _say("Not changed: answer the question(s) above by saying the sentence again with what is missing.")
        return 4
    answer = _confirmed(args, "Change the chore to this? [y/N] ")
    if not answer:
        _say("Not changed." if answer is False else "Not changed: confirm on a terminal, or pass --yes.")
        return 1
    chore = store.replace(chore.id, understood["sentence"], Spec.from_dict(understood["spec"]), budget=understood["budget_values"])
    print(f"Changed chore {chore.id}. {_next_line(store, chore).capitalize()}.")
    return 0


def cmd_list(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    store = ChoreStore(env)
    chores = store.list()
    if args.json:
        print(json.dumps({"chores": [c.public() for c in chores]}, indent=2, ensure_ascii=False))
        return 0
    if not chores:
        print("No chores yet. Add one: muse chores add \"Every Monday 08:00, summarise the feeds in ~/feeds\"")
        return 0
    for chore in chores:
        print(f"{chore.id}  {chore.sentence}")
        print(f"          {_next_line(store, chore)} · {schedule.budget_text(chore.budget, 'en')}")
        last = f"{chore.last_status}: {chore.last_summary} ({_local(chore.last_run_at, chore.tz)})" if chore.last_status else "not run yet"
        print(f"          last result: {last}")
    return 0


def cmd_show(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    store = ChoreStore(env)
    try:
        chore = store.find(args.id)
    except ChoreError as exc:
        _say(f"Error: {exc}")
        return 1
    runs = store.runs(chore.id, limit=20)
    if args.json:
        print(json.dumps({"chore": chore.public(), "lines": schedule.describe(chore, env), "runs": [r.public() for r in runs]},
                         indent=2, ensure_ascii=False))
        return 0
    print(f"{chore.id}  {chore.sentence}")
    print(schedule.understood_text({"lang": chore.spec.lang, **schedule.describe(chore, env)}))
    print(f"Privacy: {chore.privacy or 'the default'}")
    print(_next_line(store, chore).capitalize())
    if chore.spec.urls:
        print(f"Web addresses kept, not fetched (needs the sandbox): {len(chore.spec.urls)}")
    print("History:" if runs else "History: not run yet")
    for run in runs:
        where = f" -> {run.out_path}" if run.out_path and run.status == "done" else ""
        card = f" (card {run.card_id})" if run.card_id and run.status == "waiting_card" else ""
        draft = f" (what it got: {_pending(env, run.id, draft=True)})" if run.status == "needs_choice" and _pending(env, run.id, draft=True).exists() else ""
        print(f"  {_local(run.started_at or run.created_at, chore.tz)}  {run.status:<13} {run.summary or ''}{where}{card}{draft}")
    return 0


def cmd_run_now(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    store = ChoreStore(env)
    try:
        chore = store.find(args.id)
    except ChoreError as exc:
        _say(f"Error: {exc}")
        return 1

    def confirm(path: str, size: int) -> bool:
        _say(f"The chore wants to write one new file: {path} ({size} bytes)")
        try:
            return input("Write it? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    deliver = "write" if args.yes else ("ask" if sys.stdin.isatty() and not args.json else "card")
    queued = store.add_manual_run(chore.id)
    claimed = store.claim(store.clock(), f"{os.getpid()}-now", queued.id)
    if claimed is None:
        _say("Error: the run could not be started.")
        return 1
    done = run_one(store, claimed, env, deliver=deliver, confirm=confirm, requeue=False)
    if args.json:
        print(json.dumps({"run": done.public()}, indent=2, ensure_ascii=False))
    else:
        print(f"{done.status}: {done.summary or ''}")
        if done.status == "done" and done.out_path:
            print(f"Wrote {done.out_path}")
        if done.status == "waiting_card":
            print(f"The result waits for your answer on card {done.card_id}. It is written when you approve it and "
                  "`muse chores tick` next runs.")
        if done.status == "needs_choice" and _pending(env, done.id, draft=True).exists():
            print(f"What it got (not checked): {_pending(env, done.id, draft=True)}")
    return {"done": 0, "waiting_card": 0, "partial": 3, "needs_choice": 4}.get(done.status, 1)


def _switch(args: argparse.Namespace, env: Mapping[str, str], enabled: bool) -> int:
    store = ChoreStore(env)
    try:
        chore = store.set_enabled(store.find(args.id).id, enabled)
    except ChoreError as exc:
        _say(f"Error: {exc}")
        return 1
    print(f"Chore {chore.id} " + (f"resumed. {_next_line(store, chore).capitalize()}." if enabled else "paused. It keeps its history."))
    return 0


def cmd_rm(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    store = ChoreStore(env)
    try:
        chore = store.find(args.id)
    except ChoreError as exc:
        _say(f"Error: {exc}")
        return 1
    answer = _confirmed(args, f"Delete chore {chore.id} and its history? [y/N] ")
    if not answer:
        _say("Not deleted." if answer is False else "Not deleted: confirm on a terminal, or pass --yes.")
        return 1
    for run in store.remove(chore.id):
        _forget(env, run.id)
    print(f"Deleted chore {chore.id}. Files it already wrote are untouched.")
    return 0


def cmd_tick(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    summary = tick(env, run=not args.enqueue_only, wait_card=max(0.0, float(args.wait_card or 0)))
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    if summary["paused"] and not args.enqueue_only:
        print("Muse is paused: due chores were queued, none was run. `muse resume` lets them run.")
    for run in summary["settled"]:
        print(f"{run['chore_id']}  {run['status']}: {run['summary'] or ''}")
    for run in summary["ran"]:
        print(f"{run['chore_id']}  {run['status']}: {run['summary'] or ''}")
    if not (summary["settled"] or summary["ran"] or summary["queued"]):
        print("Nothing is due.")
    elif summary["queued"] and not summary["ran"]:
        print(f"{len(summary['queued'])} run(s) queued.")
    return 0


def register(sub: Any, env: Callable[[], Mapping[str, str]]) -> None:
    """Add `muse chores` to the top-level parser. `env` returns the merged environment when a command runs."""
    p = sub.add_parser("chores", help="things the helper does on a schedule: add one as a sentence, list, run, pause")
    csub = p.add_subparsers(dest="chores_cmd", metavar="list|add|show|run-now|pause|resume|edit|rm|tick")

    def bind(parser: argparse.ArgumentParser, fn: Callable[[argparse.Namespace, Mapping[str, str]], int]) -> None:
        parser.set_defaults(func=lambda args: fn(args, env()))

    def sentence_options(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("sentence", help='what to do and when, e.g. "Every Monday 08:00, summarise the feeds in ~/feeds"')
        parser.add_argument("--folder", action="append", metavar="PATH", help="a folder the chore reads (repeatable), beside any in the sentence")
        parser.add_argument("--out", metavar="PATH", help="the folder its dated files go to (default: Muse's own)")
        parser.add_argument("--requests", type=int, metavar="N", help="the most model requests one run may make")
        parser.add_argument("--no-model", action="store_true", help="read the sentence with rules only; never ask a model")
        parser.add_argument("--yes", action="store_true", help="confirm without the question (after reading what was understood)")

    ls = csub.add_parser("list", help="every chore, its next run, last result and budget")
    ls.add_argument("--json", action="store_true")
    bind(ls, cmd_list)
    ad = csub.add_parser("add", help="add a chore from one sentence (exit 0 added, 4 a question is open, 1 not confirmed)")
    sentence_options(ad)
    ad.add_argument("--privacy", choices=("public", "standard", "private", "local"), help="the privacy class of everything it sends")
    ad.add_argument("--json", action="store_true", help="print what was understood as JSON (adds only with --yes)")
    bind(ad, cmd_add)
    sh = csub.add_parser("show", help="one chore: what it does, when, its budget and history")
    sh.add_argument("id")
    sh.add_argument("--json", action="store_true")
    bind(sh, cmd_show)
    rn = csub.add_parser("run-now", help="run a chore once, now (exit 0 done, 3 partial, 4 needs a choice, 1 failed)")
    rn.add_argument("id")
    rn.add_argument("--yes", action="store_true", help="write the file it proposes without asking")
    rn.add_argument("--json", action="store_true")
    bind(rn, cmd_run_now)
    pa = csub.add_parser("pause", help="stop a chore running by itself; it keeps its history")
    pa.add_argument("id")
    bind(pa, lambda args, e: _switch(args, e, False))
    rs = csub.add_parser("resume", help="let a paused chore run again, from its next time")
    rs.add_argument("id")
    bind(rs, lambda args, e: _switch(args, e, True))
    ed = csub.add_parser("edit", help="change a chore by saying it again as a sentence")
    ed.add_argument("id")
    sentence_options(ed)
    bind(ed, cmd_edit)
    rm = csub.add_parser("rm", help="delete a chore and its history")
    rm.add_argument("id")
    rm.add_argument("--yes", action="store_true")
    bind(rm, cmd_rm)
    tk = csub.add_parser("tick", help="one pass: settle answered cards, queue what is due, run it (call this from a timer)")
    tk.add_argument("--enqueue-only", action="store_true", help="queue what is due but run nothing")
    tk.add_argument("--wait-card", type=float, metavar="SECONDS", default=0.0,
                    help="after a run, wait up to this long for its card to be answered (default: do not wait)")
    tk.add_argument("--json", action="store_true")
    bind(tk, cmd_tick)

    def usage(args: argparse.Namespace) -> int:
        p.print_help(sys.stderr)
        return 2

    p.set_defaults(func=usage)
