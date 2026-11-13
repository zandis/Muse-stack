"""Read-only check of the catalogue against the pages its facts were read on.

`muse catalog check` answers three questions and changes nothing in the
catalogue:

1. Which entries are stale (their `checked_on` is older than N days)?
2. Which source pages changed since the last check (`--pages`)? Each
   `source_urls` page is fetched once and fingerprinted. The fingerprints live
   in MUSE_HOME/catalog-pages.json, outside the repository. A change means "a
   person should re-read this page"; it is never turned into an edit.
3. Which names on a public lead list are not in the catalogue (`--leads`)? A
   lead is a name to look up on the provider's own pages, nothing more.

`--report` also writes a small summary to MUSE_HOME/catalog-check.json (or the
file named), so the weekly job leaves something the home can show later, such
as "3 pages changed since last week". It holds dates, entry ids, page
addresses and lead names. It never holds page content.

How a page is fingerprinted, and what the three levels mean
-----------------------------------------------------------
Two things are remembered for each page:

- a hash of all the visible text (no scripts, styles or markup);
- a hash of each FACT LINE: a line or sentence of visible text that holds a
  number and one of a small set of allowance words (requests, tokens, per day,
  free, limit, GB, neurons, messages, 每日, 每分鐘, 免費, 則 ... and the units the
  citing entries use). Dates and clock times are masked first, and thousands
  separators are dropped, so "1,000" and "1000" are the same.

On the next check a page is one of:

- `unchanged`      the visible text is the same;
- `text_changed`   the text differs but the set of fact lines is the same:
                   new wording, navigation or a banner. Low priority;
- `facts_changed`  a fact line appeared, went away or changed: worth a
                   person's look.

Limits of this heuristic, stated plainly. It cannot read. A number in a line
with none of the words (a bare table cell holding "200") is not a fact line,
so a change to it shows as `text_changed` only. A line that has the words and
a number but is not about the allowance (a counter such as "12,345 developers
build for free") is a fact line, so it raises `facts_changed` when it moves. A
page drawn by scripts has little visible text to compare. And a page can stay
the same while the provider changes the allowance somewhere else. So
`text_changed` is "probably nothing", never "nothing", and `checked_on` still
goes stale after N days whatever the pages say.

Rules this module keeps (README "Keeping the catalogue honest"):

- It only reads. It never writes config/, never signs up for anything, never
  sends a key, and never adds a provider.
- Requests are plain GETs over verified https with a generic client name. They
  carry nothing about the user or this installation.
- Redirects are followed only to https on the same host, at most three times.
- It is polite: one request per page, a pause between requests to the same
  host, and `--max-pages` caps a run (the pages checked longest ago go first).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from muse import catalog, paths

STATE_FILE = "catalog-pages.json"
REPORT_FILE = "catalog-check.json"
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_DELAY_S = 1.0
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 20
USER_AGENT = "muse-catalog-check"
# CC0-1.0 list, used as names to re-check only.
LEADS_URL = "https://raw.githubusercontent.com/mnfst/awesome-free-llm-apis/main/README.md"
CHANGED = ("facts_changed", "text_changed")
STATUSES = (*CHANGED, "new", "unreachable", "unchanged")

Fetch = Callable[[str], bytes]


class FetchError(Exception):
    """Why a page could not be read. The message never contains page content."""


@dataclass(frozen=True)
class Entry:
    kind: str  # provider | storage | compute | chain | messaging | subscription
    id: str
    name: str
    checked_on: str | None
    source_urls: tuple[str, ...]
    keywords: tuple[str, ...] = ()  # the entry's own allowance units, as words


@dataclass(frozen=True)
class PageResult:
    url: str
    entries: tuple[str, ...]
    status: str  # one of STATUSES, or "skipped" (over --max-pages)
    detail: str = ""


def _unit_words(allowance: Iterable[catalog.AllowanceEntry]) -> tuple[str, ...]:
    words = {w for a in allowance for w in a.unit.lower().split("_") if len(w) >= 3}
    return tuple(sorted(words))


def entries(cat: catalog.Catalog) -> list[Entry]:
    out: list[Entry] = []
    for p in cat.ai_providers():
        out.append(Entry("provider", p.id, p.name or p.id, p.checked_on, p.source_urls, _unit_words(p.allowance)))
    for r in cat.resources:
        name = r.name.get("en") or next(iter(r.name.values()), r.id) if isinstance(r.name, Mapping) else str(r.name)
        out.append(Entry(r.kind, r.id, name, r.checked_on, r.source_urls, _unit_words(r.allowance)))
    for s in cat.subscriptions:
        out.append(Entry("subscription", s.id, s.name, s.checked_on, s.source_urls))
    return out


def stale(items: Iterable[Entry], today: date, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> list[tuple[Entry, int | None]]:
    """Entries whose facts are older than max_age_days, oldest first. Age None means undated or unreadable."""
    found: list[tuple[Entry, int | None]] = []
    for item in items:
        try:
            age: int | None = (today - date.fromisoformat(str(item.checked_on))).days
        except ValueError:
            age = None
        if age is None or age > max_age_days:
            found.append((item, age))
    return sorted(found, key=lambda pair: (pair[1] is not None, -(pair[1] or 0), pair[0].id))


# ------------------------------------------------------------------ fetching


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:  # noqa: ARG002
        return None  # handled by hand in fetch_https, so the rules below apply


def _https_host(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise FetchError("not a plain https link")
    return parts.hostname.lower()


def fetch_https(url: str) -> bytes:
    """One page over verified https. Same-host https redirects only; at most MAX_BYTES are read."""
    host = _https_host(url)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect)
    for _ in range(4):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,*/*"})
        try:
            with opener.open(request, timeout=TIMEOUT_S) as response:
                return response.read(MAX_BYTES)
        except urllib.error.HTTPError as err:
            target = err.headers.get("Location") if err.code in (301, 302, 303, 307, 308) else None
            if not target:
                raise FetchError(f"HTTP {err.code}") from None
            url = urllib.parse.urljoin(url, target)
            if _https_host(url) != host:
                raise FetchError("redirects to another host") from None
        except (urllib.error.URLError, OSError, ValueError) as err:
            raise FetchError(type(getattr(err, "reason", err)).__name__) from None
    raise FetchError("too many redirects")


# -------------------------------------------------------------- fingerprints


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}
    # A fact usually sits in its own block or cell; these tags end a line.
    BREAK = {"p", "div", "li", "tr", "td", "th", "br", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "pre", "table",
             "section", "article", "header", "footer", "nav", "ul", "ol", "option", "summary", "blockquote", "title"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:  # noqa: ARG002
        if tag in self.SKIP:
            self.depth += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.depth:
            self.depth -= 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def _raw_text(body: bytes) -> str:
    raw = body.decode("utf-8", errors="replace")
    parser = _Text()
    try:
        parser.feed(raw)
        parser.close()
        return " ".join(parser.parts)
    except Exception:
        return raw


def visible_text(body: bytes) -> str:
    """The words a reader sees: no scripts, styles or markup, whitespace collapsed."""
    return re.sub(r"\s+", " ", _raw_text(body)).strip()


def fingerprint(body: bytes) -> str:
    return hashlib.sha256(visible_text(body).encode("utf-8")).hexdigest()


_SENTENCE = re.compile(r"(?<=[。！？])|(?<=[.!?])\s+")
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATES = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?|\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b|\b\d{1,2}[-/.]\d{1,2}[-/.]\d{4}\b"
    r"|\b" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"\b\.?(?:,?\s+\d{4})?"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b|(?:©|\(c\)|copyright)\s*\d{4}(?:\s*[-–]\s*\d{4})?",
    re.IGNORECASE,
)
_FACT_WORDS = re.compile(
    r"\b(?:requests?|tokens?|per\s+(?:second|minute|hour|day|week|month|year)|daily|monthly|free|limits?|quotas?|credits?"
    r"|[kmgt]b|neurons?|calls?|messages?|rpm|rpd|tpm|tpd)\b"
    r"|/\s*(?:min|minute|hour|day|month|mo)\b"
    r"|每日|每天|每分鐘|每小時|每月|免費|上限|額度|則|次",
    re.IGNORECASE,
)


def visible_lines(body: bytes) -> list[str]:
    """Visible text as lines: one per block, cell or sentence."""
    out: list[str] = []
    for block in _raw_text(body).split("\n"):
        for piece in _SENTENCE.split(block):
            line = re.sub(r"\s+", " ", piece).strip()
            if line:
                out.append(line)
    return out


def fact_lines(body: bytes, keywords: Iterable[str] = ()) -> list[str]:
    """The normalised lines that hold a number and an allowance word. See the module docstring for the limits."""
    extra = [k.lower() for k in keywords if k]
    found: set[str] = set()
    for line in visible_lines(body):
        text = _DATES.sub(" ", line.lower())
        text = re.sub(r"(?<=\d)[,  ](?=\d{3}\b)", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not re.search(r"\d", text):
            continue
        if _FACT_WORDS.search(text) or any(k in text for k in extra):
            found.add(text)
    return sorted(found)


def fact_hashes(body: bytes, keywords: Iterable[str] = ()) -> list[str]:
    return sorted(hashlib.sha256(line.encode("utf-8")).hexdigest()[:16] for line in fact_lines(body, keywords))


# --------------------------------------------------------------------- state


def state_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / STATE_FILE


def report_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / REPORT_FILE


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pages = data.get("pages") if isinstance(data, Mapping) else None
    if not isinstance(pages, Mapping):
        return {}
    return {str(u): dict(v) for u, v in pages.items() if isinstance(v, Mapping) and isinstance(v.get("sha256"), str)}


def _write_json(path: Path, data: Any) -> None:
    """Private, atomic. The only place this module writes; never under config/."""
    target = path.expanduser().resolve()
    config = (paths.REPO_ROOT / "config").resolve()
    if target == config or config in target.parents:
        raise ValueError("this command never writes under config/")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


def save_state(path: Path, pages: Mapping[str, Mapping[str, Any]]) -> None:
    _write_json(path, {"version": 2, "pages": pages})


def _pick(urls: list[str], state: Mapping[str, Mapping[str, Any]], max_pages: int | None) -> list[str]:
    """The pages for this run: all of them, or the `max_pages` never seen or checked longest ago."""
    if max_pages is None or max_pages >= len(urls):
        return urls
    oldest_first = sorted(urls, key=lambda u: (u in state, str((state.get(u) or {}).get("last_checked") or ""), u))
    return sorted(oldest_first[: max(0, max_pages)])


def check_pages(items: Iterable[Entry], state: Mapping[str, Mapping[str, Any]], fetch: Fetch, today: date, *,
                max_pages: int | None = None, delay_s: float = 0.0, sleep: Callable[[float], None] = time.sleep,
                ) -> tuple[list[PageResult], dict[str, dict[str, Any]]]:
    """Fetch every source page once and compare with what was remembered.

    Returns the results and the state to remember. An unreachable page keeps its
    old fingerprint, so one bad day is not reported as a change the next time.
    A page left out by `max_pages` keeps its state and is reported as skipped.
    """
    owners: dict[str, list[str]] = {}
    words: dict[str, set[str]] = {}
    for item in items:
        for url in item.source_urls:
            owners.setdefault(url, []).append(f"{item.kind}:{item.id}")
            words.setdefault(url, set()).update(item.keywords)
    chosen = set(_pick(sorted(owners), state, max_pages))
    results: list[PageResult] = []
    new_state: dict[str, dict[str, Any]] = {}
    hosts_seen: set[str] = set()
    for url in sorted(owners):
        who = tuple(owners[url])
        old = state.get(url)
        if url not in chosen:
            results.append(PageResult(url, who, "skipped", "over --max-pages; next run"))
            if old:
                new_state[url] = dict(old)
            continue
        host = urllib.parse.urlsplit(url).hostname or ""
        if host in hosts_seen and delay_s > 0:
            sleep(delay_s)
        hosts_seen.add(host)
        try:
            body = fetch(url)
        except FetchError as err:
            results.append(PageResult(url, who, "unreachable", str(err)))
            if old:
                new_state[url] = dict(old)
            continue
        digest = fingerprint(body)
        facts = fact_hashes(body, sorted(words[url]))
        seen = today.isoformat()
        if old is not None and old["sha256"] == digest:
            results.append(PageResult(url, who, "unchanged", f"same since {old.get('first_seen', '?')}"))
            new_state[url] = {**old, "facts": old["facts"] if isinstance(old.get("facts"), list) else facts, "last_checked": seen}
            continue
        if old is None:
            results.append(PageResult(url, who, "new", "first time seen; remembered"))
        elif not isinstance(old.get("facts"), list):
            results.append(PageResult(url, who, "facts_changed", f"differs from {old.get('first_seen', '?')}; no earlier fact lines to compare, re-read it"))
        else:
            gone, came = len(set(old["facts"]) - set(facts)), len(set(facts) - set(old["facts"]))
            if gone or came:
                results.append(PageResult(url, who, "facts_changed", f"{gone} fact line(s) gone, {came} new since {old.get('first_seen', '?')}; re-read it"))
            else:
                results.append(PageResult(url, who, "text_changed", f"other text differs from {old.get('first_seen', '?')}; the fact lines are the same"))
        new_state[url] = {"sha256": digest, "facts": facts, "first_seen": seen, "last_checked": seen}
    return results, new_state


# --------------------------------------------------------------------- leads

_LINK = re.compile(r"\[([^\]\n]{2,80})\]\((https://[^)\s]+)\)")
_IGNORED_HOSTS = ("github.com", "raw.githubusercontent.com", "shields.io", "img.shields.io", "creativecommons.org")


def _site(host: str) -> str:
    """The registrable-looking tail of a host: api.groq.com and console.groq.com are both groq.com."""
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def known_sites(cat: catalog.Catalog) -> set[str]:
    sites: set[str] = set()
    for item in entries(cat):
        for url in item.source_urls:
            host = urllib.parse.urlsplit(url).hostname
            if host:
                sites.add(_site(host))
    return sites


def leads(markdown: str, cat: catalog.Catalog) -> list[tuple[str, str]]:
    """(name, link) pairs from a lead list whose site and name are not in the catalogue. Names to re-check only."""
    sites = known_sites(cat)
    names = {re.sub(r"[^a-z0-9]", "", e.name.lower()) for e in entries(cat)} | {re.sub(r"[^a-z0-9]", "", e.id.lower()) for e in entries(cat)}
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, url in _LINK.findall(markdown):
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if not host or any(host == h or host.endswith("." + h) for h in _IGNORED_HOSTS):
            continue
        site = _site(host)
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if site in sites or site in seen or not key or any(key in n or n in key for n in names if n):
            continue
        seen.add(site)
        out.append((name.strip(), url))
    return out


# ---------------------------------------------------------------------- run


def summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """The small report kept for the home: dates, ids, page addresses and lead names. No page content."""
    out: dict[str, Any] = {
        "version": 1,
        "checked": report["checked"],
        "max_age_days": report["max_age_days"],
        "stale": [{"kind": s["kind"], "id": s["id"], "checked_on": s["checked_on"], "age_days": s["age_days"]} for s in report["stale"]],
    }
    if "pages" in report:
        pages = report["pages"]
        out["pages"] = {
            "checked": sum(1 for p in pages if p["status"] != "skipped"),
            "skipped": sum(1 for p in pages if p["status"] == "skipped"),
            "counts": {s: sum(1 for p in pages if p["status"] == s) for s in STATUSES},
            "changed": [{"url": p["url"], "level": p["status"], "entries": p["entries"]} for p in pages if p["status"] in CHANGED],
            "unreachable": [{"url": p["url"], "entries": p["entries"], "detail": p["detail"]} for p in pages if p["status"] == "unreachable"],
        }
    if "leads" in report:
        out["leads"] = [lead["name"] for lead in report["leads"]]
    if "leads_error" in report:
        out["leads_error"] = report["leads_error"]
    return out


def run(*, pages: bool, want_leads: bool, max_age_days: int, save: bool, as_json: bool,
        out: Callable[[str], None] = print, fetch: Fetch = fetch_https, today: date | None = None,
        environ: Mapping[str, str] | None = None, cat: catalog.Catalog | None = None,
        report_file: Path | None = None, max_pages: int | None = None, delay_s: float = DEFAULT_DELAY_S,
        sleep: Callable[[float], None] = time.sleep) -> int:
    """Exit code 0: nothing to do. 1: something to re-read. The catalogue is never written."""
    today = today or date.today()
    cat = cat or catalog.load_catalog()
    items = entries(cat)
    old = stale(items, today, max_age_days)
    report: dict[str, Any] = {
        "checked": today.isoformat(),
        "max_age_days": max_age_days,
        "stale": [{"kind": e.kind, "id": e.id, "checked_on": e.checked_on, "age_days": age} for e, age in old],
    }
    page_results: list[PageResult] = []
    if pages:
        path = state_path(environ)
        page_results, new_state = check_pages(items, load_state(path), fetch, today, max_pages=max_pages, delay_s=delay_s, sleep=sleep)
        if save:
            save_state(path, new_state)
        report["pages"] = [{"url": r.url, "entries": list(r.entries), "status": r.status, "detail": r.detail} for r in page_results]
    found: list[tuple[str, str]] = []
    if want_leads:
        try:
            found = leads(fetch(LEADS_URL).decode("utf-8", errors="replace"), cat)
            report["leads"] = [{"name": n, "url": u} for n, u in found]
        except FetchError as err:
            report["leads_error"] = str(err)
    if report_file is not None:
        _write_json(report_file, summary(report))
    changed = [r for r in page_results if r.status in CHANGED]
    code = 1 if old or changed else 0
    if as_json:
        out(json.dumps(report, indent=1, ensure_ascii=False))
        return code

    out(f"Catalogue check, {today.isoformat()}. Nothing in the catalogue is changed by this command.")
    if old:
        out(f"\nOlder than {max_age_days} days ({len(old)}):")
        for e, age in old:
            out(f"  - {e.kind} {e.id}: checked {e.checked_on or 'never'}" + (f" ({age} days ago)" if age is not None else ""))
    else:
        out(f"\nEvery entry was checked within {max_age_days} days.")
    if pages:
        counts = {s: sum(1 for r in page_results if r.status == s) for s in (*STATUSES, "skipped")}
        out(f"\nSource pages: {counts['facts_changed']} with changed facts, {counts['text_changed']} with other text changed, "
            f"{counts['new']} new, {counts['unreachable']} unreachable, {counts['unchanged']} unchanged"
            + (f", {counts['skipped']} left for the next run." if counts["skipped"] else "."))
        for level in (*CHANGED, "new", "unreachable"):
            for r in page_results:
                if r.status == level:
                    out(f"  - {r.status}: {r.url}  [{', '.join(r.entries)}]  {r.detail}")
        if changed:
            out("  facts_changed: a line with a number and an allowance word changed. Re-read the page, then edit the entry and its checked_on by hand.")
            out("  text_changed: only other wording or layout changed. Low priority, but not proof that nothing changed.")
    if want_leads:
        if "leads_error" in report:
            out(f"\nLead list could not be read ({report['leads_error']}).")
        else:
            out(f"\nNames on the lead list that are not in the catalogue ({len(found)}). These are names to look up on the")
            out("provider's own pages, not facts. Nothing is added automatically.")
            for name, url in found:
                out(f"  - {name}: {url}")
    if report_file is not None:
        out(f"\nReport written to {report_file}.")
    return code
