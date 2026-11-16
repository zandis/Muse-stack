"""Records over the home API: what the home writes down itself, what a device may report, and what a reader sees."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from home_fakes import assert_error  # noqa: E402
from muse import cli, keys  # noqa: E402
from muse.home import api, auth  # noqa: E402
from muse.records import format as fmt  # noqa: E402
from muse.records.home import HomeRecords, records_path  # noqa: E402

KEY_CANARY = "gsk_CANARYkey0123456789abcdefABCDEF"
OWNER = auth.Device("mei-phone", role="owner")
HOST = auth.Device("bot-tools", role="host")
VIEWER = auth.Device("wall-screen", role="viewer")


class Clock:
    def __init__(self, now: float = 1_794_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        self.now += 1.0  # every look at the clock is a second later, so records have distinct, ordered times
        return self.now


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / "keys.env"))
    monkeypatch.setenv("MUSE_TZ", "UTC")
    monkeypatch.setattr(keys, "_keyring", lambda: None)
    for name in list(keys.KNOWN_KEYS) + ["MUSE_DEFAULT_PRIVACY", "MUSE_APPS_FILE"]:
        monkeypatch.delenv(name, raising=False)
    return api.Home(proxy_running=lambda: False, clock=Clock())


def call(home, who, method, path, body=None):
    resp = api.dispatch(home, who, method, path, b"" if body is None else json.dumps(body).encode())
    return resp.status, resp.payload


def ok(home, who, method, path, body=None, status=200):
    got, payload = call(home, who, method, path, body)
    assert got == status, (got, payload)
    return payload


def chain() -> list[dict]:
    return HomeRecords().records()


def brief(record: dict) -> tuple:
    return record["action"], record["actor"], record["mandate"], record["outcome"], record.get("egress"), record["summary"]


# ---------------------------------------------------------------- reading


def test_a_home_with_no_records_says_so(home):
    assert ok(home, OWNER, "GET", "/v1/receipts") == {
        "records": [], "order": "newest_first", "next_before": None, "verify": {"ok": True, "size": 0}, "sealed": None, "device": None}
    assert not (Path(os.environ["MUSE_HOME"]) / "device.key").exists()  # looking makes no key


def test_records_come_newest_first_in_bounded_pages(home):
    for i in range(7):
        ok(home, HOST, "POST", "/v1/receipts", {"action": "host.files.write", "summary": f"Wrote file number {i}"}, status=201)
    first = ok(home, OWNER, "GET", "/v1/receipts?limit=3")
    assert [r["seq"] for r in first["records"]] == [6, 5, 4] and first["next_before"] == 4 and first["order"] == "newest_first"
    assert first["verify"] == {"ok": True, "size": 7} and first["device"] == HomeRecords().device() and first["sealed"] is None
    second = ok(home, OWNER, "GET", f"/v1/receipts?limit=3&before={first['next_before']}")
    assert [r["seq"] for r in second["records"]] == [3, 2, 1] and second["next_before"] == 1
    last = ok(home, OWNER, "GET", "/v1/receipts?limit=3&before=1")
    assert [r["seq"] for r in last["records"]] == [0] and last["next_before"] is None
    assert len(ok(home, OWNER, "GET", "/v1/receipts")["records"]) == 7  # the default page is larger than seven
    for record in first["records"]:
        fmt.verify_record(record, first["device"])  # exactly the stored, signed record
    assert api.MAX_RECEIPTS_PAGE == 200 and api.DEFAULT_RECEIPTS_PAGE == 50


def test_filters_since_action_and_left_device(home):
    post = lambda body: ok(home, HOST, "POST", "/v1/receipts", body, status=201)["record"]  # noqa: E731
    post({"action": "host.files.write", "summary": "Wrote a file", "egress": "none"})
    sent = post({"action": "host.message.send", "summary": "Sent a message", "egress": "service:gmail"})
    post({"action": "host.files.read", "summary": "Read a file"})  # does not say where it went
    ok(home, OWNER, "POST", "/v1/pause")
    seqs = lambda query: [r["seq"] for r in ok(home, VIEWER, "GET", "/v1/receipts" + query)["records"]]  # noqa: E731
    assert seqs("") == [3, 2, 1, 0]
    assert seqs("?action=host.files") == [2, 0] and seqs("?action=host.files.write") == [0] and seqs("?action=host") == [2, 1, 0] and seqs("?action=home") == [3]
    assert seqs("?left_device=true") == [1] and seqs("?left_device=false") == [3, 2, 1, 0]
    when = api._iso_ms(sent["time"])
    assert seqs(f"?since={when}") == [3, 2, 1] and seqs("?since=2100-01-01T00:00:00Z") == []
    assert seqs(f"?since={when}&action=host.files&limit=1") == [2]


def test_filter_by_the_permission_a_record_ran_under(home):
    HomeRecords().write("files.write", "Saved a file", actor="helper", mandate="m1_" + "a" * 64, egress="none")
    HomeRecords().write("files.write", "Saved another file", actor="helper", mandate="m1_" + "b" * 64, egress="none")
    ok(home, HOST, "POST", "/v1/receipts", {"action": "host.files.write", "summary": "Wrote a file"}, status=201)
    seqs = lambda query: [r["seq"] for r in ok(home, VIEWER, "GET", "/v1/receipts" + query)["records"]]  # noqa: E731
    assert seqs("?mandate=m1_" + "a" * 64) == [0] and seqs("?mandate=m1_" + "b" * 64) == [1]
    assert seqs("?mandate=device:bot-tools") == [2] and seqs("?mandate=m1_" + "c" * 64) == []
    assert seqs("?mandate=device:bot-tools&action=files") == []


def test_export_is_the_file_anyone_can_check_and_only_the_person_may_make_it(home):
    from muse.records.seal import verify_export

    assert_error(*call(home, OWNER, "POST", "/v1/receipts/export", {}), 409, "conflict")  # nothing to export yet
    ok(home, OWNER, "POST", "/v1/pause")
    ok(home, OWNER, "POST", "/v1/resume")
    got = ok(home, OWNER, "POST", "/v1/receipts/export", {})
    report = verify_export(got["export"])
    assert report["complete"] and report["count"] == got["count"] == got["size"] == 2 and got["redacted"] == 0
    assert got["export"]["type"] == "muse.records.export" and got["device"] == report["device"]
    assert got["file_name"].startswith("muse-records-") and got["file_name"].endswith(".json") and got["recorded"] is True
    assert chain()[-1]["action"] == "records.export"  # written down, after the seal
    hidden = ok(home, OWNER, "POST", "/v1/receipts/export", {"redact_summaries": True})
    assert hidden["redacted"] == hidden["count"] == 3 and all("summary" not in r for r in hidden["export"]["records"])
    assert verify_export(hidden["export"])["redacted"] == 3
    for who in (HOST, VIEWER):
        assert_error(*call(home, who, "POST", "/v1/receipts/export", {}), 403, "forbidden")
    for body in ({"redact_summaries": "yes"}, {"since": "2026-11-01T00:00:00Z"}):
        assert_error(*call(home, OWNER, "POST", "/v1/receipts/export", body), 400, "invalid_request")


def test_records_that_do_not_check_out_are_never_exported(home):
    ok(home, OWNER, "POST", "/v1/pause")
    ok(home, OWNER, "POST", "/v1/resume")
    conn = sqlite3.connect(str(records_path()))
    body = json.loads(conn.execute("SELECT body FROM records WHERE seq=0").fetchone()[0])
    with conn:
        conn.execute("UPDATE records SET body=? WHERE seq=0", (json.dumps({**body, "summary": "Changed afterwards"}),))
    conn.close()
    assert_error(*call(home, OWNER, "POST", "/v1/receipts/export", {}), 409, "conflict")
    assert ok(home, OWNER, "GET", "/v1/receipts")["verify"] == {"ok": False, "size": 2, "kind": "summary_mismatch", "index": 0}


@pytest.mark.parametrize("query", ["?limit=0", "?limit=201", "?limit=ten", "?limit=-1", "?before=x", "?before=-3", "?since=yesterday",
                                   "?since=2026-11-01T00:00:00", "?action=Files", "?action=a.b.c.d.e", "?action=files.", "?left_device=yes",
                                   "?page=2", "?limit=1&limit=2", "?mandate=", "?mandate=a%20b", "?mandate=" + "m" * 129])
def test_bad_queries_are_refused(home, query):
    assert_error(*call(home, OWNER, "GET", "/v1/receipts" + query), 400, "invalid_request")


def test_the_reply_says_what_the_last_seal_covers(home):
    ok(home, OWNER, "POST", "/v1/pause")
    ok(home, OWNER, "POST", "/v1/resume")
    cp = HomeRecords().seal()
    ok(home, OWNER, "POST", "/v1/pause")
    got = ok(home, OWNER, "GET", "/v1/receipts")
    assert got["verify"] == {"ok": True, "size": 3}
    assert got["sealed"] == {"size": 2, "root": cp["root"], "time": api._iso_ms(cp["time"])}


def test_a_changed_records_file_is_reported_and_what_can_be_read_is_still_shown(home):
    for _ in range(3):
        ok(home, OWNER, "POST", "/v1/pause")
    assert ok(home, OWNER, "GET", "/v1/receipts")["verify"] == {"ok": True, "size": 3}
    conn = sqlite3.connect(str(records_path()))
    conn.execute("UPDATE records SET body=replace(body, 'Paused', 'Pausee') WHERE seq=1")
    conn.commit()
    conn.close()
    got = ok(home, OWNER, "GET", "/v1/receipts")  # the earlier answer is not reused: the file changed
    assert got["verify"] == {"ok": False, "size": 3, "kind": "summary_mismatch", "index": 1}
    assert [r["seq"] for r in got["records"]] == [2, 1, 0]
    # A seal over records that are then cut away is noticed too.
    home2 = api.Home(proxy_running=lambda: False)
    conn = sqlite3.connect(str(records_path()))
    conn.execute("UPDATE records SET body=replace(body, 'Pausee', 'Paused') WHERE seq=1")
    conn.commit()
    conn.close()
    HomeRecords().seal()
    conn = sqlite3.connect(str(records_path()))
    conn.execute("DELETE FROM records WHERE seq=2")
    conn.commit()
    conn.close()
    cut = ok(home2, OWNER, "GET", "/v1/receipts")
    assert cut["verify"]["ok"] is False and cut["verify"]["kind"] == "size_mismatch" and cut["sealed"]["size"] == 3


def test_the_check_is_kept_until_the_file_changes(home, monkeypatch):
    ok(home, OWNER, "POST", "/v1/pause")
    calls = []
    real = HomeRecords.verify
    monkeypatch.setattr(HomeRecords, "verify", lambda self: calls.append(1) or real(self))
    for _ in range(3):
        assert ok(home, OWNER, "GET", "/v1/receipts")["verify"]["ok"] is True
    assert len(calls) == 1
    ok(home, OWNER, "POST", "/v1/resume")
    assert ok(home, OWNER, "GET", "/v1/receipts")["verify"] == {"ok": True, "size": 2} and len(calls) == 2


# ---------------------------------------------------------------- what the home writes down itself


def test_pause_and_resume_leave_the_same_record_as_the_command_line(home, capsys):
    assert ok(home, OWNER, "POST", "/v1/pause") == {"paused": True, "recorded": True}
    assert ok(home, OWNER, "POST", "/v1/resume") == {"paused": False, "was_paused": True, "recorded": True}
    assert ok(home, OWNER, "POST", "/v1/resume") == {"paused": False, "was_paused": False, "recorded": False}
    assert cli.main(["pause"]) == 0 and cli.main(["resume"]) == 0
    capsys.readouterr()
    through_api, by_hand = chain()[:2], chain()[2:]
    assert [brief(r) for r in through_api] == [brief(r) for r in by_hand] == [
        ("home.pause", "person", None, "ok", "none", "Paused the queue"),
        ("home.resume", "person", None, "ok", "none", "Resumed the queue")]
    assert fmt.verify_chain(chain()) == 4


def test_a_card_asked_and_answered_is_recorded_with_who_and_what_was_decided(home):
    asked = ok(home, HOST, "POST", "/v1/cards", {"kind": "save_file", "title": "Save a file", "thing": "Private diary.md",
                                                  "where": "Documents/Secret plans"}, status=201)
    assert asked["recorded"] is True
    card = asked["card"]["id"]
    answered = ok(home, OWNER, "POST", f"/v1/cards/{card}/answer", {"decision": "approve", "always": True})
    assert answered["recorded"] is True
    second = ok(home, HOST, "POST", "/v1/cards", {"kind": "spend", "title": "Pay", "thing": "NT$30 to ACME",
                                                   "cost": {"amount": "30", "currency": "TWD"},
                                                   "extra": {"payee": "ACME", "limit_left": {"amount": "70", "currency": "TWD"}}}, status=201)
    ok(home, OWNER, "POST", f"/v1/cards/{second['card']['id']}/answer", {"decision": "decline"})
    records = chain()
    assert [brief(r) for r in records] == [
        ("card.create", "helper", "device:bot-tools", "ok", "none", "Asked whether to save a file"),
        ("card.answer", "person", None, "ok", "none", "Approved, and always from now on: save a file (answered on mei-phone)"),
        ("card.create", "helper", "device:bot-tools", "ok", "none", "Asked whether to spend money"),
        ("card.answer", "person", None, "declined", "none", "Declined: spend money (answered on mei-phone)")]
    assert [r["links"] for r in records] == [{"card": card}, {"card": card}, {"card": second["card"]["id"]}, {"card": second["card"]["id"]}]
    text = json.dumps(records)
    for content in ("diary", "Secret plans", "ACME", "NT$30"):
        assert content not in text  # a record says what kind of thing was asked, never the card's own text
    # A refused answer leaves no record.
    assert_error(*call(home, OWNER, "POST", f"/v1/cards/{card}/answer", {"decision": "decline"}), 409, "card_already_answered")
    assert len(chain()) == 4


def test_app_changes_are_recorded_by_name_never_by_token(home):
    made = ok(home, OWNER, "POST", "/v1/apps", {"name": "editor", "priority": "interactive"}, status=201)
    changed = ok(home, OWNER, "PATCH", "/v1/apps/editor", {"daily_tokens": 9000})
    rotated = ok(home, OWNER, "POST", "/v1/apps/editor/rotate")
    removed = ok(home, OWNER, "DELETE", "/v1/apps/editor")
    assert made["recorded"] and changed["recorded"] and rotated["recorded"] and removed == {"removed": True, "recorded": True}
    assert [brief(r) for r in chain()] == [
        ("app.create", "person", None, "ok", "none", "Added the app editor"),
        ("app.update", "person", None, "ok", "none", "Changed the settings of the app editor"),
        ("app.rotate", "person", None, "ok", "none", "Replaced the token of the app editor"),
        ("app.remove", "person", None, "ok", "none", "Removed the app editor")]
    stored = records_path().read_bytes()
    for token in (made["token"], rotated["token"]):
        assert token.encode() not in stored and hashlib.sha256(token.encode()).hexdigest().encode() not in stored
    assert_error(*call(home, OWNER, "DELETE", "/v1/apps/editor"), 404, "not_found")
    assert len(chain()) == 4  # a call that failed is not recorded as done


def test_a_stored_key_is_recorded_as_the_service_id_only(home, tmp_path):
    got = ok(home, OWNER, "POST", "/v1/keys/groq", {"key": KEY_CANARY})
    assert got["recorded"] is True and got["stored"] == ["GROQ_API_KEY"]
    assert [brief(r) for r in chain()] == [("key.store", "person", None, "ok", "none", "Stored a key for the service groq")]
    stored = records_path().read_bytes()
    assert KEY_CANARY.encode() not in stored and KEY_CANARY[:12].encode() not in stored
    assert hashlib.sha256(KEY_CANARY.encode()).hexdigest().encode() not in stored
    assert chain()[0]["resource"] is None and "links" not in chain()[0]


def test_jobs_queued_and_cancelled_are_recorded_without_the_prompt(home):
    prompt = "CANARY-PROMPT summarise my private notes"
    queued = ok(home, HOST, "POST", "/v1/jobs", {"prompt": prompt, "alias": "cheap"}, status=201)
    paid = ok(home, OWNER, "POST", "/v1/jobs", {"prompt": prompt, "alias": "cheap", "allow_paid": True}, status=201)
    cancelled = ok(home, HOST, "DELETE", f"/v1/jobs/{queued['job']['id']}")
    assert queued["recorded"] and paid["recorded"] and cancelled["recorded"]
    assert [brief(r) for r in chain()] == [
        ("job.queue", "helper", "device:bot-tools", "ok", "none", "Queued a background job"),
        ("job.queue", "person", None, "ok", "none", "Queued a background job that may use paid services"),
        ("job.cancel", "helper", "device:bot-tools", "ok", "none", "Cancelled a background job")]
    assert [r["links"] for r in chain()] == [{"job": queued["job"]["id"]}, {"job": paid["job"]["id"]}, {"job": queued["job"]["id"]}]
    assert b"CANARY-PROMPT" not in records_path().read_bytes()


def test_a_records_failure_never_breaks_the_action(home, monkeypatch, caplog):
    def broken(self):
        raise OSError("disk full at /secret/path CANARY-ERRTEXT")

    monkeypatch.setattr(api.Home, "records", broken)
    with caplog.at_level(logging.DEBUG):
        assert ok(home, OWNER, "POST", "/v1/pause") == {"paused": True, "recorded": False}
        assert home.is_paused() is True
        assert ok(home, OWNER, "POST", "/v1/resume") == {"paused": False, "was_paused": True, "recorded": False}
        asked = ok(home, HOST, "POST", "/v1/cards", {"kind": "save_file", "title": "Save a file", "thing": "a.md"}, status=201)
        assert asked["recorded"] is False and asked["card"]["status"] == "pending"
        answered = ok(home, OWNER, "POST", f"/v1/cards/{asked['card']['id']}/answer", {"decision": "approve"})
        assert answered["recorded"] is False and answered["card"]["status"] == "approved"
        app = ok(home, OWNER, "POST", "/v1/apps", {"name": "editor", "priority": "interactive"}, status=201)
        assert app["recorded"] is False and app["token"]
        assert ok(home, OWNER, "POST", "/v1/keys/groq", {"key": KEY_CANARY})["recorded"] is False
        assert home.key_configured("GROQ_API_KEY")
        job = ok(home, HOST, "POST", "/v1/jobs", {"prompt": "x", "alias": "cheap"}, status=201)
        assert job["recorded"] is False and ok(home, HOST, "DELETE", f"/v1/jobs/{job['job']['id']}")["recorded"] is False
        # The two calls that are about records say plainly that they could not.
        assert_error(*call(home, HOST, "POST", "/v1/receipts", {"action": "host.files.write", "summary": "Wrote a file"}), 503, "unavailable")
        assert_error(*call(home, OWNER, "GET", "/v1/receipts"), 503, "unavailable")
    assert "no record was written for home.pause (OSError)" in caplog.text  # the type only
    for leak in ("CANARY", "/secret/path", "disk full", KEY_CANARY):
        assert leak not in caplog.text


def test_without_the_cryptography_package_the_home_works_and_says_how_to_get_records(home, monkeypatch):
    def missing(self):
        raise ModuleNotFoundError("No module named 'cryptography'")

    monkeypatch.setattr(api.Home, "records", missing)
    assert ok(home, OWNER, "POST", "/v1/pause") == {"paused": True, "recorded": False}
    err = assert_error(*call(home, OWNER, "GET", "/v1/receipts"), 503, "unavailable")
    assert "pip install 'muse-stack[records]'" in err["message"]
    err = assert_error(*call(home, HOST, "POST", "/v1/receipts", {"action": "host.files.write", "summary": "Wrote a file"}), 503, "unavailable")
    assert "cryptography" in err["message"]


def test_a_name_shaped_like_a_key_is_not_copied_into_a_summary(home):
    long_name = auth.Device("a1" * 20, role="owner")  # 40 characters of letters and digits: the summary rule would refuse it
    card = home.cards.create(kind="save_file", title="Save a file", thing="a.md")
    assert ok(home, long_name, "POST", f"/v1/cards/{card}/answer", {"decision": "approve"})["recorded"] is True
    assert chain()[0]["summary"] == "Approved: save a file (answered on (a long name))"


# ---------------------------------------------------------------- what a device may report


def test_the_home_decides_who_did_it(home):
    by_host = ok(home, HOST, "POST", "/v1/receipts", {"action": "host.files.write", "summary": "Wrote the weekly digest",
                                                       "outcome": "ok", "egress": "none"}, status=201)
    by_owner = ok(home, OWNER, "POST", "/v1/receipts", {"action": "note.add", "summary": "Noted something by hand"}, status=201)
    assert by_host["recorded"] is True and by_host["hash"] == fmt.record_hash(by_host["record"])
    assert brief(by_host["record"]) == ("host.files.write", "helper", "device:bot-tools", "ok", "none", "Wrote the weekly digest")
    assert brief(by_owner["record"]) == ("note.add", "person", None, "ok", None, "Noted something by hand")
    assert by_host["record"]["device"] == by_owner["record"]["device"] == HomeRecords().device()  # the home's key, always
    assert by_owner["record"]["prev"] == by_host["hash"] and fmt.verify_chain(chain()) == 2
    assert chain() == [by_host["record"], by_owner["record"]]


@pytest.mark.parametrize("extra", [{"actor": "person"}, {"device": "00" * 32}, {"mandate": None}, {"mandate": "m-1"}, {"time": 1},
                                   {"seq": 0}, {"prev": "00" * 32}, {"sig": "00" * 64}, {"v": 1}, {"summary_hash": "00" * 32}])
def test_a_caller_cannot_choose_its_actor_device_or_time(home, extra):
    body = {"action": "host.files.write", "summary": "Wrote a file", **extra}
    assert_error(*call(home, HOST, "POST", "/v1/receipts", body), 400, "invalid_request")
    assert chain() == []


@pytest.mark.parametrize("action", ["home.pause", "home.resume", "card.answer", "card.create", "app.create", "key.store", "job.queue",
                                    "skill.run", "mandate.issue", "records.seal"])
def test_a_device_cannot_write_the_homes_own_actions(home, action):
    err = assert_error(*call(home, OWNER, "POST", "/v1/receipts", {"action": action, "summary": "Pretending"}), 400, "invalid_request")
    assert "only the home itself writes" in err["message"]
    err = assert_error(*call(home, HOST, "POST", "/v1/receipts", {"action": action, "summary": "Pretending"}), 400, "invalid_request")
    assert "start with host." in err["message"]
    assert chain() == [] and home.is_paused() is False


@pytest.mark.parametrize("action", ["files.write", "llm.call", "pay.spend", "message.send", "note.add", "hosts.files.write", "hostile.act"])
def test_a_hosts_own_report_is_always_filed_under_host_so_it_cannot_pass_for_what_the_home_did(home, action):
    """`files.write` and `llm.call` are written by the home itself when it writes an approved file or a skill
    calls a service. A host bot may claim it wrote a file, but only as `host.files.write`."""
    err = assert_error(*call(home, HOST, "POST", "/v1/receipts", {"action": action, "summary": "Wrote a file a skill proposed"}), 400, "invalid_request")
    assert "start with host." in err["message"] and chain() == []
    got = ok(home, HOST, "POST", "/v1/receipts", {"action": f"host.{action.split('.', 1)[1]}", "summary": "Did something"}, status=201)
    assert got["record"]["action"].startswith("host.") and got["record"]["actor"] == "helper"
    # And the family is the host bots' alone: the person's own device does not write under it.
    assert_error(*call(home, OWNER, "POST", "/v1/receipts", {"action": "host.files.write", "summary": "x"}), 400, "invalid_request")
    assert [r["action"].split(".")[0] for r in chain()] == ["host"] and api.HOST_FAMILY == "host"


@pytest.mark.parametrize("body", [
    {"action": "host.files.write"},  # no summary
    {"summary": "Wrote a file"},  # no action
    {"action": "host.files.write", "summary": ""},
    {"action": "host.files.write", "summary": "a" * 201},
    {"action": "host.files.write", "summary": "one line\nand another"},
    {"action": "host.files.write", "summary": "hidden ‮ override"},
    {"action": "write", "summary": "Not dotted"},
    {"action": "Files.Write", "summary": "Upper case"},
    {"action": "host.files.write", "summary": "x", "outcome": "done"},
    {"action": "host.files.write", "summary": "x", "cost": {"amount": "30", "currency": "TWD"}},
    {"action": "host.files.write", "summary": "x", "cost": {"currency": "TWD", "minor": 1.5}},
    {"action": "host.files.write", "summary": "x", "cost": {"currency": "twd", "minor": 1}},
    {"action": "host.files.write", "summary": "x", "links": {"note": "y"}},
    {"action": "host.files.write", "summary": "x", "links": {"undo_of": "record-3"}},
    {"action": "host.files.write", "summary": "x", "egress": "cloud"},
    {"action": "host.files.write", "summary": "x", "egress": "service:Gmail"},
    {"action": "host.files.write", "summary": "x", "resource": ""},
    {"action": "host.files.write", "summary": "x", "resource": "r" * 1025},
    {"action": "host.files.write", "summary": 7},
    {"action": ["host.files.write"], "summary": "x"},
])
def test_the_records_module_checks_every_field(home, body):
    err = assert_error(*call(home, HOST, "POST", "/v1/receipts", body), 400, "invalid_request")
    assert "\n" not in err["message"] and "override" not in err["message"]
    assert chain() == []


def test_a_summary_shaped_like_a_key_is_refused_and_never_repeated(home, caplog):
    with caplog.at_level(logging.DEBUG):
        for summary in (f"used {KEY_CANARY}", "token 0123456789abcdef0123456789abcdef0123456789abcdef", "-----BEGIN PRIVATE KEY-----"):
            status, payload = call(home, HOST, "POST", "/v1/receipts", {"action": "host.files.write", "summary": summary})
            assert_error(status, payload, 400, "invalid_request")
            assert summary not in json.dumps(payload)
    assert chain() == [] and KEY_CANARY not in caplog.text


def test_cost_links_resource_and_egress_are_kept_as_the_format_says(home):
    earlier = ok(home, HOST, "POST", "/v1/receipts", {"action": "host.files.write", "summary": "Wrote a file"}, status=201)
    got = ok(home, HOST, "POST", "/v1/receipts", {
        "action": "host.pay.spend", "summary": "Paid NT$30 for a lookup", "outcome": "ok", "cost": {"currency": "TWD", "minor": 3000},
        "links": {"job": "job-17", "payment_ref": "0x5f2c9a", "undo_of": earlier["hash"]}, "egress": "service:api.example.com",
        "resource": "payee:api.example.com"}, status=201)["record"]
    assert got["cost"] == {"currency": "TWD", "minor": 3000} and got["egress"] == "service:api.example.com"
    assert got["links"] == {"job": "job-17", "payment_ref": "0x5f2c9a", "undo_of": earlier["hash"]}
    assert len(got["resource"]) == 64 and "payee" not in got["resource"]  # a keyed reference, not the name
    assert b"payee:api.example.com" not in records_path().read_bytes()
    fmt.verify_record(got)


def test_a_device_cannot_flood_the_records(home, monkeypatch):
    monkeypatch.setattr(api, "MAX_RECORDS_PER_HOUR", 3)
    body = {"action": "host.files.write", "summary": "Wrote a file"}
    for _ in range(3):
        ok(home, HOST, "POST", "/v1/receipts", body, status=201)
    status, payload = call(home, HOST, "POST", "/v1/receipts", body)
    err = assert_error(status, payload, 429, "too_many_requests")
    assert 1 <= err["retry_after"] <= 3600
    mine = {"action": "note.add", "summary": "Noted something"}
    assert ok(home, OWNER, "POST", "/v1/receipts", mine, status=201)["record"]["seq"] == 3  # counted per device
    home.clock.now += 3600
    assert ok(home, HOST, "POST", "/v1/receipts", body, status=201)["record"]["seq"] == 4
    assert api.MAX_RECORDS_PER_HOUR == 3


def test_who_may_read_and_who_may_add(home):
    body = {"action": "host.files.write", "summary": "Wrote a file"}
    assert call(home, HOST, "POST", "/v1/receipts", body)[0] == 201
    assert_error(*call(home, HOST, "GET", "/v1/receipts"), 403, "forbidden")  # a host bot cannot read the person's records
    assert_error(*call(home, VIEWER, "POST", "/v1/receipts", body), 403, "forbidden")
    assert len(ok(home, VIEWER, "GET", "/v1/receipts")["records"]) == 1
    assert len(ok(home, OWNER, "GET", "/v1/receipts")["records"]) == 1


def test_the_home_actions_table_is_the_one_the_format_document_lists():
    doc = (Path(__file__).resolve().parents[1] / "docs" / "spec" / "interfaces" / "record-format.md").read_text(encoding="utf-8")
    for family in (*api.HOME_ACTIONS, api.HOST_FAMILY):
        assert f"`{family}`" in doc, family
    assert "`host.…`" in doc and "never merely claimed by a bot" in doc
    from muse.mcp import tools

    assert tools.HOST_FAMILY == api.HOST_FAMILY
