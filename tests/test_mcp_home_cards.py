"""`POST /v1/cards`: a device asks the person a question (added for the Muse tools server).

Through a real server on a Unix socket with the fake home behind it, and two
devices: the one that asks, and the one the person answers from.
"""

from __future__ import annotations

import json

import pytest

from home_fakes import assert_error, card_schema, client_for, running, schema_errors
from muse.home import api, cards, fake
from muse.home.auth import StaticVerifier

ASKER, ASKER_TOKEN = "host-tools", "asker-device-token-for-tests"
PHONE, PHONE_TOKEN = "my-phone", "phone-device-token-for-tests"
KEY_CANARY = "gsk_CANARYcard0123456789abcdefABCDEF"

GOOD = {"kind": "save_file", "title": "Save a file", "thing": "notes.md", "where": "Documents/Muse"}


class Rig:
    def __init__(self) -> None:
        self.home = fake.FakeHome("empty")
        self._ctx = running(self.home, StaticVerifier({ASKER: ASKER_TOKEN, PHONE: PHONE_TOKEN}, roles={ASKER: "host"}))
        self.srv = self._ctx.__enter__()
        self.asker = client_for(self.srv, ASKER_TOKEN)
        self.phone = client_for(self.srv, PHONE_TOKEN)
        self.replies: list[str] = []

    def ask(self, body, client=None):
        status, payload = (client or self.asker).call("POST", "/v1/cards", body)
        self.replies.append(json.dumps(payload))
        return status, payload

    def close(self) -> None:
        self._ctx.__exit__(None, None, None)
        self.home.close()


@pytest.fixture()
def rig():
    r = Rig()
    yield r
    r.close()


def test_a_device_asks_and_the_card_waits_with_its_name_on_it(rig):
    status, payload = rig.ask({**GOOD, "under": "Asked by a host bot", "extra": {"size_bytes": 12}})
    assert status == 201, payload
    card = payload["card"]
    assert schema_errors(card, card_schema()) == []
    assert (card["status"], card["kind"], card["thing"], card["where"]) == ("pending", "save_file", "notes.md", "Documents/Muse")
    assert card["job_ref"] == f"device:{ASKER}" == api.asked_by(api.Device(ASKER))
    assert card["under"] == {"text": "Asked by a host bot", "mandate_id": None} and card["cost"] == {"nothing": True}
    assert rig.phone.call("GET", "/v1/cards?status=pending")[1]["cards"][0]["id"] == card["id"]


def test_the_person_answers_from_another_device_and_the_asker_reads_the_answer(rig):
    card_id = rig.ask(GOOD)[1]["card"]["id"]
    status, payload = rig.phone.call("POST", f"/v1/cards/{card_id}/answer", {"decision": "decline"})
    assert status == 200 and payload["card"]["status"] == "declined" and payload["card"]["answered_by"] == PHONE
    assert rig.asker.call("GET", f"/v1/cards/{card_id}")[1]["card"]["status"] == "declined"


def test_the_asking_device_cannot_answer_its_own_card(rig):
    card_id = rig.ask(GOOD)[1]["card"]["id"]
    for decision in ("approve", "decline"):
        status, payload = rig.asker.call("POST", f"/v1/cards/{card_id}/answer", {"decision": decision})
        assert_error(status, payload, 403, "forbidden")
    assert rig.home.cards.get(card_id)["status"] == "pending"
    assert rig.phone.call("POST", f"/v1/cards/{card_id}/answer", {"decision": "approve"})[0] == 200
    # A host device answers nothing at all, also not a card the home itself made.
    own = rig.home.cards.create(kind="save_file", title="t", thing="x")
    assert_error(*rig.asker.call("POST", f"/v1/cards/{own}/answer", {"decision": "approve"}), 403, "forbidden")
    assert rig.home.cards.get(own)["status"] == "pending"
    # The person's own device may answer the home's cards, but not a question it asked itself.
    assert rig.phone.call("POST", f"/v1/cards/{own}/answer", {"decision": "approve"})[0] == 200
    asked_by_phone = rig.phone.call("POST", "/v1/cards", GOOD)[1]["card"]["id"]
    err = assert_error(*rig.phone.call("POST", f"/v1/cards/{asked_by_phone}/answer", {"decision": "approve"}), 403, "forbidden")
    assert "asked for itself" in err["message"]


def test_the_lifetime_is_the_stores_default_or_what_was_asked(rig):
    spend = {"kind": "spend", "title": "Pay", "thing": "Top-up", "cost": {"amount": "30", "currency": "TWD"},
             "extra": {"payee": "ACME", "limit_left": {"amount": "70", "currency": "TWD"}}}
    now = rig.home.clock()
    for body, seconds in ((GOOD, cards.DEFAULT_TTL_S), (spend, cards.SPEND_TTL_S), ({**GOOD, "ttl_s": 90}, 90)):
        status, payload = rig.ask(body)
        assert status == 201, payload
        row = rig.home.cards.get(payload["card"]["id"])
        from datetime import datetime

        expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")).timestamp()
        assert abs(expires - (now + seconds)) < 5


@pytest.mark.parametrize("body", [
    {},
    {"kind": "save_file", "title": "t"},  # no thing
    {**GOOD, "kind": "make_coffee"},
    {**GOOD, "title": "x" * (cards.MAX_TITLE + 1)},
    {**GOOD, "thing": ""},
    {**GOOD, "job_ref": "device:my-phone"},  # the home names the asker; a request cannot
    {**GOOD, "mandate_id": "m1"},
    {**GOOD, "status": "approved"},
    {**GOOD, "ttl_s": 0},
    {**GOOD, "ttl_s": cards.MAX_TTL_S + 1},
    {**GOOD, "ttl_s": True},
    {**GOOD, "cost": {"amount": "free", "currency": "TWD"}},
    {**GOOD, "extra": {"surprise": 1}},
    {**GOOD, "under": {"text": "x", "mandate_id": "m"}},
    {"kind": "spend", "title": "Pay", "thing": "x", "extra": {"payee": "ACME", "limit_left": {"amount": "1", "currency": "USD"}}},
    {"kind": "message", "title": "Send", "thing": "x"},  # a message needs its preview
])
def test_a_card_that_breaks_the_stores_rules_is_refused_and_nothing_is_made(rig, body):
    status, payload = rig.ask(body)
    assert_error(status, payload, 400, "invalid_request")
    assert rig.home.cards.counts()["pending"] == 0


def test_key_shaped_text_is_refused_and_never_repeated(rig, caplog):
    for body in ({**GOOD, "thing": f"use {KEY_CANARY}"}, {**GOOD, "extra": {"size_bytes": 1}, "where": KEY_CANARY},
                 {"kind": "message", "title": "Send", "thing": "x", "extra": {"preview": f"key {KEY_CANARY}"}}):
        status, payload = rig.ask(body)
        assert_error(status, payload, 400, "invalid_request")
    assert rig.home.cards.counts()["pending"] == 0
    assert KEY_CANARY not in "".join(rig.replies) and KEY_CANARY not in caplog.text
    assert "CANARY" not in "".join(rig.replies)


def test_the_reply_to_a_refused_card_never_repeats_the_request(rig):
    marker = "MARKER-7f3a-in-the-request"
    status, payload = rig.ask({**GOOD, "kind": marker})
    assert_error(status, payload, 400, "invalid_request")
    status, payload = rig.ask({**GOOD, "extra": {marker: 1}})
    assert_error(status, payload, 400, "invalid_request")
    assert marker not in "".join(rig.replies)


def test_a_device_may_have_only_so_many_cards_waiting(rig):
    ids = []
    for _ in range(api.MAX_PENDING_PER_DEVICE):
        status, payload = rig.ask(GOOD)
        assert status == 201
        ids.append(payload["card"]["id"])
    status, payload = rig.ask(GOOD)
    err = assert_error(status, payload, 409, "conflict")
    assert str(api.MAX_PENDING_PER_DEVICE) in err["message"]
    # The cap is per device: another device can still ask, and an answer frees a place.
    assert rig.ask(GOOD, rig.phone)[0] == 201
    assert rig.phone.call("POST", f"/v1/cards/{ids[0]}/answer", {"decision": "decline"})[0] == 200
    assert rig.ask(GOOD)[0] == 201
    assert rig.ask(GOOD)[0] == 409


def test_an_expired_card_frees_its_place_too(rig):
    t = [1_800_000_000.0]
    rig.home.cards.clock = lambda: t[0]
    for _ in range(api.MAX_PENDING_PER_DEVICE):
        assert rig.ask({**GOOD, "ttl_s": 60})[0] == 201
    assert rig.ask(GOOD)[0] == 409
    t[0] += 61
    assert rig.ask(GOOD)[0] == 201


def test_asking_needs_a_device_token_like_every_other_call(rig):
    for token in (None, "wrong-token"):
        status, payload = client_for(rig.srv, token).call("POST", "/v1/cards", GOOD)
        assert_error(status, payload, 401, "unauthorized")
    assert rig.home.cards.counts()["pending"] == 0


def test_the_fake_home_in_its_error_state_refuses_plainly():
    home = fake.FakeHome("error")
    try:
        with running(home, StaticVerifier({ASKER: ASKER_TOKEN})) as srv:
            status, payload = client_for(srv, ASKER_TOKEN).call("POST", "/v1/cards", GOOD)
            assert_error(status, payload, 503, "unavailable")
    finally:
        home.close()
