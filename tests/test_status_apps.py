"""Snapshot additions (apps, paid, queue, paused) and how `muse status` and the dashboard show them."""

from __future__ import annotations

import copy
import json
import subprocess
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wardmuse import apps, cli, queue, types as mtypes
from wardmuse.ledger import Ledger
from wardmuse.status import build_snapshot

from test_cli import NOW, iso, typical_snapshot
from test_dashboard import DEMO, _check_td, _start, INDEX

TS = datetime(2026, 11, 12, 7, 0, 30, tzinfo=timezone.utc).timestamp()


def env_for(home: Path, **extra: str) -> dict[str, str]:
    return {"MUSE_HOME": str(home), "MUSE_TZ": "UTC", **extra}


def test_snapshot_apps_paid_queue_paused(_isolated_muse_home):
    env = env_for(_isolated_muse_home)
    apps.add_app("notes", "background", daily_requests=10, environ=env, note="n")
    ledger = Ledger(environ=env, clock=lambda: TS)
    ledger.record_call(deployment_id="d", provider="p", upstream="p:d", alias="fast", status=200, ok=True, tokens_in=3, tokens_out=4,
                       app="notes", priority="background", now=TS - 60)
    ledger.record_call(deployment_id="d", provider="paidp", upstream="paidp:d", alias="fast", status=200, ok=True, tokens_in=3, tokens_out=4,
                       app="owner", priority="interactive", cost_usd=0.03, paid=True, now=TS - 30)
    ledger.record_call(deployment_id="d", provider="paidp", upstream="paidp:d", alias="fast", status=200, ok=True, tokens_in=1, tokens_out=1,
                       app="owner", priority="interactive", cost_usd=None, paid=True, now=TS - 20)
    ledger.record_ration(app="notes", priority="background", alias="fast", kind="defer", code="yield", now=TS - 10)
    ledger.record_ration(app="ghost", priority="normal", alias="fast", kind="refuse", code="app-tokens", now=TS - 10)
    q = queue.Queue(env, lambda: TS)
    q.add(kind="prompt", payload="SECRET-PROMPT", out_path=None)
    queue.set_paused(env)
    snap = build_snapshot(now=TS, environ=env)
    json.dumps(snap)
    by = {a["name"]: a for a in snap["apps"]}
    assert list(by)[:3] == ["owner", "anonymous", "notes"] and by["owner"]["builtin"] and not by["notes"]["builtin"]
    assert by["notes"]["today"] == {"requests": 1, "failures": 0, "tokens": 7, "cost_usd": 0.0}
    assert by["notes"]["daily_requests"] == 10 and by["notes"]["priority"] == "background" and by["notes"]["note"] == "n"
    assert by["notes"]["deferrals"] == {"defer:yield": 1} and by["notes"]["last_used_at"] == "2026-11-12T06:59:30Z"
    assert by["ghost"]["note"].startswith("not registered") and by["ghost"]["deferrals"] == {"refuse:app-tokens": 1}
    assert snap["paid"] == {"cap_usd": 0.9375, "spent_usd": 0.03, "held_usd": 0.0, "calls": 2, "unknown_cost_calls": 1,
                            "resets_at": "2026-11-13T00:00:00Z"}
    assert snap["queue"]["waiting"] == 1 and snap["queue"]["counts"]["queued"] == 1 and snap["paused"] is True
    assert "SECRET-PROMPT" not in json.dumps(snap)
    _check_td(snap, mtypes.Snapshot, "live")


def test_snapshot_paid_cap_env_and_bad_values(_isolated_muse_home):
    assert build_snapshot(now=TS, environ=env_for(_isolated_muse_home, MUSE_DAILY_PAID_CAP_USD="0.25"))["paid"]["cap_usd"] == 0.25
    assert build_snapshot(now=TS, environ=env_for(_isolated_muse_home, MUSE_DAILY_PAID_CAP_USD="lots"))["paid"]["cap_usd"] == 0.0


def test_snapshot_survives_a_broken_queue_file_and_apps_file(_isolated_muse_home):
    env = env_for(_isolated_muse_home)
    (_isolated_muse_home / "queue.sqlite").write_bytes(b"this is not sqlite" * 50)
    (_isolated_muse_home / "apps.yaml").write_text("{{{ not yaml")
    snap = build_snapshot(now=TS, environ=env)
    assert snap["queue"]["waiting"] == 0 and [a["name"] for a in snap["apps"]] == ["owner", "anonymous"]


def test_demo_snapshot_has_the_new_sections_and_validates():
    _check_td(DEMO, mtypes.Snapshot, "demo")
    assert len(DEMO["apps"]) >= 4 and {"interactive", "normal", "background"} <= {a["priority"] for a in DEMO["apps"]}
    assert any(a["deferrals"] for a in DEMO["apps"]) and DEMO["queue"]["waiting"] == 3 and DEMO["paid"]["cap_usd"] == 1.0


# ---------------------------------------------------------------- muse status


def with_extras(snap, **kw):
    snap = copy.deepcopy(snap)
    snap.update(copy.deepcopy({k: v for k, v in DEMO.items() if k in ("apps", "paid", "queue", "paused")}))
    snap["paid"]["resets_at"] = iso(timedelta(hours=5, minutes=2))
    snap["queue"]["next_due_at"] = iso(timedelta(minutes=12))
    snap.update(kw)
    return snap


def test_status_shows_apps_paid_and_queue_in_80_columns():
    text = cli.render_status(with_extras(typical_snapshot()), now=NOW)
    assert "Paid today: $0.03 of $1.00" in text and "resets in 5h 02m" in text
    assert "Queue: 3 waiting, next in 12m" in text
    assert "APPS" in text and "notes-batch" in text and "background" in text
    assert "turned away today: 14x waited for an app in use, 3x kept a reserve" in text
    assert "PAUSED" not in text
    assert all(len(line) <= 80 for line in text.splitlines()), [l for l in text.splitlines() if len(l) > 80]
    ascii_text = cli.render_status(with_extras(typical_snapshot()), now=NOW, unicode_ok=False)
    assert all(len(line) <= 80 for line in ascii_text.splitlines())


def test_status_paused_banner_cap_reached_and_hold():
    snap = with_extras(typical_snapshot(), paused=True)
    snap["paid"].update(spent_usd=1.0, unknown_cost_calls=2)
    snap["queue"]["hold_until"] = iso(timedelta(hours=2))
    text = cli.render_status(snap, now=NOW)
    assert "PAUSED" in text.splitlines()[2] and "(paused)" in text
    assert "CAP REACHED" in text and "2 unpriced calls" in text and "app cap: nothing before 2h 00m" in text
    assert all(len(line) <= 80 for line in text.splitlines())
    snap["paid"]["cap_usd"] = 0.0
    assert "Paid today: off" in cli.render_status(snap, now=NOW)


def test_status_empty_queue_and_old_snapshots_still_render():
    snap = with_extras(typical_snapshot())
    snap["queue"].update(waiting=0, next_due_at=None, counts={**snap["queue"]["counts"], "queued": 0, "deferred": 0})
    assert "Queue: empty (7 done)" in cli.render_status(snap, now=NOW)
    old = typical_snapshot()  # none of the new keys
    text = cli.render_status(old, now=NOW)
    assert "Paid today" not in text and "APPS" not in text and "PROVIDER" in text


# ---------------------------------------------------------------- dashboard

NODE_SECTIONS = r"""
const { chromium } = require('playwright');
(async () => {
  const port = process.argv[2];
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: +process.argv[3], height: 900 } });
  let fired = false;
  await p.exposeFunction('__pwned', () => { fired = true; });
  p.on('dialog', d => { fired = true; d.dismiss(); });
  const reqs = [];
  p.on('request', r => reqs.push(new URL(r.url()).origin));
  await p.goto(`http://127.0.0.1:${port}/`);
  await p.waitForSelector('#apps .card');
  await p.waitForTimeout(400);
  const out = await p.evaluate(() => ({
    imgs: document.querySelectorAll('#app img, #alerts img').length,
    text: document.body.innerText,
    apps: document.querySelectorAll('#apps article').length,
    h: document.documentElement.scrollWidth - innerWidth,
    meters: document.querySelectorAll('#apps [role=meter]').length,
  }));
  console.log(JSON.stringify({fired, origins: [...new Set(reqs)], ...out}));
  await b.close();
})();
"""


def run_node(tmp_path, snap, width=1280):
    s = _start(lambda: snap)
    script = tmp_path / "sections.js"
    script.write_text(NODE_SECTIONS)
    import os
    try:
        r = subprocess.run(["node", str(script), str(s.server_address[1]), str(width)], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "NODE_PATH": subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()})
    finally:
        s.shutdown()
        s.server_close()
    if r.returncode != 0 and "Cannot find module" in r.stderr:
        pytest.skip("playwright for node not available")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1]), s.server_address[1]


needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node not available")


def test_index_new_sections_use_text_only_and_no_new_requests():
    assert "innerHTML" not in INDEX and len(__import__("re").findall(r"fetch\(", INDEX)) == 1
    for word in ("Spending and queue", "Paid today", "Background queue", "Held back today", "Paused", "muse resume"):
        assert word in INDEX


@needs_node
def test_apps_paid_queue_and_paused_render(tmp_path):
    snap = copy.deepcopy(DEMO)
    snap["paused"] = True
    out, port = run_node(tmp_path, snap)
    t = out["text"]
    assert out["apps"] == 5 and out["meters"] >= 2 and out["h"] <= 0 and out["fired"] is False
    for expected in ("Paid today", "$0.03 of $1.00 daily cap", "Background queue", "3 jobs waiting", "Queue paused", "notes-batch",
                     "Waited for an app you were using", "Held back to keep a reserve for interactive apps", "BACKGROUND", "Paused by you"):
        assert expected.lower() in t.lower(), expected
    assert out["origins"] == [f"http://127.0.0.1:{port}"]  # nothing but the dashboard itself was requested


@needs_node
def test_apps_section_on_a_phone_does_not_overflow(tmp_path):
    out, _ = run_node(tmp_path, copy.deepcopy(DEMO), width=390)
    assert out["h"] <= 0


@needs_node
def test_untrusted_app_names_and_reasons_render_literally(tmp_path):
    evil = '<img src=x onerror="window.__pwned()">'
    snap = copy.deepcopy(DEMO)
    a = snap["apps"][3]
    a["name"] = evil
    a["note"] = evil
    a["default_privacy"] = evil
    a["priority"] = evil
    a["deferrals"] = {evil: 3, "defer:yield": 1, "refuse:" + evil: 2}
    snap["apps"][1]["name"] = evil + "2"
    snap["queue"]["counts"] = {evil: 4}
    snap["queue"]["next_due_at"] = evil
    snap["paid"]["resets_at"] = evil
    out, _ = run_node(tmp_path, snap)
    assert out["fired"] is False and out["imgs"] == 0 and evil in out["text"]
    assert out["h"] <= 0
