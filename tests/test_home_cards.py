"""muse.home.cards: the card format and the pending-approval store."""

from __future__ import annotations
from wardmuse.envutil import envget, envhas

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from home_fakes import ROOT, card_schema, schema_errors
from wardmuse.home import cards
from wardmuse.home.cards import CardAlreadyAnswered, CardError, CardExpired, CardNotFound, CardStore
from wardmuse.home.fake import demo_cards, seed_cards

SECRET = "gsk_" + "Zx9" * 12  # shaped like a provider key
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def store(clock):
    return CardStore(clock=clock)


def save_file(store, **over):
    args = dict(kind="save_file", title="Save a file", thing="Weekly digest 13 Nov.md", where="Documents/Muse",
                under="Weekly digest chore")
    args.update(over)
    return store.create(**args)


VALID_EXTRA = {
    "save_file": {"size_bytes": 10},
    "message": {"preview": "Hi Mei,\nSaturday works.", "to": "Mei", "draft": False},
    "spend": {"payee": "Deepgram", "limit_left": {"amount": "24", "currency": "TWD"}},
    "grant_permission": {"sentence": "Every Monday, summarise three feeds.", "limits": ["Reads 3 feeds"], "ends_at": "2027-02-13T00:00:00Z"},
    "oneoff_credit": {"service": "Cohere", "credit_left": "about 400 calls"},
    "send_private": {"badge": "Private", "services": ["Groq"]},
    "install_skill": {"asks_for": ["Read web pages you list"], "author": "Muse", "sha256": "ab" * 32},
}


def make(store, kind, **over):
    args = dict(kind=kind, title="Do a thing", thing="The thing", where="Somewhere", under={"text": "A chore", "mandate_id": "m-1"},
                extra=VALID_EXTRA[kind], job_ref="job:1")
    if kind == "spend":
        args["cost"] = {"amount": "6", "currency": "TWD"}
    args.update(over)
    return store.create(**args)


# ---------------------------------------------------------------- shape


def test_the_seven_kinds_are_the_ones_in_the_ux_spec():
    assert cards.KINDS == ("save_file", "message", "spend", "grant_permission", "oneoff_credit", "send_private", "install_skill")
    schema = card_schema()
    assert tuple(schema["properties"]["kind"]["enum"]) == cards.KINDS
    assert tuple(schema["properties"]["status"]["enum"]) == cards.STATUSES
    assert set(schema["required"]) == set(schema["properties"])  # every field is always present


@pytest.mark.parametrize("kind", cards.KINDS)
def test_every_kind_round_trips_and_matches_the_schema(store, kind):
    card = store.get(make(store, kind))
    assert schema_errors(card, card_schema()) == []
    assert card["kind"] == kind and card["status"] == "pending" and card["extra"] == VALID_EXTRA[kind]
    assert card["under"] == {"text": "A chore", "mandate_id": "m-1"} and card["job_ref"] == "job:1"
    assert card["answered_at"] is None and card["answered_by"] is None and card["always"] is False
    assert card["cost"] == ({"amount": "6", "currency": "TWD"} if kind == "spend" else {"nothing": True})  # always present


def test_a_created_card_has_every_field_of_the_shape(store, clock):
    card_id = save_file(store)
    card = store.get(card_id)
    assert card == {
        "id": card_id, "kind": "save_file", "title": "Save a file", "thing": "Weekly digest 13 Nov.md", "where": "Documents/Muse",
        "under": {"text": "Weekly digest chore", "mandate_id": None}, "cost": {"nothing": True}, "extra": {},
        "created_at": "2027-01-15T08:00:00Z", "expires_at": "2027-01-16T08:00:00Z", "status": "pending", "answered_at": None,
        "answered_by": None, "always": False, "job_ref": None, "title_key": None, "params": {},
    }


def test_text_is_stored_exactly_as_given_never_escaped(store):
    risky = '<img src=x onerror="alert(1)"> & "quotes" 中文 ‮'
    card = store.get(save_file(store, title=risky[:80], thing=risky, where=risky, under=risky))
    assert card["thing"] == risky and card["where"] == risky and card["under"]["text"] == risky and card["title"] == risky[:80]


def test_the_demo_cards_cover_every_kind_and_match_the_schema(store, clock):
    demo = demo_cards()
    assert {c["kind"] for c in demo if c["state"] == "pending"} == set(cards.KINDS)
    ids = seed_cards(store, demo, clock())
    got = [store.get(i) for i in ids]
    assert all(schema_errors(c, card_schema()) == [] for c in got)
    assert sorted(c["status"] for c in got).count("pending") == 7
    assert {c["status"] for c in got} == set(cards.STATUSES)
    approved = next(c for c in got if c["status"] == "approved")
    assert approved["always"] is True and approved["answered_by"] == "desktop" and approved["answered_at"]


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("change, words", [
    (dict(kind="reboot"), "kind must be one of"),
    (dict(title=""), "title cannot be empty"),
    (dict(title="   "), "title cannot be empty"),
    (dict(title="x" * 81), "longer than 80"),
    (dict(thing="x" * 201), "longer than 200"),
    (dict(where="x" * 201), "longer than 200"),
    (dict(under="x" * 201), "longer than 200"),
    (dict(title=7), "must be text"),
    (dict(thing="a\x00b"), "NUL"),
    (dict(mandate_id="has space"), "mandate_id"),
    (dict(job_ref="x" * 129), "job_ref"),
    (dict(under={"text": "ok", "who": "x"}), "under takes only"),
    (dict(cost={"amount": 6, "currency": "TWD"}), "decimal string"),
    (dict(cost={"amount": "6.123456789", "currency": "TWD"}), "decimal string"),
    (dict(cost={"amount": "-6", "currency": "TWD"}), "decimal string"),
    (dict(cost={"amount": "6", "currency": "twd"}), "currency"),
    (dict(cost={"amount": "6"}), "exactly an amount and a currency"),
    (dict(cost={"nothing": False}), "cost.nothing must be true"),
    (dict(cost={}), "exactly an amount and a currency"),
    (dict(extra={"bogus": 1}), "does not take bogus"),
    (dict(extra=[1]), "extra must be an object"),
    (dict(extra={"size_bytes": -1}), "whole number"),
    (dict(extra={"size_bytes": True}), "whole number"),
    (dict(ttl_s=0), "must expire"),
    (dict(ttl_s=31 * 86400), "must expire"),
])
def test_invalid_cards_are_refused_with_a_plain_reason(store, change, words):
    with pytest.raises(CardError) as err:
        save_file(store, **change)
    assert words in str(err.value)
    assert store.list() == []


@pytest.mark.parametrize("kind, extra, words", [
    ("message", {}, "needs preview"),
    ("message", {"preview": "x" * 501}, "longer than 500"),
    ("message", {"preview": "hi", "draft": "yes"}, "draft must be true or false"),
    ("spend", {"payee": "X"}, "needs limit_left"),
    ("spend", {"payee": "X", "limit_left": {"amount": "1"}}, "limit_left"),
    ("grant_permission", {"sentence": "s", "limits": []}, "needs ends_at"),
    ("grant_permission", {"sentence": "s", "limits": ["a"] * 13, "ends_at": None}, "list of at most 12"),
    ("grant_permission", {"sentence": "s", "limits": [], "ends_at": "next year"}, "ends_at"),
    ("oneoff_credit", {}, "needs service"),
    ("send_private", {"badge": "Private", "services": []}, "1 to 12"),
    ("install_skill", {"asks_for": ["x"], "author": "A", "sha256": "ABC"}, "sha256"),
    ("install_skill", {"asks_for": "everything", "author": "A"}, "must be a list"),
])
def test_the_kind_specific_line_is_validated(store, kind, extra, words):
    with pytest.raises(CardError) as err:
        make(store, kind, extra=extra)
    assert words in str(err.value)


def test_a_spend_card_must_say_how_much(store):
    with pytest.raises(CardError, match="needs an amount"):
        make(store, "spend", cost=None)
    with pytest.raises(CardError, match="needs an amount"):
        make(store, "spend", cost={"nothing": True})


@pytest.mark.parametrize("field", ["title", "thing", "where", "under", "extra", "job_ref"])
def test_nothing_in_a_card_may_look_like_a_key(store, field):
    over = {field: SECRET}
    if field == "extra":
        over = {"kind": "message", "extra": {"preview": f"my key is {SECRET}"}}
    with pytest.raises(CardError) as err:
        save_file(store, **over)
    assert "looks like a key or token" in str(err.value)
    assert SECRET not in str(err.value)  # the refusal never repeats it
    assert store.list() == []
    assert SECRET.encode() not in store.path.read_bytes()


def test_the_schema_refuses_what_the_store_refuses():
    """The file and the code describe the same shape: break a valid card one way at a time."""
    schema = card_schema()
    good = {
        "id": "0123456789abcdef", "kind": "spend", "title": "Pay", "thing": "A transcript", "where": "Deepgram",
        "under": {"text": "Paid services", "mandate_id": None}, "cost": {"amount": "6", "currency": "TWD"},
        "extra": {"payee": "Deepgram", "limit_left": {"amount": "24", "currency": "TWD"}},
        "created_at": "2026-11-13T06:00:00Z", "expires_at": "2026-11-13T06:05:00Z", "status": "pending",
        "answered_at": None, "answered_by": None, "always": False, "job_ref": None, "title_key": None, "params": {},
    }
    assert schema_errors(good, schema) == []
    breaks = [
        ("id", "XYZ"), ("kind", "reboot"), ("title", ""), ("title", "x" * 81), ("thing", "x" * 201), ("status", "maybe"),
        ("cost", {"nothing": True}), ("cost", {"nothing": False}), ("cost", {"amount": 6, "currency": "TWD"}), ("cost", {}),
        ("extra", {"payee": "X"}), ("extra", {"payee": "X", "limit_left": {"amount": "1", "currency": "TWD"}, "more": 1}),
        ("under", {"text": "x"}), ("under", {"text": "x", "mandate_id": "has space"}), ("created_at", "yesterday"),
        ("always", True), ("answered_by", "phone"), ("always", "no"), ("job_ref", "x" * 129),
    ]
    for name, value in breaks:
        assert schema_errors({**good, name: value}, schema), (name, value)
    for name in good:
        assert schema_errors({k: v for k, v in good.items() if k != name}, schema), name
    assert schema_errors({**good, "surprise": 1}, schema)
    for kind, extra in VALID_EXTRA.items():
        cost = good["cost"] if kind == "spend" else {"nothing": True}
        assert schema_errors({**good, "kind": kind, "extra": extra, "cost": cost}, schema) == []
        assert schema_errors({**good, "kind": kind, "extra": {**extra, "bogus": 1}, "cost": cost}, schema)


# ---------------------------------------------------------------- lifecycle


def test_list_is_newest_first_and_filters_by_status(store, clock):
    first = save_file(store, thing="one")
    clock.now += 10
    second = save_file(store, thing="two")
    clock.now += 10
    third = save_file(store, thing="three")
    store.answer(second, "decline", "phone")
    assert [c["id"] for c in store.list()] == [third, second, first]
    assert [c["id"] for c in store.list("pending")] == [third, first]
    assert [c["id"] for c in store.list(["declined", "approved"])] == [second]
    assert store.list("expired") == []
    assert store.counts() == {"pending": 2, "approved": 0, "declined": 1, "expired": 0}
    with pytest.raises(CardError, match="status must be one of"):
        store.list("open")


@pytest.mark.parametrize("decision, status, always", [("approve", "approved", False), ("approve", "approved", True), ("decline", "declined", False)])
def test_answering_records_who_when_and_always(store, clock, decision, status, always):
    card_id = save_file(store)
    clock.now += 42
    card = store.answer(card_id, decision, "mei-phone", always)
    assert card["status"] == status and card["answered_by"] == "mei-phone" and card["always"] is always
    assert card["answered_at"] == "2027-01-15T08:00:42Z"
    assert store.get(card_id) == card
    assert schema_errors(card, card_schema()) == []


@pytest.mark.parametrize("second", ["approve", "decline"])
def test_answering_twice_is_an_error_and_changes_nothing(store, clock, second):
    card_id = save_file(store)
    before = store.answer(card_id, "decline", "phone")
    clock.now += 5
    with pytest.raises(CardAlreadyAnswered):
        store.answer(card_id, second, "desktop", second == "approve")
    assert store.get(card_id) == before


def test_bad_answers_are_refused(store):
    card_id = save_file(store)
    for args, exc in [
        ((card_id, "maybe", "phone"), CardError), ((card_id, "decline", "phone", True), CardError),
        ((card_id, "approve", "phone", "yes"), CardError), ((card_id, "approve", ""), CardError),
        (("0" * 16, "approve", "phone"), CardNotFound), (("../../etc", "approve", "phone"), CardNotFound),
        (("", "approve", "phone"), CardNotFound),
    ]:
        with pytest.raises(exc):
            store.answer(*args)
    assert store.get(card_id)["status"] == "pending"
    with pytest.raises(CardNotFound):
        store.get("f" * 16)


def test_default_life_is_24_hours_and_5_minutes_for_a_payment(store, clock):
    plain, pay = save_file(store), make(store, "spend")
    assert store.get(plain)["expires_at"] == "2027-01-16T08:00:00Z"
    assert store.get(pay)["expires_at"] == "2027-01-15T08:05:00Z"
    clock.now += 299
    assert store.get(pay)["status"] == "pending"
    clock.now += 1
    assert store.get(pay)["status"] == "expired" and store.get(plain)["status"] == "pending"
    clock.now = T0 + 86400
    assert store.get(plain)["status"] == "expired"
    assert schema_errors(store.get(plain), card_schema()) == []


def test_an_expired_card_cannot_be_approved_or_declined(store, clock):
    card_id = make(store, "spend")
    clock.now += 300  # the moment it runs out, before anything else noticed
    with pytest.raises(CardExpired):
        store.answer(card_id, "approve", "phone")
    card = store.get(card_id)
    assert card["status"] == "expired" and card["answered_by"] is None and card["answered_at"] is None
    with pytest.raises(CardExpired):
        store.answer(card_id, "decline", "phone")


def test_expire_due_marks_only_what_ran_out_and_leaves_answers_alone(store, clock):
    short = save_file(store, ttl_s=60)
    answered = save_file(store, ttl_s=60)
    long_ = save_file(store, ttl_s=3600)
    store.answer(answered, "approve", "phone")
    assert store.expire_due(T0 + 59) == 0
    assert store.expire_due(T0 + 60) == 1
    assert store.expire_due(T0 + 60) == 0
    clock.now = T0 + 61
    assert {c["id"]: c["status"] for c in store.list()} == {short: "expired", answered: "approved", long_: "pending"}
    assert store.expire_due() == 0


def test_a_custom_life_is_kept(store):
    assert store.get(save_file(store, ttl_s=90))["expires_at"] == "2027-01-15T08:01:30Z"


def test_too_many_waiting_cards_are_refused(store, monkeypatch):
    monkeypatch.setattr(cards, "MAX_PENDING", 3)
    ids = [save_file(store) for _ in range(3)]
    with pytest.raises(CardError, match="already 3 cards waiting"):
        save_file(store)
    store.answer(ids[0], "decline", "phone")
    save_file(store)  # room again


# ---------------------------------------------------------------- wait


def test_wait_returns_when_another_thread_answers():
    store = CardStore()
    card_id = save_file(store)
    timer = threading.Timer(0.3, lambda: CardStore().answer(card_id, "approve", "phone"))
    timer.start()
    started = time.monotonic()
    try:
        assert store.wait(card_id, timeout=10, poll_s=0.05) == "approved"
    finally:
        timer.join()
    assert time.monotonic() - started < 5


def test_wait_times_out_still_pending_and_sees_expiry(store, clock):
    card_id = save_file(store, ttl_s=60)
    assert store.wait(card_id, timeout=0.2, poll_s=0.05) == "pending"
    assert store.wait(card_id, timeout=0) == "pending"
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += 61

    assert store.wait(card_id, timeout=5, poll_s=0.1, sleep=sleep) == "expired"
    assert len(sleeps) == 1
    with pytest.raises(CardNotFound):
        store.wait("0" * 16, timeout=1)


# ---------------------------------------------------------------- the file


def test_the_database_is_private_and_separate():
    store = CardStore()
    save_file(store)
    home = Path(envget(os.environ, "MUSE_HOME"))
    assert store.path == home / "cards.sqlite"
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert not (home / "ledger.sqlite").exists() and not (home / "queue.sqlite").exists()


def test_a_loosened_file_is_tightened_again():
    store = CardStore()
    save_file(store)
    os.chmod(store.path, 0o644)
    store.list()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


ANSWER_SCRIPT = """
import sys, time
from wardmuse.home.cards import CardStore, CardError
card_id, device, decision, go_at = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
store = CardStore()
time.sleep(max(0.0, go_at - time.time()))
try:
    store.answer(card_id, decision, device)
    print("won")
except CardError as exc:
    print(exc.code)
"""


def test_two_processes_answering_at_once_exactly_one_wins():
    store = CardStore()
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    for round_ in range(4):
        card_id = save_file(store, thing=f"round {round_}")
        go_at = time.time() + 1.0
        procs = [
            subprocess.Popen([sys.executable, "-c", ANSWER_SCRIPT, card_id, device, decision, str(go_at)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
            for device, decision in (("phone", "approve"), ("desktop", "decline"), ("tablet", "approve"))
        ]
        results = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            assert proc.returncode == 0, err
            results.append(out.strip())
        assert sorted(results) == ["card_already_answered", "card_already_answered", "won"], results
        card = store.get(card_id)
        winner = ("phone", "desktop", "tablet")[results.index("won")]
        assert card["answered_by"] == winner
        assert card["status"] == ("declined" if winner == "desktop" else "approved")


def test_many_threads_creating_and_answering_do_not_collide():
    store = CardStore()
    ids = [save_file(store, thing=f"card {i}") for i in range(8)]
    wins: list[str] = []
    errors: list[Exception] = []

    def answer(card_id: str, device: str) -> None:
        try:
            CardStore().answer(card_id, "approve", device)
            wins.append(card_id)
        except CardAlreadyAnswered:
            pass
        except Exception as exc:  # pragma: no cover - the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=answer, args=(card_id, f"d{n}")) for card_id in ids for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sorted(wins) == sorted(ids)
    assert json.dumps(store.counts()) == json.dumps({"pending": 0, "approved": 8, "declined": 0, "expired": 0})
