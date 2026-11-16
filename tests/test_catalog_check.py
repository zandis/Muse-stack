"""muse.catalog_check: a reader. No network here; every page comes from a fake fetcher."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from wardmuse import catalog, catalog_check as cc
from wardmuse import cli

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 20)


def tree_digest() -> str:
    h = hashlib.sha256()
    for path in sorted((ROOT / "config").rglob("*")):
        if path.is_file():
            h.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + path.read_bytes())
    return h.hexdigest()


class Pages:
    def __init__(self, default: bytes = b"<html><body><p>Free tier: 10 requests</p></body></html>") -> None:
        self.default = default
        self.bodies: dict[str, bytes] = {}
        self.broken: dict[str, str] = {}
        self.asked: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.asked.append(url)
        if url in self.broken:
            raise cc.FetchError(self.broken[url])
        return self.bodies.get(url, self.default)


def run(tmp_path, fake: Pages, **kw):
    lines: list[str] = []
    options = dict(pages=True, want_leads=False, max_age_days=30, save=True, as_json=False, sleep=lambda s: None)
    options.update(kw)
    code = cc.run(out=lines.append, fetch=fake, today=options.pop("today", TODAY), environ={"MUSE_HOME": str(tmp_path)}, **options)
    return code, "\n".join(lines)


def test_entries_cover_providers_resources_and_subscriptions():
    items = cc.entries(catalog.load_catalog())
    kinds = {e.kind for e in items}
    assert kinds == {"provider", "storage", "compute", "chain", "messaging", "subscription"}
    assert not any(e.id in ("ollama", "lmstudio", "local_openai") for e in items)  # local runtimes have no page to watch
    assert all(u.startswith("https://") for e in items for u in e.source_urls)


def test_stale_lists_old_and_undated_entries_oldest_first():
    items = [
        cc.Entry("provider", "fresh", "Fresh", "2026-11-12", ()),
        cc.Entry("provider", "old", "Old", "2026-08-01", ()),
        cc.Entry("provider", "older", "Older", "2026-05-01", ()),
        cc.Entry("provider", "undated", "Undated", None, ()),
        cc.Entry("provider", "garbled", "Garbled", "last week", ()),
    ]
    found = cc.stale(items, TODAY, 30)
    assert [e.id for e, _ in found] == ["garbled", "undated", "older", "old"]
    assert found[-1][1] == 111 and found[0][1] is None
    assert cc.stale(items[:1], TODAY, 30) == []
    assert [e.id for e, _ in cc.stale(items[:1], TODAY, 7)] == ["fresh"]


def test_visible_text_ignores_markup_scripts_and_whitespace():
    a = b"<html><head><style>p{color:red}</style><script>var t=1731400000;</script></head><body><p>Free:  10   requests</p></body></html>"
    b = b"<html><head><script>var t=1731499999;</script></head>\n<body>\n<div class='x'><p>Free: 10 requests</p></div></body></html>"
    assert cc.visible_text(a) == "Free: 10 requests"
    assert cc.fingerprint(a) == cc.fingerprint(b)  # a new build id or layout is not a change
    assert cc.fingerprint(a) != cc.fingerprint(a.replace(b"10", b"5"))  # a new number is
    assert cc.visible_text(b"\xff\xfe plain \x00 text")  # never raises on odd bytes


def test_first_run_remembers_then_reports_only_real_changes(tmp_path):
    before = tree_digest()
    pages = Pages()
    code, text = run(tmp_path, pages)
    urls = sorted({u for e in cc.entries(catalog.load_catalog()) for u in e.source_urls})
    assert sorted(pages.asked) == urls  # each page once, even when two entries cite it
    assert "0 with changed facts, 0 with other text changed" in text and f"{len(urls)} new" in text
    state = json.loads((tmp_path / cc.STATE_FILE).read_text())
    assert set(state["pages"]) == set(urls)
    assert oct((tmp_path / cc.STATE_FILE).stat().st_mode & 0o777) == "0o600"

    code, text = run(tmp_path, Pages())
    assert f"{len(urls)} unchanged" in text and "0 with changed facts, 0 with other text changed" in text

    changed = Pages()
    changed.bodies[urls[0]] = b"<p>Free tier: 5 requests</p>"
    changed.broken[urls[1]] = "HTTP 503"
    code, text = run(tmp_path, changed)
    assert code == 1
    assert f"facts_changed: {urls[0]}" in text and f"unreachable: {urls[1]}" in text and "HTTP 503" in text
    assert "edit the entry and its checked_on by hand" in text
    assert "5 requests" not in text  # page content is never echoed

    # The unreachable page kept its old fingerprint: it is not a "change" once it is back.
    code, text = run(tmp_path, Pages())
    assert f"changed: {urls[1]}" not in text and f"changed: {urls[0]}" in text  # urls[0] went back to the old text
    assert tree_digest() == before  # config/ is never written


def test_no_save_leaves_no_state_and_stale_alone_needs_no_network(tmp_path):
    pages = Pages()
    run(tmp_path, pages, save=False)
    assert not (tmp_path / cc.STATE_FILE).exists()
    pages = Pages()
    code, text = run(tmp_path, pages, pages=False, today=date(2027, 3, 1))
    assert pages.asked == [] and code == 1 and "Older than 30 days" in text
    code, text = run(tmp_path, Pages(), pages=False, today=date(2026, 11, 13))
    assert code == 0 and "Every entry was checked within 30 days" in text


def test_json_report(tmp_path):
    lines: list[str] = []
    cc.run(pages=True, want_leads=False, max_age_days=30, save=False, as_json=True, out=lines.append, fetch=Pages(),
           delay_s=0, today=TODAY, environ={"MUSE_HOME": str(tmp_path)})
    report = json.loads("\n".join(lines))
    assert report["checked"] == "2026-11-20" and report["stale"] == []
    assert {p["status"] for p in report["pages"]} == {"new"} and all(p["entries"] for p in report["pages"])


def test_leads_are_names_only_and_skip_what_the_catalogue_has(tmp_path):
    markdown = """
# Free LLM APIs
- [Groq](https://console.groq.com/docs/rate-limits) fast
- [Google AI Studio](https://aistudio.google.com/) gemini
- [OpenRouter](https://openrouter.ai/models?q=free)
- [Shiny New Cloud](https://api.shinynew.example/pricing) 1M tokens
- [Shiny docs](https://docs.shinynew.example/)
- [Another One](https://another.example/free)
- [badge](https://img.shields.io/badge/x-y-z)
- [repo](https://github.com/someone/thing)
- [plain http](http://insecure.example/)
"""
    cat = catalog.load_catalog()
    found = cc.leads(markdown, cat)
    assert found == [("Shiny New Cloud", "https://api.shinynew.example/pricing"), ("Another One", "https://another.example/free")]

    before = tree_digest()
    pages = Pages()
    pages.bodies[cc.LEADS_URL] = markdown.encode()
    code, text = run(tmp_path, pages, pages=False, want_leads=True, today=date(2026, 11, 13))
    assert pages.asked == [cc.LEADS_URL] and code == 0  # a lead is not a problem to fix
    assert "Shiny New Cloud" in text and "Nothing is added automatically" in text
    assert tree_digest() == before
    assert [p.id for p in catalog.load_catalog().providers] == [p.id for p in cat.providers]

    pages = Pages()
    pages.broken[cc.LEADS_URL] = "HTTP 404"
    code, text = run(tmp_path, pages, pages=False, want_leads=True, today=date(2026, 11, 13))
    assert "Lead list could not be read (HTTP 404)" in text


@pytest.mark.parametrize("url", [
    "http://example.com/page", "ftp://example.com/x", "https://user:pw@example.com/", "file:///etc/hosts", "https:///nohost", "example.com",
])
def test_fetch_refuses_anything_but_plain_https(url):
    with pytest.raises(cc.FetchError):
        cc.fetch_https(url)


def test_module_cannot_write_the_catalogue_or_send_a_key():
    source = (ROOT / "wardmuse" / "catalog_check.py").read_text(encoding="utf-8")
    for banned in ("yaml.safe_dump", "yaml.dump", "from wardmuse import keys", "muse.keys", "Authorization", "api_key", "webbrowser", "method=\"POST\""):
        assert banned not in source, banned
    assert "CATALOG_PATH" not in source and "write_text" in source  # the only write is the fingerprint file
    assert source.count("write_text") == 1  # state and report share one private, atomic writer that refuses config/


def test_cli_catalog_check_runs_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MUSE_HOME", str(tmp_path))

    def no_network(url: str) -> bytes:
        raise AssertionError("no request without --pages or --leads")

    monkeypatch.setattr(cc, "fetch_https", no_network)
    code = cli.main(["catalog", "check", "--max-age-days", "100000"])
    out = capsys.readouterr().out
    assert code == 0 and "Nothing in the catalogue is changed" in out
    code = cli.main(["catalog", "check", "--max-age-days", "100000", "--json"])
    assert code == 0 and json.loads(capsys.readouterr().out)["stale"] == []


# ------------------------------------------------- three levels (A15, weekly check)

EN_PAGE = """<html><head><title>Rate limits</title><script>window.build='{build}'</script></head><body>
<nav><a>Docs</a><a>Pricing</a><a>{nav}</a></nav>
<div class="banner">{banner}</div>
<h1>Rate limits</h1>
<p>The free plan allows {per_day} requests per day and 20 requests per minute. Paid plans are described elsewhere.</p>
<table><tr><th>Plan</th><th>Tokens per day</th></tr><tr><td>Free</td><td>{tokens} tokens</td></tr></table>
<p>Embedding models are counted separately.</p>
<footer>Last updated {updated}. &copy; 2026 Example Inc. All rights reserved.</footer>
</body></html>"""

ZH_PAGE = """<html><body>
<div class="news">{news}</div>
<h1>官方帳號方案</h1>
<p>輕用量方案月費 0 元，每月提供 {free} 則免費訊息，不可加購。</p>
<p>中用量方案每月提供 3,000 則免費訊息。</p>
<p>Messaging API 的 Reply API 不列入計費。</p>
<p>系統更新期間：{window}。</p>
</body></html>"""


def en(**kw) -> bytes:
    values = dict(build="a1", nav="Blog", banner="New: our autumn release notes", per_day="1,000", tokens="1,000,000", updated="November 12, 2026")
    values.update(kw)
    return EN_PAGE.format(**values).encode()


def zh(**kw) -> bytes:
    values = dict(news="最新消息：商家俱樂部會員計畫開放申請", free="200", window="2026年10月28日早上8點起至2026年11月2日")
    values.update(kw)
    return ZH_PAGE.format(**values).encode()


def level(old: bytes, new: bytes, keywords=()) -> str:
    item = cc.Entry("provider", "x", "X", "2026-11-12", ("https://example.com/limits",), tuple(keywords))
    _, state = cc.check_pages([item], {}, lambda url: old, date(2026, 11, 13))
    results, _ = cc.check_pages([item], state, lambda url: new, date(2026, 11, 20))
    return results[0].status


def test_fact_lines_are_lines_with_a_number_and_an_allowance_word():
    lines = cc.fact_lines(en())
    assert lines == [
        "1000000 tokens",
        "the free plan allows 1000 requests per day and 20 requests per minute.",
    ]  # the footer's date and year are masked; "Embedding models..." has no number; nav and banner have none
    assert cc.fact_lines(zh()) == ["中用量方案每月提供 3000 則免費訊息。", "輕用量方案月費 0 元，每月提供 200 則免費訊息，不可加購。"]
    assert cc.fact_lines(b"<p>Up to 5 GB of storage</p><p>Version 3 of the API</p><p>10 GPU minutes</p>") == ["up to 5 gb of storage"]
    assert cc.fact_lines(b"<p>10 GPU minutes</p>", ["gpu", "minutes"]) == ["10 gpu minutes"]  # the entry's own unit words count too


def test_unchanged_when_only_markup_and_scripts_differ():
    assert level(en(), en(build="b2")) == "unchanged"
    assert level(en(), en().replace(b"<p>", b"<p class='x'>")) == "unchanged"


def test_rotating_boilerplate_is_text_changed_not_facts_changed():
    assert level(en(), en(banner="Join our webinar next week")) == "text_changed"
    assert level(en(), en(nav="Changelog")) == "text_changed"
    assert level(en(), en(updated="November 19, 2026")) == "text_changed"  # a new date alone is not a fact line
    assert level(en(), en(per_day="1000")) == "text_changed"  # 1,000 and 1000 are the same number
    assert level(zh(), zh(news="最新消息：系統維護公告")) == "text_changed"
    assert level(zh(), zh(window="2026年11月24日早上8點起至2026年11月25日")) == "text_changed"


def test_a_changed_number_on_a_fact_line_is_facts_changed():
    assert level(en(), en(per_day="500")) == "facts_changed"
    assert level(en(), en(tokens="500,000")) == "facts_changed"
    assert level(zh(), zh(free="100")) == "facts_changed"
    # a fact line that disappears, or a new one, counts as well
    assert level(en(), en().replace(b"<tr><td>Free</td><td>1,000,000 tokens</td></tr>", b"")) == "facts_changed"
    assert level(zh(), zh().replace("<h1>".encode(), "<p>高用量方案每月提供 6,000 則免費訊息。</p><h1>".encode())) == "facts_changed"


def test_the_heuristics_documented_limits():
    # 1. A number in a cell with none of the words is not a fact line: its change is only text_changed.
    bare = b"<table><tr><th>Messages</th></tr><tr><td>%d</td></tr></table>"
    assert cc.fact_lines(bare % 200) == [] and level(bare % 200, bare % 100) == "text_changed"
    # 2. A counter that has the words is a fact line, so it is a false alarm when it moves.
    counter = "<p>{} developers build for free</p><p>50 requests per day</p>"
    assert level(counter.format("12,345").encode(), counter.format("12,399").encode()) == "facts_changed"
    for phrase in ("holds a\n  number and one of a small set", "It cannot read", "is not a fact line", 'never "nothing"'):
        assert phrase in (cc.__doc__ or ""), phrase


def test_state_from_before_fact_lines_is_upgraded_without_a_false_alarm():
    url = "https://example.com/limits"
    item = cc.Entry("provider", "x", "X", "2026-11-12", (url,))
    v1 = {url: {"sha256": cc.fingerprint(en()), "first_seen": "2026-11-01"}}
    results, state = cc.check_pages([item], v1, lambda u: en(build="zz"), date(2026, 11, 20))
    assert results[0].status == "unchanged" and state[url]["first_seen"] == "2026-11-01"
    assert state[url]["facts"] == cc.fact_hashes(en()) and state[url]["last_checked"] == "2026-11-20"
    assert all(len(h) == 16 for h in state[url]["facts"])  # hashes only: no page text in the state
    # With no earlier fact lines there is nothing to compare, so a difference is reported at the higher level.
    results, _ = cc.check_pages([item], v1, lambda u: en(banner="other"), date(2026, 11, 20))
    assert results[0].status == "facts_changed" and "no earlier fact lines" in results[0].detail


def test_max_pages_takes_the_pages_checked_longest_ago_and_keeps_the_rest():
    urls = [f"https://site{i}.example/p" for i in range(5)]
    items = [cc.Entry("provider", f"p{i}", f"P{i}", "2026-11-12", (u,)) for i, u in enumerate(urls)]
    asked: list[str] = []

    def fetch(url: str) -> bytes:
        asked.append(url)
        return b"<p>10 requests per day</p>"

    results, state = cc.check_pages(items, {}, fetch, date(2026, 11, 13), max_pages=2)
    assert asked == urls[:2] and [r.status for r in results] == ["new", "new", "skipped", "skipped", "skipped"]
    asked.clear()
    results, state = cc.check_pages(items, state, fetch, date(2026, 11, 14), max_pages=2)
    assert asked == urls[2:4]  # never-seen pages before pages seen yesterday
    asked.clear()
    results, state = cc.check_pages(items, state, fetch, date(2026, 11, 15), max_pages=2)
    assert asked == [urls[0], urls[4]] and set(state) == set(urls)  # the last unseen one, then the one checked longest ago
    assert {r.url: r.status for r in results}[urls[1]] == "skipped" and state[urls[1]]["last_checked"] == "2026-11-13"


def test_one_pause_between_requests_to_the_same_host_and_none_across_hosts():
    items = [cc.Entry("provider", "a", "A", "2026-11-12", ("https://a.example/1", "https://a.example/2", "https://a.example/3", "https://b.example/1"))]
    pauses: list[float] = []
    cc.check_pages(items, {}, lambda url: b"<p>x</p>", date(2026, 11, 13), delay_s=1.5, sleep=pauses.append)
    assert pauses == [1.5, 1.5]
    pauses.clear()
    cc.check_pages(items, {}, lambda url: b"<p>x</p>", date(2026, 11, 13), delay_s=0, sleep=pauses.append)
    assert pauses == []


def test_requests_carry_a_generic_client_name_and_nothing_else():
    source = (ROOT / "wardmuse" / "catalog_check.py").read_text(encoding="utf-8")
    assert cc.USER_AGENT == "muse-catalog-check"
    assert 'headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*"}' in source and source.count("headers=") == 1
    for banned in ("Cookie", "Referer", "platform.", "gethostname", "getpass", "uuid", "__version__"):
        assert banned not in source, banned


def test_report_file_is_small_private_and_holds_no_page_content(tmp_path):
    before = tree_digest()
    run(tmp_path, Pages())
    urls = sorted({u for e in cc.entries(catalog.load_catalog()) for u in e.source_urls})
    changed = Pages()
    changed.bodies[urls[0]] = b"<p>Free tier: 5 requests</p><p>SECRET-PAGE-WORDS</p>"
    changed.bodies[urls[2]] = b"<p>Free tier: 10 requests</p><p>a new banner</p>"
    changed.broken[urls[1]] = "HTTP 503"
    changed.bodies[cc.LEADS_URL] = b"- [Shiny New Cloud](https://api.shinynew.example/pricing)"
    target = tmp_path / cc.REPORT_FILE
    code, text = run(tmp_path, changed, want_leads=True, report_file=target, today=date(2027, 3, 1))
    assert code == 1 and f"Report written to {target}" in text
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    raw = target.read_text(encoding="utf-8")
    report = json.loads(raw)
    assert report["version"] == 1 and report["checked"] == "2027-03-01" and report["stale"]
    assert report["pages"]["counts"] == {"facts_changed": 1, "text_changed": 1, "new": 0, "unreachable": 1, "unchanged": len(urls) - 3}
    assert report["pages"]["checked"] == len(urls) and report["pages"]["skipped"] == 0
    by_url = {c["url"]: c for c in report["pages"]["changed"]}
    assert by_url[urls[0]]["level"] == "facts_changed" and by_url[urls[2]]["level"] == "text_changed"
    assert all(c["entries"] and all(":" in e for e in c["entries"]) for c in report["pages"]["changed"])  # which entries cite the page
    assert [(u["url"], u["detail"]) for u in report["pages"]["unreachable"]] == [(urls[1], "HTTP 503")] and report["pages"]["unreachable"][0]["entries"]
    assert report["leads"] == ["Shiny New Cloud"]
    assert "SECRET-PAGE-WORDS" not in raw and "5 requests" not in raw and len(raw) < 20000
    assert str(tmp_path) not in raw  # nothing about this installation
    assert tree_digest() == before


def test_report_is_refused_under_config(tmp_path):
    before = tree_digest()
    with pytest.raises(ValueError, match="never writes under config/"):
        run(tmp_path, Pages(), pages=False, report_file=ROOT / "config" / "catalog-check.json")
    with pytest.raises(ValueError):
        cc.save_state(ROOT / "config" / "resources" / "x.json", {})
    assert tree_digest() == before and not (ROOT / "config" / "catalog-check.json").exists()


def test_cli_report_defaults_to_the_home_folder_and_exit_codes_are_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MUSE_HOME", str(tmp_path))
    asked: list[str] = []

    def fake(url: str) -> bytes:
        asked.append(url)
        return b"<p>Free tier: 10 requests</p>"

    monkeypatch.setattr(cc, "fetch_https", fake)
    assert cli.main(["catalog", "check", "--max-age-days", "100000", "--report"]) == 0
    assert asked == [] and json.loads((tmp_path / "catalog-check.json").read_text())["stale"] == []
    capsys.readouterr()
    code = cli.main(["catalog", "check", "--max-age-days", "100000", "--pages", "--max-pages", "3", "--delay", "0", "--report", str(tmp_path / "r.json")])
    report = json.loads((tmp_path / "r.json").read_text())
    assert code == 0 and len(asked) == 3 and report["pages"]["checked"] == 3 and report["pages"]["skipped"] > 0
    assert "left for the next run" in capsys.readouterr().out
    monkeypatch.setattr(cc, "fetch_https", lambda url: b"<p>Free tier: 9 requests</p>")
    assert cli.main(["catalog", "check", "--max-age-days", "100000", "--pages", "--max-pages", "100000", "--delay", "0"]) == 1  # a change is still exit 1
    assert cli.main(["catalog", "check", "--max-age-days", "100000", "--report", str(ROOT / "config" / "x.json")]) == 2
    assert not (ROOT / "config" / "x.json").exists()
