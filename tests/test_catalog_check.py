"""muse.catalog_check: a reader. No network here; every page comes from a fake fetcher."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from muse import catalog, catalog_check as cc
from muse import cli

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
    options = dict(pages=True, want_leads=False, max_age_days=30, save=True, as_json=False)
    options.update(kw)
    code = cc.run(out=lines.append, fetch=fake, today=options.pop("today", TODAY), environ={"MUSE_HOME": str(tmp_path)}, **options)
    return code, "\n".join(lines)


def test_entries_cover_providers_resources_and_subscriptions():
    items = cc.entries(catalog.load_catalog())
    kinds = {e.kind for e in items}
    assert kinds == {"provider", "storage", "compute", "chain", "subscription"}
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
    assert "0 changed" in text and f"{len(urls)} new" in text
    state = json.loads((tmp_path / cc.STATE_FILE).read_text())
    assert set(state["pages"]) == set(urls)
    assert oct((tmp_path / cc.STATE_FILE).stat().st_mode & 0o777) == "0o600"

    code, text = run(tmp_path, Pages())
    assert f"{len(urls)} unchanged" in text and "0 changed" in text

    changed = Pages()
    changed.bodies[urls[0]] = b"<p>Free tier: 5 requests</p>"
    changed.broken[urls[1]] = "HTTP 503"
    code, text = run(tmp_path, changed)
    assert code == 1
    assert f"changed: {urls[0]}" in text and f"unreachable: {urls[1]}" in text and "HTTP 503" in text
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
           today=TODAY, environ={"MUSE_HOME": str(tmp_path)})
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
    source = (ROOT / "muse" / "catalog_check.py").read_text(encoding="utf-8")
    for banned in ("yaml.safe_dump", "yaml.dump", "from muse import keys", "muse.keys", "Authorization", "api_key", "webbrowser", "method=\"POST\""):
        assert banned not in source, banned
    assert "CATALOG_PATH" not in source and "write_text" in source  # the only write is the fingerprint file
    assert source.count("write_text") == 1


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
