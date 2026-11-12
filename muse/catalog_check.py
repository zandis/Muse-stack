"""Read-only check of the catalogue against the pages its facts were read on.

`muse catalog check` answers three questions and changes nothing in the
catalogue:

1. Which entries are stale (their `checked_on` is older than N days)?
2. Which source pages changed since the last check (`--pages`)? Each
   `source_urls` page is fetched once and its visible text is fingerprinted.
   The fingerprints live in MUSE_HOME/catalog-pages.json, outside the
   repository. A changed fingerprint means "a person should re-read this page";
   it is never turned into an edit.
3. Which names on a public lead list are not in the catalogue (`--leads`)? A
   lead is a name to look up on the provider's own pages, nothing more.

Rules this module keeps (docs/reuse-plan.md, catalogue sync):

- It only reads. It never writes config/, never signs up for anything, never
  sends a key, and never adds a provider.
- Requests are plain GETs over verified https with a generic client name. They
  carry nothing about the user or this installation.
- Redirects are followed only to https on the same host, at most three times.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
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
DEFAULT_MAX_AGE_DAYS = 30
MAX_BYTES = 2 * 1024 * 1024
TIMEOUT_S = 20
USER_AGENT = "muse-catalog-check"
# CC0-1.0 list, used as names to re-check only (docs/reuse-plan.md).
LEADS_URL = "https://raw.githubusercontent.com/mnfst/awesome-free-llm-apis/main/README.md"

Fetch = Callable[[str], bytes]


class FetchError(Exception):
    """Why a page could not be read. The message never contains page content."""


@dataclass(frozen=True)
class Entry:
    kind: str  # provider | storage | compute | chain | subscription
    id: str
    name: str
    checked_on: str | None
    source_urls: tuple[str, ...]


@dataclass(frozen=True)
class PageResult:
    url: str
    entries: tuple[str, ...]
    status: str  # unchanged | changed | new | unreachable
    detail: str = ""


def entries(cat: catalog.Catalog) -> list[Entry]:
    out: list[Entry] = []
    for p in cat.ai_providers():
        out.append(Entry("provider", p.id, p.name or p.id, p.checked_on, p.source_urls))
    for r in cat.resources:
        name = r.name.get("en") or next(iter(r.name.values()), r.id) if isinstance(r.name, Mapping) else str(r.name)
        out.append(Entry(r.kind, r.id, name, r.checked_on, r.source_urls))
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


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:  # noqa: ARG002
        if tag in self.SKIP:
            self.depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self.depth:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.depth:
            self.parts.append(data)


def visible_text(body: bytes) -> str:
    """The words a reader sees: no scripts, styles or markup, whitespace collapsed."""
    raw = body.decode("utf-8", errors="replace")
    parser = _Text()
    try:
        parser.feed(raw)
        parser.close()
        text = " ".join(parser.parts)
    except Exception:
        text = raw
    return re.sub(r"\s+", " ", text).strip()


def fingerprint(body: bytes) -> str:
    return hashlib.sha256(visible_text(body).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------- state


def state_path(environ: Mapping[str, str] | None = None) -> Path:
    return paths.home(environ) / STATE_FILE


def load_state(path: Path) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pages = data.get("pages") if isinstance(data, Mapping) else None
    if not isinstance(pages, Mapping):
        return {}
    return {str(u): dict(v) for u, v in pages.items() if isinstance(v, Mapping) and isinstance(v.get("sha256"), str)}


def save_state(path: Path, pages: Mapping[str, Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps({"version": 1, "pages": pages}, indent=1, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def check_pages(items: Iterable[Entry], state: Mapping[str, Mapping[str, str]], fetch: Fetch, today: date,
                ) -> tuple[list[PageResult], dict[str, dict[str, str]]]:
    """Fetch every source page once and compare with the remembered fingerprint.

    Returns the results and the state to remember. An unreachable page keeps its
    old fingerprint, so one bad day is not reported as a change the next time.
    """
    owners: dict[str, list[str]] = {}
    for item in items:
        for url in item.source_urls:
            owners.setdefault(url, []).append(f"{item.kind}:{item.id}")
    results: list[PageResult] = []
    new_state: dict[str, dict[str, str]] = {}
    for url in sorted(owners):
        who = tuple(owners[url])
        old = state.get(url)
        try:
            digest = fingerprint(fetch(url))
        except FetchError as err:
            results.append(PageResult(url, who, "unreachable", str(err)))
            if old:
                new_state[url] = dict(old)
            continue
        if old is None:
            results.append(PageResult(url, who, "new", "first time seen; remembered"))
        elif old["sha256"] == digest:
            results.append(PageResult(url, who, "unchanged", f"same since {old.get('first_seen', '?')}"))
            new_state[url] = dict(old)
            continue
        else:
            results.append(PageResult(url, who, "changed", f"differs from {old.get('first_seen', '?')}; re-read it"))
        new_state[url] = {"sha256": digest, "first_seen": today.isoformat()}
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


def run(*, pages: bool, want_leads: bool, max_age_days: int, save: bool, as_json: bool,
        out: Callable[[str], None] = print, fetch: Fetch = fetch_https, today: date | None = None,
        environ: Mapping[str, str] | None = None, cat: catalog.Catalog | None = None) -> int:
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
        page_results, new_state = check_pages(items, load_state(path), fetch, today)
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
    changed = [r for r in page_results if r.status == "changed"]
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
        counts = {s: sum(1 for r in page_results if r.status == s) for s in ("changed", "new", "unreachable", "unchanged")}
        out(f"\nSource pages: {counts['changed']} changed, {counts['new']} new, {counts['unreachable']} unreachable, {counts['unchanged']} unchanged.")
        for r in page_results:
            if r.status != "unchanged":
                out(f"  - {r.status}: {r.url}  [{', '.join(r.entries)}]  {r.detail}")
        if changed:
            out("  A changed page may only have new wording or layout. Re-read it, then edit the entry and its checked_on by hand.")
    if want_leads:
        if "leads_error" in report:
            out(f"\nLead list could not be read ({report['leads_error']}).")
        else:
            out(f"\nNames on the lead list that are not in the catalogue ({len(found)}). These are names to look up on the")
            out("provider's own pages, not facts. Nothing is added automatically.")
            for name, url in found:
                out(f"  - {name}: {url}")
    return code
