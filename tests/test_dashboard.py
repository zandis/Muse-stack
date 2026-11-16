"""Dashboard server and page checks (stdlib client)."""

from __future__ import annotations

import copy
import http.client
import json
import re
import shutil
import subprocess
import threading
import typing
from pathlib import Path

import pytest

from wardmuse import types as mtypes
from wardmuse.dashboard import server as dash

DASH = Path(dash.__file__).parent
INDEX = (DASH / "index.html").read_text(encoding="utf-8")
DEMO = json.loads((DASH / "demo_snapshot.json").read_text(encoding="utf-8"))


def _start(snapshot_fn=None):
    srv = dash.make_server("127.0.0.1", 0, snapshot_fn=snapshot_fn or (lambda: copy.deepcopy(DEMO)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def srv():
    s = _start()
    yield s
    s.shutdown()
    s.server_close()


def req(srv, path="/", method="GET", host=None):
    port = srv.server_address[1]
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"127.0.0.1:{port}")
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    headers = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, headers, body


def test_routes(srv):
    st, h, body = req(srv, "/")
    assert st == 200 and h["content-type"].startswith("text/html") and b"<title>" in body
    st, h, body = req(srv, "/api/status")
    assert st == 200 and json.loads(body)["providers"]
    st, _, body = req(srv, "/api/status?demo=1")
    assert st == 200 and json.loads(body)["generated_at"].endswith("Z")
    assert req(srv, "/healthz")[0] == 200
    for p in ("/nope", "/api", "/index.html", "/muse/types.py"):
        assert req(srv, p)[0] == 404


def test_security_headers(srv):
    for path in ("/", "/api/status", "/healthz", "/nope"):
        _, h, _ = req(srv, path)
        assert h["content-security-policy"] == dash.CSP
        assert "default-src 'none'" in h["content-security-policy"]
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"
        assert h["cache-control"] == "no-store"
        assert not any(k.startswith("access-control-") for k in h)


def test_host_allowlist(srv):
    port = srv.server_address[1]
    assert req(srv, "/", host="evil.example")[0] == 403
    assert req(srv, "/api/status", host=f"evil.example:{port}")[0] == 403
    assert req(srv, "/", host="127.0.0.1:1")[0] == 403
    for ok in (f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"):
        assert req(srv, "/healthz", host=ok)[0] == 200


def test_no_write_routes(srv):
    for m in ("POST", "PUT", "DELETE", "PATCH"):
        st, h, _ = req(srv, "/api/status", method=m)
        assert st == 405 and h["allow"] == "GET"
    assert "do_POST" not in INDEX and "method:" not in INDEX and "'POST'" not in INDEX


def test_non_loopback_refused(monkeypatch):
    monkeypatch.delenv("MUSE_DASH_ALLOW_REMOTE", raising=False)
    with pytest.raises(ValueError):
        dash.make_server("0.0.0.0", 0)
    with pytest.raises(SystemExit):
        dash.serve("0.0.0.0", 0)
    monkeypatch.setenv("MUSE_DASH_ALLOW_REMOTE", "1")
    s = dash.make_server("0.0.0.0", 0)
    s.server_close()


def test_503_without_traceback():
    def boom():
        raise RuntimeError("secret /home/x/ledger.sqlite exploded")

    s = _start(boom)
    try:
        st, h, body = req(s, "/api/status")
        assert st == 503 and h["content-type"].startswith("application/json")
        data = json.loads(body)
        assert set(data) == {"error", "hint"}
        text = body.decode()
        assert "Traceback" not in text and "secret" not in text and "ledger.sqlite" not in text
    finally:
        s.shutdown()
        s.server_close()


def test_default_snapshot_missing_module_is_503(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "wardmuse.status", None)  # makes the import fail
    s = dash.make_server("127.0.0.1", 0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        st, _, body = req(s, "/api/status")
        assert st == 503 and b"Traceback" not in body
    finally:
        s.shutdown()
        s.server_close()


def _check(value, tp, path):
    origin = typing.get_origin(tp)
    if typing.is_typeddict(tp):
        _check_td(value, tp, path)
    elif origin is typing.Literal:
        assert value in typing.get_args(tp), f"{path}: {value!r} not in {typing.get_args(tp)}"
    elif origin is list:
        assert isinstance(value, list), path
        for i, v in enumerate(value):
            _check(v, typing.get_args(tp)[0], f"{path}[{i}]")
    elif origin is dict:
        assert isinstance(value, dict), path
        for k, v in value.items():
            _check(v, typing.get_args(tp)[1], f"{path}.{k}")
    elif origin is typing.Union or str(origin) == "<class 'types.UnionType'>":
        errs = []
        for a in typing.get_args(tp):
            if a is type(None):
                if value is None:
                    return
                continue
            try:
                _check(value, a, path)
                return
            except AssertionError as e:
                errs.append(str(e))
        raise AssertionError(f"{path}: {value!r} matches none of {errs}")
    elif tp is bool:
        assert isinstance(value, bool), path
    elif tp is int:
        assert isinstance(value, int) and not isinstance(value, bool), path
    elif tp is float:
        assert isinstance(value, (int, float)), path
    elif tp is str:
        assert isinstance(value, str), path


def _check_td(value, td, path):
    assert isinstance(value, dict), path
    hints = typing.get_type_hints(td)
    assert set(value) == set(hints), f"{path}: keys differ: {set(value) ^ set(hints)}"
    for k, tp in hints.items():
        _check(value[k], tp, f"{path}.{k}")


def test_demo_snapshot_matches_types():
    _check_td(DEMO, mtypes.Snapshot, "snapshot")
    states = {p["state"] for p in DEMO["providers"]}
    assert states == {"ok", "cooling", "exhausted", "no_key", "disabled", "unknown"}
    assert {l["source"] for p in DEMO["providers"] for l in p["limits"]} == {"header", "catalog", "estimate"}
    assert any(not c["ok"] for c in DEMO["recent"])


def test_demo_served_with_shifted_times(srv):
    _, _, body = req(srv, "/api/status?demo=1")
    data = json.loads(body)
    _check_td(data, mtypes.Snapshot, "served")
    assert data["generated_at"] != DEMO["generated_at"]


def test_index_is_offline_and_static():
    assert not re.search(r"<script[^>]*\ssrc", INDEX, re.I)
    assert not re.search(r"<link\b", INDEX, re.I)
    assert "@import" not in INDEX
    assert not re.search(r"url\(\s*['\"]?https?:", INDEX, re.I)
    assert not re.search(r"\b(src|href|action|poster)\s*=\s*[\"']https?:", INDEX, re.I)
    assert "innerHTML" not in INDEX and "outerHTML" not in INDEX and "document.write" not in INDEX
    assert "XMLHttpRequest" not in INDEX and "sendBeacon" not in INDEX
    assert len(re.findall(r"fetch\(", INDEX)) == 1


NODE_TEST = r"""
const { chromium } = require('playwright');
(async () => {
  const port = process.argv[2];
  const b = await chromium.launch();
  const p = await b.newPage();
  let fired = false;
  await p.exposeFunction('__pwned', () => { fired = true; });
  p.on('dialog', d => { fired = true; d.dismiss(); });
  await p.goto(`http://127.0.0.1:${port}/`);
  await p.waitForSelector('.card');
  await p.waitForTimeout(500);
  await p.evaluate(() => document.querySelectorAll('details').forEach(d => { d.open = true; }));
  const out = await p.evaluate(() => ({
    imgs: document.querySelectorAll('#app img, #alerts img, #chips img').length,
    text: document.body.innerText,
    h: document.documentElement.scrollWidth - innerWidth,
    jsLinks: document.querySelectorAll('a[href^="javascript"], a[href^="data"], a[href^="claude"]').length,
  }));
  console.log(JSON.stringify({fired, ...out}));
  await b.close();
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_untrusted_strings_render_literally(tmp_path):
    evil = '<img src=x onerror="window.__pwned()">'
    snap = copy.deepcopy(DEMO)
    p = snap["providers"][0]
    p["id"] = evil
    p["policy"]["note"] = evil
    p["policy"]["terms_url"] = "javascript:window.__pwned()"
    p["block_reason"] = evil
    p["deployments"][0]["id"] = evil
    p["deployments"][0]["model"] = evil
    p["deployments"][0]["aliases"] = [evil]
    snap["aliases"][0]["next"] = evil
    snap["recent"][0]["deployment"] = evil
    snap["recent"][0]["provider"] = evil
    snap["home"] = evil
    # strings that come from the catalogue (a bot proposes them, a person approves): same rules
    snap["providers"][0]["name"] = evil
    snap["providers"][0]["checked_on"] = evil
    sg = snap["suggestions"][0]
    sg["name"], sg["id"] = evil, evil
    for lang in sg["text"]:
        sg["text"][lang] = {"gain": evil, "meta": evil, "friction": evil}
    rs = snap["resources"][0]
    rs["name"] = {"en": evil, "zh-TW": evil}
    rs["summary"] = {"en": evil, "zh-TW": evil}
    rs["how"] = {"en": evil, "zh-TW": evil}
    rs["provider"] = evil
    rs["key_url"] = "javascript:window.__pwned()"
    rs["headline"]["text"] = {"en": evil, "zh-TW": evil}
    rs["reset"]["text"] = {"en": evil, "zh-TW": evil}
    sb = snap["subscriptions"][0]
    sb["name"] = evil
    sb["description"] = {"en": evil, "zh-TW": evil}
    sb["web_url"] = "javascript:window.__pwned()"
    sb["app_url"] = "claude://claude.ai/new"
    s = _start(lambda: snap)
    script = tmp_path / "xss.js"
    script.write_text(NODE_TEST)
    try:
        r = subprocess.run(["node", str(script), str(s.server_address[1])], capture_output=True, text=True, timeout=60,
                           env={**__import__("os").environ, "NODE_PATH": subprocess.run(
                               ["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()})
    finally:
        s.shutdown()
        s.server_close()
    if r.returncode != 0 and "Cannot find module" in r.stderr:
        pytest.skip("playwright for node not available")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["fired"] is False
    assert out["imgs"] == 0
    assert out["jsLinks"] == 0  # a javascript: link from the catalogue is not turned into a link
    assert out["text"].count(evil) >= 8  # provider, suggestion, resource and subscription strings all render as literal text
    assert out["h"] <= 0


SECTIONS_TEST = r"""
const { chromium } = require('playwright');
(async () => {
  const port = process.argv[2];
  const b = await chromium.launch();
  const res = {};
  for (const lang of ['en', 'zh-TW']) {
    const p = await b.newPage({viewport: {width: 390, height: 844}});
    const reqs = [];
    p.on('request', r => { if (!r.url().startsWith('http://127.0.0.1:' + port) && !r.url().startsWith('data:')) reqs.push(r.url()); });
    await p.goto(`http://127.0.0.1:${port}/?demo=1&lang=${lang}`);
    await p.waitForSelector('#add-helper');
    const first = await p.evaluate(() => ({
      rows: document.querySelectorAll('#add-helper .addrow').length,
      firstRank: document.querySelector('#add-helper .addrow .rank').textContent,
      best: document.querySelectorAll('#add-helper .best').length,
      title: document.querySelector('#h-add').childNodes[0].textContent,
      otherOpen: document.querySelector('#other-allowances details').open,
      subs: document.querySelectorAll('#subscriptions .mini').length,
      subLinks: [...document.querySelectorAll('#subscriptions a')].map(a => a.getAttribute('href')),
      connect: [...document.querySelectorAll('.card code')].some(c => c.textContent.startsWith('muse connect ')),
      h: document.documentElement.scrollWidth - innerWidth,
      lang: document.documentElement.lang,
    }));
    await p.click('#other-allowances summary');
    first.otherItems = await p.evaluate(() => document.querySelectorAll('#other-allowances .mini').length);
    first.otherText = await p.evaluate(() => document.querySelector('#other-allowances').innerText);
    first.reqs = reqs;
    res[lang] = first;
    await p.close();
  }
  console.log(JSON.stringify(res));
  await b.close();
})();
"""


@pytest.mark.skipif(not shutil.which("node"), reason="node not available")
def test_catalogue_sections_render_and_stay_offline(tmp_path):
    s = _start()
    script = tmp_path / "sections.js"
    script.write_text(SECTIONS_TEST)
    try:
        r = subprocess.run(["node", str(script), str(s.server_address[1])], capture_output=True, text=True, timeout=90,
                           env={**__import__("os").environ, "NODE_PATH": subprocess.run(
                               ["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()})
    finally:
        s.shutdown()
        s.server_close()
    if r.returncode != 0 and "Cannot find module" in r.stderr:
        pytest.skip("playwright for node not available")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    en, zh = out["en"], out["zh-TW"]
    assert en["rows"] == zh["rows"] == len(DEMO["suggestions"]) and en["firstRank"] == "1" and en["best"] == 1
    assert en["title"] == "Add a helper" and zh["title"] == "新增幫手" and zh["lang"] == "zh-Hant"
    assert en["otherOpen"] is False  # collapsed until asked
    assert en["otherItems"] == len(DEMO["resources"]) and "shown, not managed yet" in en["otherText"].lower() and "僅顯示，尚未管理" in zh["otherText"]
    assert en["subs"] == 3 and all(u.startswith("https://") for u in en["subLinks"])
    assert en["connect"] is True
    assert en["h"] <= 0 and zh["h"] <= 0
    assert en["reqs"] == [] and zh["reqs"] == []  # nothing leaves the page


def test_demo_has_no_unconfirmed_numbers_in_gain():
    """The demo is what people screenshot: a gain is only quoted where the catalogue has a verified number."""
    for s in DEMO["suggestions"]:
        if s["gain"] is None:
            assert s["text"]["en"]["gain"] in ("allowance not confirmed", "no daily limit; runs on this computer")
    assert [s["rank"] for s in DEMO["suggestions"]] == list(range(1, len(DEMO["suggestions"]) + 1))
    assert all(r["managed"] is False for r in DEMO["resources"]) and all(s["handoff_only"] for s in DEMO["subscriptions"])
