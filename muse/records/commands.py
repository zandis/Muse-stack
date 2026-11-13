"""What `muse records ...` does (the argument parser is in muse/cli.py).

Each function prints for a person and returns a process exit code: 0 when the
records check out or the command did its job, 1 when something is wrong.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from muse.records import format as fmt
from muse.records.errors import RecordError
from muse.records.home import HomeRecords
from muse.records.jcs import loads_strict
from muse.records.seal import export_text, verify_export

MAX_EXPORT_BYTES = 256 * 1024 * 1024

# Decimal places of the minor unit: ISO 4217 for currencies, the token's own for USDC.
MINOR_DIGITS = {"JPY": 0, "KRW": 0, "VND": 0, "TWD": 2, "USD": 2, "EUR": 2, "GBP": 2, "CNY": 2, "HKD": 2, "USDC": 6}

EXPLAIN = {
    "malformed": "it is not in the record format",
    "wrong_device": "it was signed by another device",
    "bad_signature": "its signature does not match its content: it was changed after it was signed",
    "summary_mismatch": "its summary text is not the one that was signed",
    "missing": "the record that should come before it is gone, or the order was changed",
    "duplicate": "the same record appears twice",
    "fork": "there are two different signed records with this number",
    "wrong_prev": "it does not link to the record before it: a record was replaced",
    "time_went_back": "its time is earlier than the record before it",
    "bad_checkpoint": "the seal is damaged or was not made by this device",
    "size_mismatch": "records that were sealed are no longer there",
    "root_mismatch": "the records are not the ones that were sealed",
    "bad_proof": "its proof does not lead to the seal",
    "summary": "its summary is not allowed",
}


def _say(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _when(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()


def format_cost(cost: Mapping[str, Any] | None) -> str:
    if not cost:
        return ""
    digits = MINOR_DIGITS.get(cost["currency"])
    if digits is None:
        return f"{cost['minor']:,} minor units of {cost['currency']}"
    if digits == 0:
        return f"{cost['currency']} {cost['minor']:,}"
    whole, part = divmod(cost["minor"], 10**digits)
    return f"{cost['currency']} {whole:,}.{part:0{digits}d}"


def parse_since(text: str, now: float) -> int:
    """`7d`, `36h`, `2026-11-01` (local midnight) or an ISO date-time, as milliseconds. ValueError otherwise."""
    text = text.strip()
    m = re.fullmatch(r"(\d{1,6})([dhm])", text)
    if m:
        seconds = int(m.group(1)) * {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
        return max(0, int((now - seconds) * 1000))
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return max(0, int(moment.timestamp() * 1000))


def make_filter(since_ms: int | None, action: str | None, cost_only: bool,
                left_device: bool = False) -> Callable[[dict[str, Any]], bool] | None:
    """The filter the list and export commands (and the home API) share; None when nothing is filtered.

    `left_device`: only records that say their content left the person's devices (`egress` is
    `service:...` or `host:...`). A record that does not say is not among them."""
    if since_ms is None and not action and not cost_only and not left_device:
        return None

    def keep(record: dict[str, Any]) -> bool:
        if since_ms is not None and record["time"] < since_ms:
            return False
        if action and record["action"] != action and not record["action"].startswith(action.rstrip(".") + "."):
            return False
        if cost_only and not (record["cost"] and record["cost"]["minor"] > 0):
            return False
        if left_device and not fmt.left_devices(record):
            return False
        return True

    return keep


def egress_words(record: Mapping[str, Any]) -> str:
    """Where the content went, in a few plain words; "" when it stayed or the record does not say."""
    egress = record.get("egress")
    if not isinstance(egress, str):
        return ""
    if egress.startswith("service:"):
        return f"sent to {egress[len('service:'):]}"
    if egress.startswith("host:"):
        return f"given to the host bot {egress[len('host:'):]}"
    return "sent to your home" if egress == "home" else ""


def _well_formed(record: Any) -> bool:
    try:
        fmt.check_record(record)
    except RecordError:
        return False
    return True


def _problem(exc: RecordError, what: str = "Record") -> str:
    reason = EXPLAIN.get(exc.kind, exc.kind)
    where = f"{what} {exc.index:,}" if exc.index is not None else "The record file"
    return f"{where} does not match: {reason} ({exc.kind})."


def verify(environ: Mapping[str, str], as_json: bool = False) -> int:
    home = HomeRecords(environ)
    try:
        state = home.verify()
    except RecordError as exc:
        if as_json:
            _say(json.dumps({"ok": False, "kind": exc.kind, "index": exc.index}))
        else:
            _say(_problem(exc))
            _say("What to do: keep records.sqlite as it is (do not delete it), and compare with an export you made earlier:")
            _say("`muse records check-file FILE` shows whether that copy is intact.")
        return 1
    cp = state["checkpoint"]
    if as_json:
        _say(json.dumps({"ok": True, "size": state["size"], "device": state["device"], "sealed": cp}))
        return 0
    _say(f"All records check out ({state['size']:,} record{'s' if state['size'] != 1 else ''}).")
    if cp is None:
        _say("Not sealed yet (`muse records seal`, or any export, seals).")
    else:
        _say(f"Last sealed {_when(cp['time']).strftime('%Y-%m-%d %H:%M')} ({cp['size']:,} of {state['size']:,} records).")
    if state["device"]:
        _say(f"This home's device key: {state['device']}")
    return 0


def seal(environ: Mapping[str, str]) -> int:
    home = HomeRecords(environ)
    try:
        cp = home.seal()
    except RecordError as exc:
        _say(_problem(exc))
        return 1
    _say(f"Sealed {cp['size']:,} record{'s' if cp['size'] != 1 else ''} at {_when(cp['time']).strftime('%Y-%m-%d %H:%M')}.")
    _say(f"Root {cp['root']}")
    _say("The seal is kept on this computer only; anchoring it somewhere else is not built yet.")
    return 0


def list_records(environ: Mapping[str, str], since: str | None, action: str | None, cost_only: bool, as_json: bool,
                 now: float | None = None, left_device: bool = False) -> int:
    try:
        since_ms = parse_since(since, time.time() if now is None else now) if since else None
    except ValueError:
        _err("Error: --since takes 7d, 36h, 2026-11-01 or 2026-11-01T09:00")
        return 2
    home = HomeRecords(environ)
    records = home.records()
    try:
        fmt.verify_chain(records, home.device())
        note = ""
    except RecordError as exc:
        note = _problem(exc) + " Run `muse records verify`."
        records = [r for r in records if _well_formed(r)]  # show what can be shown
    keep = make_filter(since_ms, action, cost_only, left_device)
    chosen = [r for r in records if keep is None or keep(r)]
    if as_json:
        _say(json.dumps(chosen, ensure_ascii=False))
        return 1 if note else 0
    if note:
        _say(note)
    unsaid = sum(1 for r in records if "egress" not in r) if left_device else 0
    if not chosen:
        _say("No records yet." if not records else "No records match.")
    if unsaid:
        _say(f"{unsaid:,} record{'s do' if unsaid != 1 else ' does'} not say where the content went (written before records "
             "carried that, or by a writer that did not know); they are not listed here.")
    if not chosen:
        return 1 if note else 0
    day = ""
    for record in reversed(chosen):
        when = _when(record["time"])
        if when.strftime("%Y-%m-%d") != day:
            day = when.strftime("%Y-%m-%d")
            _say(day)
        who = f"under {record['mandate']}" if record["mandate"] else "by you" if record["actor"] == "person" else f"by the {record['actor']}"
        outcome = "" if record["outcome"] == "ok" else f" [{record['outcome']}]"
        cost = format_cost(record["cost"])
        went = egress_words(record)
        line = f"  {when.strftime('%H:%M')}  #{record['seq']:<5} {record.get('summary', '(summary removed)')}{outcome}"
        _say(f"{line}  ({record['action']}, {who}{', ' + cost if cost else ''}{', ' + went if went else ''})")
    return 1 if note else 0


def export_file(environ: Mapping[str, str], path: str, since: str | None, action: str | None, cost_only: bool,
                redact_summaries: bool, now: float | None = None) -> int:
    try:
        since_ms = parse_since(since, time.time() if now is None else now) if since else None
    except ValueError:
        _err("Error: --since takes 7d, 36h, 2026-11-01 or 2026-11-01T09:00")
        return 2
    home = HomeRecords(environ)
    try:
        obj = home.export(make_filter(since_ms, action, cost_only), redact_summaries=redact_summaries)
        report = verify_export(obj)  # never hand out a file that does not check
    except RecordError as exc:
        _say(_problem(exc))
        _say("Nothing was exported. Run `muse records verify`.")
        return 1
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _err(f"Error: {path} exists; choose another name.")
        return 1
    except OSError as exc:
        _err(f"Error: cannot write {path} ({type(exc).__name__}).")
        return 1
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(export_text(obj))
    what = "all" if report["complete"] else "the matching"
    _say(f"Exported {what} {report['count']:,} of {report['size']:,} records to {path}"
         + (" (summaries removed)." if redact_summaries else "."))
    _say("Anyone can check the file with `muse records check-file FILE`. It proves the records were signed by this key:")
    _say(f"  {report['device']}")
    _say("Give them that key another way (read it out, or show `muse records verify`), so they know the file is yours.")
    if not redact_summaries:
        _say("The file shows each record's summary text. `--redact-summaries` leaves the text out and it still checks.")
    return 0


def check_file(path: str, device: str | None, as_json: bool = False) -> int:
    try:
        if os.path.getsize(path) > MAX_EXPORT_BYTES:
            _err("Error: the file is too large to be a records export.")
            return 1
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        _err(f"Error: cannot read {path} ({type(exc).__name__}).")
        return 1
    try:
        report = verify_export(loads_strict(raw), device)
    except RecordError as exc:
        if as_json:
            _say(json.dumps({"ok": False, "kind": exc.kind, "index": exc.index}))
            return 1
        if exc.kind == "wrong_device" and exc.index is None:
            _say("This file was made by another device than the one you expected (wrong_device).")
        elif exc.kind == "malformed" and exc.index is None:
            _say("This is not a records export, or it was damaged (malformed).")
        else:
            _say("This file does not check out. " + _problem(exc, "Entry"))
        return 1
    if as_json:
        _say(json.dumps({"ok": True, **report}))
        return 0
    scope = f"all {report['count']:,} records" if report["complete"] else f"{report['count']:,} of {report['size']:,} records, each proven to be part of the sealed set"
    _say(f"The file checks out: {scope}.")
    if report["redacted"]:
        _say(f"{report['redacted']:,} record{'s have' if report['redacted'] != 1 else ' has'} the summary text removed; the rest of each is still signed.")
    _say(f"Signed by device key: {report['device']}")
    if device is None:
        _say("Compare that key with the one the owner shows you (`muse records verify` on their computer).")
        _say("The file alone cannot tell you whose key it is, or whether newer records exist.")
    return 0


def note_home_event(environ: Mapping[str, str], action: str, summary: str, clock: Callable[[], float] = time.time) -> None:
    """Append one record of the person's own action on this home (`muse pause`, `muse resume`).

    Pause and resume go through `muse.records.home.record_pause`, the function the home API also uses,
    so the command line and the API leave the same record."""
    if action in ("home.pause", "home.resume"):
        from muse.records.home import record_pause

        record_pause(environ, action == "home.pause", clock=clock)
        return
    HomeRecords(environ, clock=clock).write(action, summary, actor="person")


__all__ = ["check_file", "egress_words", "export_file", "format_cost", "list_records", "make_filter", "note_home_event",
           "parse_since", "seal", "verify"]
