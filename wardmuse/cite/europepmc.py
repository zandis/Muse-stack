"""Europe PMC citations through the public REST search.

One request, no account. ``resultType=lite`` is enough for PMID, DOI, title,
journal and year. Checked against https://europepmc.org/RestfulWebService on
2026-11-17. The article page is on europepmc.org; the search itself is the EBI host.
"""

from __future__ import annotations

import json
import re
from html import unescape
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

from wardmuse.cite.http import Fetch, call, clean_query, default_get

SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
ARTICLE = "https://europepmc.org/article/{source}/{id}"

_PMID = re.compile(r"[1-9][0-9]{0,8}\Z")
_DOI = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\Z")
_YEAR = re.compile(r"\b(1[89][0-9]{2}|20[0-9]{2})\b")
_TAG = re.compile(r"<[^>]{0,200}>")
_SOURCE = re.compile(r"[A-Z]{2,10}\Z")
_DOC = re.compile(r"[A-Za-z0-9]+\Z")

Note = Callable[[str], None]


def _query(text: str) -> str:
    if _PMID.fullmatch(text):
        return f"EXT_ID:{text} AND SRC:MED"
    if _DOI.fullmatch(text):
        return f"DOI:{text}"
    return text


def _plain(value: Any) -> str:
    return " ".join(_TAG.sub(" ", unescape(str(value or ""))).split())


def _article_url(source: str, doc_id: str) -> str | None:
    if _SOURCE.fullmatch(source) and _DOC.fullmatch(doc_id):
        return ARTICLE.format(source=source, id=doc_id)
    return None


def parse_search(payload: bytes, *, limit: int) -> list[dict[str, Any]]:
    """Citations from a Europe PMC search document, in the order returned."""

    from wardmuse.cite.http import CiteError

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise CiteError("bad_response", "Europe PMC did not return the expected document.") from None
    if not isinstance(data, dict):
        raise CiteError("bad_response", "Europe PMC did not return the expected document.")
    result_list = data.get("resultList")
    if result_list is None and data.get("hitCount") in (0, "0"):
        return []
    rows = result_list.get("result") if isinstance(result_list, dict) else None
    if rows is None:
        raise CiteError("bad_response", "Europe PMC did not return the expected document.")
    if isinstance(rows, dict):  # a single hit is sometimes one object, not a list
        rows = [rows]
    if not isinstance(rows, list):
        raise CiteError("bad_response", "Europe PMC did not return the expected document.")
    citations: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pmid = str(row.get("pmid") or "")
        pmid = pmid if _PMID.fullmatch(pmid) else None
        doi = str(row.get("doi") or "").strip()
        doi = doi if _DOI.fullmatch(doi) else None
        title = _plain(row.get("title"))
        journal = _plain(row.get("journalTitle") or row.get("journal"))
        year_text = str(row.get("pubYear") or "")
        year = year_text if _YEAR.fullmatch(year_text) else None
        source = str(row.get("source") or "").strip().upper()
        doc_id = str(row.get("id") or pmid or "").strip()
        url = _article_url(source, doc_id) or ("https://doi.org/" + doi if doi else None)
        if not title or not (pmid or doi) or not url:
            continue
        citations.append(
            {
                "source": "europepmc",
                "pmid": pmid,
                "doi": doi,
                "title": title,
                "journal": journal,
                "year": year,
                "url": url,
            }
        )
        if len(citations) >= limit:
            break
    return citations


def lookup_europepmc(
    query: str,
    *,
    limit: int = 5,
    fetch: Fetch | None = None,
    timeout: float = 20.0,
    note: Note | None = None,
) -> list[dict[str, Any]]:
    """Citations for ``query`` (a PMID, a DOI, or words). ``fetch`` replaces the network in tests.

    ``note`` is accepted so the command can pass one callback to every source; a search does not use it.
    """

    text = clean_query(query, limit=limit)
    _ = note
    params = {"query": _query(text), "format": "json", "resultType": "lite", "pageSize": str(limit)}
    url = SEARCH + "?" + urlencode(params)
    getter = fetch or (lambda address, timeout_: default_get(address, timeout_, max_bytes=2_000_000))
    body = call(getter, url, timeout, what="Europe PMC")
    return parse_search(body, limit=limit)
